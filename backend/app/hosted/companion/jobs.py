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
``recollect`` (part 7) is ``Recollector.catch_up`` around the unchanged ``pending`` and ``distil``,
which read only the reader's own messages and keep only notes Jev finds the reader said; its
queue, ``RecollectQueue``, is the ``recollector`` the chats are given. ``story_memory`` (ADR 0042
part 3) is ``remember_chapter`` for the next chapter the reader has finished, one chapter per run;
its queue, ``StoryMemoryQueue``, is asked where the notes queue is. Nothing here decides what a
reader may be shown.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from datetime import timedelta

import psycopg

from app.catalog.catalog import HOME_SPEND
from app.chapter_notes import NotesBuilder, build_missing
from app.cost.limits import CostCeilingExceeded
from app.home_conversation import HOME
from app.hosted.companion import CompanionDatabaseBusy, OwnerBoundAdapter, as_uuid
from app.hosted.companion.memory import HostedBookStore
from app.hosted.companion.source import HostedBookSource
from app.hosted.companion.store import HostedCompanionCatalog
from app.hosted.companion.store_recollections import RecollectionCursorMoved
from app.hosted.companion.story_memory import SelfRelation, WorkingCopyMismatch, remember_chapter
from app.hosted.jobs import (
    CHAPTER_FAILURES,
    PAUSABLE_PROVIDERS,
    ChapterAlreadyPublished,
    ClaimedJob,
    LostLeaseError,
    PostgresWorkerRepository,
    SanitizedFailure,
    WorkerConfigurationError,
)
from app.hosted.tenant.models import OwnerId
from app.hosted.worker import HandledJobFailure
from app.ingest.extraction.validation import ExtractionGroundingRejected, ExtractionSafetyError
from app.llm.client import ProviderUnavailable
from app.llm.jev import JevUnavailable
from app.llm.versioning import ModelMigrationRequired
from app.read_map import read_snapshot
from app.recollections import BATCH_AFTER, RUN_MOST, STORE_CAP, distil, pending

# Companion jobs are short next to extraction, so they are claimed first (ADR 0041 section 5).
COMPANION_PRIORITY = 10
# A chapter of story memory takes minutes: below notes and recollections, above the old
# reader-key extraction (ADR 0042 section 6).
STORY_MEMORY_PRIORITY = 5
# The provider the checks are paid at: a refusal of the platform's account there pauses every
# reader's companion jobs for the local pause's five minutes.
CHECKER_PROVIDER = "typesafe"


class ChapterNotesQueue(OwnerBoundAdapter):
    """The ``notes_builder`` for one hosted reader: ``ensure`` asks for their notes to be written."""

    __slots__ = ()
    KIND, KEY, PRIORITY = "chapter_notes", "chapter-notes", COMPANION_PRIORITY

    def ensure(self, book_id) -> bool:
        """Insert a waiting job of this queue's kind for this reader's live book; True if one was
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
                       SELECT book.owner_id,book.id,book.incarnation,%s,%s,%s
                       FROM public.books AS book
                       WHERE book.owner_id=%s AND book.id=%s AND book.deleted_at IS NULL
                       ON CONFLICT DO NOTHING
                       RETURNING id""",
                    (self.KIND, f"{self.KEY}:{uuid.uuid4().hex}", self.PRIORITY,
                     self._owner.value, book)).fetchone()
        except (psycopg.Error, CompanionDatabaseBusy):
            return False
        return row is not None


class StoryMemoryQueue(ChapterNotesQueue):
    """One hosted reader's story-memory queue (ADR 0042 part 3): ``ensure`` asks for the book's
    memory to be carried on to the chapters they have finished, one chapter per run."""

    __slots__ = ()
    KIND, KEY, PRIORITY = "story_memory", "story-memory", STORY_MEMORY_PRIORITY


def _conversation(catalog, book_id):
    """(incarnation, title, the book its spend is recorded against) of one conversation, or None:
    ``Recollector._conversation`` (``app/recollections.py``), over the hosted catalog."""
    if book_id is None:
        books = catalog.list_books()
        # Spend is recorded against the first book on the shelf, as the reply's is; with an empty
        # shelf, in the home conversation's own ledger (book NULL here).
        return (HOME, None, books[0]["book_id"] if books else HOME_SPEND)
    book = catalog.get_book(book_id)
    return (book["incarnation"], book["title"], book_id) if book else None


