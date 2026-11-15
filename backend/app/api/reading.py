"""Position routes (ADR 0007 D-A10 + LIT-12). The reader (LIT-13) reports its CFI plus the monotonic
char OFFSET (order-isomorphic to the CFI — the reader computes it from the CFI + chapter ranges); the
server derives the integer bookmark through the frontier over the import-time atom manifest and
persists it as a monotonic high-water (the catalog's SQL MAX — a backward page never lowers it,
matching ``max(cfi_to_bookmark(cfi), stored_bookmark)``). Every route here FAILS CLOSED on a corrupt /
store-divergent manifest (D-A10's version check made concrete)."""
from fastapi import APIRouter, Depends, HTTPException, Request
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt

from app.deps import book_lifecycle, get_catalog, get_settings, get_store, get_worker
from app.ingest.manifest import AtomSetMismatch, assert_matches_store, bounds_of, load_manifest
from app.reader import frontier
from app.read_frontier import ReadAnchor, confirm_anchor, current_snapshot
from app.read_map import chapter_fractions, import_offer, mark_chapters, record_page, sync_counter
from app.reader_place import skipped_before

router = APIRouter(prefix="/api/books/{book_id}", tags=["reading"])


@router.get("/read-frontier", dependencies=[Depends(book_lifecycle)])
def get_read_frontier(book_id: str, catalog=Depends(get_catalog), settings=Depends(get_settings),
                      worker=Depends(get_worker)):
    if not settings.reader_page_context:
        raise HTTPException(404, "page context is not enabled")
    try:
        if settings.reader_read_map:
            return sync_counter(book_id, catalog, settings, worker)
        return current_snapshot(book_id, catalog, settings, worker)[0]
    except LookupError as exc:
        raise HTTPException(404, "unknown book") from exc
    except (ValueError, AtomSetMismatch) as exc:
        raise HTTPException(409, "read boundary could not be verified against the source") from exc


@router.put("/read-frontier", dependencies=[Depends(book_lifecycle)])
def put_read_frontier(book_id: str, body: ReadAnchor, catalog=Depends(get_catalog),
                      settings=Depends(get_settings), worker=Depends(get_worker)):
    if not settings.reader_page_context:
        raise HTTPException(404, "page context is not enabled")
    try:
        if settings.reader_read_map:
            # With the read map a page counts when it is read, not when it is reached.
            return sync_counter(book_id, catalog, settings, worker)
        return confirm_anchor(book_id, body, catalog, settings, worker)
    except LookupError as exc:
        raise HTTPException(404, "unknown book") from exc
    except RuntimeError as exc:
        raise HTTPException(409, "reading pass changed; reload before continuing") from exc
    except (ValueError, AtomSetMismatch) as exc:
        raise HTTPException(422, "page anchor could not be verified against the source") from exc


class PositionIn(BaseModel):
    # untrusted input: a real CFI is well under 2 KB — the cap stops unbounded strings from being
    # stored verbatim in reading_state and echoed back (the LIT-11 global body cap is separate)
    cfi: str = Field(max_length=4096)
    # StrictInt: a stringified/boolean/float offset is REJECTED, never coerced — a malformed offset
    # feeding the frontier could otherwise mis-derive the spoiler bookmark (fail-closed, D-A10).
    offset: StrictInt = Field(ge=0)
    # Offline coalescing keeps the latest resume cursor while retaining the furthest geometry
    # observed in the same reset epoch. It is not a caller-supplied chapter ordinal.
    high_water_offset: StrictInt | None = Field(default=None, ge=0, le=2**63 - 1)
    # Epoch zero keeps pre-LIT-17 clients compatible until the first explicit reset. Thereafter a
    # missing epoch is deliberately stale, so an old tab cannot undo the user's new pass.
    position_epoch: StrictInt | None = Field(default=None, ge=0, le=2**63 - 1)


class PositionResetIn(BaseModel):
    position_epoch: StrictInt = Field(ge=0, le=2**63 - 1)
    # Reading again keeps what Mothli knows of the book. ``fresh`` forgets it until it is read again.
    fresh: bool = False


class ReadPage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: ReadAnchor
    end: ReadAnchor


class ReadChapters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    atoms: list[StrictInt] = Field(min_length=1, max_length=2000)
    read: bool
    source: Literal["told", "import"] = "told"


def _map_enabled(settings):
    if not (settings.reader_page_context and settings.reader_read_map):
        raise HTTPException(404, "the read map is not enabled")


