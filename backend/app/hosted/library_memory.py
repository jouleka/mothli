"""Library editions: story memory of free-library books, built once by the operator (ADR 0042 part
5). An operator command, never part of the web process or the worker.

    python -m app.hosted.library_memory build 1342 11    # build these catalogue entries
    python -m app.hosted.library_memory report           # how every edition stands

It needs HOSTED_LIBRARY_DSN (its own role, below), HOSTED_TENANT_DSN and the storage settings of
app.env, and the free library's network (it fetches each book as a reader's add does).

``build`` makes the **library account** once: one reserved user that never signs in (no sign-in
identity, and suspended, which every session check refuses), recorded in ``library_account``. For
each catalogue entry it then adds the book to that account by the very path a reader's add takes
(``app.hosted.tenant.classics.add_entry``: the guarded fetch, the upload's validation, encryption,
storage and limits) and asks for its story memory with the reader's own queue
(``StoryMemoryQueue``). The worker then builds it chapter by chapter with the same job, extraction
and publish as a reader's, to the book's last chapter, within the library account's own caps
(set with ``python -m app.hosted.limits set <account>``) and the platform's day. No reader's
allowance is touched. Readers whose copy is that exact file then get it through the door, up to
their own place.

Safe to run again: a title on the account's shelf is not added again, a built one is not rebuilt,
one half built carries on, and one stopped (three chapters in a row that could not be remembered)
stays stopped and is reported. Nothing is logged; the report names titles, numbers and codes only.

Its database role reaches two tables: it adds the account's user row and reads and adds the one
``library_account`` row, with row-level security on (it sets the new account as the owner for its
own insert). Everything else goes through the tenant role, bound to the library account.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from types import SimpleNamespace

import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

from app.classics import find_classic, source_tag
from app.hosted.companion.jobs import StoryMemoryQueue
from app.hosted.jobs import UNREMEMBERED_IN_A_ROW
from app.hosted.tenant.grants import ALL_TABLE_PRIVILEGES, LIBRARY_TABLE_PRIVILEGES
from app.hosted.tenant.models import OwnerId

ACCOUNT_NAME = "Mothli library"


class LibraryConfigurationError(RuntimeError):
    """A refusal whose message names a check, never a value."""


@dataclass(slots=True)
class PostgresLibraryAccount:
    """The library role: the library account's user row and its ``library_account`` row."""

    _dsn: str = field(repr=False)

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self._dsn, row_factory=dict_row)

    def check_runtime_role(self) -> None:
        """A non-superuser that does not bypass row security, inherits nothing, and holds exactly
        its privileges (``grants.py``) and nothing on any other table."""
        with self._connect() as conn:
            role = conn.execute(
                """
                SELECT rolsuper, rolbypassrls, rolcreatedb, rolcreaterole, rolreplication,
                       rolinherit,
                       EXISTS (SELECT 1 FROM pg_auth_members WHERE member = role_row.oid)
                         AS member
                FROM pg_roles AS role_row WHERE rolname = current_user
                """
            ).fetchone()
            if role is None or any(role.values()):
                raise LibraryConfigurationError(
                    "HOSTED_LIBRARY_DSN must use an isolated non-superuser role that does not "
                    "bypass row security")
            held = conn.execute(
                """
                SELECT table_class.relname AS name,
                       ARRAY(SELECT privilege FROM unnest(%s::text[]) AS privilege
                             WHERE has_table_privilege(current_user, table_class.oid, privilege))
                         AS privileges
                FROM pg_class AS table_class
                JOIN pg_namespace AS namespace ON namespace.oid = table_class.relnamespace
                WHERE namespace.nspname = 'public' AND table_class.relkind IN ('r', 'p')
                """,
                (sorted(ALL_TABLE_PRIVILEGES),),
            ).fetchall()
        actual = {row["name"]: frozenset(row["privileges"]) for row in held if row["privileges"]}
        if actual != LIBRARY_TABLE_PRIVILEGES:
            raise LibraryConfigurationError(
                "library role privileges are not exactly its reviewed list")

    def account(self) -> uuid.UUID | None:
        with self._connect() as conn:
            row = conn.execute("SELECT account_id FROM public.library_account").fetchone()
        return None if row is None else row["account_id"]

    def ensure_account(self) -> uuid.UUID:
        """The library account, made now if there is none (one transaction; at most one ever)."""
        with self._connect() as conn:
            # Two builds at once make one account: the second waits here, then finds it.
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('library_account', 49))")
            row = conn.execute("SELECT account_id FROM public.library_account").fetchone()
            if row is not None:
                return row["account_id"]
            account = uuid.uuid4()
            conn.execute("SELECT set_config('app.owner_id', %s, true)", (str(account),))
            conn.execute(
                "INSERT INTO public.users (id, display_name, status) VALUES (%s, %s, 'suspended')",
                (account, ACCOUNT_NAME))
            conn.execute("INSERT INTO public.library_account (account_id) VALUES (%s)",
                         (account,))
        return account


