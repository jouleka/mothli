"""The stored conversation's three catalog methods over Postgres, for one owner (ADR 0041 part 2).

Same names, arguments and return shapes as ``app.catalog.catalog.Catalog``. Nothing here decides
which turns may be shown again: that is ``app.read_map.visible_turns`` (inside a book) and
``app.home_conversation.home_turns`` (across books), run unchanged over this adapter.

The home conversation is the local ``book_id=None`` with incarnation ``"home"``; here it is the row
with no book. ``turn_id`` is the order of a conversation and the recollection cursor; it is
returned to the modules, as locally, and never put in a URL or a response.
"""

from __future__ import annotations

from datetime import UTC

from psycopg.types.json import Jsonb

from app.home_conversation import HOME
from app.hosted.companion import OwnerBoundAdapter, as_uuid, check_position

_TURN_COLUMNS = ("turn_id,position_epoch,atom,source_offset,role,content,reaction,created_at,"
                 "drew_on")


def _check_epoch(value) -> None:
    """The local catalog's integer guard for the reading pass, with the BIGINT bound."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"source position must be an int ordinal, got {value!r}")
    if value < 0 or value >= 2**63:
        raise ValueError(f"source position must be in [0, 2**63), got {value!r}")


def _turn(row):
    return {"turn_id": row["turn_id"], "position_epoch": row["position_epoch"],
            "atom": row["atom"], "source_offset": row["source_offset"], "role": row["role"],
            "content": row["content"], "reaction": int(row["reaction"]),
            "created_at": row["created_at"].astimezone(UTC).isoformat(),
            "drew_on": row["drew_on"]}


class TurnStore(OwnerBoundAdapter):
    __slots__ = ()

    def add_turn(self, book_id, *, incarnation, position_epoch, atom, offset, role, content,
                 reaction=False, request_id=None, drew_on=None):
        """Store one turn; returns its id (an increasing integer, the conversation's order).

        ``drew_on``: {book_id: [incarnation, read map]} as it was when the turn's prompt was
        built; it decides alone whether the turn may be shown again. A turn of a book is stored
        only against a live book of this owner, in that shelf lifetime.
        """
        _check_epoch(position_epoch)
        check_position(atom)
        check_position(offset)
        record = None if drew_on is None else Jsonb(drew_on)
        values = (position_epoch, atom, offset, role, content, bool(reaction), request_id, record)
        with self._transaction() as conn:
            if book_id is None:
                if incarnation != HOME:
                    raise ValueError("a turn of no book belongs to the home conversation")
                row = conn.execute(
                    """INSERT INTO public.conversation_turns
                         (owner_id,book_id,book_incarnation,position_epoch,atom,source_offset,
                          role,content,reaction,request_id,drew_on)
                       VALUES (%s,NULL,NULL,%s,%s,%s,%s,%s,%s,%s,%s)
                       RETURNING turn_id""",
                    (self._owner.value, *values)).fetchone()
            else:
                row = conn.execute(
                    """INSERT INTO public.conversation_turns
                         (owner_id,book_id,book_incarnation,position_epoch,atom,source_offset,
                          role,content,reaction,request_id,drew_on)
                       SELECT book.owner_id,book.id,book.incarnation,%s,%s,%s,%s,%s,%s,%s,%s
                       FROM public.books AS book
                       WHERE book.owner_id=%s AND book.id=%s AND book.incarnation=%s
                         AND book.deleted_at IS NULL
                       RETURNING turn_id""",
                    (*values, self._owner.value, as_uuid(book_id),
                     as_uuid(incarnation))).fetchone()
                if row is None:
                    raise LookupError("unknown book")
        return row["turn_id"]

    def get_turns(self, book_id, *, incarnation, reactions=False, after=0):
        """Every stored turn of one conversation, oldest first, whether or not it may be read
        back; ``after`` leaves out turns up to that id."""
        if not isinstance(after, int) or isinstance(after, bool):
            raise ValueError(f"after must be a turn id, got {after!r}")
        only_reactions = " AND reaction" if reactions else ""
        with self._transaction() as conn:
            if book_id is None:
                if incarnation != HOME:
                    return []
                rows = conn.execute(
                    f"""SELECT {_TURN_COLUMNS} FROM public.conversation_turns
                        WHERE owner_id=%s AND book_id IS NULL AND turn_id>%s{only_reactions}
                        ORDER BY turn_id""",
                    (self._owner.value, after)).fetchall()
            else:
                rows = conn.execute(
                    f"""SELECT {_TURN_COLUMNS} FROM public.conversation_turns
                        WHERE owner_id=%s AND book_id=%s AND book_incarnation=%s
                          AND turn_id>%s{only_reactions}
                        ORDER BY turn_id""",
                    (self._owner.value, as_uuid(book_id), as_uuid(incarnation),
                     after)).fetchall()
        return [_turn(row) for row in rows]

    def clear_turns(self, book_id):
        """Forget one conversation of this owner, in every shelf lifetime of the book.

        Locally this also forgets how far the conversation was looked at for recollections; the
        hosted cursor table arrives with part 7 (migration 0022), which adds its delete here.
        """
        with self._transaction() as conn:
            if book_id is None:
                conn.execute(
                    "DELETE FROM public.conversation_turns WHERE owner_id=%s AND book_id IS NULL",
                    (self._owner.value,))
            else:
                conn.execute(
                    "DELETE FROM public.conversation_turns WHERE owner_id=%s AND book_id=%s",
                    (self._owner.value, as_uuid(book_id)))