@router.get("/read-map", dependencies=[Depends(book_lifecycle)])
def get_read_map(book_id: str, catalog=Depends(get_catalog), settings=Depends(get_settings),
                 worker=Depends(get_worker)):
    """How much of each chapter the reader has read, as a fraction of its text."""
    _map_enabled(settings)
    try:
        return {"chapters": chapter_fractions(book_id, catalog, settings, worker)}
    except LookupError as exc:
        raise HTTPException(404, "unknown book") from exc
    except (ValueError, AtomSetMismatch) as exc:
        raise HTTPException(409, "read map could not be verified against the source") from exc


@router.get("/read-offer", dependencies=[Depends(book_lifecycle)])
def get_read_offer(book_id: str, catalog=Depends(get_catalog), settings=Depends(get_settings),
                   worker=Depends(get_worker)):
    """What another reading app recorded about this book, for the reader to accept or not."""
    _map_enabled(settings)
    from app.api.history import _history, _with_shelf
    history = _history(settings)
    _total, entries = history.list(limit=500)
    mine = [history.detail(entry["id"]) for entry in _with_shelf(entries, catalog, settings)
            if entry["book_id"] == book_id]
    try:
        return {"offer": import_offer(book_id, mine, catalog, settings, worker)}
    except LookupError as exc:
        raise HTTPException(404, "unknown book") from exc
    except (ValueError, AtomSetMismatch) as exc:
        raise HTTPException(409, "read map could not be verified against the source") from exc


@router.put("/read-page", dependencies=[Depends(book_lifecycle)])
def put_read_page(book_id: str, body: ReadPage, request: Request, catalog=Depends(get_catalog),
                  settings=Depends(get_settings), worker=Depends(get_worker)):
    """The reader has read one page: both ends are checked against the source before it counts."""
    _map_enabled(settings)
    try:
        finished = record_page(book_id, body.start, body.end, catalog, settings, worker)
        point, place = sync_counter(book_id, catalog, settings, worker, with_place=True)
        # The reader has settled on a page here. If chapters before it were gone past, the app
        # may ask once whether they have been read.
        point = {**point, "skipped": skipped_before(place)}
    except LookupError as exc:
        raise HTTPException(404, "unknown book") from exc
    except RuntimeError as exc:
        raise HTTPException(409, "reading pass changed; reload before continuing") from exc
    except (ValueError, AtomSetMismatch) as exc:
        raise HTTPException(422, "page could not be verified against the source") from exc
    if finished and settings.reader_story_notes and settings.reader_conversation_jev:
        request.app.state.notes_builder.ensure(book_id)
    return point


@router.put("/read-chapters", dependencies=[Depends(book_lifecycle)])
def put_read_chapters(book_id: str, body: ReadChapters, request: Request, catalog=Depends(get_catalog),
                      settings=Depends(get_settings), worker=Depends(get_worker)):
    """The reader's own word: "I've read this" or "I haven't read this" for whole chapters."""
    _map_enabled(settings)
    try:
        mark_chapters(book_id, body.atoms, read=body.read, source=body.source, catalog=catalog,
                      settings=settings, worker=worker)
        point = sync_counter(book_id, catalog, settings, worker)
    except LookupError as exc:
        raise HTTPException(404, "unknown book") from exc
    except (ValueError, AtomSetMismatch) as exc:
        raise HTTPException(422, "chapters could not be verified against the source") from exc
    if body.read and settings.reader_story_notes and settings.reader_conversation_jev:
        request.app.state.notes_builder.ensure(book_id)
    return point


def _position_conflict(error: ValueError):
    if "position epoch" in str(error):
        raise HTTPException(
            409, "reading position was reset in another session; reload before continuing"
        ) from error
    raise error


def _load_checked(book_id, catalog, store, settings):
    """Shared fail-closed prologue: known book -> self-verified manifest -> store cross-check (under
    the lock) -> frontier bounds."""
    if catalog.get_book(book_id) is None:
        raise HTTPException(404, "unknown book")
    try:
        manifest = load_manifest(settings.data_dir, book_id)
        with store.book(book_id) as mem:
            assert_matches_store(manifest, mem)
            durable = mem.completion_frontier(manifest["atoms"])
        bounds = bounds_of(manifest)
    except AtomSetMismatch as e:
        raise HTTPException(409, "atom-set mismatch: the stored reading position cannot be safely "
                                 "interpreted (re-import the book)") from e
    return manifest, bounds, durable