class RecollectQueue(OwnerBoundAdapter):
    """The ``recollector`` for one hosted reader: ``ensure`` asks for one conversation (a book's,
    or ``None``: the home conversation) to be distilled into notes."""

    __slots__ = ()

    def ensure(self, book_id) -> bool:
        """Insert a waiting ``recollect`` job for this conversation once enough of the reader's
        messages wait to be looked at (``BATCH_AFTER``, counted by the unchanged ``pending``, as
        ``Recollector.catch_up`` counts them); True if one was added. Fewer messages, a job
        already waiting, a book that is not this reader's, or a database that cannot be reached
        add nothing: the next reply asks again, and the reply never depends on it."""
        try:
            book = None if book_id is None else as_uuid(book_id)
        except LookupError:
            return False
        try:
            catalog = HostedCompanionCatalog(self._dsn, self._owner)
            conversation = _conversation(catalog, None if book is None else str(book))
            if conversation is None:
                return False
            _batch, waiting = pending(catalog, None if book is None else str(book),
                                      conversation[0])
            if waiting < BATCH_AFTER:
                return False
            with self._transaction() as conn:
                if book is None:
                    row = conn.execute(
                        """INSERT INTO public.jobs (owner_id,kind,idempotency_key,priority)
                           VALUES (%s,'recollect',%s,%s)
                           ON CONFLICT DO NOTHING
                           RETURNING id""",
                        (self._owner.value, f"recollect:{uuid.uuid4().hex}",
                         COMPANION_PRIORITY)).fetchone()
                else:
                    row = conn.execute(
                        """INSERT INTO public.jobs
                             (owner_id,book_id,book_incarnation,kind,idempotency_key,priority)
                           SELECT book.owner_id,book.id,book.incarnation,'recollect',%s,%s
                           FROM public.books AS book
                           WHERE book.owner_id=%s AND book.id=%s AND book.deleted_at IS NULL
                           ON CONFLICT DO NOTHING
                           RETURNING id""",
                        (f"recollect:{uuid.uuid4().hex}", COMPANION_PRIORITY,
                         self._owner.value, book)).fetchone()
        except (psycopg.Error, CompanionDatabaseBusy, LookupError, ValueError):
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
        self.lost = self.cancelled = False

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
                self.lost = self.cancelled = True
            raise LostLeaseError("job was cancelled")

    def stopped(self) -> bool:
        """After a failure: True when the job was cancelled (the worker then records that), a
        ``LostLeaseError`` when the lease went to another worker, False otherwise."""
        if self.cancelled:
            return True
        if self.lost:
            raise LostLeaseError("job lease was lost")
        return False


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

    def keep_recollections(self, changes, **batch):
        self._lease.hold()
        return super().keep_recollections(changes, **batch)


KINDS = frozenset({"chapter_notes", "recollect", "story_memory"})
# Two runs that failed on a chapter for a reason the chapter causes make the next run keep it as
# an unremembered chapter (ADR 0042 section 2).
UNREMEMBERED_AFTER = 2


def companion_kinds(settings) -> frozenset[str]:
    """The kinds this process's settings turn on: notes and recollections as the chat's settings
    say, story memory only with ``HOSTED_STORY_MEMORY``."""
    return frozenset(kind for kind, on in (("chapter_notes", settings.reader_story_notes),
                                           ("recollect", settings.reader_recollections),
                                           ("story_memory", settings.hosted_story_memory))
                     if on)


def _provider_refusal(exc: BaseException | None) -> ProviderUnavailable | None:
    """The writer's ``ProviderUnavailable`` behind a failure: raised by an extraction call, or
    the cause of the review's ``ExtractionSafetyError`` when a review call failed."""
    while exc is not None:
        if isinstance(exc, ProviderUnavailable):
            return exc
        exc = exc.__cause__
    return None


