"""Durable PostgreSQL ingestion job leases and idempotent chapter commits (LIT-44)."""

from __future__ import annotations

import hashlib
import re
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.hosted.audit import record_event
from app.hosted.credentials import (
    CredentialCipher,
    CredentialUnavailableError,
    EncryptedCredential,
    ResolvedProviderCredential,
)
from app.hosted.limits import LimitExceededError
from app.hosted.tenant.grants import ALL_TABLE_PRIVILEGES, WORKER_TABLE_PRIVILEGES

# ADR 0041 section 5: the companion's background kinds. At most one of an owner, kind and book
# waits (a unique index) and at most one runs (the claim); they wait while the platform's provider
# account is paused and while their owner is at a companion cap.
COMPANION_KINDS = frozenset({"chapter_notes", "recollect"})
PAUSABLE_PROVIDERS = frozenset({"typesafe", "openai-compatible", "anthropic"})
PAUSE_REASONS = frozenset({"no_credits", "unauthorized"})

_WORKER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_FAILURE_CODES = frozenset(
    {
        "attempts_exhausted",
        "budget_exceeded",
        "cancelled",
        "internal_error",
        "invalid_model_output",
        "provider_rejected",
        "provider_unavailable",
        "source_integrity",
        "source_missing",
    }
)


class LostLeaseError(RuntimeError):
    pass


class ChapterAlreadyPublished(RuntimeError):
    """A chapter's story memory is already published: a second publish changes nothing."""


class StoryMemoryMoved(RuntimeError):
    """A book's story memory changed after its working copy was taken (a reader's correction):
    the chapter must be prepared again against the memory as it is now. Nothing was written."""


class JobIdempotencyError(RuntimeError):
    pass


class WorkerConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SanitizedFailure:
    code: str
    retryable: bool

    def __post_init__(self) -> None:
        if self.code not in _FAILURE_CODES:
            raise ValueError("failure code must use the reviewed content-free vocabulary")
        if not isinstance(self.retryable, bool):
            raise TypeError("retryable must be a boolean")

    def as_json(self) -> dict[str, str | bool]:
        return {"code": self.code, "retryable": self.retryable}


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    owner_id: uuid.UUID
    job_id: uuid.UUID
    book_id: uuid.UUID | None
    book_incarnation: uuid.UUID | None
    kind: str
    attempt_no: int
    worker_id: str
    lease_token: str = field(repr=False)
    credential_id: uuid.UUID | None = None


def _seconds(value: timedelta, *, allow_zero: bool = False) -> float:
    seconds = value.total_seconds()
    if seconds < 0 or (seconds == 0 and not allow_zero) or seconds > 24 * 60 * 60:
        raise ValueError("job duration must be within the allowed positive bound")
    return seconds


def _token_digest(value: str) -> bytes:
    return hashlib.sha256(value.encode("ascii")).digest()


