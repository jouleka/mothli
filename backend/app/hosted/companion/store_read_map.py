"""The read map's seven catalog methods over Postgres, for one owner (ADR 0041 part 1), and
``has_read_page``, which only hosted story memory asks (ADR 0044 section 3).

Same names, arguments and return shapes as ``app.catalog.catalog.Catalog``; ranges are merged by
the local catalog's own rule. Read-then-write methods hold the per-book advisory lock inside their
transaction, so two threads or two web processes never merge over each other.
"""

from __future__ import annotations

from app.catalog.catalog import Catalog
from app.hosted.companion import OwnerBoundAdapter, as_uuid, check_position


def _by_atom(rows):
    by_atom = {}
    for row in rows:
        by_atom.setdefault(row["atom"], []).append((row["start_offset"], row["end_offset"]))
    return {atom: Catalog._merged(ranges) for atom, ranges in sorted(by_atom.items())}


class ReadMapStore(OwnerBoundAdapter):
    __slots__ = ()

    def add_read_range(self, book_id, *, incarnation, atom, start, end, source):
        """Add [start, end) of one chapter; ranges from the same source that touch become one."""
        for value in (atom, start, end):
            check_position(value)
        if end <= start:
            return
        book, incarnation = as_uuid(book_id), as_uuid(incarnation)
        key = (self._owner.value, book, incarnation, atom, source)
        with self._transaction() as conn:
            self._lock_book(conn, book)
            held = [(row["start_offset"], row["end_offset"]) for row in conn.execute(
                """SELECT start_offset,end_offset FROM public.read_ranges
                   WHERE owner_id=%s AND book_id=%s AND book_incarnation=%s AND atom=%s
                     AND source=%s""", key).fetchall()]
            conn.execute(
                """DELETE FROM public.read_ranges
                   WHERE owner_id=%s AND book_id=%s AND book_incarnation=%s AND atom=%s
                     AND source=%s""", key)
            with conn.cursor() as cursor:
                cursor.executemany(
                    """INSERT INTO public.read_ranges
                         (owner_id,book_id,book_incarnation,atom,start_offset,end_offset,source)
                       VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                    [(self._owner.value, book, incarnation, atom, a, b, source)
                     for a, b in Catalog._merged([*held, (start, end)])])

    def get_read_map(self, book_id, *, incarnation):
        """{chapter: [(start, end), ...]} over every source, merged and in order."""
        with self._transaction() as conn:
            rows = conn.execute(
                """SELECT atom,start_offset,end_offset FROM public.read_ranges
                   WHERE owner_id=%s AND book_id=%s AND book_incarnation=%s""",
                (self._owner.value, as_uuid(book_id), as_uuid(incarnation))).fetchall()
        return _by_atom(rows)

    def add_skimmed_range(self, book_id, *, incarnation, atom, start, end):
        """Add [start, end) of one chapter as skimmed: seen in passing, not read."""
        for value in (atom, start, end):
            check_position(value)
        if end <= start:
            return
        book, incarnation = as_uuid(book_id), as_uuid(incarnation)
        key = (self._owner.value, book, incarnation, atom)
        with self._transaction() as conn:
            self._lock_book(conn, book)
            held = [(row["start_offset"], row["end_offset"]) for row in conn.execute(
                """SELECT start_offset,end_offset FROM public.skimmed_ranges
                   WHERE owner_id=%s AND book_id=%s AND book_incarnation=%s AND atom=%s""",
                key).fetchall()]
            conn.execute(
                """DELETE FROM public.skimmed_ranges
                   WHERE owner_id=%s AND book_id=%s AND book_incarnation=%s AND atom=%s""", key)
            with conn.cursor() as cursor:
                cursor.executemany(
                    """INSERT INTO public.skimmed_ranges
                         (owner_id,book_id,book_incarnation,atom,start_offset,end_offset)
                       VALUES (%s,%s,%s,%s,%s,%s)""",
                    [(self._owner.value, book, incarnation, atom, a, b)
                     for a, b in Catalog._merged([*held, (start, end)])])

    def get_skimmed(self, book_id, *, incarnation):
        """{chapter: [(start, end), ...]} of skimmed text. Never part of the read map."""
        with self._transaction() as conn:
            rows = conn.execute(
                """SELECT atom,start_offset,end_offset FROM public.skimmed_ranges
                   WHERE owner_id=%s AND book_id=%s AND book_incarnation=%s""",
                (self._owner.value, as_uuid(book_id), as_uuid(incarnation))).fetchall()
        return _by_atom(rows)

    def clear_read_map(self, book_id, *, atom=None):
        """Forget what was read, and skimmed, of one chapter or of the whole book."""
        if atom is not None:
            check_position(atom, "chapter")
        book = as_uuid(book_id)
        where = "owner_id=%s AND book_id=%s" + (" AND atom=%s" if atom is not None else "")
        params = (self._owner.value, book, *([atom] if atom is not None else []))
        with self._transaction() as conn:
            self._lock_book(conn, book)
            conn.execute(f"DELETE FROM public.read_ranges WHERE {where}", params)
            conn.execute(f"DELETE FROM public.skimmed_ranges WHERE {where}", params)
            if atom is None:
                # The row stays: it marks the map as begun, so it is never seeded a second time.
                conn.execute(
                    """UPDATE public.read_position SET atom=0,source_offset=0,updated_at=now()
                       WHERE owner_id=%s AND book_id=%s""",
                    (self._owner.value, book))

    def set_read_position(self, book_id, *, incarnation, atom, offset):
        for value in (atom, offset):
            check_position(value)
        with self._transaction() as conn:
            conn.execute(
                """INSERT INTO public.read_position
                     (owner_id,book_id,book_incarnation,atom,source_offset)
                   VALUES (%s,%s,%s,%s,%s)
                   ON CONFLICT (owner_id,book_id,book_incarnation) DO UPDATE
                     SET atom=excluded.atom,source_offset=excluded.source_offset,
                         updated_at=now()""",
                (self._owner.value, as_uuid(book_id), as_uuid(incarnation), atom, offset))

    def has_read_page(self, book_id, *, incarnation):
        """Whether the reader has read a page of this book, one reported page by page and not
        skimmed (``source='read'``): not chapters they marked read, not their reading imported
        from elsewhere, not the map's first seeding. Hosted mode's alone: story memory builds
        nothing for a book until it is true (ADR 0044 section 3)."""
        with self._transaction() as conn:
            row = conn.execute(
                """SELECT EXISTS (SELECT 1 FROM public.read_ranges
                                  WHERE owner_id=%s AND book_id=%s AND book_incarnation=%s
                                    AND source='read') AS read""",
                (self._owner.value, as_uuid(book_id), as_uuid(incarnation))).fetchone()
        return row["read"]

    def get_read_position(self, book_id, *, incarnation):
        with self._transaction() as conn:
            row = conn.execute(
                """SELECT atom,source_offset FROM public.read_position
                   WHERE owner_id=%s AND book_id=%s AND book_incarnation=%s""",
                (self._owner.value, as_uuid(book_id), as_uuid(incarnation))).fetchone()
        return (row["atom"], row["source_offset"]) if row else None