@router.get("/position", dependencies=[Depends(book_lifecycle)])
def get_position(book_id: str, catalog=Depends(get_catalog), store=Depends(get_store),
                 settings=Depends(get_settings), worker=Depends(get_worker)):
    manifest, bounds, durable = _load_checked(book_id, catalog, store, settings)
    st = catalog.get_state(book_id)
    progress = min(st["ingest_progress"], durable)
    # With the read map: the chapter the reader is in, which can be far past the chapter counter.
    place = (catalog.get_read_position(book_id, incarnation=catalog.get_book(book_id)["incarnation"])
             if settings.reader_read_map else None)
    return {"bookmark": st["bookmark"], "cfi": st["cfi"], "ingest_progress": progress,
            "position_epoch": st["position_epoch"], "atoms": len(manifest["atoms"]),
            "place_chapter": place[0] if place else 0}


@router.put("/position", dependencies=[Depends(book_lifecycle)])
def put_position(book_id: str, pos: PositionIn, request: Request, catalog=Depends(get_catalog),
                 store=Depends(get_store), settings=Depends(get_settings),
                 worker=Depends(get_worker)):
    # One incarnation owns the whole transition: validation, position commit, and enqueue publication.
    # Deletion cannot cross between any of them and let this request bind work to a re-imported row.
    manifest, bounds, _durable = _load_checked(book_id, catalog, store, settings)
    prev = catalog.high_water(book_id)
    completed_offset = max(pos.offset, pos.high_water_offset or 0)
    # With the read map the counter moves when chapters are read, not when a page is reached.
    bookmark = prev if settings.reader_read_map and settings.reader_page_context else (
        frontier.bookmark_high_water(prev, completed_offset, bounds))
    try:
        state = catalog.set_position(
            book_id, pos.cfi, bookmark,
            expected_epoch=pos.position_epoch if pos.position_epoch is not None else 0,
        )
    except ValueError as error:
        _position_conflict(error)
    bookmark = state["bookmark"]  # SQL MAX may have observed a newer cross-instance report.
    if bookmark > 0 and settings.reader_memory_ingest:
        # The worker validates explicit v2 completion markers even when legacy catalog progress
        # already reaches this bookmark; matching durable receipts replay idempotently.
        worker.enqueue(book_id, bookmark)
    warmer = request.app.state.recap_warmer
    if warmer is not None:
        warmer.notify(book_id, catalog.get_book(book_id)["incarnation"], foreground=True)
    if bookmark > prev and settings.reader_story_notes and settings.reader_conversation_jev:
        request.app.state.notes_builder.ensure(book_id)      # a chapter was just finished
    return {
        "bookmark": bookmark,
        "cfi": pos.cfi,
        "current_chapter": frontier.current_chapter(pos.offset, bounds),
        "chapter_progress": round(frontier.chapter_progress(pos.offset, bounds), 4),
        "position_epoch": state["position_epoch"],
        "atoms": len(manifest["atoms"]),
    }


@router.post("/position/reset", dependencies=[Depends(book_lifecycle)])
def reset_position(book_id: str, reset: PositionResetIn, request: Request, catalog=Depends(get_catalog),
                   store=Depends(get_store), settings=Depends(get_settings)):
    """Start a new pass while retaining extracted memory, receipts, and spend history."""
    manifest, _bounds, durable = _load_checked(book_id, catalog, store, settings)
    try:
        state = catalog.reset_position(book_id, expected_epoch=reset.position_epoch)
    except ValueError as error:
        _position_conflict(error)
    if reset.fresh:
        catalog.clear_read_map(book_id)
    if settings.reader_read_map and settings.reader_page_context:
        # Reading again keeps what was read, so the views that follow the counter keep it too.
        try:
            sync_counter(book_id, catalog, settings, request.app.state.worker)
        except (LookupError, ValueError, AtomSetMismatch) as exc:
            raise HTTPException(409, "read map could not be verified against the source") from exc
        state = {**state, "bookmark": catalog.high_water(book_id)}
    warmer = request.app.state.recap_warmer
    if warmer is not None:
        warmer.notify(book_id, catalog.get_book(book_id)["incarnation"], foreground=True)
    return {
        "bookmark": state["bookmark"],
        "cfi": state["cfi"],
        "ingest_progress": min(state["ingest_progress"], durable),
        "position_epoch": state["position_epoch"],
        "atoms": len(manifest["atoms"]),
    }
