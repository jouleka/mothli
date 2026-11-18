"""Session-derived hosted tenant API. Client-supplied owner identity is never accepted."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

from app.hosted.auth.api import (
    AuthenticatedSession,
    authenticated_session,
    csrf_valid,
    pending_deletion_detail,
)
from app.hosted.limits import LimitExceededError
from app.hosted.runtime import CacheNamespace, ResourceKind, TenantCacheKey, TenantResourceKey
from app.hosted.storage import (
    EPUB_MEDIA_TYPE,
    ObjectConflictError,
    ObjectIntegrityError,
    ObjectNotFoundError,
    ObjectPolicyError,
    ObjectStorageUnavailableError,
    SourceObjectRef,
    new_source_ref,
)
from app.hosted.tenant.models import (
    FuturePositionVersionError,
    InvalidPositionError,
    MissingTenantResourceError,
    OwnerId,
    StalePositionEpochError,
)
from app.ingest.book_type import detect_book_type
from app.ingest.extraction.chapter_text import segment_for_ingest
from app.ingest.segmentation.epub_segmenter import EpubDrmError

router = APIRouter(prefix="/api", tags=["hosted-library"])
_LOG = logging.getLogger(__name__)


class PositionResetIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    position_epoch: StrictInt = Field(ge=0, le=2**63 - 1)


class PositionUpdateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cfi: str = Field(min_length=1, max_length=4096)
    offset: StrictInt = Field(ge=0, le=2**63 - 1)
    completed_chapter: StrictInt = Field(ge=0, le=2**31 - 1)
    position_epoch: StrictInt = Field(ge=0, le=2**63 - 1)
    base_version: StrictInt = Field(ge=0, le=2**63 - 1)
    client_id: uuid.UUID
    client_sequence: StrictInt = Field(ge=1, le=2**63 - 1)


class ReaderPreferencesIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    font_size: Literal["small", "book", "large", "x-large"]
    line_height: Literal["compact", "comfortable", "relaxed"]
    measure: Literal["narrow", "balanced", "wide"]
    theme: Literal["paper", "sepia", "night", "system"]
    margins: Literal["compact", "balanced", "generous"]
    typeface: Literal["publisher", "serif", "sans"]


class TextQuoteAnchor(BaseModel):
    model_config = ConfigDict(extra="forbid")
    exact: str = Field(min_length=1, max_length=2000)
    prefix: str = Field(default="", max_length=200)
    suffix: str = Field(default="", max_length=200)


class ReaderMarkAnchor(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cfi: str = Field(min_length=1, max_length=4096, pattern=r"^epubcfi\(.+\)$")
    atom: StrictInt = Field(ge=1, le=2**31 - 1)
    quote: TextQuoteAnchor | None = None


class HighlightIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    anchor: ReaderMarkAnchor
    color: Literal["yellow", "green", "blue", "pink"] = "yellow"
    selected_text: str = Field(min_length=1, max_length=2000)

    @field_validator("selected_text")
    @classmethod
    def selected_text_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("selected text is blank")
        return value


class AnnotationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    anchor: ReaderMarkAnchor
    body: str = Field(min_length=1, max_length=10000)
    highlight_id: uuid.UUID | None = None

    @field_validator("body")
    @classmethod
    def body_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("annotation is blank")
        return value


class BookmarkIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    anchor: ReaderMarkAnchor
    label: str | None = Field(default=None, min_length=1, max_length=500)

    @field_validator("label")
    @classmethod
    def label_not_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("bookmark label is blank")
        return value


class HighlightUpdateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    color: Literal["yellow", "green", "blue", "pink"]


class AnnotationUpdateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    body: str = Field(min_length=1, max_length=10000)

    @field_validator("body")
    @classmethod
    def body_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("annotation is blank")
        return value


class BookmarkUpdateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(min_length=1, max_length=500)

    @field_validator("label")
    @classmethod
    def label_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("bookmark label is blank")
        return value


class HostedEntityCorrectionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_entity_id: uuid.UUID
    canonical_name: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=1000)
    bookmark: StrictInt = Field(ge=1, le=2**31 - 1)

    @field_validator("canonical_name", "reason")
    @classmethod
    def correction_text_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("value must not be blank")
        return value


async def _session(request: Request) -> AuthenticatedSession:
    value = await authenticated_session(request)
    if value is None:
        raise HTTPException(401, "Authentication required")
    # A reader who asked to delete their account may sign in again only to keep it or sign out
    # (ADR 0041 part 8). Every reader route refuses them here, before anything of theirs is read
    # or any model is called, with a code the app recognises.
    if value.principal.erase_after is not None:
        raise HTTPException(403, pending_deletion_detail(value.principal.erase_after))
    return value


async def _owner_input_guard(request: Request) -> None:
    forbidden = {
        "owner",
        "ownerid",
        "userid",
        "credentialid",
        "objectid",
        "objectkey",
        "sourceobjectid",
        "storagekey",
    }
    for key in request.query_params:
        normalized = key.casefold().replace("_", "").replace("-", "")
        if normalized in forbidden:
            raise HTTPException(422, "storage and owner identity are server-derived")


async def _owner(
    request: Request,
    session: AuthenticatedSession = Depends(_session),
    _guard: None = Depends(_owner_input_guard),
) -> OwnerId:
    owner_id = OwnerId(session.principal.owner_id)
    try:
        await request.app.state.tenant_repository.consume_request_limit(owner_id)
    except LimitExceededError as exc:
        raise _limit_failure(exc) from exc
    return owner_id


async def _csrf_owner(
    request: Request,
    session: AuthenticatedSession = Depends(_session),
    _guard: None = Depends(_owner_input_guard),
) -> OwnerId:
    if not csrf_valid(request, session):
        raise HTTPException(403, "CSRF validation failed")
    owner_id = OwnerId(session.principal.owner_id)
    try:
        await request.app.state.tenant_repository.consume_request_limit(owner_id)
    except LimitExceededError as exc:
        raise _limit_failure(exc) from exc
    return owner_id


def _private(response: Response) -> None:
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Pragma"] = "no-cache"


def _resource(owner_id: OwnerId, kind: ResourceKind, resource_id: uuid.UUID) -> TenantResourceKey:
    return TenantResourceKey(owner_id, kind, resource_id)


def _storage_failure(exc: Exception) -> HTTPException:
    if isinstance(exc, ObjectPolicyError):
        return HTTPException(422, str(exc))
    if isinstance(exc, ObjectNotFoundError):
        return HTTPException(404, "source EPUB not found")
    return HTTPException(503, "source object storage is unavailable")


def _limit_failure(exc: LimitExceededError) -> HTTPException:
    status = 429 if exc.retry_after_seconds is not None else (
        413 if exc.code == "upload_size_exceeded" else 409
    )
    headers = (
        {"Retry-After": str(exc.retry_after_seconds)}
        if exc.retry_after_seconds is not None
        else None
    )
    return HTTPException(
        status,
        {
            "code": exc.code,
            "limit": exc.limit,
            "retry_after_seconds": exc.retry_after_seconds,
            "action": exc.action,
        },
        headers=headers,
    )


@router.post("/books", status_code=201)
async def upload_book(
    file: UploadFile,
    request: Request,
    response: Response,
    owner_id: OwnerId = Depends(_csrf_owner),
):
    _private(response)
    form = await request.form()
    if set(form) != {"file"} or len(form.getlist("file")) != 1:
        raise HTTPException(422, "upload accepts exactly one server-addressed EPUB file")
    if file.content_type != EPUB_MEDIA_TYPE:
        raise HTTPException(415, "EPUB upload must use application/epub+zip")
    limit = request.app.state.settings.epub_max_upload_bytes
    blob = await file.read(limit + 1)
    await file.close()
    if len(blob) > limit:
        raise HTTPException(413, "EPUB upload exceeds the configured size limit")
    if not blob:
        raise HTTPException(422, "empty upload")
    return await store_epub(
        request, owner_id, blob, fallback_title=(file.filename or "book.epub").rsplit(".", 1)[0]
    )


async def store_epub(
    request: Request,
    owner_id: OwnerId,
    blob: bytes,
    *,
    fallback_title: str,
    fallback_author: str | None = None,
    source_kind: str = "upload",
) -> dict:
    """The hosted import of one EPUB for this owner: validated and segmented, stored encrypted
    under the owner's own object, then its rows and search text written in one transaction under the owner's limits; the object is removed again if that
    transaction fails.
    The upload route and the free library's add both come here, so a book from either is
    imported the same way (ADR 0041 part 11)."""
    book_id = uuid.uuid4()
    incarnation = uuid.uuid4()
    ref = new_source_ref(owner_id)
    try:
        result, chapters = await asyncio.to_thread(segment_for_ingest, blob, str(book_id))
    except EpubDrmError as exc:
        raise HTTPException(
            422, "DRM-protected EPUBs are not supported; import a DRM-free EPUB"
        ) from exc
    except Exception as exc:
        raise HTTPException(422, f"not a readable EPUB: {type(exc).__name__}") from exc
    if result.mode == "none" or not chapters:
        raise HTTPException(422, "no body chapters detected in this EPUB")

    profile = detect_book_type(result, chapters)
    title = result.title or fallback_title
    author = result.author or fallback_author
    checksum = hashlib.sha256(blob).hexdigest()
    char_start = 0
    search_documents = []
    for _atom, chapter in zip(result.atoms, chapters, strict=True):
        char_end = char_start + max(1, len(chapter["text"]))
        search_documents.append(
            {
                "ordinal": chapter["ordinal"],
                "href": chapter["href"],
                "title": chapter["title"],
                "part_label": chapter["part_label"],
                "content": chapter["text"],
                "char_start": char_start,
                "char_end": char_end,
            }
        )
        char_start = char_end
    library_key = _resource(owner_id, ResourceKind.LIBRARY, owner_id.value)
    async with request.app.state.hosted_runtime.serialized(library_key):
        try:
            receipt = await asyncio.to_thread(
                request.app.state.object_storage.put,
                ref,
                blob,
                media_type=EPUB_MEDIA_TYPE,
                expected_sha256=checksum,
            )
        except (
            ObjectConflictError,
            ObjectPolicyError,
            ObjectIntegrityError,
            ObjectStorageUnavailableError,
        ) as exc:
            raise _storage_failure(exc) from exc
        try:
            book = await request.app.state.tenant_repository.create_uploaded_book(
                owner_id,
                book_id=book_id,
                incarnation=incarnation,
                title=title,
                author=author,
                file_hash=checksum,
                content_language=result.content_language,
                book_type=profile.book_type,
                object_id=ref.object_id,
                storage_provider=receipt.provider,
                media_type=receipt.media_type,
                byte_size=receipt.byte_size,
                encryption=receipt.encryption,
                search_documents=search_documents,
                source_kind=source_kind,
            )
        except BaseException as exc:
            try:
                await asyncio.shield(
                    asyncio.to_thread(request.app.state.object_storage.delete, ref)
                )
            except Exception:
                _LOG.error("source object compensation failed after metadata transaction error")
            if isinstance(exc, LimitExceededError):
                raise _limit_failure(exc) from exc
            raise
    return {**book, "mode": result.mode, "atoms": len(chapters)}


@router.get("/limits")
async def get_limits(
    request: Request,
    response: Response,
    owner_id: OwnerId = Depends(_owner),
):
    _private(response)
    return await request.app.state.tenant_repository.limit_status(owner_id)


@router.get("/books")
async def list_books(request: Request, response: Response, owner_id: OwnerId = Depends(_owner)):
    _private(response)
    key = _resource(owner_id, ResourceKind.LIBRARY, owner_id.value)
    async with request.app.state.hosted_runtime.serialized(key):
        return await request.app.state.tenant_repository.list_books(owner_id)


@router.get("/books/{book_id}")
async def get_book(
    book_id: uuid.UUID,
    request: Request,
    response: Response,
    owner_id: OwnerId = Depends(_owner),
):
    _private(response)
    resource = _resource(owner_id, ResourceKind.BOOK, book_id)
    cache_key = TenantCacheKey(resource, CacheNamespace.BOOK_METADATA, "v1")
    async with request.app.state.hosted_runtime.serialized(resource):
        cached = await request.app.state.hosted_runtime.cache_get(cache_key)
        if cached is not None:
            return cached
        book = await request.app.state.tenant_repository.get_book(owner_id, book_id)
        if book is None:
            raise HTTPException(404, "unknown book")
        await request.app.state.hosted_runtime.cache_set(cache_key, book)
        return book


@router.get("/books/{book_id}/manifest")
async def get_book_manifest(
    book_id: uuid.UUID,
    request: Request,
    response: Response,
    owner_id: OwnerId = Depends(_owner),
):
    _private(response)
    key = _resource(owner_id, ResourceKind.BOOK, book_id)
    async with request.app.state.hosted_runtime.serialized(key):
        manifest = await request.app.state.tenant_repository.get_book_manifest(
            owner_id, book_id
        )
        if manifest is None:
            raise HTTPException(404, "unknown book")
        return manifest


@router.get("/books/{book_id}/epub")
async def stream_epub(
    book_id: uuid.UUID,
    request: Request,
    response: Response,
    owner_id: OwnerId = Depends(_owner),
):
    _private(response)
    resource = _resource(owner_id, ResourceKind.BOOK, book_id)
    async with request.app.state.hosted_runtime.serialized(resource):
        record = await request.app.state.tenant_repository.source_object(owner_id, book_id)
        if record is None:
            raise HTTPException(404, "unknown book")
        if record.provider != request.app.state.object_storage.provider:
            raise HTTPException(503, "source object storage is unavailable")
        ref = SourceObjectRef(owner_id, record.object_id)
        try:
            stored = await asyncio.to_thread(request.app.state.object_storage.get, ref)
        except (ObjectNotFoundError, ObjectIntegrityError, ObjectStorageUnavailableError) as exc:
            raise _storage_failure(exc) from exc
        if (
            stored.media_type != record.media_type
            or stored.byte_size != record.byte_size
            or stored.sha256 != record.sha256
        ):
            raise HTTPException(503, "source object metadata does not match stored bytes")

    def chunks():
        for offset in range(0, len(stored.data), 64 * 1024):
            yield stored.data[offset : offset + 64 * 1024]

    return StreamingResponse(
        chunks(),
        media_type=EPUB_MEDIA_TYPE,
        headers={
            "Content-Length": str(stored.byte_size),
            "Content-Disposition": f'inline; filename="{book_id}.epub"',
            "Cache-Control": "private, no-store",
            "Pragma": "no-cache",
        },
    )


@router.delete("/books/{book_id}", status_code=204)
async def delete_book(
    book_id: uuid.UUID,
    request: Request,
    owner_id: OwnerId = Depends(_csrf_owner),
):
    resource = _resource(owner_id, ResourceKind.BOOK, book_id)
    async with request.app.state.hosted_runtime.serialized(resource):
        record = await request.app.state.tenant_repository.source_object(owner_id, book_id)
        if record is None:
            raise HTTPException(404, "unknown book")
        if record.provider != request.app.state.object_storage.provider:
            raise HTTPException(503, "source object storage is unavailable")
        try:
            await asyncio.to_thread(
                request.app.state.object_storage.delete,
                SourceObjectRef(owner_id, record.object_id),
            )
        except ObjectStorageUnavailableError as exc:
            raise _storage_failure(exc) from exc
        if not await request.app.state.tenant_repository.remove_book(owner_id, book_id):
            raise HTTPException(404, "unknown book")
        await request.app.state.hosted_runtime.cache_invalidate_resource(resource)
    return Response(status_code=204)


@router.get("/books/{book_id}/position")
async def get_position(
    book_id: uuid.UUID,
    request: Request,
    response: Response,
    owner_id: OwnerId = Depends(_owner),
):
    _private(response)
    key = _resource(owner_id, ResourceKind.BOOK, book_id)
    async with request.app.state.hosted_runtime.serialized(key):
        position = await request.app.state.tenant_repository.get_position(owner_id, book_id)
        if position is None:
            raise HTTPException(404, "unknown book")
        return position


@router.get("/books/{book_id}/preferences")
async def get_reader_preferences(
    book_id: uuid.UUID,
    request: Request,
    response: Response,
    owner_id: OwnerId = Depends(_owner),
):
    _private(response)
    key = _resource(owner_id, ResourceKind.BOOK, book_id)
    async with request.app.state.hosted_runtime.serialized(key):
        preferences = await request.app.state.tenant_repository.get_reader_preferences(
            owner_id, book_id
        )
        if preferences is None:
            raise HTTPException(404, "unknown book")
        return preferences


@router.get("/books/{book_id}/search")
async def search_book(
    book_id: uuid.UUID,
    request: Request,
    response: Response,
    q: str = Query(min_length=2, max_length=200),
    limit: int = Query(default=20, ge=1, le=50),
    owner_id: OwnerId = Depends(_owner),
):
    _private(response)
    query = q.strip()
    if len(query) < 2:
        raise HTTPException(422, "search query is too short")
    key = _resource(owner_id, ResourceKind.BOOK, book_id)
    async with request.app.state.hosted_runtime.serialized(key):
        results = await request.app.state.tenant_repository.search_book(
            owner_id, book_id, query, limit=limit
        )
        if results is None:
            raise HTTPException(404, "unknown book")
        return results


@router.get("/books/{book_id}/marks")
async def list_reader_marks(
    book_id: uuid.UUID,
    request: Request,
    response: Response,
    owner_id: OwnerId = Depends(_owner),
):
    _private(response)
    key = _resource(owner_id, ResourceKind.BOOK, book_id)
    async with request.app.state.hosted_runtime.serialized(key):
        marks = await request.app.state.tenant_repository.list_reader_marks(owner_id, book_id)
        if marks is None:
            raise HTTPException(404, "unknown book")
        return marks


@router.get("/books/{book_id}/marks/export")
async def export_reader_marks(
    book_id: uuid.UUID,
    request: Request,
    owner_id: OwnerId = Depends(_owner),
):
    key = _resource(owner_id, ResourceKind.BOOK, book_id)
    async with request.app.state.hosted_runtime.serialized(key):
        marks = await request.app.state.tenant_repository.list_reader_marks(owner_id, book_id)
        if marks is None:
            raise HTTPException(404, "unknown book")
    payload = {
        "format": "litlet-reader-marks",
        "version": 1,
        "book_id": str(book_id),
        **marks,
    }
    return Response(
        content=json.dumps(payload, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={
            "Cache-Control": "private, no-store",
            "Pragma": "no-cache",
            "Content-Disposition": f'attachment; filename="litlet-marks-{book_id}.json"',
        },
    )


async def _create_reader_mark(
    *,
    kind: str,
    body: HighlightIn | AnnotationIn | BookmarkIn,
    book_id: uuid.UUID,
    request: Request,
    response: Response,
    owner_id: OwnerId,
):
    _private(response)
    values = body.model_dump()
    anchor = values.pop("anchor")
    key = _resource(owner_id, ResourceKind.BOOK, book_id)
    async with request.app.state.hosted_runtime.serialized(key):
        try:
            return await request.app.state.tenant_repository.create_reader_mark(
                owner_id, book_id, kind=kind, anchor=anchor, **values
            )
        except MissingTenantResourceError as exc:
            raise HTTPException(404, "unknown book or linked highlight") from exc
        except InvalidPositionError as exc:
            raise HTTPException(409, str(exc)) from exc


@router.post("/books/{book_id}/highlights", status_code=201)
async def create_highlight(
    book_id: uuid.UUID,
    body: HighlightIn,
    request: Request,
    response: Response,
    owner_id: OwnerId = Depends(_csrf_owner),
):
    return await _create_reader_mark(
        kind="highlight", body=body, book_id=book_id, request=request,
        response=response, owner_id=owner_id,
    )


@router.post("/books/{book_id}/annotations", status_code=201)
async def create_annotation(
    book_id: uuid.UUID,
    body: AnnotationIn,
    request: Request,
    response: Response,
    owner_id: OwnerId = Depends(_csrf_owner),
):
    return await _create_reader_mark(
        kind="annotation", body=body, book_id=book_id, request=request,
        response=response, owner_id=owner_id,
    )


@router.post("/books/{book_id}/bookmarks", status_code=201)
async def create_bookmark(
    book_id: uuid.UUID,
    body: BookmarkIn,
    request: Request,
    response: Response,
    owner_id: OwnerId = Depends(_csrf_owner),
):
    return await _create_reader_mark(
        kind="bookmark", body=body, book_id=book_id, request=request,
        response=response, owner_id=owner_id,
    )


async def _update_reader_mark(
    *,
    kind: str,
    value: str,
    book_id: uuid.UUID,
    mark_id: uuid.UUID,
    request: Request,
    response: Response,
    owner_id: OwnerId,
):
    _private(response)
    key = _resource(owner_id, ResourceKind.BOOK, book_id)
    async with request.app.state.hosted_runtime.serialized(key):
        mark = await request.app.state.tenant_repository.update_reader_mark(
            owner_id, book_id, mark_id, kind=kind, value=value
        )
        if mark is None:
            raise HTTPException(404, "unknown mark")
        return mark


@router.patch("/books/{book_id}/highlights/{mark_id}")
async def update_highlight(
    book_id: uuid.UUID,
    mark_id: uuid.UUID,
    body: HighlightUpdateIn,
    request: Request,
    response: Response,
    owner_id: OwnerId = Depends(_csrf_owner),
):
    return await _update_reader_mark(
        kind="highlight", value=body.color, book_id=book_id, mark_id=mark_id,
        request=request, response=response, owner_id=owner_id,
    )


@router.patch("/books/{book_id}/annotations/{mark_id}")
async def update_annotation(
    book_id: uuid.UUID,
    mark_id: uuid.UUID,
    body: AnnotationUpdateIn,
    request: Request,
    response: Response,
    owner_id: OwnerId = Depends(_csrf_owner),
):
    return await _update_reader_mark(
        kind="annotation", value=body.body, book_id=book_id, mark_id=mark_id,
        request=request, response=response, owner_id=owner_id,
    )


@router.patch("/books/{book_id}/bookmarks/{mark_id}")
async def update_bookmark(
    book_id: uuid.UUID,
    mark_id: uuid.UUID,
    body: BookmarkUpdateIn,
    request: Request,
    response: Response,
    owner_id: OwnerId = Depends(_csrf_owner),
):
    return await _update_reader_mark(
        kind="bookmark", value=body.label, book_id=book_id, mark_id=mark_id,
        request=request, response=response, owner_id=owner_id,
    )


async def _delete_reader_mark(
    *,
    kind: str,
    book_id: uuid.UUID,
    mark_id: uuid.UUID,
    request: Request,
    owner_id: OwnerId,
):
    key = _resource(owner_id, ResourceKind.BOOK, book_id)
    async with request.app.state.hosted_runtime.serialized(key):
        deleted = await request.app.state.tenant_repository.delete_reader_mark(
            owner_id, book_id, mark_id, kind=kind
        )
        if not deleted:
            raise HTTPException(404, "unknown mark")
    return Response(status_code=204)


@router.delete("/books/{book_id}/highlights/{mark_id}", status_code=204)
async def delete_highlight(
    book_id: uuid.UUID,
    mark_id: uuid.UUID,
    request: Request,
    owner_id: OwnerId = Depends(_csrf_owner),
):
    return await _delete_reader_mark(
        kind="highlight", book_id=book_id, mark_id=mark_id,
        request=request, owner_id=owner_id,
    )


@router.delete("/books/{book_id}/annotations/{mark_id}", status_code=204)
async def delete_annotation(
    book_id: uuid.UUID,
    mark_id: uuid.UUID,
    request: Request,
    owner_id: OwnerId = Depends(_csrf_owner),
):
    return await _delete_reader_mark(
        kind="annotation", book_id=book_id, mark_id=mark_id,
        request=request, owner_id=owner_id,
    )


@router.delete("/books/{book_id}/bookmarks/{mark_id}", status_code=204)
async def delete_bookmark(
    book_id: uuid.UUID,
    mark_id: uuid.UUID,
    request: Request,
    owner_id: OwnerId = Depends(_csrf_owner),
):
    return await _delete_reader_mark(
        kind="bookmark", book_id=book_id, mark_id=mark_id,
        request=request, owner_id=owner_id,
    )


@router.put("/books/{book_id}/preferences")
async def put_reader_preferences(
    book_id: uuid.UUID,
    preferences: ReaderPreferencesIn,
    request: Request,
    response: Response,
    owner_id: OwnerId = Depends(_csrf_owner),
):
    _private(response)
    key = _resource(owner_id, ResourceKind.BOOK, book_id)
    async with request.app.state.hosted_runtime.serialized(key):
        try:
            return await request.app.state.tenant_repository.upsert_reader_preferences(
                owner_id,
                book_id,
                **preferences.model_dump(),
            )
        except MissingTenantResourceError as exc:
            raise HTTPException(404, "unknown book") from exc


@router.put("/books/{book_id}/position")
async def update_position(
    book_id: uuid.UUID,
    update: PositionUpdateIn,
    request: Request,
    response: Response,
    owner_id: OwnerId = Depends(_csrf_owner),
):
    _private(response)
    key = _resource(owner_id, ResourceKind.BOOK, book_id)
    settings = request.app.state.settings
    async with request.app.state.hosted_runtime.serialized(key):
        try:
            return await request.app.state.tenant_repository.update_position(
                owner_id,
                book_id,
                cfi=update.cfi,
                offset=update.offset,
                completed_chapter=update.completed_chapter,
                expected_epoch=update.position_epoch,
                base_version=update.base_version,
                client_id=update.client_id,
                client_sequence=update.client_sequence,
                # With the read map the chapter counter moves when chapters are read, not when a
                # page is reached, as locally (app/api/reading.py put_position): a chapter the
                # reader said they have not read is never counted again by turning past it.
                move_counter=not (settings.reader_read_map and settings.reader_page_context),
            )
        except MissingTenantResourceError as exc:
            raise HTTPException(404, "unknown book") from exc
        except StalePositionEpochError as exc:
            raise HTTPException(
                409, "reading position was reset in another session; reload before continuing"
            ) from exc
        except FuturePositionVersionError as exc:
            raise HTTPException(
                409, "reading position version is ahead of the server; reload before continuing"
            ) from exc
        except InvalidPositionError as exc:
            raise HTTPException(422, str(exc)) from exc


@router.post("/books/{book_id}/position/reset")
async def reset_position(
    book_id: uuid.UUID,
    reset: PositionResetIn,
    request: Request,
    response: Response,
    owner_id: OwnerId = Depends(_csrf_owner),
):
    _private(response)
    key = _resource(owner_id, ResourceKind.BOOK, book_id)
    async with request.app.state.hosted_runtime.serialized(key):
        try:
            return await request.app.state.tenant_repository.reset_position(
                owner_id, book_id, reset.position_epoch
            )
        except MissingTenantResourceError as exc:
            raise HTTPException(404, "unknown book") from exc
        except StalePositionEpochError as exc:
            raise HTTPException(
                409, "reading position was reset in another session; reload before continuing"
            ) from exc


@router.get("/books/{book_id}/memory")
async def memory_snapshot(
    book_id: uuid.UUID,
    request: Request,
    response: Response,
    owner_id: OwnerId = Depends(_owner),
):
    _private(response)
    key = _resource(owner_id, ResourceKind.BOOK, book_id)
    async with request.app.state.hosted_runtime.serialized(key):
        snapshot = await request.app.state.tenant_repository.memory_snapshot(owner_id, book_id)
        if snapshot is None:
            raise HTTPException(404, "unknown book")
        return snapshot


@router.get("/books/{book_id}/memory-corrections")
async def memory_corrections(
    book_id: uuid.UUID,
    request: Request,
    response: Response,
    bookmark: int | None = Query(default=None, ge=0),
    owner_id: OwnerId = Depends(_owner),
):
    _private(response)
    key = _resource(owner_id, ResourceKind.BOOK, book_id)
    async with request.app.state.hosted_runtime.serialized(key):
        result = await request.app.state.tenant_repository.memory_corrections(
            owner_id, book_id, bookmark
        )
        if result is None:
            raise HTTPException(404, "unknown book")
        return result


@router.post("/books/{book_id}/memory-corrections")
async def correct_memory(
    book_id: uuid.UUID,
    body: HostedEntityCorrectionIn,
    request: Request,
    response: Response,
    owner_id: OwnerId = Depends(_csrf_owner),
):
    _private(response)
    key = _resource(owner_id, ResourceKind.BOOK, book_id)
    async with request.app.state.hosted_runtime.serialized(key):
        try:
            result = await request.app.state.tenant_repository.replace_memory_entity(
                owner_id,
                book_id,
                source_entity_id=body.source_entity_id,
                canonical_name=body.canonical_name,
                reason=body.reason,
                bookmark=body.bookmark,
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        if result is None:
            raise HTTPException(404, "unknown book")
        return result


@router.get("/costs")
async def list_costs(
    request: Request,
    response: Response,
    book_id: uuid.UUID | None = None,
    owner_id: OwnerId = Depends(_owner),
):
    _private(response)
    kind = ResourceKind.BOOK if book_id is not None else ResourceKind.COSTS
    key = _resource(owner_id, kind, book_id or owner_id.value)
    async with request.app.state.hosted_runtime.serialized(key):
        costs = await request.app.state.tenant_repository.list_costs(owner_id, book_id)
        if costs is None:
            raise HTTPException(404, "unknown book")
        return costs
