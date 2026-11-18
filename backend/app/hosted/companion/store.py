"""``HostedCompanionCatalog``: the catalog the companion modules call, for one hosted reader.

ADR 0041 section 1. Each feature's methods live in their own file and are combined here; this
file holds the book-level methods every module reaches through ``read_frontier.source_state``.
Only live (not soft-deleted) books are returned, and every other read is reached through them.
"""

from __future__ import annotations

import threading

from app.hosted.companion import OwnerBoundAdapter, as_uuid, check_position
from app.hosted.companion.spend import SpendStore
from app.hosted.companion.store_notes import NotesStore
from app.hosted.companion.store_read_map import ReadMapStore
from app.hosted.companion.store_recollections import RecollectionStore
from app.hosted.companion.store_turns import TurnStore
from app.hosted.storage import SourceObjectRef
from app.hosted.tenant.models import OwnerId

_BOOK_COLUMNS = """
    SELECT book.id,book.incarnation,book.title,book.author,book.file_hash,
           book.content_language,book.book_type,book.created_at,book.source_kind
    FROM public.books AS book
"""


def _book(row):
    # ``source`` as locally: "upload", or the free-library tag ("gutenberg:<number>") of a book
    # added from the library, by which suggestions know that book is already on the shelf.
    return {"book_id": str(row["id"]), "incarnation": str(row["incarnation"]),
            "title": row["title"], "author": row["author"], "file_hash": row["file_hash"],
            "source": row["source_kind"],
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

        ``bookmark`` is the companion's chapter counter: ``memory_through``, which only
        ``set_bookmark`` (the read map's ``sync_counter``) writes, so a device's position report
        never moves it up (ADR 0042 section 3). The older counter, which "start over" sets to 0,
        can only bring it down until the map writes both again, as it did before. Until the
        book's read map has begun, it is that older counter, from which the shared code begins
        the map once; with the read map off it is all there is (the place devices report).

        ``ingest_progress`` counts the chapters, from the first, whose story memory is built: a
        live receipt for a live chapter at every ordinal up to it (0 before extraction). For a
        library book they are the receipts the door hands this reader, so it never passes their
        place.
        """
        try:
            book = as_uuid(book_id)
        except LookupError:
            return None
        with self._transaction() as conn:
            row = conn.execute(
                """SELECT state.book_incarnation,state.current_cfi,state.position_epoch,
                          state.updated_at,
                          CASE WHEN EXISTS (
                                 SELECT 1 FROM public.read_position AS place
                                 WHERE (place.owner_id,place.book_id,place.book_incarnation)
                                     =(state.owner_id,state.book_id,state.book_incarnation))
                               THEN LEAST(state.memory_through,state.bookmark)
                               ELSE state.bookmark END AS counter
                   FROM public.reading_state AS state
                   JOIN public.books AS book
                     ON (book.owner_id,book.id,book.incarnation)
                      =(state.owner_id,state.book_id,state.book_incarnation)
                   WHERE state.owner_id=%s AND state.book_id=%s AND book.deleted_at IS NULL""",
                (self._owner.value, book)).fetchone()
            if row is None:
                return None
            door = conn.execute("SELECT completed FROM public.library_story_memory(%s)",
                                (book,)).fetchone()
            if door is not None:
                built = sorted(receipt["ordinal"] for receipt in door["completed"])
            else:
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
        return {"bookmark": row["counter"], "cfi": row["current_cfi"],
                "ingest_progress": progress, "position_epoch": row["position_epoch"],
                "updated_at": row["updated_at"].isoformat()}

    def set_bookmark(self, book_id, bookmark):
        """Set the chapter counter exactly, lowering it if need be (only the read map calls this).

        It writes ``memory_through``, the reader's place for the library door, and nothing else
        in the app writes that column; the older counter the device position route raises is
        set to the same value, as before, so the reader's position still follows the map.
        Lowering never reveals anything. The cross-device position clock is left alone: this is
        the server's own reading of the map, not a device's report.
        """
        check_position(bookmark, "bookmark")
        with self._transaction() as conn:
            conn.execute(
                """UPDATE public.reading_state AS state
                   SET bookmark=%s,memory_through=%s,updated_at=now()
                   FROM public.books AS book
                   WHERE (book.owner_id,book.id,book.incarnation)
                       =(state.owner_id,state.book_id,state.book_incarnation)
                     AND state.owner_id=%s AND state.book_id=%s AND book.deleted_at IS NULL""",
                (bookmark, bookmark, self._owner.value, as_uuid(book_id)))

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


class _ChapterSpendStore(SpendStore):
    """The two catalog calls the shared chapter extraction makes beyond spend (ADR 0042 section 1).

    ``app.ingest.chapter_extraction`` asks for a chapter's outstanding extraction reservations
    before it pays for the chapter again, and calls ``finalize_ingest`` once the chapter's memory
    is committed. In hosted mode the commit is the publish, which already wrote the receipt, so
    finalizing only settles the chapter's deferred extraction calls, each once.
    """

    __slots__ = ()

    def cost_reservation_ids(self, book_id, *, phase=None, chapter_ordinal=None):
        """This owner's open companion reservations of one live book, oldest first, optionally
        of one purpose (the local phase name) and one chapter. A book that is not this reader's
        live book has none."""
        try:
            book = as_uuid(book_id)
        except LookupError:
            return []
        clauses, params = [], []
        if phase is not None:
            clauses.append(" AND reservation.purpose=%s")
            params.append(phase)
        if chapter_ordinal is not None:
            clauses.append(" AND reservation.chapter_ordinal=%s")
            params.append(chapter_ordinal)
        with self._transaction() as conn:
            rows = conn.execute(
                """SELECT reservation.id FROM public.cost_reservations AS reservation
                   JOIN public.books AS book
                     ON (book.owner_id,book.id)=(reservation.owner_id,reservation.book_id)
                   WHERE reservation.owner_id=%s AND reservation.book_id=%s
                     AND book.deleted_at IS NULL
                     AND reservation.state='reserved' AND reservation.purpose IS NOT NULL"""
                + "".join(clauses) + " ORDER BY reservation.created_at,reservation.id",
                (self._owner.value, book, *params)).fetchall()
        return [row["id"].hex for row in rows]

    def finalize_ingest(self, book_id, n, *, cost=None, incarnation=None):
        """Settle chapter ``n``'s deferred extraction reservations, at their noted size, once.

        ``cost`` (the receipt's own total) is not used: each extraction call was reserved and
        its actual usage noted, so settling the reservations records exactly what was spent,
        and a reservation already settled is not open any more. Raises ``ValueError`` when the
        book is not this reader's live book in ``incarnation``, as locally.
        """
        check_position(n, "ingest_progress")
        book = self.get_book(book_id)
        if book is None or (incarnation is not None and book["incarnation"] != incarnation):
            raise ValueError(f"stale catalog incarnation for book {book_id!r}")
        for reservation in self.cost_reservation_ids(book_id, phase="extraction",
                                                     chapter_ordinal=n):
            self.settle_cost(book_id, reservation)


# The combining line: each part adds its feature's methods here.
class HostedCompanionCatalog(_BookStore, ReadMapStore, TurnStore, _ChapterSpendStore, NotesStore,
                             RecollectionStore):
    """The companion's catalog for one hosted reader. Construct per request or per job:
    ``HostedCompanionCatalog(dsn, owner_id)``."""

    __slots__ = ()


class ReplyCatalog(HostedCompanionCatalog):
    """The catalog for one reply a reader is waiting on. Construct one per reply.

    The reply's first model call is checked against the caps like any other. Once that call is
    admitted, the reply's later calls are not refused for a dollar cap (``spend.py``), so a reply
    that began under the cap is finished. The owner is fixed as in every adapter; the only thing
    that changes is whether this one reply has been admitted.
    """

    __slots__ = ("_admitted",)

    def __init__(self, dsn: str, owner_id: OwnerId) -> None:
        super().__init__(dsn, owner_id)
        object.__setattr__(self, "_admitted", threading.Event())


class ChapterCatalog(ReplyCatalog):
    """The catalog for one chapter of story memory (ADR 0042 section 4). Construct one per
    chapter.

    A reply's rule, for the same reason: the chapter's first model call is checked against the
    caps like any other, and once it is admitted the chapter's later calls are not refused for a
    dollar cap, so a chapter begun under the cap is finished and its paid calls are not thrown
    away. A reader already at a cap is refused at the first call, before anything is spent.
    """

    __slots__ = ()
