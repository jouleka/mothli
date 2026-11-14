"""Reading history brought in from other reading apps (see ``app.history``).

The reader's browser parses the files they exported and sends plain records here, a batch at a
time. No file, credential or address of another service ever reaches this server: only titles,
dates, ratings and the reader's own highlights and notes."""
import os
import tempfile
import threading
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from app.classics import find_classic, match_title, source_tag
from app.deps import get_catalog, get_settings
from app.history import STATUSES, ReadingHistory
from app.history_sources import UnreadableSource, fetch_readwise, is_sqlite, read_databases

router = APIRouter(prefix="/api/history", tags=["history"])

MAX_BATCH_BOOKS = 500
MAX_BATCH_HIGHLIGHTS = 1000
Source = Literal["goodreads", "storygraph", "kindle", "libby", "readwise", "koreader", "calibre", "other"]
MAX_DATABASE_FILES = 3
MAX_DATABASE_BYTES = 600 * 1024 * 1024  # all files of one upload together; a Kobo database is tens of MB
_DATE = r"^\d{4}-\d{2}-\d{2}$"
_stores = {}
_stores_lock = threading.Lock()


def _history(settings) -> ReadingHistory:
    path = os.path.join(settings.data_dir, "reading-history.db")
    with _stores_lock:
        store = _stores.get(path)
        if store is None:
            store = _stores[path] = ReadingHistory(path)
        return store


