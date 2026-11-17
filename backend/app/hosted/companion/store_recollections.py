"""What Mothli remembers about the reader, over Postgres, for one owner (ADR 0041 part 7).

Same names, arguments and return shapes as ``app.catalog.catalog.Catalog``. Nothing here decides
what is kept or when a note may be used: ``app.recollections`` does (``distil``, ``usable``,
``listed``, ``recall``), unchanged. What differs from the local catalog:

- Each note also has a ``public_id`` (a UUID), the only id a route takes or returns; the integer
  ``recollection_id`` orders the notes and numbers them for the model, always with this owner.
- The cap and its eviction order apply to this owner's notes only.
- A note tied to a book is read only while that book is on this owner's shelf (books are
  soft-deleted here, so the local cascade becomes a filter); such a note goes first under the cap.
- ``keep_recollections`` takes the cursor the batch was read with (``expected``) and stores
  nothing if the cursor has moved or the batch's last message is gone since: two workers, a lost
  lease, a cleared conversation or "forget everything" can never store a batch twice or bring a
  cleared message back. That replaces the local in-process guard (``Recollector._guard``).
"""

from __future__ import annotations

from datetime import UTC

from psycopg.types.json import Jsonb

from app.home_conversation import HOME
from app.hosted.companion import OwnerBoundAdapter, as_uuid

_COLUMNS = ("note.recollection_id,note.public_id,note.kind,note.text,note.tied,note.book_id,"
            "note.drew_on,note.said,note.said_at,note.created_at,note.used_at")
# A note of this owner, with the live book it is tied to (none when untied or the book is gone).
_NOTES = """
    FROM public.recollections AS note
    LEFT JOIN public.books AS book
      ON (book.owner_id,book.id,book.incarnation)=(note.owner_id,note.book_id,note.book_incarnation)
     AND book.deleted_at IS NULL
    WHERE note.owner_id=%s"""
_GONE = "(note.book_id IS NOT NULL AND book.id IS NULL)"


class RecollectionCursorMoved(RuntimeError):
    """The batch's cursor moved, or its messages were cleared, after the batch was read."""


def _stamp(value) -> str:
    return value.astimezone(UTC).isoformat()


def _note(row):
    return {"recollection_id": row["recollection_id"], "public_id": str(row["public_id"]),
            "kind": row["kind"], "text": row["text"], "tied": row["tied"],
            "book_id": None if row["book_id"] is None else str(row["book_id"]),
            "drew_on": row["drew_on"], "said": row["said"], "said_at": _stamp(row["said_at"]),
            "created_at": _stamp(row["created_at"]), "used_at": _stamp(row["used_at"])}


def _conversation(book_id, incarnation) -> tuple[str, str] | None:
    """The cursor's key for one conversation, or None for a conversation there cannot be."""
    if book_id is None:
        return ("", HOME) if incarnation == HOME else None
    try:
        return str(as_uuid(book_id)), str(as_uuid(incarnation))
    except LookupError:
        return None


def _turn_id(value) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value < 2**63:
        raise ValueError(f"turn must be a turn id, got {value!r}")
    return value


