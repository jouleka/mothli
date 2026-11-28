"""The free library in hosted mode (ADR 0041 part 11), at the local paths.

A signed-in reader browses and searches the same shipped catalogue local mode uses (it holds
nothing about any reader), and adds a book to their shelf: the server fetches that one catalogue
entry's EPUB with the local module's own guarded fetch (``app.classics.fetch_classic``: a URL built
from the entry's number on Project Gutenberg's fixed hosts, HTTPS only, redirects followed only
within those hosts, the upload limit read as it streams) and hands it to ``store_epub``, the one
hosted import an uploaded file goes through: the same validation, encryption, object storage and
limits (and no job). The book is then the reader's own like
any upload; nothing fetched is kept for, or shared with, another reader.

Fenced in further than locally: one fetch in flight per reader and ``MAX_FETCHES`` in this
process; a full shelf, or a book already on it, is refused before anything is fetched; the whole
fetch has a deadline (``FETCH_SECONDS``) beside the per-step timeouts; and no proxy from the
environment is used. On-demand cover fetches and cached images are scoped to the reader.
"""

from __future__ import annotations

import asyncio
import time
import uuid

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from app.classics import (
    _TIMEOUT,
    _USER_AGENT,
    MAX_PAGE,
    SOURCE_NAME,
    SOURCE_URL,
    ClassicTooLarge,
    ClassicUnavailable,
    catalog_summary,
    fetch_classic,
    fetch_cover,
    find_classic,
    front_page,
    search,
    source_tag,
)
from app.hosted.limits import LimitExceededError
from app.hosted.runtime import CacheNamespace, ResourceKind, TenantCacheKey, TenantResourceKey
from app.ingest.cover import image_type
from app.hosted.tenant.api import _csrf_owner, _limit_failure, _owner, _private, store_epub
from app.hosted.tenant.models import AlreadyOnShelfError, OwnerId

router = APIRouter(prefix="/api/classics", tags=["hosted-library"])

FETCH_SECONDS = 120.0
MAX_FETCHES = 4
# Readers with a fetch in flight in this process. Touched only on the event loop, so the check
# and the add below are one step.
_fetching: set = set()

# What a reader is told. Each says what happened and what they can do.
UNKNOWN = "That book isn’t in the library."
ON_SHELF = "This book is already on your shelf."
ADDING = "Mothli is still fetching the last book you asked for. Give it a moment, then try again."
BUSY = "The library is busy just now, so nothing was added. Please try again in a minute."
UNAVAILABLE = ("Mothli couldn’t get this book from Project Gutenberg just now, so nothing was "
               "added. Please try again in a little while.")
TOO_LARGE = "This book’s file is larger than Mothli can keep, so it wasn’t added."
NOT_A_BOOK = ("This book’s file from Project Gutenberg couldn’t be opened, so nothing was added. "
              "Another edition of it may work.")
NOT_SAVED = "Mothli couldn’t save this book just now, so nothing was added. Please try again in a moment."
LIMITS = {
    "book_quota_exceeded": ("Your shelf is full, so this book wasn’t added. Remove a book you no "
                            "longer need, then add it again."),
    "library_storage_exceeded": ("Your shelf has no room left for this book, so it wasn’t added. "
                                 "Remove a book you no longer need, then add it again."),
    "upload_size_exceeded": TOO_LARGE,
}


def _refusal(status: int, code: str, message: str, **extra) -> HTTPException:
    headers = {"Retry-After": str(extra["retry_after_seconds"])} if extra.get(
        "retry_after_seconds") else None
    return HTTPException(status, {"code": code, "message": message, **extra}, headers=headers)


def _limited(exc: LimitExceededError) -> HTTPException:
    """The upload's own limit refusal (same status, code and fields), with a line for the reader."""
    failure = _limit_failure(exc)
    failure.detail = {**failure.detail, "message": LIMITS.get(exc.code, UNAVAILABLE)}
    return failure


def _language():
    return Query("", max_length=12, pattern=r"^[a-z-]*$")


def _tag():
    return Query("", max_length=24, pattern=r"^[a-z]*$")


_BASE = {"source": SOURCE_NAME, "source_url": SOURCE_URL, "covers": True}


def _card(entry, shelved):
    return {"id": entry["id"], "title": entry["title"], "author": entry["author"],
            "language": entry["language"], "book_id": shelved.get(source_tag(entry))}


