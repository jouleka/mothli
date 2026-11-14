"""A reader's history from other reading apps: the books they have read or mean to read, and the
highlights and notes they made there.

It is brought in from files the reader exported themselves (a Goodreads or StoryGraph library, a
Kindle clippings file, a Libby export) and parsed in their browser. Nothing here is a book: a title
in this list is a record that the reader knows the book, with an offer to add it when the free
library has it. Imported highlights are kept beside the title rather than among Mothli's own marks,
because those are pinned to a place in a book's text and these are not.

Importing is repeatable: the same file brought in twice adds nothing, and a second source for a
book already listed fills in what is missing rather than making a second row."""
import hashlib
import os
import sqlite3
import threading
from datetime import UTC, datetime
from difflib import SequenceMatcher

from app.classics import author_key, fold, title_key

STATUSES = ("read", "reading", "want", "abandoned")
MAX_ENTRIES = 50_000
MAX_HIGHLIGHTS = 300_000
_SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY,
    match_key TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    author TEXT NOT NULL DEFAULT '',
    isbn TEXT,
    status TEXT,
    rating REAL,
    finished_on TEXT,
    added_on TEXT,
    review TEXT,
    sources TEXT NOT NULL DEFAULT '',
    classic_id TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS highlights (
    id INTEGER PRIMARY KEY,
    entry_id INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    fingerprint TEXT NOT NULL,
    text TEXT NOT NULL DEFAULT '',
    note TEXT,
    location TEXT,
    position REAL,
    made_at TEXT,
    source TEXT NOT NULL,
    UNIQUE (entry_id, fingerprint)
);
CREATE INDEX IF NOT EXISTS highlights_by_entry ON highlights (entry_id, position, id);
"""
def match_key(title, author):
    return f"{title_key(title)}|{author_key(author)}"


class ReadingHistory:
    def __init__(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self):
        with self._lock:
            self._conn.close()

    # ---- bringing things in -------------------------------------------------------------------

    def _entry_for(self, title, author, source, now, counts):
        key = match_key(title, author)
        if key.startswith("|"):
            return None
        row = self._conn.execute("SELECT * FROM entries WHERE match_key = ?", (key,)).fetchone()
        if row is not None:
            return row
        # the same book with the author spelt a little differently (Dostoevsky, Dostoyevsky)
        book, _, surname = key.partition("|")
        # every key for this title: "title|" up to, not including, "title}" (the character after "|")
        same_title = self._conn.execute(
            "SELECT * FROM entries WHERE match_key >= ? AND match_key < ?", (f"{book}|", f"{book}}}")).fetchall()
        for near in same_title:
            other = near["match_key"].rpartition("|")[2]
            if surname and other and SequenceMatcher(None, surname, other).ratio() >= 0.84:
                return near
        if self._conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] >= MAX_ENTRIES:
            counts["skipped"] += 1
            return None
        self._conn.execute(
            "INSERT INTO entries (match_key, title, author, sources, created_at) VALUES (?, ?, ?, ?, ?)",
            (key, title.strip(), (author or "").strip(), source, now))
        counts["books_added"] += 1
        counts["fresh"].add(key)
        return self._conn.execute("SELECT * FROM entries WHERE match_key = ?", (key,)).fetchone()

    def import_batch(self, source, books, highlights, *, match=None):
        """Merge one batch. ``match(title, author)`` names the free-library book for a new title."""
        now = datetime.now(UTC).isoformat(timespec="seconds")
        counts = {"books_added": 0, "books_updated": 0, "highlights_added": 0, "skipped": 0, "fresh": set()}
        with self._lock, self._conn:
            for book in books:
                row = self._entry_for(book["title"], book.get("author"), source, now, counts)
                if row is None:
                    continue
                changes = {}
                # a fact the reader recorded elsewhere fills a gap; it does not overwrite what is here
                for field in ("isbn", "status", "rating", "finished_on", "added_on", "review"):
                    value = book.get(field)
                    if value not in (None, "") and row[field] in (None, ""):
                        changes[field] = value
                # "read" outranks a wish-list entry for the same book from another app
                if book.get("status") == "read" and row["status"] in ("want", "reading"):
                    changes["status"] = "read"
                sources = [name for name in row["sources"].split(",") if name]
                if source not in sources:
                    changes["sources"] = ",".join([*sources, source])
                if changes:
                    assignments = ", ".join(f"{field} = ?" for field in changes)
                    self._conn.execute(f"UPDATE entries SET {assignments} WHERE id = ?", (*changes.values(), row["id"]))
                    if row["match_key"] not in counts["fresh"]:
                        counts["books_updated"] += 1
            total = self._conn.execute("SELECT COUNT(*) FROM highlights").fetchone()[0]
            for mark in highlights:
                row = self._entry_for(mark["title"], mark.get("author"), source, now, counts)
                if row is None:
                    continue
                if total >= MAX_HIGHLIGHTS:
                    counts["skipped"] += 1
                    continue
                text, note = (mark.get("text") or "").strip(), (mark.get("note") or "").strip() or None
                fingerprint = hashlib.sha256(f"{fold(text)}\x00{fold(note or '')}".encode()).hexdigest()[:32]
                added = self._conn.execute(
                    "INSERT OR IGNORE INTO highlights (entry_id, fingerprint, text, note, location, position, made_at, source)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (row["id"], fingerprint, text, note, mark.get("location"), mark.get("position"),
                     mark.get("made_at"), source)).rowcount
                counts["highlights_added"] += added
                total += added
                sources = [name for name in row["sources"].split(",") if name]
                if source not in sources:
                    self._conn.execute("UPDATE entries SET sources = ? WHERE id = ?", (",".join([*sources, source]), row["id"]))
            if match is not None:
                for row in self._conn.execute("SELECT id, title, author FROM entries WHERE classic_id IS NULL AND created_at = ?", (now,)).fetchall():
                    found = match(row["title"], row["author"])
                    if found:
                        self._conn.execute("UPDATE entries SET classic_id = ? WHERE id = ?", (found, row["id"]))
        del counts["fresh"]
        return counts

    # ---- reading it back ----------------------------------------------------------------------

    @staticmethod
    def _public(row):
        entry = {key: row[key] for key in ("id", "title", "author", "status", "rating", "finished_on", "added_on", "classic_id")}
        entry["sources"] = [name for name in row["sources"].split(",") if name]
        entry["highlights"] = row["highlight_count"]
        entry["has_review"] = bool(row["review"])
        return entry

    def summary(self):
        with self._lock:
            by_status = dict(self._conn.execute("SELECT COALESCE(status, ''), COUNT(*) FROM entries GROUP BY 1").fetchall())
            books = sum(by_status.values())
            highlights = self._conn.execute("SELECT COUNT(*) FROM highlights").fetchone()[0]
            marked = self._conn.execute("SELECT COUNT(DISTINCT entry_id) FROM highlights").fetchone()[0]
            free = self._conn.execute("SELECT COUNT(*) FROM entries WHERE classic_id IS NOT NULL").fetchone()[0]
        return {"books": books, "highlights": highlights, "with_highlights": marked, "free": free,
                "by_status": {status: by_status.get(status, 0) for status in STATUSES}}

    def list(self, *, status=None, query="", only=None, offset=0, limit=60):
        where, values = [], []
        if status in STATUSES:
            where.append("e.status = ?")
            values.append(status)
        if only == "highlights":
            where.append("EXISTS (SELECT 1 FROM highlights h WHERE h.entry_id = e.id)")
        elif only == "free":
            where.append("e.classic_id IS NOT NULL")
        words = fold(query).split()[:6]
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        sql = (f"SELECT e.*, (SELECT COUNT(*) FROM highlights h WHERE h.entry_id = e.id) AS highlight_count "
               f"FROM entries e {clause} ORDER BY COALESCE(e.finished_on, e.added_on, '') DESC, e.id DESC")
        with self._lock:
            rows = self._conn.execute(sql, values).fetchall()
        if words:
            rows = [row for row in rows if all(word in fold(f"{row['title']} {row['author']}") for word in words)]
        return len(rows), [self._public(row) for row in rows[offset:offset + limit]]

    def detail(self, entry_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT e.*, (SELECT COUNT(*) FROM highlights h WHERE h.entry_id = e.id) AS highlight_count"
                " FROM entries e WHERE e.id = ?", (entry_id,)).fetchone()
            if row is None:
                return None
            marks = self._conn.execute(
                "SELECT id, text, note, location, made_at, source FROM highlights WHERE entry_id = ?"
                " ORDER BY position IS NULL, position, id", (entry_id,)).fetchall()
        return {**self._public(row), "review": row["review"], "isbn": row["isbn"],
                "marks": [dict(mark) for mark in marks]}

    def forget(self, entry_id=None):
        """Remove one title with its highlights, or everything that was imported."""
        with self._lock, self._conn:
            if entry_id is None:
                self._conn.execute("DELETE FROM highlights")
                return self._conn.execute("DELETE FROM entries").rowcount
            return self._conn.execute("DELETE FROM entries WHERE id = ?", (entry_id,)).rowcount
