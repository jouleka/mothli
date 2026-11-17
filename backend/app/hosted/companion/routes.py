"""The read-map, conversation and streamed chat routes in hosted mode, at the local paths (ADR
0041 parts 1, 2, 5 and 7, section 8): inside a book (``router``) and across the reader's books
(``reader_router``: the home chat, the profile, what Mothli remembers).

Each route runs the unchanged local modules (``app.read_map``, ``app.stream_conversation``,
``app.home_conversation``, ``app.recollections``) against a ``HostedCompanionCatalog``, a
``HostedBookSource`` and, for the chats, a ``HostedBookStore`` bound to the signed-in reader. The owner comes from the session only
(``_owner`` / ``_csrf_owner``, which also apply CSRF and the per-owner request limit). A book that
is not this reader's is unknown: the same 404 as a book that does not exist.

These are plain ``def`` routes: FastAPI runs them in its thread pool, and every adapter call is one
short synchronous transaction (ADR 0041 section 2), so a streamed reply holds no database
connection while a model answers. Only the streamed chat calls a model: the platform's, paid by
the owner of Mothli and held to the reader's caps by the catalog's spend methods (section 6).
Where the local routes start chapter notes (a finished chapter, a reply) or recollections (a
reply), these add a durable ``chapter_notes`` or ``recollect`` job for the worker instead (parts 6
and 7); none of them waits for it.
"""

from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.api.profile import reader_profile
from app.api.reading import ReadChapters, ReadPage
from app.ask import AskRequest
from app.home_conversation import HomeAsk, home_turns, read_maps, shelf_books, stream_home
from app.hosted.companion import CompanionDatabaseBusy
from app.hosted.companion.jobs import ChapterNotesQueue, RecollectQueue
from app.hosted.companion.memory import HostedBookStore
from app.hosted.companion.source import HostedBookSource
from app.hosted.companion.store import HostedCompanionCatalog, ReplyCatalog
from app.hosted.tenant.api import _csrf_owner, _owner
from app.hosted.tenant.models import OwnerId
from app.read_frontier import ReadAnchor
from app.read_map import (
    chapter_fractions,
    mark_chapters,
    read_snapshot,
    record_page,
    sync_counter,
    visible_turns,
)
from app.reader_place import skipped_before
from app.recollections import listed
from app.stream_conversation import stream_read_prefix

router = APIRouter(prefix="/api/books/{book_id}", tags=["hosted-library"])


def _recollector(request: Request, owner_id: OwnerId) -> RecollectQueue:
    """This reader's ``recollector``: ``ensure`` adds a durable ``recollect`` job (part 7). The
    chats call it only while recollections are on."""
    return RecollectQueue(request.app.state.companion_dsn, owner_id)


def _notes_wanted(settings) -> bool:
    """The local routes' condition for asking for chapter notes (``app/api/reading.py``)."""
    return bool(settings.reader_story_notes and settings.reader_conversation_jev)


def _notes_queue(request: Request, owner_id: OwnerId) -> ChapterNotesQueue:
    """This reader's ``notes_builder``: ``ensure`` adds a durable ``chapter_notes`` job."""
    return ChapterNotesQueue(request.app.state.companion_dsn, owner_id)


def _bound(request: Request, owner_id: OwnerId, catalog_type=HostedCompanionCatalog):
    """(catalog, source, settings) for this reader, or 404 while the read map is off."""
    settings = request.app.state.settings
    if not (settings.reader_page_context and settings.reader_read_map):
        raise HTTPException(404, "the read map is not enabled")
    catalog = catalog_type(request.app.state.companion_dsn, owner_id)
    source = HostedBookSource(request.app.state.object_storage, catalog,
                              request.app.state.companion_sources)
    return catalog, source, settings


def _busy(exc: CompanionDatabaseBusy) -> HTTPException:
    return HTTPException(503, "the reading service is busy; try again shortly")


@router.get("/read-map")
def get_read_map(book_id: uuid.UUID, request: Request, owner_id: OwnerId = Depends(_owner)):
    """How much of each chapter the reader has read, as a fraction of its text."""
    catalog, source, settings = _bound(request, owner_id)
    try:
        return {"chapters": chapter_fractions(str(book_id), catalog, settings, source)}
    except CompanionDatabaseBusy as exc:
        raise _busy(exc) from exc
    except LookupError as exc:
        raise HTTPException(404, "unknown book") from exc
    except ValueError as exc:
        raise HTTPException(409, "read map could not be verified against the source") from exc