def _find(classic_id, settings):
    entry = None
    if settings.classics_enabled:
        entry = find_classic(classic_id, min_years_since_death=settings.classics_min_years_since_death)
    if entry is None:
        raise _refusal(404, "unknown_classic", UNKNOWN)
    return entry


@router.get("")
async def list_classics(
    request: Request,
    response: Response,
    q: str = Query("", max_length=120),
    language: str = _language(),
    genre: str = _tag(),
    mood: str = _tag(),
    offset: int = Query(0, ge=0, le=200_000),
    limit: int = Query(40, ge=1, le=MAX_PAGE),
    owner_id: OwnerId = Depends(_owner),
):
    """One page of the catalogue, as locally; ``book_id`` marks the reader's own copy."""
    _private(response)
    settings = request.app.state.settings
    if not settings.classics_enabled:
        return {**_BASE, "enabled": False, "total": 0, "matches": 0, "languages": [], "genres": [],
                "moods": [], "books": []}
    rule = settings.classics_min_years_since_death
    shelved = await request.app.state.tenant_repository.shelved_classics(owner_id)
    matches, page = await asyncio.to_thread(
        search, q, language=language or None, genre=genre or None, mood=mood or None,
        offset=offset, limit=limit, min_years_since_death=rule)
    summary = await asyncio.to_thread(catalog_summary, min_years_since_death=rule)
    return {**_BASE, "enabled": True, **summary, "matches": matches,
            "books": [_card(entry, shelved) for entry in page]}


@router.get("/front")
async def classics_front_page(request: Request, response: Response, language: str = _language(),
                              owner_id: OwnerId = Depends(_owner)):
    """The shelves shown before a reader searches: most read, one per mood, and a few genres."""
    _private(response)
    settings = request.app.state.settings
    if not settings.classics_enabled:
        raise _refusal(404, "library_off", UNKNOWN)
    shelved = await request.app.state.tenant_repository.shelved_classics(owner_id)
    shelves = await asyncio.to_thread(front_page, language=language or None,
                                      min_years_since_death=settings.classics_min_years_since_death)
    return {"covers": True,
            "shelves": [{**shelf, "books": [_card(entry, shelved) for entry in shelf["books"]]}
                        for shelf in shelves]}


@router.get("/{classic_id}/cover")
async def classic_cover(classic_id: str, request: Request, owner_id: OwnerId = Depends(_owner)):
    entry = _find(classic_id, request.app.state.settings)
    runtime = request.app.state.hosted_runtime
    resource = TenantResourceKey(owner_id, ResourceKind.CLASSIC, uuid.UUID(int=entry["pg"]))
    key = TenantCacheKey(resource, CacheNamespace.COVER, "v1")
    async with runtime.serialized(resource):
        blob = await runtime.cache_get(key)
        if blob is None:
            async with request.app.state.cover_downloads:
                blob = await asyncio.to_thread(_download_cover, entry)
            if not blob:
                raise HTTPException(404, "no cover")
            if len(blob) <= 128 * 1024:
                await runtime.cache_set(key, blob)
    return Response(blob, media_type=image_type(blob))


@router.get("/{classic_id}")
async def classic_detail(classic_id: str, request: Request, response: Response,
                         owner_id: OwnerId = Depends(_owner)):
    _private(response)
    entry = _find(classic_id, request.app.state.settings)
    shelved = await request.app.state.tenant_repository.shelved_classics(owner_id)
    labels = {genre["id"]: genre["label"] for genre in catalog_summary()["genres"]}
    return {**_card(entry, shelved),
            "genres": [{"id": genre, "label": labels[genre]} for genre in entry["genres"]],
            "minutes": entry["minutes"], "blurb": entry["blurb"],
            "source_url": f"{SOURCE_URL}ebooks/{entry['pg']}"}


@router.post("/{classic_id}", status_code=201)
async def add_classic(classic_id: str, request: Request, response: Response,
                      owner_id: OwnerId = Depends(_csrf_owner)):
    """Fetch one catalogue entry's EPUB and import it onto this reader's shelf as an upload is.

    Refused before any fetch: an unknown entry (404), a fetch of this reader's already in flight
    (429), the process's fetches all in use (503), the book already on the shelf (409, with its
    id), a shelf at its limits (the upload's own refusal). A fetch or import that fails leaves
    nothing behind: no row, no stored file."""
    _private(response)
    return await add_entry(request, owner_id, _find(classic_id, request.app.state.settings))