class RecollectionStore(OwnerBoundAdapter):
    __slots__ = ()

    def get_recollections(self):
        """Every note of this owner, oldest first, whether or not it may be used now; a note tied
        to a book no longer on the shelf is left out, as the local catalog deletes it."""
        with self._transaction() as conn:
            rows = conn.execute(
                f"SELECT {_COLUMNS} {_NOTES} AND NOT {_GONE} ORDER BY note.recollection_id",
                (self._owner.value,)).fetchall()
        return [_note(row) for row in rows]

    def keep_recollections(self, changes, *, cap, cursor=None, expected=None):
        """Store one batch's checked notes, move the conversation's cursor and apply the cap, at
        once. Returns how many were stored.

        As locally: a change that ``replaces`` a note takes its place and is dropped if that note
        is gone; ``cursor`` is (book_id or None, incarnation, turn_id) and never moves back; over
        ``cap`` the least recently used go first, the notes about who the reader is last. Here a
        note tied to a book no longer on the shelf goes before any other, and a change tied to
        such a book is dropped.

        ``expected``, given with ``cursor``, is the cursor the batch was read with: if the cursor
        holds anything else now, or the batch's last message is no longer stored, nothing is
        stored and ``RecollectionCursorMoved`` is raised.
        """
        _turn_id(cap)
        key = None
        if cursor is not None:
            book_id, incarnation, last = cursor
            key, last = _conversation(book_id, incarnation), _turn_id(last)
            if key is None:
                raise LookupError("unknown conversation")
        stored = 0
        with self._transaction() as conn:
            self._lock_recollections(conn)
            if key is not None and expected is not None:
                held = conn.execute(
                    """SELECT turn_id FROM public.recollection_cursors
                       WHERE owner_id=%s AND conversation=%s AND incarnation=%s""",
                    (self._owner.value, *key)).fetchone()
                there = conn.execute(
                    """SELECT 1 FROM public.conversation_turns
                       WHERE owner_id=%s AND turn_id=%s
                         AND COALESCE(book_id::text,'')=%s
                         AND COALESCE(book_incarnation::text,'home')=%s""",
                    (self._owner.value, last, *key)).fetchone()
                if (held["turn_id"] if held else 0) != _turn_id(expected) or there is None:
                    raise RecollectionCursorMoved("the conversation moved on since the batch")
            for change in changes:
                if change.get("replaces") is not None and conn.execute(
                        "DELETE FROM public.recollections WHERE owner_id=%s AND recollection_id=%s",
                        (self._owner.value, _turn_id(change["replaces"]))).rowcount == 0:
                    continue
                values = (change["kind"], change["text"], bool(change["tied"]),
                          None if change["drew_on"] is None else Jsonb(change["drew_on"]),
                          change["said"], change["said_at"])
                if change["book_id"] is None:
                    row = conn.execute(
                        """INSERT INTO public.recollections
                             (owner_id,kind,text,tied,drew_on,said,said_at)
                           VALUES (%s,%s,%s,%s,%s,%s,%s::timestamptz) RETURNING recollection_id""",
                        (self._owner.value, *values)).fetchone()
                else:
                    try:
                        book = as_uuid(change["book_id"])
                    except LookupError:
                        continue
                    row = conn.execute(
                        """INSERT INTO public.recollections
                             (owner_id,book_id,book_incarnation,kind,text,tied,drew_on,said,said_at)
                           SELECT book.owner_id,book.id,book.incarnation,%s,%s,%s,%s,%s,
                                  %s::timestamptz
                           FROM public.books AS book
                           WHERE book.owner_id=%s AND book.id=%s AND book.deleted_at IS NULL
                           RETURNING recollection_id""",
                        (*values, self._owner.value, book)).fetchone()
                stored += row is not None
            if key is not None:
                conn.execute(
                    """INSERT INTO public.recollection_cursors
                         (owner_id,conversation,incarnation,turn_id) VALUES (%s,%s,%s,%s)
                       ON CONFLICT (owner_id,conversation,incarnation) DO UPDATE
                       SET turn_id=GREATEST(public.recollection_cursors.turn_id,EXCLUDED.turn_id),
                           updated_at=now()""",
                    (self._owner.value, *key, last))
            conn.execute(
                f"""DELETE FROM public.recollections WHERE owner_id=%s AND recollection_id IN (
                      SELECT note.recollection_id {_NOTES}
                      ORDER BY {_GONE} DESC,note.kind IN ('about','taste'),note.used_at,
                               note.recollection_id
                      LIMIT GREATEST(0,(SELECT count(*) FROM public.recollections
                                        WHERE owner_id=%s)-%s))""",
                (self._owner.value, self._owner.value, self._owner.value, cap))
        return stored

    def touch_recollections(self, ids):
        """These notes were just given to a reply."""
        chosen = [_turn_id(value) for value in ids]
        if not chosen:
            return
        with self._transaction() as conn:
            conn.execute(
                """UPDATE public.recollections SET used_at=now()
                   WHERE owner_id=%s AND recollection_id=ANY(%s)""",
                (self._owner.value, chosen))

    def delete_recollection(self, public_id):
        """Forget one note of this owner, named by its public id. False when there is none: a
        malformed, missing or another reader's id are the same."""
        try:
            note = as_uuid(public_id)
        except LookupError:
            return False
        with self._transaction() as conn:
            self._lock_recollections(conn)
            gone = conn.execute(
                "DELETE FROM public.recollections WHERE owner_id=%s AND public_id=%s",
                (self._owner.value, note)).rowcount
        return bool(gone)

    def clear_recollections(self):
        """Forget every note of this owner, and count every message stored so far as looked at,
        so that nothing said before this moment becomes a note afterwards."""
        with self._transaction() as conn:
            self._lock_recollections(conn)
            conn.execute("DELETE FROM public.recollections WHERE owner_id=%s",
                         (self._owner.value,))
            conn.execute(
                """INSERT INTO public.recollection_cursors (owner_id,conversation,incarnation,turn_id)
                   SELECT owner_id,COALESCE(book_id::text,''),COALESCE(book_incarnation::text,'home'),
                          max(turn_id)
                   FROM public.conversation_turns WHERE owner_id=%s
                   GROUP BY owner_id,COALESCE(book_id::text,''),COALESCE(book_incarnation::text,'home')
                   ON CONFLICT (owner_id,conversation,incarnation) DO UPDATE
                   SET turn_id=GREATEST(public.recollection_cursors.turn_id,EXCLUDED.turn_id),
                       updated_at=now()""",
                (self._owner.value,))

    def get_recollection_cursor(self, book_id, *, incarnation):
        """The id of the last turn of this conversation looked at for recollections (0: none)."""
        key = _conversation(book_id, incarnation)
        if key is None:
            return 0
        with self._transaction() as conn:
            row = conn.execute(
                """SELECT turn_id FROM public.recollection_cursors
                   WHERE owner_id=%s AND conversation=%s AND incarnation=%s""",
                (self._owner.value, *key)).fetchone()
        return row["turn_id"] if row else 0
