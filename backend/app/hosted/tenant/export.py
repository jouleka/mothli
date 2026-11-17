"""Hosted mode: "Take a copy of my data" (ADR 0041 part 8).

One zip for the signed-in reader: a short plain-text read-me that says what each file is, and JSON
files a person can open and another program can parse. Everything in it is the reader's own or
about them, and nothing Mothli wrote goes further than the app would show them now: each book's
conversation through ``visible_turns``, Mothli's chapter notes through ``usable_notes`` over the
read text of ``read_snapshot``, the home chat through ``home_turns``, what Mothli remembers through
``listed``, and highlights, notes and bookmarks through the repository's ``list_reader_marks``,
the same functions the app's screens use. The book files themselves are not in it.

Taking a copy changes nothing: the one write a read can cause (a book's read map beginning from
the older position record, ``read_map._started``) is kept in memory for this copy only. The zip is
written one book at a time into a spooled temporary file and streamed from there, so memory holds
one book's parse and conversation at most; book parses go through a cache of this copy's own, so
other readers' cached books stay cached.

Which tables are in it is checked against the database's catalogue in
``tests/hosted/test_account_export_postgres.py``: every table with an ``owner_id`` is either named
in ``IN_THE_COPY`` or in ``LEFT_OUT`` with the reason.
"""

from __future__ import annotations

import asyncio
import json
import re
import tempfile
import threading
import time
import uuid
import zipfile
from collections.abc import Callable, Iterator
from datetime import UTC, datetime

from app.catalog.catalog import Catalog
from app.chapter_notes import VERSION, usable_notes
from app.home_conversation import HOME, home_turns
from app.hosted.companion.source import HostedBookSource, SourceParseCache
from app.hosted.companion.store import HostedCompanionCatalog
from app.hosted.tenant.models import OwnerId
from app.read_map import chapter_fractions, read_snapshot, visible_turns
from app.recollections import listed

# Every table with an owner_id, and where it is in the copy.
IN_THE_COPY = {
    "books": "shelf.json and each book's file: title, author, when added, where it came from",
    "reading_state": "each book's file: where you are",
    "read_ranges": "each book's file: how much of each chapter you have read",
    "read_position": "each book's file: where you are",
    "highlights": "each book's file, with the words highlighted",
    "annotations": "each book's file, with the words they are on",
    "bookmarks": "each book's file",
    "reader_preferences": "each book's file: how it looks on your screen",
    "conversation_turns": "each book's file (its chat) and home chat.json",
    "chapter_notes": "each book's file: Mothli's notes on the chapters you finished",
    "recollections": "what Mothli remembers.json",
}
LEFT_OUT = {
    "users": "you.json holds your name, email and when you joined; the rest is account state",
    "external_identities": "the sign-in service's ids for you: security records",
    "sessions": "sign-in sessions: security records",
    "audit_events": "the security audit trail: internal records",
    "provider_credentials": "encrypted keys: secrets",
    "provider_model_settings": "settings for keys hosted mode does not take",
    "owner_limits": "limits the owner of Mothli sets; not shown anywhere in the app",
    "owner_request_windows": "a request counter, used to slow down floods",
    "cost_ledger": "what replies cost: the app does not show it to readers",
    "cost_reservations": "money held while a reply is written: not shown in the app",
    "jobs": "background work: internal records with nothing of yours in them",
    "job_attempts": "background work: internal records",
    "source_objects": "where the book files are stored: the files are downloaded from each book",
    "book_search_documents": "the books' own text, indexed for search, not yours",
    "skimmed_ranges": "pages you turned through quickly: never counted as read",
    "recollection_cursors": "how far Mothli has looked through a chat: internal",
    "local_library_migrations": "the record of a library moved in from a computer: internal",
    "chapters": "what Mothli learned from a book's own pages, not yours",
    "ingested_chapters": "what Mothli learned from a book's own pages, not yours",
    "chapter_summaries": "what Mothli learned from a book's own pages, not yours",
    "entities": "what Mothli learned from a book's own pages, not yours",
    "aliases": "what Mothli learned from a book's own pages, not yours",
    "edges": "what Mothli learned from a book's own pages, not yours",
    "events": "what Mothli learned from a book's own pages, not yours",
    "event_participants": "what Mothli learned from a book's own pages, not yours",
    "entity_state": "what Mothli learned from a book's own pages, not yours",
    "entity_corrections": "what Mothli learned from a book's own pages, not yours",
    "themes": "what Mothli learned from a book's own pages, not yours",
    "chunks": "a book's own text, cut up for search, not yours",
    "chunk_embeddings": "numbers computed from a book's own text, not yours",
}

