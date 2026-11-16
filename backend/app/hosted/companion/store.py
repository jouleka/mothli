"""``HostedCompanionCatalog``: the catalog the companion modules call, for one hosted reader.

ADR 0041 section 1. Each feature's methods live in their own file and are combined here; this
file holds the book-level methods every module reaches through ``read_frontier.source_state``.
Only live (not soft-deleted) books are returned, and every other read is reached through them.
"""

from __future__ import annotations

from app.hosted.companion import OwnerBoundAdapter, as_uuid, check_position
from app.hosted.companion.spend import SpendStore
from app.hosted.companion.store_notes import NotesStore
from app.hosted.companion.store_read_map import ReadMapStore
from app.hosted.companion.store_turns import TurnStore
from app.hosted.storage import SourceObjectRef

_BOOK_COLUMNS = """
    SELECT book.id,book.incarnation,book.title,book.author,book.file_hash,
           book.content_language,book.book_type,book.created_at
    FROM public.books AS book
"""


def _book(row):
    return {"book_id": str(row["id"]), "incarnation": str(row["incarnation"]),
            "title": row["title"], "author": row["author"], "file_hash": row["file_hash"],
            "content_language": row["content_language"], "book_type": row["book_type"],
            "added_at": row["created_at"].isoformat()}


class _BookStore(OwnerBoundAdapter):
    __slots__ = ()

    def get_book(self, book_id):
        try:
            book = as_uuid(book_id)
        except LookupError:
            return None
        with self._transaction() as conn:
            row = conn.execute(
                _BOOK_COLUMNS + " WHERE book.owner_id=%s AND book.id=%s AND book.deleted_at IS NULL",
                (self._owner.value, book)).fetchone()
        return _book(row) if row else None

    def list_books(self):
        """Every live book of this owner, oldest first."""
        with self._transaction() as conn:
            rows = conn.execute(
                _BOOK_COLUMNS + " WHERE book.owner_id=%s AND book.deleted_at IS NULL"
                " ORDER BY book.created_at,book.id",
                (self._owner.value,)).fetchall()
        return [_book(row) for row in rows]

    def get_state(self, book_id):
        """{bookmark, cfi, ingest_progress, position_epoch, updated_at} of a live book, or None.

        ``ingest_progress`` counts the chapters, from the first, whose story memory is built: a
        live receipt for a live chapter at every ordinal up to it (0 before extraction).
        """
        try:
            book = as_uuid(book_id)
        except LookupError:
            return None
        with self._transaction() as conn:
            row = conn.execute(
                """SELECT state.book_incarnation,state.bookmark,state.current_cfi,
                          state.position_epoch,state.updated_at
                   FROM public.reading_state AS state
                   JOIN public.books AS book
                     ON (book.owner_id,book.id,book.incarnation)
                      =(state.owner_id,state.book_id,state.book_incarnation)
                   WHERE state.owner_id=%s AND state.book_id=%s AND book.deleted_at IS NULL""",
                (self._owner.value, book)).fetchone()
            if row is None:
                return None
            built = [receipt["revealed_at"] for receipt in conn.execute(
                """SELECT chapter.revealed_at
                   FROM public.ingested_chapters AS receipt
                   JOIN public.chapters AS chapter
                     ON (chapter.owner_id,chapter.book_id,chapter.book_incarnation,chapter.id)
                      =(receipt.owner_id,receipt.book_id,receipt.book_incarnation,
                        receipt.chapter_id)
                   WHERE receipt.owner_id=%s AND receipt.book_id=%s
                     AND receipt.book_incarnation=%s
                     AND receipt.retracted_at IS NULL AND chapter.retracted_at IS NULL
                   ORDER BY chapter.revealed_at""",
                (self._owner.value, book, row["book_incarnation"])).fetchall()]
        progress = 0
        for ordinal in built:
            if ordinal != progress + 1:
                break
            progress = ordinal
        return {"bookmark": row["bookmark"], "cfi": row["current_cfi"],
                "ingest_progress": progress, "position_epoch": row["position_epoch"],
                "updated_at": row["updated_at"].isoformat()}

    def set_bookmark(self, book_id, bookmark):
        """Set the chapter counter exactly, lowering it if need be (only the read map calls this).

        Lowering never reveals anything. The cross-device position clock is left alone: this is
        the server's own reading of the map, not a device's report.
        """
        check_position(bookmark, "bookmark")
        with self._transaction() as conn:
            conn.execute(
                """UPDATE public.reading_state AS state SET bookmark=%s,updated_at=now()
                   FROM public.books AS book
                   WHERE (book.owner_id,book.id,book.incarnation)
                       =(state.owner_id,state.book_id,state.book_incarnation)
                     AND state.owner_id=%s AND state.book_id=%s AND book.deleted_at IS NULL""",
                (bookmark, self._owner.value, as_uuid(book_id)))

    def get_conversation_frontier(self, book_id):
        """Always None: hosted mode runs only with the read map, which never saves an anchor.

        The map is then seeded from the chapter counter, as locally for a book with no anchor.
        """
        return None

    def source_record(self, book_id):
        """The stored EPUB of a live book and the chapter lengths recorded when it was uploaded.

        {ref, incarnation, provider, media_type, byte_size, sha256, atoms: [(ordinal, href,
        char_len)]}, or None. The object reference carries this adapter's owner, never one taken
        from a caller.
        """
        try:
            book = as_uuid(book_id)
        except LookupError:
            return None
        with self._transaction() as conn:
            row = conn.execute(
                """SELECT source.book_incarnation,source.storage_key,source.storage_provider,
                          source.media_type,source.byte_size,source.sha256
                   FROM public.source_objects AS source
                   JOIN public.books AS book
                     ON (book.owner_id,book.id,book.incarnation)
                      =(source.owner_id,source.book_id,source.book_incarnation)
                   WHERE source.owner_id=%s AND source.book_id=%s
                     AND source.deleted_at IS NULL AND book.deleted_at IS NULL""",
                (self._owner.value, book)).fetchone()
            if row is None:
                return None
            atoms = conn.execute(
                """SELECT ordinal,href,char_end-char_start AS char_len
                   FROM public.book_search_documents
                   WHERE owner_id=%s AND book_id=%s AND book_incarnation=%s
                   ORDER BY ordinal""",
                (self._owner.value, book, row["book_incarnation"])).fetchall()
        try:
            object_id = as_uuid(row["storage_key"])
        except LookupError as exc:
            raise ValueError("source is unavailable") from exc
        return {"ref": SourceObjectRef(self._owner, object_id),
                "incarnation": str(row["book_incarnation"]),
                "provider": row["storage_provider"], "media_type": row["media_type"],
                "byte_size": row["byte_size"], "sha256": row["sha256"],
                "atoms": [(atom["ordinal"], atom["href"], atom["char_len"]) for atom in atoms]}


# The combining line: each part adds its feature's methods here.
class HostedCompanionCatalog(_BookStore, ReadMapStore, TurnStore, SpendStore, NotesStore):
    """The companion's catalog for one hosted reader. Construct per request or per job:
    ``HostedCompanionCatalog(dsn, owner_id)``."""

    __slots__ = ()