def story_memory_failure(exc: Exception) -> SanitizedFailure | None:
    """What one failed story-memory run becomes (ADR 0042 sections 2 and 6), or None to leave it
    to the dispatcher's common handling (a lost lease or cancellation, a cap, Jev, and anything
    else, which is retried within the job's attempts).

    The chapter cannot be done, so the job does not retry it and two such runs make it an
    unremembered chapter: the review rejected it after its repair (``grounding_rejected``); the
    extraction or the review's verdict was malformed past its repair, the review could not cover
    it, a call would pass a per-call ceiling, or the chapter ties a person to themselves
    (``invalid_model_output``). The writer's account refused or unreachable is retried and never
    counted against the chapter. Memory that no longer matches the book, or a book begun with
    another extraction model, stops the book's memory (not retried; nothing was spent)."""
    if isinstance(exc, ExtractionGroundingRejected):
        return SanitizedFailure("grounding_rejected", retryable=False)
    refusal = _provider_refusal(exc)
    if refusal is not None:
        return SanitizedFailure("provider_rejected" if refusal.kind == "authentication"
                                else "provider_unavailable", retryable=True)
    malformed = (type(exc) is ValueError and str(exc).startswith("malformed extraction")
                 and isinstance(exc.__cause__, (TypeError, ValueError)))
    unreviewable = isinstance(exc, ExtractionSafetyError) and (
        exc.__cause__ is None or isinstance(exc.__cause__, (TypeError, ValueError)))
    ceiling = isinstance(exc, CostCeilingExceeded) and exc.__cause__ is None
    if malformed or unreviewable or ceiling or isinstance(exc, SelfRelation):
        return SanitizedFailure("invalid_model_output", retryable=False)
    if isinstance(exc, WorkingCopyMismatch):
        return SanitizedFailure("source_integrity", retryable=False)
    if isinstance(exc, ModelMigrationRequired):
        return SanitizedFailure("internal_error", retryable=False)
    return None


