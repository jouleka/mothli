"""Chapter notes' two catalog methods over Postgres, for one owner (ADR 0041 part 6).

Same names, arguments and return shapes as ``app.catalog.catalog.Catalog``. Nothing here decides
which notes a reply may use: ``app.chapter_notes.usable_notes`` does, unchanged, by hashing the
chapter text inside the reader's read position against ``source_hash``; and nothing here decides
what is written: ``app.chapter_notes.build_missing`` does, unchanged, in a companion job.

Notes belong to the book's current shelf lifetime (its incarnation) and are read and written only
through a live book of this owner.
"""

from __future__ import annotations

from psycopg.types.json import Jsonb

from app.hosted.companion import OwnerBoundAdapter, as_uuid, check_position


class NotesStore(OwnerBoundAdapter):
    __slots__ = ()

    def get_chapter_notes(self, book_id):
        """Every note of this owner's live book, by chapter: {atom, source_hash, version,
        sentences (a list), dropped}."""
        try:
            book = as_uuid(book_id)
        except LookupError:
            return []
        with self._transaction() as conn:
            rows = conn.execute(
                """SELECT note.atom,note.source_hash,note.version,note.sentences,note.dropped
                   FROM public.chapter_notes AS note
                   JOIN public.books AS book
                     ON (book.owner_id,book.id,book.incarnation)
                      =(note.owner_id,note.book_id,note.book_incarnation)
                   WHERE note.owner_id=%s AND note.book_id=%s AND book.deleted_at IS NULL
                   ORDER BY note.atom""",
                (self._owner.value, book)).fetchall()
        return [{"atom": row["atom"], "source_hash": row["source_hash"],
                 "version": row["version"], "sentences": list(row["sentences"]),
                 "dropped": row["dropped"]} for row in rows]

    def set_chapter_notes(self, book_id, atom, *, source_hash, version, sentences, dropped):
        """Store one chapter's checked notes, unless notes from the same text and version are
        already there.

        Locally this replaces the row; ``build_missing`` only ever writes where notes for the
        current text and version are missing, so the result is the same. Here a second job that
        wrote the same chapter (a lease lost mid-run, ADR 0041 section 5) stores nothing: the
        notes already kept stay as they were. Raises ``LookupError`` for a book that is not this
        owner's live book.
        """
        check_position(atom, "chapter")
        check_position(dropped, "dropped sentences")
        kept = list(sentences)
        if not all(isinstance(sentence, str) for sentence in kept):
            raise ValueError("chapter notes must be sentences")
        with self._transaction() as conn:
            book = conn.execute(
                """SELECT id,incarnation FROM public.books
                   WHERE owner_id=%s AND id=%s AND deleted_at IS NULL
                   FOR SHARE""",
                (self._owner.value, as_uuid(book_id))).fetchone()
            if book is None:
                raise LookupError("unknown book")
            conn.execute(
                """INSERT INTO public.chapter_notes
                     (owner_id,book_id,book_incarnation,atom,source_hash,version,sentences,dropped)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (owner_id,book_id,book_incarnation,atom) DO UPDATE
                   SET source_hash=EXCLUDED.source_hash,version=EXCLUDED.version,
                       sentences=EXCLUDED.sentences,dropped=EXCLUDED.dropped,created_at=now()
                   WHERE (public.chapter_notes.source_hash,public.chapter_notes.version)
                         IS DISTINCT FROM (EXCLUDED.source_hash,EXCLUDED.version)""",
                (self._owner.value, book["id"], book["incarnation"], atom, source_hash, version,
                 Jsonb(kept), dropped))
