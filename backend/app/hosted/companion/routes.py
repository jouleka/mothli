"""The read-map, conversation and streamed chat routes in hosted mode, at the local paths (ADR
0041 parts 1, 2 and 5, section 8).

Each route runs the unchanged local modules (``app.read_map``, ``app.stream_conversation``)
against a ``HostedCompanionCatalog``, a ``HostedBookSource`` and, for the chat, a
``HostedBookStore`` bound to the signed-in reader. The owner comes from the session only
(``_owner`` / ``_csrf_owner``, which also apply CSRF and the per-owner request limit). A book that
is not this reader's is unknown: the same 404 as a book that does not exist.

These are plain ``def`` routes: FastAPI runs them in its thread pool, and every adapter call is one
short synchronous transaction (ADR 0041 section 2), so a streamed reply holds no database
connection while a model answers. Only the streamed chat calls a model: the platform's, paid by
the owner of Mothli and held to the reader's caps by the catalog's spend methods (section 6).
"""

from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.api.reading import ReadChapters, ReadPage
from app.ask import AskRequest
from app.hosted.companion import CompanionDatabaseBusy
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
from app.stream_conversation import stream_read_prefix

router = APIRouter(prefix="/api/books/{book_id}", tags=["hosted-library"])


class _NotYet:
    """The background workers handed to the streamed chat (``notes_builder``, ``recollector``).

    The chat calls ``ensure`` only while chapter notes or recollections are on, which hosted
    settings refuse until parts 6 and 7 give them durable jobs; until then this does nothing.
    """

    __slots__ = ()

    def ensure(self, book_id) -> None:
        return None


_NOT_YET = _NotYet()


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
    Project Gutenberg's own page) is accepted and adds nothing.
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
        return {**point, "skipped": skipped_before(place)}
    except CompanionDatabaseBusy as exc:
        raise _busy(exc) from exc
    except LookupError as exc:
        raise HTTPException(404, "unknown book") from exc
    except RuntimeError as exc:
        raise HTTPException(409, "reading pass changed; reload before continuing") from exc
    except ValueError as exc:
        raise HTTPException(422, "page could not be verified against the source") from exc


@router.put("/read-chapters")
def put_read_chapters(book_id: uuid.UUID, body: ReadChapters, request: Request,
                      owner_id: OwnerId = Depends(_csrf_owner)):
    """The reader's own word: "I've read this" or "I haven't read this" for whole chapters."""
    catalog, source, settings = _bound(request, owner_id)
    book = str(book_id)
    try:
        mark_chapters(book, body.atoms, read=body.read, source=body.source, catalog=catalog,
                      settings=settings, worker=source)
        return sync_counter(book, catalog, settings, source)
    except CompanionDatabaseBusy as exc:
        raise _busy(exc) from exc
    except LookupError as exc:
        raise HTTPException(404, "unknown book") from exc
    except ValueError as exc:
        raise HTTPException(422, "chapters could not be verified against the source") from exc


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
                                    notes_builder=_NOT_YET, recollector=_NOT_YET)
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
    """Forget this book's conversation. Locally, with recollections on, what the reader said is
    distilled first; hosted mode refuses recollections until part 7, and then clearing just
    deletes (ADR 0041 section 5), so this stays a plain ``clear_turns``."""
    catalog, _source, _settings = _bound(request, owner_id)
    book = str(book_id)
    try:
        if catalog.get_book(book) is None:
            raise HTTPException(404, "unknown book")
        catalog.clear_turns(book)
    except CompanionDatabaseBusy as exc:
        raise _busy(exc) from exc