@dataclass(slots=True)
class PostgresWorkerRepository:
    """Privileged cross-tenant scheduler boundary; every post-claim mutation carries owner + job."""

    _dsn: str = field(repr=False)
    _credential_cipher: CredentialCipher | None = field(default=None, repr=False)

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self._dsn, row_factory=dict_row)

    def check_runtime_role(self) -> None:
        required = {
            f"public.{table}": set(privileges)
            for table, privileges in WORKER_TABLE_PRIVILEGES.items()
        }
        all_privileges = set(ALL_TABLE_PRIVILEGES)
        with self._connect() as conn:
            role = conn.execute(
                """
                SELECT rolsuper,rolbypassrls,rolcreatedb,rolcreaterole,rolreplication,rolinherit,
                       EXISTS (SELECT 1 FROM pg_auth_members WHERE member=role_row.oid)
                         AS has_membership
                FROM pg_roles AS role_row WHERE rolname=current_user
                """
            ).fetchone()
            invalid = role is None or role["rolsuper"] or not role["rolbypassrls"] or any(
                role[name]
                for name in (
                    "rolcreatedb", "rolcreaterole", "rolreplication", "rolinherit", "has_membership"
                )
            )
            if invalid:
                raise WorkerConfigurationError(
                    "HOSTED_WORKER_DSN must use an isolated non-superuser BYPASSRLS role"
                )
            incorrect = []
            for table, expected in sorted(required.items()):
                for privilege in sorted(all_privileges):
                    held = bool(
                        conn.execute(
                            "SELECT has_table_privilege(current_user,%s,%s)",
                            (table, privilege),
                        ).fetchone()["has_table_privilege"]
                    )
                    if held != (privilege in expected):
                        incorrect.append(f"{table}:{privilege}")
            unexpected = conn.execute(
                """
                SELECT table_name FROM information_schema.tables
                WHERE table_schema='public' AND table_type='BASE TABLE'
                  AND table_name<>ALL(%s)
                  AND has_table_privilege(
                    current_user,quote_ident(table_schema)||'.'||quote_ident(table_name),
                    'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                ORDER BY table_name
                """,
                ([name.removeprefix("public.") for name in required],),
            ).fetchall()
        if incorrect or unexpected:
            raise WorkerConfigurationError(
                "worker runtime role privileges are not the reviewed repository allow-list"
            )

    @staticmethod
    def _validate_worker_id(worker_id: str) -> None:
        if not isinstance(worker_id, str) or _WORKER_ID_RE.fullmatch(worker_id) is None:
            raise ValueError("worker id must use the bounded content-free identifier format")

    def resolve_credential(
        self,
        claim: ClaimedJob,
    ) -> ResolvedProviderCredential:
        """Resolve only the active credential explicitly bound to this claimed job, just in time."""
        credential_id = claim.credential_id
        if credential_id is None or self._credential_cipher is None:
            raise CredentialUnavailableError("job credential is unavailable")
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT credential.owner_id,credential.id,credential.provider,
                       credential.masked_label,credential.ciphertext,
                       credential.encrypted_data_key,credential.encryption_algorithm,
                       credential.key_version,credential.nonce
                FROM public.jobs AS job
                JOIN public.job_attempts AS attempt
                  ON (attempt.owner_id,attempt.job_id)=(job.owner_id,job.id)
                JOIN public.provider_credentials AS credential
                  ON (credential.owner_id,credential.id)=(job.owner_id,job.credential_id)
                JOIN public.provider_model_settings AS setting
                  ON setting.owner_id=job.owner_id
                 AND setting.capability='extraction'
                 AND setting.credential_id=job.credential_id
                WHERE job.owner_id=%s AND job.id=%s AND job.credential_id=%s
                  AND job.state='running' AND attempt.attempt_no=%s
                  AND attempt.worker_id=%s AND attempt.lease_token_digest=%s
                  AND attempt.finished_at IS NULL AND attempt.lease_expires_at>now()
                  AND credential.disabled_at IS NULL AND credential.deleted_at IS NULL
                  AND setting.enabled AND setting.validation_status='ready'
                """,
                (
                    claim.owner_id,
                    claim.job_id,
                    credential_id,
                    claim.attempt_no,
                    claim.worker_id,
                    _token_digest(claim.lease_token),
                ),
            ).fetchone()
        if row is None:
            raise CredentialUnavailableError("job credential is unavailable")
        envelope = EncryptedCredential(
            owner_id=row["owner_id"],
            credential_id=row["id"],
            provider=row["provider"],
            masked_label=row["masked_label"],
            ciphertext=bytes(row["ciphertext"]),
            encrypted_data_key=bytes(row["encrypted_data_key"]),
            encryption_algorithm=row["encryption_algorithm"],
            key_version=row["key_version"],
            nonce=bytes(row["nonce"]),
        )
        return ResolvedProviderCredential(
            envelope.provider, self._credential_cipher.decrypt(envelope)
        )

    @staticmethod
    def _requeue(
        conn: psycopg.Connection,
        *,
        owner_id: uuid.UUID,
        job_id: uuid.UUID,
        kind: str,
        retry_seconds: float,
        failure: SanitizedFailure | None = None,
    ) -> None:
        """Put a job back to wait. A companion job whose owner, kind and book already has a job
        waiting is cancelled instead: the waiting one does the same work from the rows as they
        are then (ADR 0041 section 5), and only one may wait."""
        error = None if failure is None else psycopg.types.json.Jsonb(failure.as_json())
        pending = (
            """
            UPDATE public.jobs
            SET state='pending',run_after=now()+(%s*interval '1 second'),updated_at=now(),
                sanitized_error=COALESCE(%s::jsonb,sanitized_error)
            WHERE owner_id=%s AND id=%s
            """,
            (retry_seconds, error, owner_id, job_id),
        )
        if kind not in COMPANION_KINDS:
            conn.execute(*pending)
            return
        try:
            with conn.transaction():
                conn.execute(*pending)
            return
        except psycopg.errors.UniqueViolation:
            pass
        conn.execute(
            """
            UPDATE public.jobs
            SET state='cancelled',completed_at=now(),updated_at=now(),
                sanitized_error=COALESCE(%s::jsonb,sanitized_error)
            WHERE owner_id=%s AND id=%s
            """,
            (error, owner_id, job_id),
        )

    @classmethod
    def _recover_expired(cls, conn: psycopg.Connection) -> None:
        rows = conn.execute(
            """
            SELECT attempt.owner_id,attempt.job_id,attempt.attempt_no,job.kind,
                   job.attempt_count,job.max_attempts,job.cancellation_requested_at
            FROM public.job_attempts AS attempt
            JOIN public.jobs AS job
              ON (job.owner_id,job.id)=(attempt.owner_id,attempt.job_id)
            WHERE attempt.finished_at IS NULL AND attempt.lease_expires_at<=now()
              AND job.state IN ('leased','running')
            ORDER BY attempt.lease_expires_at,attempt.owner_id,attempt.job_id
            FOR UPDATE OF attempt,job SKIP LOCKED
            """
        ).fetchall()
        for row in rows:
            conn.execute(
                """
                UPDATE public.job_attempts
                SET finished_at=now(),outcome='expired'
                WHERE owner_id=%s AND job_id=%s AND attempt_no=%s AND finished_at IS NULL
                """,
                (row["owner_id"], row["job_id"], row["attempt_no"]),
            )
            if row["cancellation_requested_at"] is not None:
                conn.execute(
                    """
                    UPDATE public.jobs
                    SET state='cancelled',completed_at=now(),updated_at=now()
                    WHERE owner_id=%s AND id=%s
                    """,
                    (row["owner_id"], row["job_id"]),
                )
            elif row["attempt_count"] >= row["max_attempts"]:
                conn.execute(
                    """
                    UPDATE public.jobs
                    SET state='failed',completed_at=now(),updated_at=now(),
                        sanitized_error='{"code":"attempts_exhausted","retryable":false}'::jsonb
                    WHERE owner_id=%s AND id=%s
                    """,
                    (row["owner_id"], row["job_id"]),
                )
            else:
                cls._requeue(
                    conn,
                    owner_id=row["owner_id"],
                    job_id=row["job_id"],
                    kind=row["kind"],
                    retry_seconds=0,
                )

    @staticmethod
    def _companion_capped(conn: psycopg.Connection, owner_id: uuid.UUID) -> bool:
        """Whether the companion's ``reserve_cost`` would refuse this owner's next call for their
        day or month cap: the same SQL and the same comparison (``reader_cap_reached``), read
        under the owner's spend lock, so the claim and the first paid call cannot disagree."""
        from app.hosted.companion.spend import reader_cap_reached

        try:
            return reader_cap_reached(conn, owner_id) is not None
        except LookupError as exc:
            raise WorkerConfigurationError("owner limit policy is unavailable") from exc

    def claim_next(
        self,
        *,
        worker_id: str,
        lease_for: timedelta,
        kinds: frozenset[str] | None = None,
    ) -> ClaimedJob | None:
        """Lease the next job, of ``kinds`` only when given (the kinds this worker can run).

        A companion job is skipped while another of the same owner, kind and book is leased or
        running, while any platform provider pause is in force, and while its owner is at a
        companion cap; a capped owner's job is moved to the start of the next UTC day.
        """
        self._validate_worker_id(worker_id)
        lease_seconds = _seconds(lease_for)
        wanted = None if kinds is None else sorted(kinds)
        with self._connect() as conn, conn.transaction():
            self._recover_expired(conn)
            blocked_owners: list[uuid.UUID] = []
            capped_owners: list[uuid.UUID] = []
            while True:
                row = conn.execute(
                    """
                    SELECT job.owner_id,job.id,job.book_id,job.book_incarnation,
                           job.credential_id,job.kind,job.attempt_count
                    FROM public.jobs AS job
                    WHERE job.state='pending' AND job.run_after<=now()
                      AND job.cancellation_requested_at IS NULL
                      AND job.attempt_count<job.max_attempts
                      AND NOT (job.owner_id=ANY(%(blocked)s::uuid[]))
                      AND (%(kinds)s::text[] IS NULL OR job.kind=ANY(%(kinds)s::text[]))
                      AND (job.kind<>ALL(%(companion)s::text[]) OR (
                        NOT (job.owner_id=ANY(%(capped)s::uuid[]))
                        AND NOT EXISTS (SELECT 1 FROM public.provider_pauses AS pause
                                        WHERE pause.paused_until>now())
                        AND NOT EXISTS (SELECT 1 FROM public.jobs AS active
                                        WHERE active.owner_id=job.owner_id
                                          AND active.kind=job.kind
                                          AND active.book_id IS NOT DISTINCT FROM job.book_id
                                          AND active.state IN ('leased','running'))))
                    ORDER BY job.priority DESC,job.run_after,job.created_at,job.id
                    FOR UPDATE OF job SKIP LOCKED
                    LIMIT 1
                    """,
                    {"blocked": blocked_owners, "kinds": wanted, "capped": capped_owners,
                     "companion": sorted(COMPANION_KINDS)},
                ).fetchone()
                if row is None:
                    return None
                lock = conn.execute(
                    "SELECT pg_try_advisory_xact_lock(hashtextextended(%s::text,47)) AS acquired",
                    (row["owner_id"],),
                ).fetchone()
                if not lock["acquired"]:
                    blocked_owners.append(row["owner_id"])
                    continue
                if row["kind"] in COMPANION_KINDS and self._companion_capped(
                    conn, row["owner_id"]
                ):
                    # Nothing of theirs could be paid for before the next UTC day.
                    conn.execute(
                        """
                        UPDATE public.jobs
                        SET run_after=(date_trunc('day', now() AT TIME ZONE 'UTC')
                                       + interval '1 day') AT TIME ZONE 'UTC',
                            updated_at=now()
                        WHERE owner_id=%s AND id=%s AND state='pending'
                        """,
                        (row["owner_id"], row["id"]),
                    )
                    capped_owners.append(row["owner_id"])
                    continue
                concurrency = conn.execute(
                    """
                    SELECT limits.max_provider_concurrency,
                           (SELECT count(*) FROM public.jobs AS active
                            WHERE active.owner_id=limits.owner_id
                              AND active.state IN ('leased','running')) AS active
                    FROM public.owner_limits AS limits WHERE limits.owner_id=%s
                    """,
                    (row["owner_id"],),
                ).fetchone()
                if concurrency is None:
                    raise WorkerConfigurationError("owner limit policy is unavailable")
                if concurrency["active"] < concurrency["max_provider_concurrency"]:
                    break
                blocked_owners.append(row["owner_id"])
            token = secrets.token_urlsafe(32)
            attempt_no = row["attempt_count"] + 1
            conn.execute(
                """
                UPDATE public.jobs
                SET state='leased',attempt_count=%s,updated_at=now()
                WHERE owner_id=%s AND id=%s AND state='pending'
                """,
                (attempt_no, row["owner_id"], row["id"]),
            )
            conn.execute(
                """
                INSERT INTO public.job_attempts
                  (owner_id,job_id,attempt_no,worker_id,lease_token_digest,leased_at,
                   lease_expires_at,heartbeat_at)
                VALUES (%s,%s,%s,%s,%s,now(),now()+(%s*interval '1 second'),now())
                """,
                (
                    row["owner_id"],
                    row["id"],
                    attempt_no,
                    worker_id,
                    _token_digest(token),
                    lease_seconds,
                ),
            )
            record_event(
                conn,
                owner_id=row["owner_id"],
                actor_kind="worker",
                action="job.claim",
                target_kind="job",
                target_id=row["id"],
                result="succeeded",
            )
        return ClaimedJob(
            owner_id=row["owner_id"],
            job_id=row["id"],
            book_id=row["book_id"],
            book_incarnation=row["book_incarnation"],
            kind=row["kind"],
            attempt_no=attempt_no,
            worker_id=worker_id,
            lease_token=token,
            credential_id=row["credential_id"],
        )

    @staticmethod
    def _locked_lease(conn: psycopg.Connection, claim: ClaimedJob) -> dict:
        row = conn.execute(
            """
            SELECT job.state,job.kind,job.cancellation_requested_at,job.attempt_count,
                   job.max_attempts,attempt.lease_expires_at>now() AS lease_live
            FROM public.jobs AS job
            JOIN public.job_attempts AS attempt
              ON (attempt.owner_id,attempt.job_id)=(job.owner_id,job.id)
            WHERE job.owner_id=%s AND job.id=%s AND attempt.attempt_no=%s
              AND attempt.worker_id=%s AND attempt.lease_token_digest=%s
              AND attempt.finished_at IS NULL
            FOR UPDATE OF job,attempt
            """,
            (
                claim.owner_id,
                claim.job_id,
                claim.attempt_no,
                claim.worker_id,
                _token_digest(claim.lease_token),
            ),
        ).fetchone()
        if row is None or not row["lease_live"]:
            raise LostLeaseError("job lease is missing or expired")
        return row

    def reserve_spend(
        self,
        claim: ClaimedJob,
        *,
        phase: str,
        provider: str,
        model: str,
        reserved_input_tokens: int,
        reserved_output_tokens: int,
        reserved_usd: str | Decimal,
        idempotency_key: str,
    ) -> uuid.UUID:
        """Atomically reserve optional owner spend before provider I/O."""
        if phase not in {"extraction", "synthesis", "embedding", "judge"}:
            raise ValueError("unsupported cost phase")
        amount = Decimal(reserved_usd)
        if reserved_input_tokens < 0 or reserved_output_tokens < 0 or amount < 0:
            raise ValueError("cost reservation values cannot be negative")
        with self._connect() as conn, conn.transaction():
            lease = self._locked_lease(conn, claim)
            if lease["state"] != "running" or lease["cancellation_requested_at"] is not None:
                raise LostLeaseError("job is not eligible to reserve spend")
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s::text,47))",
                (claim.owner_id,),
            )
            limits = conn.execute(
                "SELECT max_spend_usd FROM public.owner_limits WHERE owner_id=%s",
                (claim.owner_id,),
            ).fetchone()
            if limits is None:
                raise WorkerConfigurationError("owner limit policy is unavailable")
            existing = conn.execute(
                """
                SELECT id,phase,provider,model,reserved_input_tokens,reserved_output_tokens,
                       reserved_usd,state
                FROM public.cost_reservations
                WHERE owner_id=%s AND idempotency_key=%s
                """,
                (claim.owner_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                names = (
                    "phase", "provider", "model", "reserved_input_tokens",
                    "reserved_output_tokens", "reserved_usd", "state"
                )
                expected = (
                    phase,
                    provider,
                    model,
                    reserved_input_tokens,
                    reserved_output_tokens,
                    amount,
                    "reserved",
                )
                if tuple(existing[name] for name in names) != expected:
                    raise JobIdempotencyError("cost reservation changed across retry")
                return existing["id"]
            ceiling = limits["max_spend_usd"]
            if ceiling is not None:
                usage = conn.execute(
                    """
                    SELECT COALESCE((SELECT sum(usd) FROM public.cost_ledger
                                     WHERE owner_id=%s),0)
                         + COALESCE((SELECT sum(reserved_usd) FROM public.cost_reservations
                                     WHERE owner_id=%s AND state='reserved'),0) AS usd
                    """,
                    (claim.owner_id, claim.owner_id),
                ).fetchone()["usd"]
                if usage + amount > ceiling:
                    raise LimitExceededError(
                        "spend_limit_exceeded",
                        str(ceiling),
                        None,
                        "Ask an operator to raise the spend limit before starting more AI work.",
                    )
            reservation_id = uuid.uuid4()
            conn.execute(
                """
                INSERT INTO public.cost_reservations
                  (owner_id,id,book_id,book_incarnation,job_id,phase,provider,model,
                   reserved_input_tokens,reserved_output_tokens,reserved_usd,idempotency_key)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    claim.owner_id,
                    reservation_id,
                    claim.book_id,
                    claim.book_incarnation,
                    claim.job_id,
                    phase,
                    provider,
                    model,
                    reserved_input_tokens,
                    reserved_output_tokens,
                    amount,
                    idempotency_key,
                ),
            )
        return reservation_id

    @staticmethod
    def _cancel_locked(conn: psycopg.Connection, claim: ClaimedJob) -> None:
        conn.execute(
            """
            UPDATE public.job_attempts
            SET finished_at=now(),outcome='cancelled'
            WHERE owner_id=%s AND job_id=%s AND attempt_no=%s AND finished_at IS NULL
            """,
            (claim.owner_id, claim.job_id, claim.attempt_no),
        )
        conn.execute(
            """
            UPDATE public.jobs
            SET state='cancelled',completed_at=now(),updated_at=now()
            WHERE owner_id=%s AND id=%s
            """,
            (claim.owner_id, claim.job_id),
        )

    def start(self, claim: ClaimedJob) -> bool:
        with self._connect() as conn, conn.transaction():
            row = self._locked_lease(conn, claim)
            if row["cancellation_requested_at"] is not None:
                self._cancel_locked(conn, claim)
                return False
            if row["state"] != "leased":
                raise LostLeaseError("job is not in leased state")
            conn.execute(
                "UPDATE public.jobs SET state='running',updated_at=now() "
                "WHERE owner_id=%s AND id=%s",
                (claim.owner_id, claim.job_id),
            )
            conn.execute(
                """
                UPDATE public.job_attempts SET started_at=now(),heartbeat_at=now()
                WHERE owner_id=%s AND job_id=%s AND attempt_no=%s
                """,
                (claim.owner_id, claim.job_id, claim.attempt_no),
            )
            record_event(
                conn,
                owner_id=claim.owner_id,
                actor_kind="worker",
                action="job.start",
                target_kind="job",
                target_id=claim.job_id,
                result="succeeded",
            )
        return True

    def heartbeat(self, claim: ClaimedJob, *, lease_for: timedelta) -> bool:
        lease_seconds = _seconds(lease_for)
        with self._connect() as conn, conn.transaction():
            row = self._locked_lease(conn, claim)
            if row["state"] not in {"leased", "running"}:
                raise LostLeaseError("job is not active")
            conn.execute(
                """
                UPDATE public.job_attempts
                SET heartbeat_at=now(),lease_expires_at=now()+(%s*interval '1 second')
                WHERE owner_id=%s AND job_id=%s AND attempt_no=%s
                """,
                (lease_seconds, claim.owner_id, claim.job_id, claim.attempt_no),
            )
            return row["cancellation_requested_at"] is not None

    def pause_provider(
        self, claim: ClaimedJob, *, provider: str, reason: str, pause_for: timedelta
    ) -> None:
        """Record that the platform's account at ``provider`` was refused while running ``claim``.

        The account is the platform's, so the pause is platform-wide: no companion job of any
        owner is claimed until it ends (ADR 0041 section 5). One row per provider: a later
        refusal extends the pause and never adds a second one.
        """
        if provider not in PAUSABLE_PROVIDERS or reason not in PAUSE_REASONS:
            raise ValueError("provider pause must use the reviewed provider and reason")
        pause_seconds = _seconds(pause_for)
        with self._connect() as conn, conn.transaction():
            conn.execute(
                """
                INSERT INTO public.provider_pauses (provider,reason,paused_until)
                VALUES (%s,%s,now()+(%s*interval '1 second'))
                ON CONFLICT (provider) DO UPDATE
                SET reason=EXCLUDED.reason,
                    paused_until=GREATEST(public.provider_pauses.paused_until,
                                          EXCLUDED.paused_until),
                    updated_at=now()
                """,
                (provider, reason, pause_seconds),
            )
            record_event(
                conn,
                owner_id=claim.owner_id,
                actor_kind="worker",
                action="provider.pause",
                target_kind="job",
                target_id=claim.job_id,
                result="failed",
                reason_code=reason,
            )

    def succeed(self, claim: ClaimedJob) -> bool:
        with self._connect() as conn, conn.transaction():
            row = self._locked_lease(conn, claim)
            if row["cancellation_requested_at"] is not None:
                self._cancel_locked(conn, claim)
                return False
            if row["state"] != "running":
                raise LostLeaseError("job is not running")
            conn.execute(
                """
                UPDATE public.job_attempts
                SET finished_at=now(),outcome='succeeded'
                WHERE owner_id=%s AND job_id=%s AND attempt_no=%s
                """,
                (claim.owner_id, claim.job_id, claim.attempt_no),
            )
            conn.execute(
                """
                UPDATE public.jobs
                SET state='succeeded',completed_at=now(),updated_at=now(),sanitized_error=NULL
                WHERE owner_id=%s AND id=%s
                """,
                (claim.owner_id, claim.job_id),
            )
            record_event(
                conn,
                owner_id=claim.owner_id,
                actor_kind="worker",
                action="job.succeed",
                target_kind="job",
                target_id=claim.job_id,
                result="succeeded",
            )
        return True

    def fail(
        self,
        claim: ClaimedJob,
        *,
        failure: SanitizedFailure,
        retry_after: timedelta,
    ) -> None:
        if not isinstance(failure, SanitizedFailure):
            raise TypeError("worker failures must use SanitizedFailure")
        retry_seconds = _seconds(retry_after, allow_zero=True)
        with self._connect() as conn, conn.transaction():
            row = self._locked_lease(conn, claim)
            conn.execute(
                """
                UPDATE public.job_attempts
                SET finished_at=now(),outcome='failed',sanitized_error=%s::jsonb
                WHERE owner_id=%s AND job_id=%s AND attempt_no=%s
                """,
                (
                    psycopg.types.json.Jsonb(failure.as_json()),
                    claim.owner_id,
                    claim.job_id,
                    claim.attempt_no,
                ),
            )
            if row["cancellation_requested_at"] is not None:
                conn.execute(
                    """
                    UPDATE public.jobs SET state='cancelled',completed_at=now(),updated_at=now()
                    WHERE owner_id=%s AND id=%s
                    """,
                    (claim.owner_id, claim.job_id),
                )
            elif failure.retryable and row["attempt_count"] < row["max_attempts"]:
                self._requeue(
                    conn,
                    owner_id=claim.owner_id,
                    job_id=claim.job_id,
                    kind=row["kind"],
                    retry_seconds=retry_seconds,
                    failure=failure,
                )
            else:
                conn.execute(
                    """
                    UPDATE public.jobs
                    SET state='failed',completed_at=now(),updated_at=now(),sanitized_error=%s::jsonb
                    WHERE owner_id=%s AND id=%s
                    """,
                    (
                        psycopg.types.json.Jsonb(failure.as_json()),
                        claim.owner_id,
                        claim.job_id,
                    ),
                )
            record_event(
                conn,
                owner_id=claim.owner_id,
                actor_kind="worker",
                action="job.fail",
                target_kind="job",
                target_id=claim.job_id,
                result="failed",
                reason_code=failure.code,
            )

    def commit_chapter(
        self,
        claim: ClaimedJob,
        *,
        book_id: uuid.UUID,
        book_incarnation: uuid.UUID,
        chapter_id: uuid.UUID,
        chapter_key: str,
        ordinal: int,
        content_hash: str,
        summary: str,
        extractor_version: str,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        usd: str | Decimal,
        cost_idempotency_key: str,
    ) -> bool:
        if claim.book_id != book_id or claim.book_incarnation != book_incarnation:
            raise JobIdempotencyError("chapter commit does not match the claimed book")
        with self._connect() as conn, conn.transaction():
            lease = self._locked_lease(conn, claim)
            if lease["state"] != "running" or lease["cancellation_requested_at"] is not None:
                raise LostLeaseError("job is not eligible to commit")
            receipt = conn.execute(
                """
                SELECT content_hash FROM public.ingested_chapters
                WHERE owner_id=%s AND book_id=%s AND book_incarnation=%s AND chapter_id=%s
                """,
                (claim.owner_id, book_id, book_incarnation, chapter_id),
            ).fetchone()
            if receipt is not None:
                if receipt["content_hash"] != content_hash:
                    raise JobIdempotencyError("chapter receipt content hash changed across retry")
                return False

            amount = Decimal(usd)
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s::text,47))",
                (claim.owner_id,),
            )
            limits = conn.execute(
                "SELECT max_spend_usd FROM public.owner_limits WHERE owner_id=%s",
                (claim.owner_id,),
            ).fetchone()
            if limits is None:
                raise WorkerConfigurationError("owner limit policy is unavailable")
            reservation = conn.execute(
                """
                SELECT id,phase,provider,model,state FROM public.cost_reservations
                WHERE owner_id=%s AND idempotency_key=%s FOR UPDATE
                """,
                (claim.owner_id, cost_idempotency_key),
            ).fetchone()
            if limits["max_spend_usd"] is not None and reservation is None:
                raise LimitExceededError(
                    "spend_reservation_required",
                    str(limits["max_spend_usd"]),
                    None,
                    "Reserve owner spend before making the provider request.",
                )
            if reservation is not None:
                if (
                    reservation["phase"] != "extraction"
                    or reservation["provider"] != provider
                    or reservation["model"] != model
                    or reservation["state"] != "reserved"
                ):
                    raise JobIdempotencyError("cost reservation does not match chapter commit")
                if limits["max_spend_usd"] is not None:
                    other_usage = conn.execute(
                        """
                        SELECT COALESCE((SELECT sum(usd) FROM public.cost_ledger
                                         WHERE owner_id=%s),0)
                             + COALESCE((SELECT sum(reserved_usd)
                                         FROM public.cost_reservations
                                         WHERE owner_id=%s AND state='reserved' AND id<>%s),0) AS usd
                        """,
                        (claim.owner_id, claim.owner_id, reservation["id"]),
                    ).fetchone()["usd"]
                    if other_usage + amount > limits["max_spend_usd"]:
                        raise LimitExceededError(
                            "spend_limit_exceeded",
                            str(limits["max_spend_usd"]),
                            None,
                            "The actual provider cost exceeds the owner's spend limit.",
                        )

            existing = conn.execute(
                """
                SELECT id,revealed_at,content_hash FROM public.chapters
                WHERE owner_id=%s AND book_id=%s AND book_incarnation=%s AND chapter_key=%s
                """,
                (claim.owner_id, book_id, book_incarnation, chapter_key),
            ).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO public.chapters
                      (owner_id,book_id,book_incarnation,id,chapter_key,revealed_at,title,
                       content_hash,extractor_version)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        claim.owner_id,
                        book_id,
                        book_incarnation,
                        chapter_id,
                        chapter_key,
                        ordinal,
                        chapter_key,
                        content_hash,
                        extractor_version,
                    ),
                )
            elif (
                existing["id"] != chapter_id
                or existing["revealed_at"] != ordinal
                or existing["content_hash"] != content_hash
            ):
                raise JobIdempotencyError("chapter identity changed across retry")

            conn.execute(
                """
                INSERT INTO public.chapter_summaries
                  (owner_id,book_id,book_incarnation,id,source_chapter_id,kind,summary,
                   revealed_at,extractor_version)
                VALUES (%s,%s,%s,%s,%s,'chapter',%s,%s,%s)
                """,
                (
                    claim.owner_id,
                    book_id,
                    book_incarnation,
                    uuid.uuid4(),
                    chapter_id,
                    summary,
                    ordinal,
                    extractor_version,
                ),
            )
            conn.execute(
                """
                INSERT INTO public.cost_ledger
                  (owner_id,id,book_id,book_incarnation,job_id,chapter_ordinal,phase,provider,model,
                   input_tokens,output_tokens,usd,idempotency_key)
                VALUES (%s,%s,%s,%s,%s,%s,'extraction',%s,%s,%s,%s,%s,%s)
                """,
                (
                    claim.owner_id,
                    uuid.uuid4(),
                    book_id,
                    book_incarnation,
                    claim.job_id,
                    ordinal,
                    provider,
                    model,
                    input_tokens,
                    output_tokens,
                    amount,
                    cost_idempotency_key,
                ),
            )
            if reservation is not None:
                conn.execute(
                    """
                    UPDATE public.cost_reservations
                    SET actual_input_tokens=%s,actual_output_tokens=%s,actual_usd=%s,
                        state='settled',settled_at=now()
                    WHERE owner_id=%s AND id=%s AND state='reserved'
                    """,
                    (
                        input_tokens,
                        output_tokens,
                        amount,
                        claim.owner_id,
                        reservation["id"],
                    ),
                )
            # Receipt is deliberately last; all derived rows above reference it through deferred FKs.
            conn.execute(
                """
                INSERT INTO public.ingested_chapters
                  (owner_id,book_id,book_incarnation,chapter_id,content_hash,extractor_model,
                   input_tokens,output_tokens,usd,completed_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,now())
                """,
                (
                    claim.owner_id,
                    book_id,
                    book_incarnation,
                    chapter_id,
                    content_hash,
                    model,
                    input_tokens,
                    output_tokens,
                    amount,
                ),
            )
            record_event(
                conn,
                owner_id=claim.owner_id,
                actor_kind="worker",
                action="chapter.commit",
                target_kind="chapter",
                target_id=chapter_id,
                result="succeeded",
            )
        return True

    def commit_chapter_memory(
        self,
        claim: ClaimedJob,
        *,
        book_id: uuid.UUID,
        book_incarnation: uuid.UUID,
        copied: str,
        chapter: dict,
        receipt: dict,
        rows: dict,
    ) -> uuid.UUID:
        """Publish what one chapter added to a book's story memory, in one transaction fenced by
        the job's lease (ADR 0042 section 1), the completion receipt last. Returns the chapter's id.

        ``copied`` is the digest of the memory the chapter was extracted against
        (``story_memory.copy_digest``); if the memory now reads differently (a reader's
        correction arrived meanwhile) nothing is written and ``StoryMemoryMoved`` is raised. A
        chapter already published raises ``ChapterAlreadyPublished`` and writes nothing. Every
        row must be stamped with the chapter's own ordinal: anything else is refused. Raw text,
        passages and vectors are never written here. The book's per-book lock (the one removing
        a book and a reader's correction take) is held first, so neither can interleave.
        """
        from app.hosted.companion.story_memory import COPY_SQL, copy_digest

        if claim.book_id != book_id or claim.book_incarnation != book_incarnation:
            raise JobIdempotencyError("chapter memory does not match the claimed book")
        ordinal = chapter["ordinal"]
        stamped = [row["revealed_at"] for name in (
            "summaries", "entities", "aliases", "states", "edges", "events", "participants",
            "themes") for row in rows[name]]
        stamped += [row["invalid_at"] for row in rows["closed_states"]]
        if not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal < 1 or any(
                value != ordinal for value in stamped):
            raise ValueError("every published row must carry the chapter's own ordinal")
        scope = (claim.owner_id, book_id, book_incarnation)
        with self._connect() as conn, conn.transaction():
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s::text || ':' || %s::text, 48))",
                (claim.owner_id, book_id),
            )
            lease = self._locked_lease(conn, claim)
            if lease["state"] != "running" or lease["cancellation_requested_at"] is not None:
                raise LostLeaseError("job is not eligible to publish")
            published = conn.execute(
                """
                SELECT 1 FROM public.chapters AS chapter
                JOIN public.ingested_chapters AS receipt
                  ON (receipt.owner_id,receipt.book_id,receipt.book_incarnation,receipt.chapter_id)
                   =(chapter.owner_id,chapter.book_id,chapter.book_incarnation,chapter.id)
                WHERE chapter.owner_id=%s AND chapter.book_id=%s AND chapter.book_incarnation=%s
                  AND (chapter.chapter_key=%s OR chapter.revealed_at=%s)
                  AND chapter.retracted_at IS NULL
                """,
                (*scope, chapter["key"], ordinal),
            ).fetchone()
            if published is not None:
                raise ChapterAlreadyPublished("this chapter's story memory is already published")
            now = conn.execute(
                COPY_SQL, {"owner": claim.owner_id, "book": book_id, "incarnation": book_incarnation}
            ).fetchone()
            if copy_digest(now) != copied:
                raise StoryMemoryMoved("the story memory changed after it was copied")

            # Rows first revealed at one chapter are read back in the order they were recorded:
            # give each its own moment, in the order the chapter wrote them.
            order = iter(range(1, 1_000_000_000))

            def insert(table: str, values: dict) -> None:
                columns = ["owner_id", "book_id", "book_incarnation", *values, "recorded_at"]
                conn.execute(
                    sql.SQL(
                        "INSERT INTO public.{} ({}) VALUES ({}, now()+(%s*interval '1 microsecond'))"
                    ).format(
                        sql.Identifier(table),
                        sql.SQL(",").join(sql.Identifier(name) for name in columns),
                        sql.SQL(",").join(sql.Placeholder() for _ in range(len(columns) - 1)),
                    ),
                    (*scope, *values.values(), next(order)),
                )

            chapter_id = uuid.uuid4()
            insert("chapters", {
                "id": chapter_id, "chapter_key": chapter["key"], "revealed_at": ordinal,
                "href": chapter["href"], "fragment": chapter["fragment"],
                "title": chapter["title"], "part_label": chapter["part_label"],
                "kind": chapter["kind"], "content_hash": chapter["content_hash"],
                "extractor_version": chapter["extractor_version"]})
            source = {"source_chapter_id": chapter_id}
            for row in rows["summaries"]:
                insert("chapter_summaries", {**source, **row})
            for row in rows["entities"]:
                insert("entities", {**source, **row})
            for row in rows["aliases"]:
                insert("aliases", {**source, **row})
            for row in rows["states"]:
                insert("entity_state", {**source, **row, "status": Jsonb(row["status"])})
            for row in rows["closed_states"]:
                closed = conn.execute(
                    """
                    UPDATE public.entity_state SET invalid_at=%s
                    WHERE owner_id=%s AND book_id=%s AND book_incarnation=%s AND id=%s
                      AND invalid_at IS NULL AND revealed_at<%s
                    """,
                    (ordinal, *scope, row["id"], ordinal),
                )
                if closed.rowcount != 1:
                    raise StoryMemoryMoved("a state the chapter replaces is no longer open")
            for row in rows["edges"]:
                insert("edges", {**source, **row})
            for row in rows["events"]:
                insert("events", {**source, **row})
            for row in rows["participants"]:
                insert("event_participants", {**source, **row})
            for row in rows["themes"]:
                insert("themes", {**source, **row})
            # The receipt is last; every row above refers to it through a deferred foreign key.
            conn.execute(
                """
                INSERT INTO public.ingested_chapters
                  (owner_id,book_id,book_incarnation,chapter_id,content_hash,extractor_model,
                   input_tokens,output_tokens,usd,completed_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,now())
                """,
                (*scope, chapter_id, chapter["content_hash"], receipt["extractor_model"],
                 receipt["input_tokens"], receipt["output_tokens"],
                 None if receipt["usd"] is None else Decimal(str(receipt["usd"]))),
            )
            record_event(
                conn,
                owner_id=claim.owner_id,
                actor_kind="worker",
                action="memory.publish",
                target_kind="chapter",
                target_id=chapter_id,
                result="succeeded",
            )
        return chapter_id
