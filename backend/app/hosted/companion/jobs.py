"""The companion's background work in hosted mode: durable jobs on the existing job table (ADR 0041
section 5, part 6).

Two halves. ``ChapterNotesQueue`` is the ``notes_builder`` the hosted routes and the streamed chat
are given: its ``ensure(book_id)`` inserts one waiting ``chapter_notes`` job for a live book of its
reader, through the RLS-enforced tenant role; asking again while one waits adds nothing. The
``CompanionJobs`` dispatcher is the worker's handler (``HOSTED_JOB_HANDLER=
app.hosted.companion.jobs:CompanionJobs``): after the BYPASSRLS worker role has claimed a job, all
of the owner's work is done through the tenant role, with adapters bound to the claimed owner, so
both barriers hold in the background as on the request path.

``chapter_notes`` runs the unchanged ``app.chapter_notes.build_missing``: it decides which chapters
are fully read, writes from those chapters alone and keeps only sentences that pass every check.
Nothing here decides what a reader may be shown. ``recollect`` is part 7's.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from datetime import timedelta

import psycopg

from app.chapter_notes import NotesBuilder, build_missing
from app.cost.limits import CostCeilingExceeded
from app.hosted.companion import CompanionDatabaseBusy, OwnerBoundAdapter, as_uuid
from app.hosted.companion.memory import HostedBookStore
from app.hosted.companion.source import HostedBookSource
from app.hosted.companion.store import HostedCompanionCatalog
from app.hosted.jobs import (
    ClaimedJob,
    LostLeaseError,
    PostgresWorkerRepository,
    SanitizedFailure,
    WorkerConfigurationError,
)
from app.hosted.tenant.models import OwnerId
from app.hosted.worker import HandledJobFailure
from app.llm.jev import JevUnavailable

# Companion jobs are short next to extraction, so they are claimed first (ADR 0041 section 5).
COMPANION_PRIORITY = 10
# The provider the checks are paid at: a refusal of the platform's account there pauses every
# reader's companion jobs for the local pause's five minutes.
CHECKER_PROVIDER = "typesafe"


class ChapterNotesQueue(OwnerBoundAdapter):
    """The ``notes_builder`` for one hosted reader: ``ensure`` asks for their notes to be written."""

    __slots__ = ()

    def ensure(self, book_id) -> bool:
        """Insert a waiting ``chapter_notes`` job for this reader's live book; True if one was
        added. A job already waiting for the book, a book that is not this reader's, or a
        database that cannot be reached adds nothing: as locally, the next page turn or reply
        asks again, and the caller's answer never depends on it."""
        try:
            book = as_uuid(book_id)
        except LookupError:
            return False
        try:
            with self._transaction() as conn:
                row = conn.execute(
                    """INSERT INTO public.jobs
                         (owner_id,book_id,book_incarnation,kind,idempotency_key,priority)
                       SELECT book.owner_id,book.id,book.incarnation,'chapter_notes',%s,%s
                       FROM public.books AS book
                       WHERE book.owner_id=%s AND book.id=%s AND book.deleted_at IS NULL
                       ON CONFLICT DO NOTHING
                       RETURNING id""",
                    (f"chapter-notes:{uuid.uuid4().hex}", COMPANION_PRIORITY,
                     self._owner.value, book)).fetchone()
        except (psycopg.Error, CompanionDatabaseBusy):
            return False
        return row is not None


class _Lease:
    """The claimed job's lease, renewed before every paid call and before notes are stored.

    Once lost (expired and taken by another worker, or the job cancelled because its book was
    deleted) it stays lost: nothing more is reserved or stored by this run.
    """

    def __init__(self, repository: PostgresWorkerRepository, claim: ClaimedJob,
                 lease_for: timedelta) -> None:
        self._repository, self._claim, self._lease_for = repository, claim, lease_for
        self._guard = threading.Lock()
        self.lost = False

    def hold(self) -> None:
        if self.lost:
            raise LostLeaseError("job lease was lost")
        try:
            cancelled = self._repository.heartbeat(self._claim, lease_for=self._lease_for)
        except LostLeaseError:
            with self._guard:
                self.lost = True
            raise
        if cancelled:
            with self._guard:
                self.lost = True
            raise LostLeaseError("job was cancelled")


class _LeasedCatalog(HostedCompanionCatalog):
    """The claimed owner's catalog, which holds the job's lease before it spends or stores."""

    __slots__ = ("_lease",)

    def __init__(self, dsn: str, owner_id: OwnerId, lease: _Lease) -> None:
        super().__init__(dsn, owner_id)
        object.__setattr__(self, "_lease", lease)

    def reserve_cost(self, book_id, **call):
        self._lease.hold()
        return super().reserve_cost(book_id, **call)

    def set_chapter_notes(self, book_id, atom, **notes):
        self._lease.hold()
        return super().set_chapter_notes(book_id, atom, **notes)