EVERYTHING = 10**9          # "no limit" for the functions that keep only the latest turns
_CHUNK = 64 * 1024
_SPOOL = 4 * 1024 * 1024    # past this the zip is written to a temporary file on disk

README = """Your Mothli data
================

This is a copy of what is yours in Mothli, taken on {taken}.
Every file is plain text. The .json files open in any text editor, and other
programs can read them too.

What each file is
-----------------
you.json
    Who you are to Mothli: your name, your email, and when you joined.
shelf.json
    The books on your shelf: title, author, when you added each one, and where
    it came from. Each book points to its own file in the books folder.
books/
    One file for each book: where you are in it and how much of each chapter
    you have read; your highlights, notes and bookmarks, with the words they
    are on; your chat with Mothli about it; Mothli's notes on the chapters you
    finished; and how you like the page to look.
home chat.json
    Your chat with Mothli on your home page.
what Mothli remembers.json
    The little things Mothli remembers about you, and when you said them.

What isn't here
---------------
The books themselves. A shelf can be very big, so each book is downloaded on
its own: open the book in Mothli, press Notebook, and choose "Download the
book".

Some of Mothli's words are held back, just as they are in the app. If you
marked pages as not read, or started a book again, what Mothli said about
those pages waits until you read them again. Each file says how many lines it
held back. No spoilers, even here.

Nothing about anyone else is in here. Neither are the keys and security
records that keep your account safe.
"""


