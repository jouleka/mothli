"""The read-map, conversation, streamed chat and story memory routes in hosted mode, at the local
paths (ADR 0041 parts 1, 2, 5 and 7, section 8; ADR 0042 part 4): inside a book (``router``) and
across the reader's books (``reader_router``: the home chat, the profile, what Mothli remembers).

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

The gate (ADR 0045 step 5, ``app.hosted.companion.gate``): every route that serves what Mothli
wrote or knows (the two chats, the two stored conversations, the cast, a person's card, "Story so
far" and its progress) takes its reader through ``_mothli`` or ``_csrf_mothli``, which answer an
ended reader ``402 mothli_plan`` before the route's body runs. Reading is never gated: the read
map, page reports (taken in every state, so the place is right when Mothli is used again), "I've
read this", clearing a conversation, the profile and what Mothli remembers (the reader's own data,
to read and to forget).
"""

from __future__ import annotations

import json
import uuid

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict

from app.api.profile import reader_profile
from app.api.reading import ReadChapters, ReadPage
from app.ask import AskRequest
from app.home_conversation import HomeAsk, home_turns, read_maps, shelf_books, stream_home
from app.hosted.companion import CompanionDatabaseBusy, gate
from app.hosted.companion.jobs import ChapterNotesQueue, RecollectQueue, StoryMemoryQueue
from app.hosted.companion.memory import ContentSnapshot, HostedBookStore, StoryMemoryUnavailable
from app.hosted.companion.source import HostedBookSource
from app.hosted.companion.store import HostedCompanionCatalog, ReplyCatalog
from app.hosted.tenant.api import _csrf_owner, _owner
from app.hosted.tenant.models import OwnerId
from app.ingest.manifest import AtomSetMismatch
from app.memory_views import (
    character_payload,
    clamp,
    graph_payload,
    ingest_payload,
    notes_payload,
)
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


def _going(catalog) -> bool:
    """Whether to ask for this reader's background work at all (ADR 0045 section 5): not once
    their free weeks are over and they do not pay (``ended``). A new reader is asked for as
    everyone is; the claim holds them to the warm-up. A database that cannot be reached asks
    for nothing, as a queue that cannot be reached adds nothing: the next page or question asks
    again. Nothing here is told to the reader."""
    try:
        return catalog.plan()["state"] != "ended"
    except (psycopg.Error, CompanionDatabaseBusy):
        return False


def _standing(request: Request, owner_id: OwnerId) -> dict:
    """Where the reader stands (``gate.standing``); a database that cannot be reached is the
    busy answer, so nothing is served without the gate's yes."""
    try:
        return gate.standing(request.app.state.companion_dsn, owner_id)
    except (CompanionDatabaseBusy, psycopg.OperationalError) as exc:
        raise HTTPException(503, "the reading service is busy; try again shortly") from exc


def _gate(request: Request, owner_id: OwnerId) -> OwnerId:
    """The gate (ADR 0045 step 5): an ended reader is answered ``402 mothli_plan`` with why;
    anyone else goes on exactly as before. It reads the reader's plan alone."""
    standing = _standing(request, owner_id)
    if standing["state"] == "ended":
        raise gate.refusal(standing["why"])
    return owner_id


def _mothli(request: Request, owner_id: OwnerId = Depends(_owner)) -> OwnerId:
    """The signed-in reader of a route that serves Mothli, through the gate. As a dependency it
    runs before the route's body, so nothing of the reader's is opened for an ended reader."""
    return _gate(request, owner_id)


def _csrf_mothli(request: Request, owner_id: OwnerId = Depends(_csrf_owner)) -> OwnerId:
    """As ``_mothli``, for a route that writes (the CSRF token checked first)."""
    return _gate(request, owner_id)


def _story_memory(request: Request, owner_id: OwnerId, book: str) -> None:
    """Ask for the book's story memory (ADR 0044 section 3), for every hosted reader's book,
    whichever way the file came, while story memory is on. Not waited for: the run decides how
    far to build, and builds nothing before the reader has read a page."""
    if request.app.state.settings.hosted_story_memory:
        StoryMemoryQueue(request.app.state.companion_dsn, owner_id).ensure(book)


