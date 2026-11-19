"""Erase the accounts whose 30 days have passed (ADR 0041 part 8), and the notes of book files
nobody has held for 30 days (ADR 0044 section 6). An operator command, run once a day, under its
own database role; never part of the web process or the worker.

    python -m app.hosted.purge        # HOSTED_PURGE_DSN, and the storage settings of app.env

A reader who asked to delete their account is kept, signed out and refused everywhere, until
``users.erase_after``; then this erases them in three steps, each safe to repeat:

1. **Point of no return** (one transaction): the account becomes ``deleted`` (``erase_after``
   still set, meaning "erasing"), so it can no longer be kept or signed into, and its sessions go.
   Nothing of an erasing account is ever served again, so its rows may outlive its files.
2. **Book files**: everything under the reader's folder in object storage, listed from storage
   itself, so a file whose row was never written goes too.
3. **Rows** (one transaction): every row with this ``owner_id`` in every table that has one,
   found in the database's own catalogue (so a table a later migration adds is covered with no
   change here), children before parents. The audit trail keeps only the ``account.*`` events,
   plus one ``account.purge``; the ``users`` row stays as a bare id (the audit events refer to
   it), its name and email emptied, and ``erase_after`` cleared, meaning "erased".

A crash after step 1 leaves the account "erasing"; the next run finishes it. Nothing is logged.

Then it erases the story notes of a book file nobody holds any more (ADR 0044 section 6). An
**edition** is the notes account's ``books`` row of one file (marked by ``edition_of``); a
**holder** of the file is a reader's live ``books`` row with the same two fingerprints whose
account is not pending deletion or being erased (``_HELD``). Each run:

1. **Marks** (one transaction): an edition with a holder loses its "no holder since" mark; an
   edition without one and without a mark gets one, now. A mark already there is kept, so the
   30 days count from when the last holder was first found gone.
2. **Erases** every edition whose mark is 30 days old, one at a time: first any stored file of
   it (only an edition the removed operator's command made has one), object then record; then,
   in one transaction under the edition's own lock (the lock a chapter's publish takes), it
   decides again: a holder back clears the mark and keeps everything; otherwise the edition's
   row becomes a bare id and its chapters are deleted, and every story memory row under them
   with them (migration 0024's cascade). A publish waiting for that lock finds the edition gone
   and publishes nothing; a publish holding it finishes first, and its reader is a holder.

The notes account, its limits, its ledger and its jobs stay. A file that comes back later starts
a new edition from its first chapter.
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from dataclasses import dataclass, field

import psycopg
from psycopg import sql

from app.hosted.audit import record_event
from app.hosted.storage import SourceObjectRef
from app.hosted.tenant.grants import ALL_TABLE_PRIVILEGES, purge_table_privileges
from app.hosted.tenant.models import OwnerId
from app.hosted.tenant.repository import ERASED_WITH_THE_BOOK


class PurgeConfigurationError(RuntimeError):
    """A refusal whose message names a check, never a value."""


# ADR 0044 section 6. A live edition: a book of the notes account marked as one file's notes.
_LIVE_EDITION = "edition.edition_of IS NOT NULL AND edition.deleted_at IS NULL"
# Whether anyone holds the edition's file: a reader's live book with the same two fingerprints,
# whose account is not pending deletion ('deleting') or being erased ('deleted'). The notes
# account's own books (the edition, and any copy the operator's command left) hold nothing.
_HELD = """EXISTS (
  SELECT 1 FROM public.books AS held
  JOIN public.users AS reader ON reader.id=held.owner_id
  WHERE held.file_hash=edition.file_hash AND held.split_hash=edition.split_hash
    AND held.deleted_at IS NULL AND held.owner_id<>edition.owner_id
    AND reader.status NOT IN ('deleting','deleted'))"""
# How long an edition is kept after its last holder was first found gone.
_UNHELD_FOR = "interval '30 days'"
# The edition's lock: the one a chapter's publish into it takes (``commit_chapter_memory``).
_EDITION_LOCK = "SELECT pg_advisory_xact_lock(hashtextextended(%s::text || ':' || %s::text, 48))"


_OWNER_TABLES_SQL = """
SELECT table_class.relname
FROM pg_class AS table_class
JOIN pg_namespace AS namespace ON namespace.oid = table_class.relnamespace
JOIN pg_attribute AS attribute ON attribute.attrelid = table_class.oid
WHERE namespace.nspname = 'public' AND table_class.relkind = 'r'
  AND attribute.attname = 'owner_id' AND NOT attribute.attisdropped
