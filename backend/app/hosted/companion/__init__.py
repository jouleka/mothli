"""The companion in hosted mode: the local modules run unchanged against owner-bound adapters.

ADR 0041 section 1. An adapter is constructed with one ``OwnerId``, keeps it privately and offers
no way to change it; no method takes an owner. Every call is one short synchronous transaction
that sets ``app.owner_id`` for row-level security and still names the owner in every predicate
(section 2), so either barrier alone keeps one reader's rows from another.
"""

from __future__ import annotations

import threading
import uuid
from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg.rows import dict_row

from app.hosted.tenant.models import OwnerId

_CONNECT_WAIT_SECONDS = 30.0
_limit_guard = threading.Lock()
_connections = threading.BoundedSemaphore(20)


class CompanionDatabaseBusy(Exception):
    """Every companion database connection of this process stayed in use for too long."""


def as_uuid(value) -> uuid.UUID:
    """A book id or incarnation as the modules pass it (a string), checked as a UUID.

    A malformed id names no book of anyone's, so it is unknown, exactly like a foreign one.
    """
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise LookupError("unknown book") from exc


def check_position(value, what="source position") -> None:
    """The local catalog's integer guard (``Catalog._check_int``), with Postgres INTEGER bounds."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{what} must be an int ordinal, got {value!r}")
    if value < 0 or value > 2**31 - 1:
        raise ValueError(f"{what} must be in [0, 2**31), got {value!r}")


def set_connection_limit(limit: int) -> None:
    """Bound the connections the companion adapters hold at once in this process (startup only)."""
    global _connections
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise ValueError("the companion connection limit must be a positive integer")
    with _limit_guard:
        _connections = threading.BoundedSemaphore(limit)


class OwnerBoundAdapter:
    """Base of every hosted companion adapter: one owner, fixed at construction."""

    __slots__ = ("_dsn", "_owner")

    def __init__(self, dsn: str, owner_id: OwnerId) -> None:
        if not isinstance(owner_id, OwnerId):
            raise TypeError("owner_id must be an OwnerId")
        if not isinstance(dsn, str) or not dsn:
            raise TypeError("dsn must be a non-empty string")
        object.__setattr__(self, "_dsn", dsn)
        object.__setattr__(self, "_owner", owner_id)

    def __setattr__(self, name, value):
        raise AttributeError("an owner-bound adapter cannot be changed after construction")

    def __delattr__(self, name):
        raise AttributeError("an owner-bound adapter cannot be changed after construction")

    def __repr__(self) -> str:
        return f"{type(self).__name__}(<owner-bound>)"

    @contextmanager
    def _transaction(self) -> Iterator[psycopg.Connection]:
        """One short transaction with row-level security set to this adapter's owner."""
        semaphore = _connections
        if not semaphore.acquire(timeout=_CONNECT_WAIT_SECONDS):
            raise CompanionDatabaseBusy("companion database connections are exhausted")
        try:
            with psycopg.connect(self._dsn, row_factory=dict_row) as conn, conn.transaction():
                conn.execute(
                    "SELECT set_config('app.owner_id', %s, true)", (str(self._owner.value),)
                )
                yield conn
        finally:
            semaphore.release()

    def _lock_book(self, conn: psycopg.Connection, book_id) -> None:
        """Serialize read-then-write on one owner's book across threads and processes."""
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s::text || ':' || %s::text, 48))",
            (self._owner.value, book_id),
        )

    def _lock_recollections(self, conn: psycopg.Connection) -> None:
        """Serialize this owner's notes, their cursors and the clearing of a conversation: a batch
        is stored only while its messages and its cursor are as they were read (part 7)."""
        self._lock_book(conn, "recollections")