def _finished(request: Request, owner_id: OwnerId, book: str) -> None:
    """A chapter was finished: ask for its notes and for the book's story memory (ADR 0042
    section 2, ADR 0044 section 3). Neither is waited for."""
    if _notes_wanted(request.app.state.settings):
        _notes_queue(request, owner_id).ensure(book)
    _story_memory(request, owner_id, book)


def _bound(request: Request, owner_id: OwnerId, catalog_type=HostedCompanionCatalog, store=None):
    """(catalog, source, settings) for this reader, or 404 while the read map is off. With the
    reader's story memory (``store``), the source is checked against it too."""
    settings = request.app.state.settings
    if not (settings.reader_page_context and settings.reader_read_map):
        raise HTTPException(404, "the read map is not enabled")
    catalog = catalog_type(request.app.state.companion_dsn, owner_id)
    source = HostedBookSource(request.app.state.object_storage, catalog,
                              request.app.state.companion_sources, store)
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
    asks for that chapter's notes, as locally (a durable job here), and for the book's story
    memory; so does the first page of the book read and not skimmed (ADR 0044 section 3: the
    first chapter's notes are made when a reader first reads a page, not when the file is added).
    """
    catalog, source, settings = _bound(request, owner_id)
    book = str(book_id)
    try:
        held = catalog.get_book(book) if settings.hosted_story_memory and not body.skimmed else None
        first = held is not None and not catalog.has_read_page(book,
                                                               incarnation=held["incarnation"])
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
    # The page is recorded above in every state; only asking for Mothli's background work
    # follows where the reader stands (ADR 0045 section 5).
    if (finished or first) and _going(catalog):
        if finished:
            _finished(request, owner_id, book)
        else:
            _story_memory(request, owner_id, book)
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
    if body.read and _going(catalog):
        _finished(request, owner_id, book)
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
                        owner_id: OwnerId = Depends(_csrf_mothli)):
    """Server-sent events: one ``sentence`` per checked sentence, then ``done`` or ``error``.

    The local route's body over the hosted adapters: the unchanged ``stream_read_prefix`` with this
    reader's catalog, story memory and source, and the platform's clients. A book that is not this
    reader's is refused with 404 while the read position is taken, before any model is called or
    anything is reserved. Behind the gate.
    """
    settings = request.app.state.settings
    # The condition the session reports as ``chat`` (``Settings.companion_chat``).
    if not settings.companion_chat():
        raise HTTPException(404, "streamed replies are not enabled")
    if body.read_frontier is None:
        raise HTTPException(422, "a streamed reply needs the confirmed read position")
    # One reply: once its first call is admitted under the caps, the rest of it finishes.
    catalog, source, settings = _bound(request, owner_id, ReplyCatalog)
    # One story memory per request: every sentence's check of this reply reads the same snapshot.
    store = HostedBookStore(request.app.state.companion_dsn, owner_id)
    try:
        # A question starts the reader's two free weeks, once (ADR 0045 step 1), before anything
        # is read or reserved. Nothing else here changes with them.
        catalog.start_free_weeks()
        # ADR 0045 section 5: past the gate the reader is not ended, so the question asks for
        # the book's story memory, and a reader who has become paying again (or a new one, whose
        # question just started the free weeks) has it built up to their place as the read map
        # gives it; their chapter notes are asked for after the reply, as always.
        _story_memory(request, owner_id, str(book_id))
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
                        owner_id: OwnerId = Depends(_mothli)):
    """What was said and shown in this book and may be shown again, oldest first: the local
    route's body over the hosted adapters, through the unchanged ``visible_turns`` (ADR 0041
    part 2). A turn is shown only while everything it was written from is still read. Behind
    the gate: an ended reader's chat panel holds the invitation in its place, and the turns are
    kept and shown through the same filter when they return (ADR 0045 sections 6 and 9); the
    copy of their data holds them meanwhile."""
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


# ---- the story memory screens: the cast, a person's card, the story so far, progress (ADR 0042
# part 4). The bodies are the local ones (``app.memory_views``), over this reader's story memory.
# Each is behind the gate; none starts the free weeks (the app fetches the cast and the notes by
# itself; the reader's own tap calls ``POST /api/reader/mothli/start``).

_BOOKMARK = Query(default=None, ge=0, description="explicit bookmark; clamped to the reader's place")


def _place(state) -> int:
    """The last chapter the views may show of this reader's book: the read map's
    ``finished_through``, and never past the chapter counter, which is ``memory_through``
    (ADR 0042 part 5): only the map writes it, and the door reads it as the place."""
    return min(state["bookmark"], state["finished_through"])


def _remembered(request: Request, owner_id: OwnerId, book: str):
    """The local views' route-boundary prologue (``_effective``) over the hosted adapters: a book
    of this reader's, its source checked against its story memory, then the place, how much
    memory is complete and how building it goes. ``(state, memory, place, durable, job)``.

    A book that is not this reader's is the same 404 as one that does not exist. Every book of
    theirs has story memory in principle (ADR 0044): before their first finished chapter it is
    the same empty one, whether or not an edition of their file exists, as locally for a book
    nothing was extracted from. How building goes (``job``) is told only while the reader has
    finished chapters the door does not hand them yet: that is the only time it can matter to
    them, and at any other time it would differ between the first reader of a file and a later
    one (the first's run is building the next chapter, a later one's has nothing to build)."""
    store = HostedBookStore(request.app.state.companion_dsn, owner_id)
    catalog, source, settings = _bound(request, owner_id, store=store)
    try:
        _point, _read, state, manifest, _book, _held = read_snapshot(book, catalog, settings,
                                                                     source)
        with store.book(book) as mem:
            durable = mem.completion_frontier(manifest["atoms"])
        place = _place(state)
        job = store.memory_job(book) if durable < place else None
    except (CompanionDatabaseBusy, StoryMemoryUnavailable) as exc:
        raise HTTPException(503, "the reading service is busy; try again shortly") from exc
    except LookupError as exc:
        raise HTTPException(404, "unknown book") from exc
    except AtomSetMismatch as exc:
        raise HTTPException(409, "atom-set mismatch (re-import the book)") from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(409, "story memory could not be verified against the source") from exc
    return state, ContentSnapshot(mem, manifest), place, durable, job


@router.get("/graph")
def story_graph(book_id: uuid.UUID, request: Request, bookmark: int | None = _BOOKMARK,
                owner_id: OwnerId = Depends(_mothli)):
    """The people met so far, and how they are tied: the local ``/graph``."""
    _state, memory, place, durable, _job = _remembered(request, owner_id, str(book_id))
    return graph_payload(memory, clamp(bookmark, place, durable))


@router.get("/character/{entity_id}")
def story_character(book_id: uuid.UUID, entity_id: str, request: Request,
                    bookmark: int | None = _BOOKMARK, owner_id: OwnerId = Depends(_mothli)):
    """One person's card: the local ``/character/{id}``. A person not met yet, a malformed id
    and an id of no one are the same 404."""
    _state, memory, place, durable, _job = _remembered(request, owner_id, str(book_id))
    try:
        person = str(uuid.UUID(entity_id))
    except ValueError:
        person = None
    card = person and character_payload(memory, clamp(bookmark, place, durable), person)
    if not card:
        raise HTTPException(404, "unknown character")
    return card


@router.get("/notes")
def story_notes(book_id: uuid.UUID, request: Request, bookmark: int | None = _BOOKMARK,
                owner_id: OwnerId = Depends(_mothli)):
    """The story so far, chapter by chapter: the local ``/notes``. A chapter Mothli could not
    make notes on (ADR 0042 section 2) is marked ``unremembered``, so the panel says so in
    place of its empty summary; local mode never keeps such a chapter."""
    _state, memory, place, durable, _job = _remembered(request, owner_id, str(book_id))
    eff = clamp(bookmark, place, durable)
    notes = notes_payload(memory, eff)
    unremembered = set(memory.view(eff).unremembered())
    for chapter in notes["chapters"]:
        if chapter["revealed_at"] in unremembered:
            chapter["unremembered"] = True
    return notes


@router.get("/ingest")
def story_progress(book_id: uuid.UUID, request: Request, owner_id: OwnerId = Depends(_mothli)):
    """How many chapters' story memory is complete, and whether more is on its way (``running``)
    or waits for the next UTC day (``waiting``), told only while the reader has finished chapters
    whose notes they are not handed yet (``_remembered``): the local ``/ingest``."""
    state, _memory, _place_, durable, job = _remembered(request, owner_id, str(book_id))
    return ingest_payload(state["ingest_progress"], durable,
                          {"status": job or "idle", "flags": [], "error": None})


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
    if not request.app.state.settings.companion_chat():
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
def ask_home(body: HomeAsk, request: Request, owner_id: OwnerId = Depends(_csrf_mothli)):
    """Server-sent events, as for a book: one ``sentence`` per checked sentence, then ``done`` or
    ``error``. The local route's body over the hosted adapters: the unchanged ``stream_home`` with
    this reader's shelf, story memory and sources, and the platform's clients. A reply that names
    a book from the free library carries it in ``suggested``, which the app offers to add through
    the hosted free library (part 11), as locally."""
    # One reply: once its first call is admitted under the caps, the rest of it finishes.
    catalog, source, settings = _home(request, owner_id, ReplyCatalog)
    store = HostedBookStore(request.app.state.companion_dsn, owner_id)
    try:
        catalog.start_free_weeks()          # as in a book: a question starts the free weeks
        events = stream_home(body, catalog, store, settings, request.app.state.platform_client,
                             source, [], recollector=_recollector(request, owner_id))
    except CompanionDatabaseBusy as exc:
        raise _busy(exc) from exc
    return StreamingResponse(
        ("data: " + json.dumps(event, ensure_ascii=False) + "\n\n" for event in events),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@reader_router.get("/conversation")
def home_conversation(request: Request, owner_id: OwnerId = Depends(_mothli)):
    """What was said on the home screen and may be shown again: see ``home_turns``. Behind the
    gate, as a book's stored conversation is."""
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