ORDER BY table_class.relname
"""


def owner_tables(conn: psycopg.Connection) -> list[str]:
    """Every table in the schema with an ``owner_id`` column, from ``pg_catalog`` (which, unlike
    ``information_schema``, shows a role the tables it holds no privilege on)."""
    return [row[0] for row in conn.execute(_OWNER_TABLES_SQL).fetchall()]


def deletion_order(conn: psycopg.Connection, tables: list[str]) -> list[str]:
    """``tables`` ordered so every table comes before any table its foreign keys point at."""
    wanted = set(tables)
    references: dict[str, set[str]] = {table: set() for table in tables}
    for child, parent in conn.execute(
        """
        SELECT child.relname, parent.relname
        FROM pg_constraint AS constraint_row
        JOIN pg_class AS child ON child.oid = constraint_row.conrelid
        JOIN pg_class AS parent ON parent.oid = constraint_row.confrelid
        JOIN pg_namespace AS namespace ON namespace.oid = child.relnamespace
        WHERE constraint_row.contype = 'f' AND namespace.nspname = 'public'
        """
    ).fetchall():
        if child in wanted and parent in wanted and child != parent:
            references[parent].add(child)
    ordered: list[str] = []
    remaining = set(tables)
    while remaining:
        ready = sorted(table for table in remaining if not references[table] & remaining)
        if not ready:
            raise PurgeConfigurationError("owner tables refer to each other in a cycle")
        ordered.extend(ready)
        remaining.difference_update(ready)
    return ordered


@dataclass(slots=True)
class PostgresAccountPurge:
    _dsn: str = field(repr=False)

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self._dsn)

    def check_runtime_role(self) -> None:
        """The purge role is a non-superuser that bypasses row security (it works across owners),
        inherits nothing, and holds exactly its privileges on every table: SELECT and DELETE where
        there is an ``owner_id``, its few others (``grants.py``), and nothing anywhere else."""
        with self._connect() as conn:
            role = conn.execute(
                """
                SELECT rolsuper, rolbypassrls, rolcreatedb, rolcreaterole, rolreplication,
                       rolinherit,
                       EXISTS (SELECT 1 FROM pg_auth_members WHERE member = role_row.oid)
                FROM pg_roles AS role_row WHERE rolname = current_user
                """
            ).fetchone()
            if role is None or role[0] or not role[1] or any(role[2:]):
                raise PurgeConfigurationError(
                    "HOSTED_PURGE_DSN must use an isolated non-superuser BYPASSRLS purge role"
                )
            expected = purge_table_privileges(owner_tables(conn))
            held = conn.execute(
                """
                SELECT table_class.relname,
                       ARRAY(SELECT privilege FROM unnest(%s::text[]) AS privilege
                             WHERE has_table_privilege(current_user, table_class.oid, privilege))
                FROM pg_class AS table_class
                JOIN pg_namespace AS namespace ON namespace.oid = table_class.relnamespace
                WHERE namespace.nspname = 'public' AND table_class.relkind IN ('r', 'p')
                """,
                (sorted(ALL_TABLE_PRIVILEGES),),
            ).fetchall()
        actual = {table: frozenset(privileges) for table, privileges in held if privileges}
        if actual != expected:
            raise PurgeConfigurationError(
                "purge role privileges are not exactly the owner tables and its reviewed others"
            )

    def due(self) -> list[uuid.UUID]:
        """Accounts past their day, and any whose erasure an earlier run began and did not end."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id FROM public.users
                WHERE (status = 'deleting' AND erase_after <= now())
                   OR (status = 'deleted' AND erase_after IS NOT NULL)
                ORDER BY erase_after, id
                """
            ).fetchall()
        return [row[0] for row in rows]

    def waiting(self) -> int:
        """Accounts inside their 30 days (for the operator's check that the purge runs)."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT count(*) FROM public.users WHERE status = 'deleting'"
            ).fetchone()
        return int(row[0])

    def begin(self, owner_id: uuid.UUID) -> bool:
        """Step 1. True when this account is now being erased (just now or by an earlier run)."""
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE public.users
                SET status = 'deleted', deleted_at = now(), updated_at = now()
                WHERE id = %s AND status = 'deleting' AND erase_after <= now()
                """,
                (owner_id,),
            )
            erasing = conn.execute(
                "SELECT 1 FROM public.users "
                "WHERE id = %s AND status = 'deleted' AND erase_after IS NOT NULL",
                (owner_id,),
            ).fetchone()
            if erasing is None:
                return False
            conn.execute("DELETE FROM public.sessions WHERE owner_id = %s", (owner_id,))
        return True

    def erase_rows(self, owner_id: uuid.UUID) -> None:
        """Step 3, after the files are gone."""
        with self._connect() as conn:
            erasing = conn.execute(
                "SELECT 1 FROM public.users "
                "WHERE id = %s AND status = 'deleted' AND erase_after IS NOT NULL FOR UPDATE",
                (owner_id,),
            ).fetchone()
            if erasing is None:
                return
            tables = [table for table in owner_tables(conn) if table != "audit_events"]
            for table in deletion_order(conn, tables):
                conn.execute(
                    sql.SQL("DELETE FROM {} WHERE owner_id = %s").format(
                        sql.Identifier("public", table)
                    ),
                    (owner_id,),
                )
            # The minimum trail that this account was deleted: when it was asked for, kept (if
            # ever) and erased. No content, and nothing about the reader beyond the bare id.
            conn.execute(
                "DELETE FROM public.audit_events WHERE owner_id = %s AND action NOT LIKE 'account.%%'",
                (owner_id,),
            )
            conn.execute(
                """
                UPDATE public.users
                SET display_name = '', email = NULL, erase_after = NULL, updated_at = now()
                WHERE id = %s
                """,
                (owner_id,),
            )
            record_event(
                conn,
                owner_id=owner_id,
                actor_kind="system",
                action="account.purge",
                target_kind="account",
                target_id=None,
                result="succeeded",
            )

    # ---- editions nobody holds (ADR 0044 section 6) ---------------------------------------------

    def mark_editions(self) -> None:
        """Step 1 of the editions: a held edition loses its mark; an unheld one without a mark
        gets one, now. A mark already there is kept."""
        with self._connect() as conn:
            conn.execute(f"""UPDATE public.books AS edition SET no_holder_since=NULL
                             WHERE {_LIVE_EDITION} AND edition.no_holder_since IS NOT NULL
                               AND {_HELD}""")
            conn.execute(f"""UPDATE public.books AS edition SET no_holder_since=now()
                             WHERE {_LIVE_EDITION} AND edition.no_holder_since IS NULL
                               AND NOT {_HELD}""")

    def unheld_editions(self) -> int:
        """Editions inside their 30 days (for the operator's check that the purge runs)."""
        with self._connect() as conn:
            return int(conn.execute(f"""SELECT count(*) FROM public.books AS edition
                                        WHERE {_LIVE_EDITION}
                                          AND edition.no_holder_since IS NOT NULL"""
                                    ).fetchone()[0])

    def due_editions(self) -> list[tuple[uuid.UUID, uuid.UUID]]:
        """(owner, book) of every edition whose mark is 30 days old."""
        with self._connect() as conn:
            return [(row[0], row[1]) for row in conn.execute(
                f"""SELECT edition.owner_id,edition.id FROM public.books AS edition
                    WHERE {_LIVE_EDITION} AND edition.no_holder_since<=now()-{_UNHELD_FOR}
                    ORDER BY edition.no_holder_since,edition.id""").fetchall()]

    def edition_files(self, owner_id: uuid.UUID, book_id: uuid.UUID) -> list[tuple]:
        """(record id, storage provider, storage key) of every stored file of one edition: only
        an edition the removed operator's command made has one."""
        with self._connect() as conn:
            return [tuple(row) for row in conn.execute(
                "SELECT id,storage_provider,storage_key FROM public.source_objects"
                " WHERE owner_id=%s AND book_id=%s ORDER BY id", (owner_id, book_id)).fetchall()]

    def forget_edition_file(self, owner_id: uuid.UUID, record_id: uuid.UUID) -> None:
        """The record of a stored file, once the file itself is gone."""
        with self._connect() as conn:
            conn.execute("DELETE FROM public.source_objects WHERE owner_id=%s AND id=%s",
                         (owner_id, record_id))

    def erase_edition(self, owner_id: uuid.UUID, book_id: uuid.UUID) -> bool:
        """Erase one edition, in one transaction under its lock, deciding again once the lock is
        held: a holder back clears the mark and keeps everything (False); a mark not 30 days old,
        or an edition already gone, keeps everything (False). Otherwise its row becomes a bare
        id, as a removed book's does, and its chapters go, every story memory row under them
        with them (migration 0024's cascade), with anything else hung from the book (True)."""
        with self._connect() as conn:
            conn.execute(_EDITION_LOCK, (owner_id, book_id))
            back = conn.execute(
                f"""UPDATE public.books AS edition SET no_holder_since=NULL
                    WHERE edition.owner_id=%s AND edition.id=%s AND {_LIVE_EDITION} AND {_HELD}
                    RETURNING 1""", (owner_id, book_id)).fetchone()
            if back is not None:
                return False
            due = conn.execute(
                f"""SELECT 1 FROM public.books AS edition
                    WHERE edition.owner_id=%s AND edition.id=%s AND {_LIVE_EDITION}
                      AND edition.no_holder_since<=now()-{_UNHELD_FOR}""",
                (owner_id, book_id)).fetchone()
            if due is None:
                return False
            conn.execute(
                """UPDATE public.books
                   SET deleted_at=now(),updated_at=now(),title='',author=NULL,source_kind=NULL,
                       source_id=NULL,file_hash=NULL,split_hash=NULL,edition_of=NULL,
                       no_holder_since=NULL
                   WHERE owner_id=%s AND id=%s""", (owner_id, book_id))
            # A stored file's record is the files step's, after the file itself.
            for table in ("chapters", *(name for name in ERASED_WITH_THE_BOOK
                                        if name != "source_objects")):
                conn.execute(
                    sql.SQL("DELETE FROM {} WHERE owner_id=%s AND book_id=%s").format(
                        sql.Identifier("public", table)), (owner_id, book_id))
        return True


@dataclass(frozen=True, slots=True)
class PurgeResult:
    erased: int
    unfinished: int


def purge_account(repository: PostgresAccountPurge, storage, owner_id: uuid.UUID) -> bool:
    """Erase one account, or finish erasing it. False when it is not due."""
    if not repository.begin(owner_id):
        return False
    storage.delete_owner(OwnerId(owner_id))
    repository.erase_rows(owner_id)
    return True


def purge_due_accounts(repository: PostgresAccountPurge, storage) -> PurgeResult:
    """Every account that is due, one at a time; one that cannot be finished now (storage away,
    a lost connection) waits for the next run and never holds up the others."""
    erased = unfinished = 0
    for owner_id in repository.due():
        try:
            if purge_account(repository, storage, owner_id):
                erased += 1
        except Exception:  # noqa: BLE001 - counted; the next run picks the account up again
            unfinished += 1
    return PurgeResult(erased=erased, unfinished=unfinished)


@dataclass(frozen=True, slots=True)
class EditionPurgeResult:
    erased: int
    unfinished: int
    waiting: int


def purge_unheld_editions(repository: PostgresAccountPurge, storage) -> EditionPurgeResult:
    """Mark the editions nobody holds and erase those nobody has held for 30 days, one at a
    time; one that cannot be finished now (storage away, a lost connection) waits for the next
    run. A stored file goes first, object then record, so a crash never leaves a file that
    nothing names; an edition kept at the last moment (a holder back) is then an edition without
    a file, as every edition a reader's run made is."""
    repository.mark_editions()
    erased = unfinished = 0
    for owner_id, book_id in repository.due_editions():
        try:
            for record_id, provider, key in repository.edition_files(owner_id, book_id):
                if provider != storage.provider:
                    raise PurgeConfigurationError("an edition's file is in another store")
                storage.delete(SourceObjectRef(OwnerId(owner_id), uuid.UUID(hex=key)))
                repository.forget_edition_file(owner_id, record_id)
            if repository.erase_edition(owner_id, book_id):
                erased += 1
        except Exception:  # noqa: BLE001 - counted; the next run picks the edition up again
            unfinished += 1
    return EditionPurgeResult(erased=erased, unfinished=unfinished,
                              waiting=repository.unheld_editions())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Erase the accounts whose 30 days after asking to be deleted have passed, "
                    "and the notes of book files nobody has held for 30 days")
    parser.add_argument("--dsn-env", default="HOSTED_PURGE_DSN")
    args = parser.parse_args(argv)
    dsn = os.environ.get(args.dsn_env)
    if not dsn:
        parser.error(f"environment variable {args.dsn_env!r} is not set")
    from app.config import Settings
    from app.hosted.storage import build_object_storage

    repository = PostgresAccountPurge(dsn)
    repository.check_runtime_role()
    storage = build_object_storage(Settings())
    try:
        result = purge_due_accounts(repository, storage)
        waiting = repository.waiting()
        editions = purge_unheld_editions(repository, storage)
    finally:
        storage.close()
    print(f"erased {result.erased}, unfinished {result.unfinished}, waiting {waiting}; "
          f"editions erased {editions.erased}, unfinished {editions.unfinished}, "
          f"waiting {editions.waiting}")
    return 1 if result.unfinished or editions.unfinished else 0


if __name__ == "__main__":
    sys.exit(main())