@dataclass(frozen=True, slots=True)
class Edition:
    """How one library edition stands. Titles, numbers and codes only."""

    entry: str
    title: str | None
    chapters: int = 0
    built_through: int = 0
    unremembered: int = 0
    cost_usd: Decimal = Decimal(0)
    state: str = "not added"
    note: str | None = None

    def line(self) -> str:
        if self.title is None:
            return f"{self.entry}: {self.note}"
        text = (f"{self.entry} {self.title}: built through chapter {self.built_through} of "
                f"{self.chapters}, {self.unremembered} unremembered, ${self.cost_usd:.4f}, "
                f"{self.state}")
        return text + (f" ({self.note})" if self.note else "")


# How one edition stands, read through the tenant role as the library account.
_EDITION = """
SELECT book.id, book.incarnation, book.title,
  (SELECT count(*) FROM public.book_search_documents AS doc
   WHERE (doc.owner_id, doc.book_id, doc.book_incarnation)
       = (book.owner_id, book.id, book.incarnation)) AS chapters,
  ARRAY(SELECT chapter.revealed_at FROM public.chapters AS chapter
        JOIN public.ingested_chapters AS receipt
          ON (receipt.owner_id, receipt.book_id, receipt.book_incarnation, receipt.chapter_id)
           = (chapter.owner_id, chapter.book_id, chapter.book_incarnation, chapter.id)
        WHERE (chapter.owner_id, chapter.book_id, chapter.book_incarnation)
            = (book.owner_id, book.id, book.incarnation)
          AND chapter.retracted_at IS NULL AND receipt.retracted_at IS NULL
        ORDER BY chapter.revealed_at) AS built,
  (SELECT count(*) FROM public.chapters AS chapter
   WHERE (chapter.owner_id, chapter.book_id, chapter.book_incarnation)
       = (book.owner_id, book.id, book.incarnation)
     AND chapter.retracted_at IS NULL AND chapter.kind = 'unremembered') AS unremembered,
  (SELECT COALESCE(sum(entry.usd), 0) FROM public.cost_ledger AS entry
   WHERE entry.owner_id = book.owner_id AND entry.book_id = book.id) AS cost,
  ARRAY(SELECT job.state = 'pending' AND job.run_after
               >= (date_trunc('day', now() AT TIME ZONE 'UTC') + interval '1 day')
                  AT TIME ZONE 'UTC'
        FROM public.jobs AS job
        WHERE (job.owner_id, job.book_id, job.book_incarnation)
            = (book.owner_id, book.id, book.incarnation)
          AND job.kind = 'story_memory'
          AND job.state IN ('pending', 'leased', 'running')) AS active,
  ARRAY(SELECT DISTINCT job.sanitized_error->>'code' FROM public.jobs AS job
        WHERE (job.owner_id, job.book_id, job.book_incarnation)
            = (book.owner_id, book.id, book.incarnation)
          AND job.kind = 'story_memory' AND job.state = 'failed') AS failures
FROM public.books AS book
WHERE book.owner_id = %s AND book.source_kind = %s AND book.deleted_at IS NULL
ORDER BY book.created_at, book.id
LIMIT 1
"""

STOPPED = "stopped: three chapters in a row could not be remembered"


def _tenant(state, account: uuid.UUID):
    """One short transaction of the tenant role, bound to the library account."""
    conn = psycopg.connect(state.companion_dsn, row_factory=dict_row)
    conn.execute("SELECT set_config('app.owner_id', %s, false)", (str(account),))
    return conn


def edition(state, account: uuid.UUID, entry_id: str, entry: dict) -> Edition:
    with _tenant(state, account) as conn:
        row = conn.execute(_EDITION, (account, source_tag(entry))).fetchone()
    if row is None:
        return Edition(entry_id, entry["title"])
    through = 0
    for ordinal in row["built"]:
        if ordinal != through + 1:
            break
        through = ordinal
    with _tenant(state, account) as conn:
        kinds = {chapter["revealed_at"]: chapter["kind"] for chapter in conn.execute(
            """SELECT revealed_at,kind FROM public.chapters
               WHERE owner_id=%s AND book_id=%s AND book_incarnation=%s AND retracted_at IS NULL
                 AND revealed_at>%s AND revealed_at<=%s""",
            (account, row["id"], row["incarnation"], through - UNREMEMBERED_IN_A_ROW,
             through)).fetchall()}
    stopped = through >= UNREMEMBERED_IN_A_ROW and all(
        kinds.get(through - back) == "unremembered" for back in range(UNREMEMBERED_IN_A_ROW))
    note = None
    if row["chapters"] and through >= row["chapters"]:
        status = "built"
    elif stopped:
        status = STOPPED
    elif row["active"]:
        status = "waiting for tomorrow" if all(row["active"]) else "building"
    else:
        status = "not building"
        if row["failures"]:
            note = "failed runs: " + ", ".join(sorted(row["failures"]))
    return Edition(entry_id, row["title"], chapters=row["chapters"], built_through=through,
                   unremembered=row["unremembered"], cost_usd=Decimal(row["cost"]),
                   state=status, note=note)