class ImportedBook(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    author: str = Field("", max_length=300)
    isbn: str | None = Field(None, max_length=20, pattern=r"^[0-9Xx-]*$")
    status: Literal["read", "reading", "want", "abandoned"] | None = None
    rating: float | None = Field(None, ge=0, le=5)
    finished_on: str | None = Field(None, pattern=_DATE)
    added_on: str | None = Field(None, pattern=_DATE)
    review: str | None = Field(None, max_length=20_000)


class ImportedHighlight(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    author: str = Field("", max_length=300)
    text: str = Field("", max_length=8_000)
    note: str | None = Field(None, max_length=8_000)
    location: str | None = Field(None, max_length=80)
    position: float | None = Field(None, ge=0, le=1e9)
    made_at: str | None = Field(None, max_length=40, pattern=r"^[0-9T:.Z+-]*$")


class ImportBatch(BaseModel):
    source: Source
    books: list[ImportedBook] = Field(default_factory=list, max_length=MAX_BATCH_BOOKS)
    highlights: list[ImportedHighlight] = Field(default_factory=list, max_length=MAX_BATCH_HIGHLIGHTS)


def _with_shelf(entries, catalog, settings):
    """Says, for each title, whether it is already on the shelf or free to add from the library."""
    shelved = {book["source"]: book["book_id"] for book in catalog.list_books() if book.get("source")}
    rule = settings.classics_min_years_since_death
    for entry in entries:
        classic = None
        if settings.classics_enabled and entry["classic_id"]:
            classic = find_classic(entry["classic_id"], min_years_since_death=rule)
        entry["classic_id"] = classic["id"] if classic else None
        entry["book_id"] = shelved.get(source_tag(classic)) if classic else None
    return entries


@router.post("/import")
def import_history(batch: ImportBatch, settings=Depends(get_settings)):
    if not batch.books and not batch.highlights:
        raise HTTPException(422, "nothing to import")
    rule = settings.classics_min_years_since_death
    match = (lambda title, author: match_title(title, author, min_years_since_death=rule)) if settings.classics_enabled else None
    counts = _history(settings).import_batch(
        batch.source,
        [book.model_dump() for book in batch.books],
        [mark.model_dump() for mark in batch.highlights if mark.text.strip() or (mark.note or "").strip()],
        match=match)
    return counts


def _matcher(settings):
    rule = settings.classics_min_years_since_death
    if not settings.classics_enabled:
        return None
    return lambda title, author: match_title(title, author, min_years_since_death=rule)


def _merge(store, source, books, highlights, settings):
    """Imports a source read on the server, in batches, and adds up what changed."""
    total = {"books_added": 0, "books_updated": 0, "highlights_added": 0, "skipped": 0}
    match = _matcher(settings)
    for at in range(0, max(len(books), 1), MAX_BATCH_BOOKS):
        counts = store.import_batch(source, books[at:at + MAX_BATCH_BOOKS], [], match=match)
        total = {key: total[key] + counts[key] for key in total}
    for at in range(0, len(highlights), MAX_BATCH_HIGHLIGHTS):
        counts = store.import_batch(source, [], highlights[at:at + MAX_BATCH_HIGHLIGHTS], match=match)
        total = {key: total[key] + counts[key] for key in total}
    return total


def _import_databases(paths, settings):
    store = _history(settings)
    total = {"books_added": 0, "books_updated": 0, "highlights_added": 0, "skipped": 0}
    sources = []
    for source, books, highlights in read_databases(paths):
        counts = _merge(store, source, books, highlights, settings)
        total = {key: total[key] + counts[key] for key in total}
        sources.append({"source": source, "books": len(books), "highlights": len(highlights)})
    return {**total, "sources": sources}


@router.post("/import-databases")
async def import_databases(request: Request, settings=Depends(get_settings)):
    """Kobo's KoboReader.sqlite, or the two Apple Books database files from a Mac. The files are
    kept only for the moment it takes to read them."""
    declared = request.headers.get("content-length", "")
    if not declared.isdigit() or int(declared) > MAX_DATABASE_BYTES:
        raise HTTPException(413, "These files are too large to be reading databases.")
    try:
        form = await request.form(max_files=MAX_DATABASE_FILES, max_fields=4)
    except Exception as error:  # noqa: BLE001 - any malformed upload is the sender's mistake, not ours
        raise HTTPException(422, "That upload could not be read.") from error
    uploads = [item for item in form.getlist("files") if hasattr(item, "read")]
    if not uploads:
        raise HTTPException(422, "no files")
    paths = []
    try:
        with tempfile.TemporaryDirectory(prefix="history-", dir=settings.data_dir) as folder:
            for index, upload in enumerate(uploads):
                head = await upload.read(16)
                if not is_sqlite(head):
                    raise HTTPException(422, "One of these files is not a reading database.")
                path = os.path.join(folder, f"{index}.sqlite")
                with open(path, "wb") as out:
                    out.write(head)
                    while chunk := await upload.read(1024 * 1024):
                        out.write(chunk)
                paths.append(path)
            return await run_in_threadpool(_import_databases, paths, settings)
    except UnreadableSource as error:
        raise HTTPException(422, str(error)) from error
    finally:
        await form.close()


class ReadwiseAccount(BaseModel):
    token: str = Field(min_length=20, max_length=120, pattern=r"^[A-Za-z0-9_-]+$")


@router.post("/readwise")
def import_readwise(account: ReadwiseAccount, settings=Depends(get_settings)):
    """Brings a reader's highlights from Readwise with the access token they paste. The token is
    used for this request and not kept."""
    try:
        books, highlights = fetch_readwise(account.token)
    except UnreadableSource as error:
        raise HTTPException(502, str(error)) from error
    counts = _merge(_history(settings), "readwise", books, highlights, settings)
    return {**counts, "sources": [{"source": "readwise", "books": len(books), "highlights": len(highlights)}]}


@router.get("")
def list_history(
    status: str = Query("", pattern=r"^[a-z]*$", max_length=12),
    only: str = Query("", pattern=r"^[a-z]*$", max_length=12),
    q: str = Query("", max_length=120),
    offset: int = Query(0, ge=0, le=100_000),
    limit: int = Query(60, ge=1, le=100),
    catalog=Depends(get_catalog),
    settings=Depends(get_settings),
):
    store = _history(settings)
    matches, page = store.list(status=status if status in STATUSES else None, only=only or None,
                               query=q, offset=offset, limit=limit)
    return {**store.summary(), "matches": matches, "entries": _with_shelf(page, catalog, settings)}


@router.get("/{entry_id}")
def history_entry(entry_id: int, catalog=Depends(get_catalog), settings=Depends(get_settings)):
    entry = _history(settings).detail(entry_id)
    if entry is None:
        raise HTTPException(404, "unknown title")
    return _with_shelf([entry], catalog, settings)[0]


@router.delete("/{entry_id}", status_code=204)
def forget_entry(entry_id: int, settings=Depends(get_settings)):
    if not _history(settings).forget(entry_id):
        raise HTTPException(404, "unknown title")


@router.delete("", status_code=204)
def forget_history(settings=Depends(get_settings)):
    _history(settings).forget()