class ExportRefused(Exception):
    """A copy that cannot be made now, with a code and a line for the reader."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(code)
        self.status, self.code, self.message = status, code, message


class ExportGate:
    """How often copies are made in this web process: one at a time for a reader, a few minutes
    apart, and only a couple at once across readers, so a copy never slows anyone else down."""

    def __init__(self, *, at_once: int = 2, apart_seconds: float = 300,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._at_once, self._apart, self._clock = at_once, apart_seconds, clock
        self._running: set = set()
        self._last: dict = {}
        self._lock = threading.Lock()

    def admit(self, owner_id: OwnerId) -> None:
        with self._lock:
            if owner_id.value in self._running:
                raise ExportRefused(429, "export_in_progress",
                                    "Your copy is still being put together. It'll be ready in a moment.")
            last = self._last.get(owner_id.value)
            if last is not None and self._clock() - last < self._apart:
                raise ExportRefused(429, "export_too_soon",
                                    "You just took a copy. Give it a few minutes, then try again.")
            if len(self._running) >= self._at_once:
                raise ExportRefused(503, "export_busy",
                                    "Lots of readers are taking copies right now. Try again in a minute.")
            self._running.add(owner_id.value)

    def finished(self, owner_id: OwnerId, *, made: bool) -> None:
        with self._lock:
            self._running.discard(owner_id.value)
            if made:
                self._last[owner_id.value] = self._clock()


class _LookOnly:
    """The reader's catalog for a copy: every read passes through; the one write a read can cause
    (``read_map._started``, a read map beginning from the older position record) is held here for
    this copy only, so the copy sees what the app would and changes nothing. Any other write is
    refused."""

    __slots__ = ("_catalog", "_ranges", "_positions")

    def __init__(self, catalog) -> None:
        self._catalog = catalog
        self._ranges: dict = {}
        self._positions: dict = {}

    def __getattr__(self, name):
        if name.startswith(("get_", "list_")) or name == "source_record":
            return getattr(self._catalog, name)
        raise AttributeError(f"a copy does not change anything ({name})")

    def add_read_range(self, book_id, *, incarnation, atom, start, end, source):
        self._ranges.setdefault((str(book_id), str(incarnation)), []).append((atom, start, end))

    def set_read_position(self, book_id, *, incarnation, atom, offset):
        self._positions[(str(book_id), str(incarnation))] = (atom, offset)

    def get_read_position(self, book_id, *, incarnation):
        held = self._positions.get((str(book_id), str(incarnation)))
        return held or self._catalog.get_read_position(book_id, incarnation=incarnation)

    def get_read_map(self, book_id, *, incarnation):
        held = self._catalog.get_read_map(book_id, incarnation=incarnation)
        for atom, start, end in self._ranges.get((str(book_id), str(incarnation)), []):
            held[atom] = Catalog._merged([*held.get(atom, []), (start, end)])
        return held


def _when(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    return str(value)


def _came_from(source: str | None) -> str:
    if source and source.startswith("gutenberg:"):
        return f"Mothli's free library (Project Gutenberg book {source.split(':', 1)[1]})"
    return "You added the file yourself"


def _file_name(number: int, title: str) -> str:
    words = re.sub(r"[^A-Za-z0-9 '-]+", " ", title or "").strip()
    words = re.sub(r"\s+", " ", words)[:60].strip() or "Untitled"
    return f"books/{number:02d} {words}.json"


def _said(turn) -> dict:
    return {"who": "you" if turn["role"] == "user" else "Mothli", "said": turn["content"],
            "when": _when(turn["created_at"])}


def _marks(found: dict | None) -> dict:
    """Highlights, notes and bookmarks as the app lists them now, each with the words it is on."""
    marks = (found or {}).get("marks", [])
    words = {mark["id"]: mark.get("selected_text") for mark in marks if mark["kind"] == "highlight"}
    shown = []
    for mark in marks:
        anchor = mark.get("anchor") or {}
        item = {"kind": {"highlight": "highlight", "annotation": "note",
                         "bookmark": "bookmark"}[mark["kind"]],
                "chapter": anchor.get("atom"), "place": anchor.get("cfi"),
                "made": _when(mark.get("created_at"))}
        if mark["kind"] == "highlight":
            item.update(words=mark.get("selected_text"), colour=mark.get("color"))
        elif mark["kind"] == "annotation":
            item.update(note=mark.get("body"), words=words.get(mark.get("highlight_id")))
        else:
            item.update(label=mark.get("label"))
        shown.append(item)
    return {"highlights_notes_and_bookmarks": shown}


class _Copy:
    """Builds one reader's zip, synchronously, in a worker thread."""

    def __init__(self, *, settings, dsn: str, storage, owner_id: OwnerId, who: dict,
                 marks_of: Callable[[str], dict | None],
                 looks_of: Callable[[str], dict | None]) -> None:
        self._settings = settings
        self._catalog = HostedCompanionCatalog(dsn, owner_id)
        self._look = _LookOnly(self._catalog)
        # A cache of this copy's own: a copy reads every book once and must not push other
        # readers' books out of the shared one.
        self._source = HostedBookSource(storage, self._catalog, SourceParseCache(1))
        self._who = who
        self._marks_of = marks_of
        self._looks_of = looks_of

    def _reading_on(self) -> bool:
        return bool(self._settings.reader_page_context and self._settings.reader_read_map)

    def build(self, out) -> None:
        settings = self._settings
        taken = datetime.now(UTC)
        with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            def put(name: str, value) -> None:
                archive.writestr(name, json.dumps(value, ensure_ascii=False, indent=2) + "\n")

            archive.writestr("READ ME.txt", README.format(taken=taken.strftime("%d %B %Y")))
            put("you.json", self._who)
            # What Mothli remembers, before any book's map is looked at, exactly as the app's
            # list does it.
            if settings.reader_recollections and self._reading_on():
                remembered = listed(self._look)
                put("what Mothli remembers.json", {
                    "held_back_until_you_read_again": remembered["hidden"],
                    "remembers": [{
                        "what": note["text"], "kind": note["kind"],
                        "when_you_said_it": _when(note["said_at"]),
                        "about_book": note["book"]["title"] if note["book"] else None,
                        "your_words": note["message"]} for note in remembered["recollections"]]})
            shelf, read = [], {}
            for number, book in enumerate(self._look.list_books(), start=1):
                name = _file_name(number, book["title"])
                shelf.append({"title": book["title"], "author": book["author"],
                              "added": book["added_at"], "came_from": _came_from(book["source"]),
                              "file": name})
                entry = {"title": book["title"], "author": book["author"],
                         "added": book["added_at"], "came_from": _came_from(book["source"]),
                         "how_it_looks": {key: value for key, value in
                                          (self._looks_of(book["book_id"]) or {}).items()
                                          if key != "preference_version"},
                         **_marks(self._marks_of(book["book_id"]))}
                if self._reading_on():
                    entry.update(self._reading(book, read))
                put(name, entry)
            put("shelf.json", {"books": shelf})
            if (settings.reader_conversation_stream and self._reading_on()):
                turns = home_turns(self._look, read, limit=EVERYTHING)
                stored = self._look.get_turns(None, incarnation=HOME)
                put("home chat.json", {
                    "held_back_until_you_read_again": len(stored) - len(turns),
                    "chat": [_said(turn) for turn in turns]})

    def _reading(self, book: dict, read: dict) -> dict:
        """Where the reader is in one book, their chat in it and Mothli's chapter notes, through
        the app's own functions; ``read`` gathers the read maps the home chat is judged by."""
        book_id = book["book_id"]
        try:
            point, chapters, state, _manifest, record, held = read_snapshot(
                book_id, self._look, self._settings, self._source)
            shares = chapter_fractions(book_id, self._look, self._settings, self._source)
        except (LookupError, OSError, ValueError, RuntimeError):
            # As the home chat leaves out a book whose source cannot be verified.
            return {"reading": "This book couldn't be opened just now, so where you are, your "
                               "chat and Mothli's notes on it are not in this copy."}
        if held:
            read[book_id] = [record["incarnation"], held]
        turns = visible_turns(book_id, record, held, self._look, limit=EVERYTHING)
        stored = self._look.get_turns(book_id, incarnation=record["incarnation"])
        notes = usable_notes(book_id, chapters, self._look)
        kept = [note for note in self._look.get_chapter_notes(book_id)
                if note["version"] == VERSION and note["sentences"]]
        return {
            "where_you_are": {
                "chapter": point["atom"],
                "every_chapter_read_through": state["read_through"],
                "chapters": [{"chapter": share["atom"], "share_read": share["read"]}
                             for share in shares]},
            "chat_with_mothli": [{**_said(turn), "chapter": turn["atom"]} for turn in turns],
            "mothli_notes_on_chapters_you_finished": [
                {"chapter": note["ordinal"], "chapter_title": note["title"],
                 "notes": note["sentences"]} for note in notes],
            "held_back_until_you_read_again": {
                "chat_lines": len(stored) - len(turns),
                "chapter_notes": max(0, len(kept) - len(notes))},
        }