def _entry(state, entry_id: str) -> dict | None:
    return find_classic(entry_id,
                        min_years_since_death=state.settings.classics_min_years_since_death)


async def _add(state, account: uuid.UUID, entry: dict) -> str | None:
    """Add one entry's book to the library account as a reader's add does. The refusal's own
    line for the reader, when it is refused; None when it is on the account's shelf."""
    try:
        from app.hosted.tenant.classics import add_entry

        await add_entry(SimpleNamespace(app=SimpleNamespace(state=state)), OwnerId(account),
                        entry)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        if detail.get("code") == "already_on_shelf":
            return None
        return str(detail.get("message") or "the book could not be added")
    return None


def build(state, library: PostgresLibraryAccount, entry_ids: list[str]) -> list[Edition]:
    """Add and queue each entry's edition (see the module's text); how each stands now."""
    account = library.ensure_account()
    results = []
    for entry_id in entry_ids:
        entry = _entry(state, entry_id)
        if entry is None:
            results.append(Edition(entry_id, None, note="not in the free library"))
            continue
        refused = asyncio.run(_add(state, account, entry))
        if refused is not None:
            results.append(Edition(entry_id, entry["title"], note=refused))
            continue
        now = edition(state, account, entry_id, entry)
        if now.state not in ("built", STOPPED):
            with _tenant(state, account) as conn:
                book = conn.execute(
                    "SELECT id FROM public.books WHERE owner_id=%s AND source_kind=%s"
                    " AND deleted_at IS NULL ORDER BY created_at,id LIMIT 1",
                    (account, source_tag(entry))).fetchone()
            StoryMemoryQueue(state.companion_dsn, OwnerId(account)).ensure(str(book["id"]))
            now = edition(state, account, entry_id, entry)
        results.append(now)
    return results


def report(state, library: PostgresLibraryAccount) -> list[Edition]:
    """How every edition on the library account's shelf stands."""
    account = library.account()
    if account is None:
        return []
    with _tenant(state, account) as conn:
        tags = [row["source_kind"] for row in conn.execute(
            "SELECT source_kind FROM public.books WHERE owner_id=%s AND deleted_at IS NULL"
            " AND source_kind LIKE 'gutenberg:%%' ORDER BY created_at,id", (account,)).fetchall()]
    results = []
    for tag in tags:
        entry_id = tag.split(":", 1)[1]
        entry = _entry(state, entry_id) or {"title": entry_id, "pg": int(entry_id)}
        results.append(edition(state, account, entry_id, entry))
    return results


def _state_from_environment(settings) -> SimpleNamespace:
    """What a reader's add reads from the web process's state, built for this one command."""
    from app.hosted.runtime import HostedRuntimeRegistry
    from app.hosted.storage import build_object_storage
    from app.hosted.tenant.repository import PostgresTenantRepository

    if settings.deployment_mode != "hosted" or settings.hosted_tenant_dsn is None:
        raise LibraryConfigurationError("the library build runs in hosted mode only")
    tenant_dsn = settings.hosted_tenant_dsn.get_secret_value()
    repository = PostgresTenantRepository(tenant_dsn)
    asyncio.run(repository.check_runtime_role())
    return SimpleNamespace(
        settings=settings, object_storage=build_object_storage(settings),
        tenant_repository=repository, companion_dsn=tenant_dsn,
        hosted_runtime=HostedRuntimeRegistry(
            cache_max_entries=settings.hosted_runtime_cache_max_entries,
            lock_max_entries=settings.hosted_runtime_lock_max_entries))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build and report library editions of free-library books (story memory "
                    "prepared once for everyone)")
    parser.add_argument("--dsn-env", default="HOSTED_LIBRARY_DSN")
    sub = parser.add_subparsers(dest="command", required=True)
    building = sub.add_parser("build", help="add and build these free-library entries")
    building.add_argument("entries", nargs="+", metavar="ENTRY_ID")
    sub.add_parser("report", help="how every library edition stands")
    args = parser.parse_args(argv)
    dsn = os.environ.get(args.dsn_env)
    if not dsn:
        parser.error(f"environment variable {args.dsn_env!r} is not set")
    from app.config import Settings

    library = PostgresLibraryAccount(dsn)
    library.check_runtime_role()
    settings = Settings()
    state = _state_from_environment(settings)
    try:
        if args.command == "build":
            if not settings.hosted_story_memory:
                print("HOSTED_STORY_MEMORY is off: editions are queued, and the worker builds "
                      "them once it is on")
            editions = build(state, library, args.entries)
        else:
            editions = report(state, library)
    finally:
        state.object_storage.close()
    account = library.account()
    if account is not None:
        print(f"library account {account}")
    for item in editions:
        print(item.line())
    return 0


if __name__ == "__main__":
    sys.exit(main())