class CompanionJobs:
    """The worker's handler for the companion's kinds: ``handler(claim, repository)``."""

    def __init__(self, *, tenant_dsn: str, settings, client, storage, sources,
                 kinds: frozenset[str] = frozenset({"chapter_notes", "recollect"}),
                 extraction_client=None,
                 lease_for: timedelta = timedelta(minutes=2)) -> None:
        unknown = set(kinds) - KINDS
        if unknown or not kinds:
            raise WorkerConfigurationError(
                "the companion dispatcher runs chapter notes, recollections and story memory only")
        if "story_memory" in kinds and extraction_client is None:
            raise WorkerConfigurationError("story memory needs the platform's extraction client")
        self._dsn, self._settings, self._client = tenant_dsn, settings, client
        self._extraction_client = extraction_client
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
        from app.llm.factory import build_extraction_client, build_platform_clients

        settings = Settings()
        if settings.deployment_mode != "hosted":
            raise WorkerConfigurationError("the companion dispatcher runs in hosted mode only")
        kinds = companion_kinds(settings)
        if not (kinds and settings.reader_conversation_jev):
            raise WorkerConfigurationError(
                "the companion dispatcher needs READER_CONVERSATION_JEV and READER_STORY_NOTES "
                "or READER_RECOLLECTIONS")
        tenant_dsn = settings.hosted_tenant_dsn.get_secret_value()
        asyncio.run(PostgresTenantRepository(tenant_dsn).check_runtime_role())
        set_connection_limit(settings.hosted_companion_db_connections)
        set_spend_limits(reader_usd_per_day=settings.hosted_reader_usd_per_day,
                         reader_usd_per_month=settings.hosted_reader_usd_per_month,
                         platform_usd_per_day=settings.hosted_platform_usd_per_day)
        return cls(tenant_dsn=tenant_dsn, settings=settings,
                   client=build_platform_clients(settings),
                   storage=build_object_storage(settings),
                   sources=SourceParseCache(settings.segmentation_cache_max_entries),
                   kinds=kinds,
                   extraction_client=(build_extraction_client(settings)
                                      if "story_memory" in kinds else None))

    def __call__(self, claim: ClaimedJob, repository: PostgresWorkerRepository) -> None:
        if claim.kind not in self.kinds or (claim.kind in ("chapter_notes", "story_memory")
                                            and claim.book_id is None):
            raise HandledJobFailure(SanitizedFailure("internal_error", retryable=False))
        lease = _Lease(repository, claim, self._lease_for)
        try:
            if claim.kind == "recollect":
                self._recollect(claim, lease)
            elif claim.kind == "story_memory":
                self._story_memory(claim, lease, repository)
            else:
                self._chapter_notes(claim, lease)
        except RecollectionCursorMoved:
            if lease.stopped():
                return
            # Another run stored this batch, or the reader cleared the conversation or forgot
            # everything meanwhile: nothing is left for this run to do.
        except LostLeaseError:
            if lease.stopped():
                return          # cancelled (its book was removed): the worker records it
            raise
        except JevUnavailable as exc:
            if lease.stopped():
                return
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
            if lease.stopped():
                return
            # A cap: the next claim moves a capped reader's jobs to the next UTC day.
            raise HandledJobFailure(SanitizedFailure("budget_exceeded", retryable=True)) from None
        except Exception:
            if lease.stopped():
                return
            raise
        lease.stopped()

    def _chapter_notes(self, claim: ClaimedJob, lease: _Lease) -> None:
        owner = OwnerId(claim.owner_id)
        catalog = _LeasedCatalog(self._dsn, owner, lease)
        book_id = str(claim.book_id)
        book = catalog.get_book(book_id)
        if book is None or book["incarnation"] != str(claim.book_incarnation):
            return              # deleted, or uploaded again since: nothing of this job is left
        build_missing(book_id, catalog, HostedBookStore(self._dsn, owner), self._settings,
                      self._client, HostedBookSource(self._storage, catalog, self._sources))

    def _story_memory(self, claim: ClaimedJob, lease: _Lease,
                      repository: PostgresWorkerRepository) -> None:
        """One chapter of the claimed book's story memory (ADR 0042 sections 2 and 6): the next
        chapter without memory, only when the reader has finished it. The target is the read
        map's ``finished_through``, read now through the tenant role, never the chapter counter
        a device can push. After the chapter, the next run is asked for if the target lies
        further on; a lowered target ("I haven't read this") stops the work at the next run."""
        if not self._settings.hosted_reader_story_memory:
            return              # a reader's own book gets no memory of its own while this is off
        owner = OwnerId(claim.owner_id)
        catalog = HostedCompanionCatalog(self._dsn, owner)
        book_id = str(claim.book_id)

        def gone() -> bool:
            book = catalog.get_book(book_id)
            return book is None or book["incarnation"] != str(claim.book_incarnation)

        if gone():
            return              # deleted, or uploaded again since: nothing of this job is left
        try:
            source = HostedBookSource(self._storage, catalog, self._sources)
            _point, _read, state, *_rest = read_snapshot(book_id, catalog, self._settings, source)
            target, ordinal = state["finished_through"], state["ingest_progress"] + 1
            if ordinal > target:
                return          # nothing finished past what is built: nothing to do or spend
            failed = repository.begin_story_memory_chapter(claim, ordinal)
            remember_chapter(claim, repository, hold=lease.hold, tenant_dsn=self._dsn,
                             settings=self._settings, client=self._extraction_client,
                             storage=self._storage, sources=self._sources, ordinal=ordinal,
                             unremembered=failed >= UNREMEMBERED_AFTER)
        except ChapterAlreadyPublished:
            pass                # another run published it: carry on from there
        except LookupError:
            if gone():
                return
            raise
        except Exception as exc:
            failure = story_memory_failure(exc)
            if failure is None or lease.stopped():
                raise
            if failure.code == "provider_rejected" and (
                    self._extraction_client.provider in PAUSABLE_PROVIDERS):
                # The platform's writer account was refused: every reader's companion jobs wait.
                repository.pause_provider(
                    claim, provider=self._extraction_client.provider, reason="unauthorized",
                    pause_for=timedelta(seconds=NotesBuilder.PAUSE_SECONDS))
            if failure.code in CHAPTER_FAILURES:
                StoryMemoryQueue(self._dsn, owner).ensure(book_id)      # the chapter's next run
            raise HandledJobFailure(failure) from None
        if ordinal < target:
            StoryMemoryQueue(self._dsn, owner).ensure(book_id)

    def _recollect(self, claim: ClaimedJob, lease: _Lease) -> None:
        """``Recollector.catch_up`` (``app/recollections.py``) for one conversation, around the
        unchanged ``pending`` and ``distil``: at most ``RUN_MOST`` batches, each read after its
        cursor and stored only if that cursor has not moved since (a compare-and-set in
        ``keep_recollections``, in place of the local in-process guard)."""
        catalog = _LeasedCatalog(self._dsn, OwnerId(claim.owner_id), lease)
        book_id = None if claim.book_id is None else str(claim.book_id)
        for _ in range(RUN_MOST):
            conversation = _conversation(catalog, book_id)
            if conversation is None or (book_id is not None
                                        and conversation[0] != str(claim.book_incarnation)):
                return          # deleted, or uploaded again since: nothing of this job is left
            incarnation, title, spend_book = conversation
            # Read before the batch, so a cursor moved meanwhile can only refuse the store.
            seen = catalog.get_recollection_cursor(book_id, incarnation=incarnation)
            batch, waiting = pending(catalog, book_id, incarnation)
            if waiting < BATCH_AFTER:
                return
            held = catalog.get_recollections()
            changes = distil(batch, held, catalog=catalog, settings=self._settings,
                             client=self._client, spend_book=spend_book, book_id=book_id,
                             title=title)
            catalog.keep_recollections(changes, cap=STORE_CAP, expected=seen,
                                       cursor=(book_id, incarnation, batch[-1]["turn_id"]))