@router.put("/read-page")
def put_read_page(book_id: uuid.UUID, body: ReadPage, request: Request,
                  owner_id: OwnerId = Depends(_csrf_owner)):
    """The reader has read one page: both ends are checked against the source before it counts.

    A skimmed page is checked the same way and only remembered. A page in no chapter (a cover,
    Project Gutenberg's own page) is accepted and adds nothing. A page that finishes a chapter
    asks for that chapter's notes, as locally (a durable job here).
    """
    catalog, source, settings = _bound(request, owner_id)
    book = str(book_id)
    try:
        finished = record_page(book, body.start, body.end, catalog, settings, source,
                               skimmed=body.skimmed)
        if body.skimmed or finished is None:          # skimmed, or a page in no chapter
            return sync_counter(book, catalog, settings, source)
        point, place = sync_counter(book, catalog, settings, source, with_place=True)
        # If chapters before this page were gone past, the app may ask once whether they were read.
        point = {**point, "skipped": skipped_before(place)}
    except CompanionDatabaseBusy as exc:
        raise _busy(exc) from exc
    except LookupError as exc:
        raise HTTPException(404, "unknown book") from exc
    except RuntimeError as exc:
        raise HTTPException(409, "reading pass changed; reload before continuing") from exc
    except ValueError as exc:
        raise HTTPException(422, "page could not be verified against the source") from exc
    if finished and _notes_wanted(settings):
        _notes_queue(request, owner_id).ensure(book)
    return point


@router.put("/read-chapters")
def put_read_chapters(book_id: uuid.UUID, body: ReadChapters, request: Request,
                      owner_id: OwnerId = Depends(_csrf_owner)):
    """The reader's own word: "I've read this" or "I haven't read this" for whole chapters."""
    catalog, source, settings = _bound(request, owner_id)
    book = str(book_id)
    try:
        mark_chapters(book, body.atoms, read=body.read, source=body.source, catalog=catalog,
                      settings=settings, worker=source)
        point = sync_counter(book, catalog, settings, source)
    except CompanionDatabaseBusy as exc:
        raise _busy(exc) from exc
    except LookupError as exc:
        raise HTTPException(404, "unknown book") from exc
    except ValueError as exc:
        raise HTTPException(422, "chapters could not be verified against the source") from exc
    if body.read and _notes_wanted(settings):
        _notes_queue(request, owner_id).ensure(book)
    return point


@router.get("/read-frontier")
def get_read_frontier(book_id: uuid.UUID, request: Request,
                      owner_id: OwnerId = Depends(_owner)):
    """Where the reader is in the read map; also brings the chapter counter in line with it."""
    catalog, source, settings = _bound(request, owner_id)
    try:
        return sync_counter(str(book_id), catalog, settings, source)
    except CompanionDatabaseBusy as exc:
        raise _busy(exc) from exc
    except LookupError as exc:
        raise HTTPException(404, "unknown book") from exc
    except ValueError as exc:
        raise HTTPException(409, "read boundary could not be verified against the source") from exc


@router.put("/read-frontier")
def put_read_frontier(book_id: uuid.UUID, body: ReadAnchor, request: Request,
                      owner_id: OwnerId = Depends(_csrf_owner)):
    """With the read map a page counts when it is read, not when it is reached: the anchor moves
    nothing, and the answer is the map's point, as locally."""
    catalog, source, settings = _bound(request, owner_id)
    try:
        return sync_counter(str(book_id), catalog, settings, source)
    except CompanionDatabaseBusy as exc:
        raise _busy(exc) from exc
    except LookupError as exc:
        raise HTTPException(404, "unknown book") from exc
    except RuntimeError as exc:
        raise HTTPException(409, "reading pass changed; reload before continuing") from exc
    except ValueError as exc:
        raise HTTPException(422, "page anchor could not be verified against the source") from exc