async def add_entry(request, owner_id: OwnerId, entry: dict) -> dict:
    """The add itself, for one catalogue entry, called by the reader's route above. Only
    ``request.app.state`` is read."""
    settings = request.app.state.settings
    tag = source_tag(entry)
    if owner_id.value in _fetching:
        raise _refusal(429, "already_adding", ADDING, retry_after_seconds=5)
    if len(_fetching) >= MAX_FETCHES:
        raise _refusal(503, "library_busy", BUSY, retry_after_seconds=30)
    _fetching.add(owner_id.value)
    try:
        repository = request.app.state.tenant_repository
        shelved = await repository.shelved_classics(owner_id)
        if tag in shelved:
            raise _refusal(409, "already_on_shelf", ON_SHELF, book_id=shelved[tag])
        try:
            await repository.admit_book(owner_id)
        except LimitExceededError as exc:
            raise _limited(exc) from exc
        try:
            blob = await asyncio.to_thread(_download, entry, settings.epub_max_upload_bytes)
        except ClassicTooLarge as exc:
            raise _refusal(413, "too_large", TOO_LARGE) from exc
        except ClassicUnavailable as exc:
            raise _refusal(502, "source_unavailable", UNAVAILABLE) from exc
        try:
            book = await store_epub(request, owner_id, blob, fallback_title=entry["title"],
                                    fallback_author=entry["author"], source_kind=tag)
        except AlreadyOnShelfError as exc:
            raise _refusal(409, "already_on_shelf", ON_SHELF, book_id=str(exc.book_id)) from exc
        except HTTPException as exc:
            if isinstance(exc.__cause__, LimitExceededError):
                raise _limited(exc.__cause__) from exc
            if exc.status_code in (415, 422):
                raise _refusal(422, "not_a_book", NOT_A_BOOK) from exc
            raise _refusal(503, "not_saved", NOT_SAVED) from exc
        except Exception as exc:          # the rows could not be written; the file is gone again
            raise _refusal(503, "not_saved", NOT_SAVED) from exc
    finally:
        _fetching.discard(owner_id.value)
    return book


# ---- the fetch ----------------------------------------------------------------------------------

def _transport() -> httpx.BaseTransport:
    """The network. Tests replace this with a fixture; nothing else may."""
    return httpx.HTTPTransport()


class _Deadline(httpx.BaseTransport):
    """One whole fetch, every hop and every chunk, must finish within ``seconds``."""

    def __init__(self, inner: httpx.BaseTransport, seconds: float) -> None:
        self._inner, self._until = inner, time.monotonic() + seconds

    def _check(self) -> None:
        if time.monotonic() > self._until:
            raise httpx.ReadTimeout("the fetch took too long")

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self._check()
        response = self._inner.handle_request(request)
        return httpx.Response(response.status_code, headers=response.headers,
                              stream=_Timed(response.stream, self._check),
                              extensions=response.extensions)

    def close(self) -> None:
        self._inner.close()


class _Timed(httpx.SyncByteStream):
    def __init__(self, inner, check) -> None:
        self._inner, self._check = inner, check

    def __iter__(self):
        for chunk in self._inner:
            self._check()
            yield chunk

    def close(self) -> None:
        close = getattr(self._inner, "close", None)
        if close is not None:
            close()


def _download_cover(entry):
    with httpx.Client(transport=_Deadline(_transport(), 10), timeout=5, trust_env=False,
                      headers={"user-agent": _USER_AGENT}) as client:
        return fetch_cover(entry, client=client)


def _download(entry: dict, max_bytes: int) -> bytes:
    """``fetch_classic`` on a client of our own: no redirects followed by the client itself (the
    local module follows them only within its allowed hosts), no proxy from the environment, the
    local per-step timeouts and a deadline for the whole fetch."""
    with httpx.Client(transport=_Deadline(_transport(), FETCH_SECONDS), timeout=_TIMEOUT,
                      follow_redirects=False, trust_env=False,
                      headers={"user-agent": _USER_AGENT}) as client:
        return fetch_classic(entry, max_bytes=max_bytes, client=client)