def build_copy(out, **parts) -> None:
    """Write one reader's copy into ``out`` (a binary file). Blocking: run it in a thread."""
    _Copy(**parts).build(out)


async def make_copy(*, request, owner_id: OwnerId, who: dict):
    """The reader's copy as a spooled file, positioned at its start. The marks come from the
    async repository on the event loop; everything else runs in a worker thread."""
    loop = asyncio.get_running_loop()
    repository = request.app.state.tenant_repository

    def on_the_loop(read):
        return lambda book_id: asyncio.run_coroutine_threadsafe(
            read(owner_id, uuid.UUID(book_id)), loop).result()

    out = tempfile.SpooledTemporaryFile(max_size=_SPOOL)
    try:
        await asyncio.to_thread(
            build_copy, out, settings=request.app.state.settings,
            dsn=request.app.state.companion_dsn, storage=request.app.state.object_storage,
            owner_id=owner_id, who=who,
            marks_of=on_the_loop(repository.list_reader_marks),
            looks_of=on_the_loop(repository.get_reader_preferences))
        out.seek(0)
    except BaseException:
        out.close()
        raise
    return out


def stream(out, done: Callable[[], None]) -> Iterator[bytes]:
    """The finished zip in pieces; the file is closed (and gone) when the last piece is sent, or
    when the reader goes away."""
    try:
        while chunk := out.read(_CHUNK):
            yield chunk
    finally:
        out.close()
        done()
