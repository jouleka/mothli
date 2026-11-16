"""The read-map routes in hosted mode, at the local paths (ADR 0041 part 1 and section 8).

Each route runs the unchanged local ``app.read_map`` functions against a ``HostedCompanionCatalog``
and a ``HostedBookSource`` bound to the signed-in reader. The owner comes from the session only
(``_owner`` / ``_csrf_owner``, which also apply CSRF and the per-owner request limit). A book that
is not this reader's is unknown: the same 404 as a book that does not exist.

These are plain ``def`` routes: FastAPI runs them in its thread pool, and every adapter call is one
short synchronous transaction (ADR 0041 section 2). Nothing here calls a model.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.reading import ReadChapters, ReadPage
from app.hosted.companion import CompanionDatabaseBusy
from app.hosted.companion.source import HostedBookSource
from app.hosted.companion.store import HostedCompanionCatalog
from app.hosted.tenant.api import _csrf_owner, _owner
from app.hosted.tenant.models import OwnerId
from app.read_frontier import ReadAnchor
from app.read_map import chapter_fractions, mark_chapters, record_page, sync_counter
from app.reader_place import skipped_before

router = APIRouter(prefix="/api/books/{book_id}", tags=["hosted-library"])


def _bound(request: Request, owner_id: OwnerId):
    """(catalog, source, settings) for this reader, or 404 while the read map is off."""
    settings = request.app.state.settings
    if not (settings.reader_page_context and settings.reader_read_map):
        raise HTTPException(404, "the read map is not enabled")
    catalog = HostedCompanionCatalog(request.app.state.companion_dsn, owner_id)
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