# ---- the start of the free weeks by the reader's own tap (ADR 0045 section 2, step 5) ----------

class MothliStart(BaseModel):
    """The book the reader is in, if any: its background work is asked for, as a question's is."""

    model_config = ConfigDict(extra="forbid")
    book_id: uuid.UUID | None = None


@reader_router.post("/mothli/start")
def start_mothli(body: MothliStart, request: Request, owner_id: OwnerId = Depends(_csrf_owner)):
    """The reader opened the cast or "Story so far" themselves: their first use of Mothli, as a
    question is. Unless they are ended, it starts their free weeks once (``start_free_weeks``,
    exactly as a question does) and asks for the book's chapter notes and story memory, which
    build what is missing up to their place (section 5). An ended reader is only told where they
    stand: nothing is stamped or asked for. The answer is the session's ``mothli``. The app's own
    fetches (the cast for names in the page, the notes after a chapter) never call it."""
    if not request.app.state.settings.companion_chat():
        raise HTTPException(404, "Mothli is not enabled")
    standing = _standing(request, owner_id)
    if standing["state"] != "ended":
        try:
            HostedCompanionCatalog(request.app.state.companion_dsn, owner_id).start_free_weeks()
        except CompanionDatabaseBusy as exc:
            raise _busy(exc) from exc
        if body.book_id is not None:            # a book that is not this reader's adds nothing
            _finished(request, owner_id, str(body.book_id))
        standing = _standing(request, owner_id)
    return standing