@router.post("/ask/stream")
def ask_the_book_stream(book_id: uuid.UUID, body: AskRequest, request: Request,
                        owner_id: OwnerId = Depends(_csrf_owner)):
    """Server-sent events: one ``sentence`` per checked sentence, then ``done`` or ``error``.

    The local route's body over the hosted adapters: the unchanged ``stream_read_prefix`` with this
    reader's catalog, story memory and source, and the platform's clients. A book that is not this
    reader's is refused with 404 while the read position is taken, before any model is called or
    anything is reserved.
    """
    settings = request.app.state.settings
    if not (settings.reader_conversation_stream and settings.reader_page_context):
        raise HTTPException(404, "streamed replies are not enabled")
    if body.read_frontier is None:
        raise HTTPException(422, "a streamed reply needs the confirmed read position")
    # One reply: once its first call is admitted under the caps, the rest of it finishes.
    catalog, source, settings = _bound(request, owner_id, ReplyCatalog)
    # One story memory per request: every sentence's check of this reply reads the same snapshot.
    store = HostedBookStore(request.app.state.companion_dsn, owner_id)
    try:
        events = stream_read_prefix(str(book_id), body, catalog, store, settings,
                                    request.app.state.platform_client, source,
                                    notes_builder=_notes_queue(request, owner_id),
                                    recollector=_recollector(request, owner_id))
    except CompanionDatabaseBusy as exc:
        raise _busy(exc) from exc
    return StreamingResponse(
        ("data: " + json.dumps(event, ensure_ascii=False) + "\n\n" for event in events),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@router.get("/conversation")
def stored_conversation(book_id: uuid.UUID, request: Request,
                        owner_id: OwnerId = Depends(_owner)):
    """What was said and shown in this book and may be shown again, oldest first: the local
    route's body over the hosted adapters, through the unchanged ``visible_turns`` (ADR 0041
    part 2). A turn is shown only while everything it was written from is still read."""
    catalog, source, settings = _bound(request, owner_id)
    book = str(book_id)
    try:
        point, _chapters, _state, _manifest, record, read_map = read_snapshot(
            book, catalog, settings, source)
        turns = visible_turns(book, record, read_map, catalog, limit=80)
    except CompanionDatabaseBusy as exc:
        raise _busy(exc) from exc
    except LookupError as exc:
        raise HTTPException(404, "unknown book") from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(409, "reading context changed; refresh before continuing") from exc
    # The local response, field for field: no turn id, which orders the conversation and is
    # never handed to the reader.
    return {"read_frontier": point, "turns": [
        {"role": turn["role"], "content": turn["content"], "atom": turn["atom"],
         "reaction": bool(turn["reaction"]), "created_at": turn["created_at"]} for turn in turns]}


@router.delete("/conversation", status_code=204)
def clear_conversation(book_id: uuid.UUID, request: Request,
                       owner_id: OwnerId = Depends(_csrf_owner)):
    """Forget this book's conversation and how far it was looked at for recollections. Locally,
    with recollections on, what the reader said and nobody has looked at yet is distilled first;
    hosted clearing just deletes (ADR 0041 section 5): keeping it for a job would mean storing
    cleared text. Notes already kept stay."""
    catalog, _source, _settings = _bound(request, owner_id)
    book = str(book_id)
    try:
        if catalog.get_book(book) is None:
            raise HTTPException(404, "unknown book")
        catalog.clear_turns(book)
    except CompanionDatabaseBusy as exc:
        raise _busy(exc) from exc


# ---- the reader across their books: the home chat, the profile, what Mothli remembers (part 7) ----

# Tagged as every tenant route, so the inventory gate counts it (test_tenant_postgres.py).
reader_router = APIRouter(prefix="/api/reader", tags=["hosted-library"])


class _NoHistory:
    """The reading history imported from other apps, which hosted mode does not keep until part
    11: nothing read elsewhere (``stream_home`` is given ``elsewhere=[]`` for the same reason)."""

    __slots__ = ()

    def list(self, limit):
        return 0, []


def _home(request: Request, owner_id: OwnerId, catalog_type=HostedCompanionCatalog):
    """(catalog, source, settings) for the home conversation, or 404 while it is off (the local
    route's condition)."""
    settings = request.app.state.settings
    if not (settings.reader_read_map and settings.reader_page_context
            and settings.reader_conversation_stream):
        raise HTTPException(404, "the home conversation is not enabled")
    return _bound(request, owner_id, catalog_type)


def _remembering(request: Request, owner_id: OwnerId) -> HostedCompanionCatalog:
    """This reader's catalog for what Mothli remembers, or 404 while recollections are off."""
    settings = request.app.state.settings
    if not (settings.reader_recollections and settings.reader_read_map
            and settings.reader_page_context):
        raise HTTPException(404, "recollections are not enabled")
    return HostedCompanionCatalog(request.app.state.companion_dsn, owner_id)


@reader_router.post("/ask/stream")
def ask_home(body: HomeAsk, request: Request, owner_id: OwnerId = Depends(_csrf_owner)):
    """Server-sent events, as for a book: one ``sentence`` per checked sentence, then ``done`` or
    ``error``. The local route's body over the hosted adapters: the unchanged ``stream_home`` with
    this reader's shelf, story memory and sources, and the platform's clients. A reply that names
    a book from the free library carries it in ``suggested``, which the app offers to add through
    the hosted free library (part 11), as locally."""
    # One reply: once its first call is admitted under the caps, the rest of it finishes.
    catalog, source, settings = _home(request, owner_id, ReplyCatalog)
    store = HostedBookStore(request.app.state.companion_dsn, owner_id)
    try:
        events = stream_home(body, catalog, store, settings, request.app.state.platform_client,
                             source, [], recollector=_recollector(request, owner_id))
    except CompanionDatabaseBusy as exc:
        raise _busy(exc) from exc
    return StreamingResponse(
        ("data: " + json.dumps(event, ensure_ascii=False) + "\n\n" for event in events),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@reader_router.get("/conversation")
def home_conversation(request: Request, owner_id: OwnerId = Depends(_owner)):
    """What was said on the home screen and may be shown again: see ``home_turns``."""
    catalog, source, settings = _home(request, owner_id)
    try:
        turns = home_turns(catalog, read_maps(shelf_books(catalog, settings, source)), limit=80)
    except CompanionDatabaseBusy as exc:
        raise _busy(exc) from exc
    return {"turns": [{"role": turn["role"], "content": turn["content"],
                       "created_at": turn["created_at"]} for turn in turns]}


@reader_router.delete("/conversation", status_code=204)
def clear_home_conversation(request: Request, owner_id: OwnerId = Depends(_csrf_owner)):
    """Forget the home conversation, as for a book's: it is deleted, nothing of it is distilled
    afterwards, and notes already kept stay."""
    catalog, _source, _settings = _home(request, owner_id)
    try:
        catalog.clear_turns(None)
    except CompanionDatabaseBusy as exc:
        raise _busy(exc) from exc


@reader_router.get("/profile")
def profile(request: Request, owner_id: OwnerId = Depends(_owner)):
    """The reader across their books, from the read map: the unchanged ``reader_profile``. Nothing
    read in another app until history import (part 11)."""
    catalog, source, settings = _bound(request, owner_id)
    try:
        return reader_profile(catalog, settings, _NoHistory(), source)
    except CompanionDatabaseBusy as exc:
        raise _busy(exc) from exc


@reader_router.get("/recollections")
def recollections(request: Request, owner_id: OwnerId = Depends(_owner)):
    """What Mothli remembers about the reader, newest first: the unchanged ``listed``. Each note's
    id is its public id; a note about pages no longer read is counted in ``hidden``."""
    catalog = _remembering(request, owner_id)
    try:
        return listed(catalog)
    except CompanionDatabaseBusy as exc:
        raise _busy(exc) from exc


@reader_router.delete("/recollections/{public_id}", status_code=204)
def forget_recollection(public_id: str, request: Request,
                        owner_id: OwnerId = Depends(_csrf_owner)):
    """Forget one note. Another reader's note, a missing one and a malformed id are the same 404."""
    catalog = _remembering(request, owner_id)
    try:
        forgotten = catalog.delete_recollection(public_id)
    except CompanionDatabaseBusy as exc:
        raise _busy(exc) from exc
    if not forgotten:
        raise HTTPException(404, "unknown recollection")


@reader_router.delete("/recollections", status_code=204)
def forget_recollections(request: Request, owner_id: OwnerId = Depends(_csrf_owner)):
    """Forget every note, shown or held back, and everything said so far that might become one."""
    catalog = _remembering(request, owner_id)
    try:
        catalog.clear_recollections()
    except CompanionDatabaseBusy as exc:
        raise _busy(exc) from exc