class CompanionJobs:
    """The worker's handler for the companion's kinds: ``handler(claim, repository)``."""

    def __init__(self, *, tenant_dsn: str, settings, client, storage, sources,
                 kinds: frozenset[str] = frozenset({"chapter_notes"}),
                 lease_for: timedelta = timedelta(minutes=2)) -> None:
        unknown = set(kinds) - {"chapter_notes"}
        if unknown or not kinds:
            raise WorkerConfigurationError("the companion dispatcher runs chapter notes only")
        self._dsn, self._settings, self._client = tenant_dsn, settings, client
        self._storage, self._sources = storage, sources
        self.kinds = frozenset(kinds)
        self._lease_for = lease_for

    @classmethod
    def from_environment(cls) -> CompanionJobs:
        """Build the dispatcher from this process's settings, as the web process builds the
        companion: the tenant DSN (its role checked now), the platform's clients, object storage
        and the caps. A missing or refused setting stops the worker before any claim."""
        from app.config import Settings
        from app.hosted.companion import set_connection_limit
        from app.hosted.companion.source import SourceParseCache
        from app.hosted.companion.spend import set_spend_limits
        from app.hosted.storage import build_object_storage
        from app.hosted.tenant.repository import PostgresTenantRepository
        from app.llm.factory import build_platform_clients

        settings = Settings()
        if settings.deployment_mode != "hosted":
            raise WorkerConfigurationError("the companion dispatcher runs in hosted mode only")
        if not (settings.reader_story_notes and settings.reader_conversation_jev):
            raise WorkerConfigurationError(
                "the companion dispatcher needs READER_STORY_NOTES and READER_CONVERSATION_JEV")
        tenant_dsn = settings.hosted_tenant_dsn.get_secret_value()
        asyncio.run(PostgresTenantRepository(tenant_dsn).check_runtime_role())
        set_connection_limit(settings.hosted_companion_db_connections)
        set_spend_limits(reader_usd_per_day=settings.hosted_reader_usd_per_day,
                         reader_usd_per_month=settings.hosted_reader_usd_per_month,
                         platform_usd_per_day=settings.hosted_platform_usd_per_day)
        return cls(tenant_dsn=tenant_dsn, settings=settings,
                   client=build_platform_clients(settings),
                   storage=build_object_storage(settings),
                   sources=SourceParseCache(settings.segmentation_cache_max_entries))

    def __call__(self, claim: ClaimedJob, repository: PostgresWorkerRepository) -> None:
        if claim.kind not in self.kinds or claim.book_id is None:
            raise HandledJobFailure(SanitizedFailure("internal_error", retryable=False))
        lease = _Lease(repository, claim, self._lease_for)
        try:
            self._chapter_notes(claim, lease)
        except LostLeaseError:
            raise
        except JevUnavailable as exc:
            if lease.lost:
                raise LostLeaseError("job lease was lost") from None
            if exc.reason in NotesBuilder.PAUSE_FOR:
                # The platform's checker account was refused: every reader's notes wait.
                repository.pause_provider(
                    claim, provider=CHECKER_PROVIDER, reason=exc.reason,
                    pause_for=timedelta(seconds=NotesBuilder.PAUSE_SECONDS))
                raise HandledJobFailure(
                    SanitizedFailure("provider_rejected", retryable=True)) from None
            raise HandledJobFailure(
                SanitizedFailure("provider_unavailable", retryable=True)) from None
        except CostCeilingExceeded:
            if lease.lost:
                raise LostLeaseError("job lease was lost") from None
            # A cap: the next claim moves a capped reader's jobs to the next UTC day.
            raise HandledJobFailure(SanitizedFailure("budget_exceeded", retryable=True)) from None
        except Exception:
            if lease.lost:
                raise LostLeaseError("job lease was lost") from None
            raise
        if lease.lost:
            raise LostLeaseError("job lease was lost")

    def _chapter_notes(self, claim: ClaimedJob, lease: _Lease) -> None:
        owner = OwnerId(claim.owner_id)
        catalog = _LeasedCatalog(self._dsn, owner, lease)
        book_id = str(claim.book_id)
        book = catalog.get_book(book_id)
        if book is None or book["incarnation"] != str(claim.book_incarnation):
            return              # deleted, or uploaded again since: nothing of this job is left
        build_missing(book_id, catalog, HostedBookStore(self._dsn, owner), self._settings,
                      self._client, HostedBookSource(self._storage, catalog, self._sources))
