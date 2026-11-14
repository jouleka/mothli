"""The free library: browse and search the public-domain catalogue and add a book to the shelf.

Adding goes through the same import as an uploaded file (``import_epub_blob``), so a downloaded
book is segmented, validated and shelved under exactly the same rules. The shelved book's catalog
``source`` records which classic it is, which is how the page knows a book is already on the shelf.

Cover art is served from here rather than straight from the source: the page keeps its strict image
policy, readers' addresses are not shared with a third party, and each cover is fetched once and
kept on disk."""
import threading
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field

from app.api.books import import_epub_blob
from app.classics import (
    MAX_PAGE,
    SOURCE_NAME,
    SOURCE_URL,
    ClassicUnavailable,
    catalog_summary,
    fetch_classic,
    fetch_cover,
    find_classic,
    front_page,
    image_type,
    search,
    source_tag,
)
from app.classics_covers import CoverCache
from app.deps import get_catalog, get_settings, get_store

router = APIRouter(prefix="/api/classics", tags=["classics"])



def _language():
    return Query("", max_length=12, pattern=r"^[a-z-]*$")


def _tag():
    return Query("", max_length=24, pattern=r"^[a-z]*$")


_BASE = {"source": SOURCE_NAME, "source_url": SOURCE_URL}
_COVER_HEADERS = {"cache-control": "private, max-age=604800", "x-content-type-options": "nosniff"}
MAX_WARM = 150
_caches = {}
_caches_lock = threading.Lock()


def _covers(settings):
    """The cover cache for this install's data directory."""
    folder = str(Path(settings.data_dir) / "classics-covers")
    with _caches_lock:
        cache = _caches.get(folder)
        if cache is None:
            cache = _caches[folder] = CoverCache(folder)
        return cache


def _fetch(entry):
    return fetch_cover(entry)


class WarmCovers(BaseModel):
    ids: list[str] = Field(max_length=MAX_WARM)


def _card(entry, shelved):
    return {"id": entry["id"], "title": entry["title"], "author": entry["author"],
            "language": entry["language"], "book_id": shelved.get(source_tag(entry))}


def _shelved(catalog):
    return {book["source"]: book["book_id"] for book in catalog.list_books() if book.get("source")}


def _find(classic_id, settings):
    entry = None
    if settings.classics_enabled:
        entry = find_classic(classic_id, min_years_since_death=settings.classics_min_years_since_death)
    if entry is None:
        raise HTTPException(404, "unknown classic")
    return entry


@router.get("")
def list_classics(
    q: str = Query("", max_length=120),
    language: str = _language(),
    genre: str = _tag(),
    mood: str = _tag(),
    offset: int = Query(0, ge=0, le=200_000),
    limit: int = Query(40, ge=1, le=MAX_PAGE),
    catalog=Depends(get_catalog),
    settings=Depends(get_settings),
):
    if not settings.classics_enabled:
        return {**_BASE, "enabled": False, "total": 0, "matches": 0, "languages": [], "genres": [],
                "moods": [], "books": []}
    rule = settings.classics_min_years_since_death
    shelved = _shelved(catalog)
    matches, page = search(q, language=language or None, genre=genre or None, mood=mood or None,
                           offset=offset, limit=limit, min_years_since_death=rule)
    return {**_BASE, "enabled": True, **catalog_summary(min_years_since_death=rule), "matches": matches,
            "books": [_card(entry, shelved) for entry in page]}


@router.get("/front")
def classics_front_page(language: str = _language(), catalog=Depends(get_catalog), settings=Depends(get_settings)):
    """The shelves shown before a reader searches: most read, one per mood, and a few genres."""
    if not settings.classics_enabled:
        raise HTTPException(404, "the library is switched off")
    rule = settings.classics_min_years_since_death
    shelved = _shelved(catalog)
    shelves = front_page(language=language or None, min_years_since_death=rule)
    return {"shelves": [{**shelf, "books": [_card(entry, shelved) for entry in shelf["books"]]} for shelf in shelves]}


@router.get("/{classic_id}")
def classic_detail(classic_id: str, catalog=Depends(get_catalog), settings=Depends(get_settings)):
    entry = _find(classic_id, settings)
    labels = {genre["id"]: genre["label"] for genre in catalog_summary()["genres"]}
    return {**_card(entry, _shelved(catalog)),
            "genres": [{"id": genre, "label": labels[genre]} for genre in entry["genres"]],
            "minutes": entry["minutes"], "blurb": entry["blurb"],
            "source_url": f"{SOURCE_URL}ebooks/{entry['pg']}"}


@router.get("/{classic_id}/cover")
def classic_cover(classic_id: str, settings=Depends(get_settings)):
    entry = _find(classic_id, settings)
    blob = _covers(settings).get(entry, _fetch)
    if blob is None:
        raise HTTPException(404, "no cover")
    return Response(blob, media_type=image_type(blob), headers=_COVER_HEADERS)


@router.post("/covers/warm", status_code=202)
def warm_covers(body: WarmCovers, settings=Depends(get_settings)):
    """The page says which covers it is about to show, so they are being fetched, several at a
    time, before the pictures are asked for. Only catalogue books are fetched; anything else in
    the list is ignored."""
    if not settings.classics_enabled:
        raise HTTPException(404, "the library is switched off")
    rule = settings.classics_min_years_since_death
    entries = [entry for entry in (find_classic(classic_id, min_years_since_death=rule) for classic_id in body.ids)
               if entry is not None]
    return {"started": _covers(settings).warm(entries, _fetch)}


@router.post("/{classic_id}", status_code=201)
def add_classic(classic_id: str, catalog=Depends(get_catalog), store=Depends(get_store),
                settings=Depends(get_settings)):
    entry = _find(classic_id, settings)
    tag = source_tag(entry)
    existing = next((book for book in catalog.list_books() if book.get("source") == tag), None)
    if existing:
        raise HTTPException(409, f"book already imported as {existing['book_id']!r}")
    try:
        blob = fetch_classic(entry, max_bytes=settings.epub_max_upload_bytes)
    except ClassicUnavailable as exc:
        raise HTTPException(502, str(exc)) from exc
    return import_epub_blob(blob, filename=f"gutenberg-{entry['pg']}.epub", source=tag,
                            fallback_title=entry["title"], fallback_author=entry["author"],
                            catalog=catalog, store=store, settings=settings)
