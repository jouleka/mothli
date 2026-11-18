"""Real-PostgreSQL durable job/lease/idempotency contract (LIT-44).

The sample job is a ``chapter_notes`` job (ADR 0043 step 7 moved these tests off the retired
``ingest_book``): the plainest living kind, which the claim, lease, retry and fencing treat as every
companion kind and which has no method of its own on the worker repository. As for every companion
kind, at most one job of an owner, kind and book waits or runs at a time, so a test that needs two
of one owner's jobs at once puts them on two books."""

from __future__ import annotations

import concurrent.futures
import uuid
from dataclasses import replace
from datetime import timedelta

import psycopg
import pytest
from psycopg import conninfo, sql

from app.hosted.jobs import (
    LostLeaseError,
    PostgresWorkerRepository,
    SanitizedFailure,
    WorkerConfigurationError,
)
from app.hosted.limits import update_limits
from app.hosted.tenant.grants import worker_grants
from app.hosted.migrations import apply_migrations

pytestmark = pytest.mark.postgres

# How the companion tests list one reader's jobs: by how far along each is (finished, under way,
# waiting), then by the chapter a story-memory run did, and only then by created_at. A job's
# created_at is the database host's wall clock, which can step back (the WSL clock does when it
# resyncs), so a later job could be listed first; nothing in the product orders work by it
# beyond fairness of the claim.
JOB_ORDER = ("ORDER BY CASE WHEN state IN ('succeeded','failed','cancelled') THEN 0"
             " WHEN state IN ('leased','running') THEN 1 ELSE 2 END,"
             " (payload_metadata->>'chapter')::integer NULLS LAST, created_at, id")


def step_clock_back(dsn: str, owner: uuid.UUID) -> None:
    """Reverse the created_at order of an owner's jobs, as a clock stepping back between two
    inserts would."""
    with psycopg.connect(dsn) as conn:
        conn.execute("UPDATE jobs SET created_at = timestamptz '2000-01-01'"
                     " - (created_at - timestamptz '2000-01-01') WHERE owner_id=%s", (owner,))


@pytest.fixture(scope="module")
def admin_dsn() -> str:
    import os

    dsn = os.environ.get("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN is required for the real PostgreSQL suite")
    return dsn


@pytest.fixture()
def database(admin_dsn: str):
    database_name = f"lit44_{uuid.uuid4().hex}"
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
    dsn = conninfo.make_conninfo(admin_dsn, dbname=database_name)
    apply_migrations(dsn)
    try:
        yield dsn
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname=%s AND pid<>pg_backend_pid()",
                (database_name,),
            )
            admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(database_name)))


@pytest.fixture()
def worker_dsn(database: str, admin_dsn: str):
    role = f"lit44_worker_{uuid.uuid4().hex}"
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(
            sql.SQL(
                "CREATE ROLE {} LOGIN NOSUPERUSER NOINHERIT BYPASSRLS "
                "NOCREATEDB NOCREATEROLE NOREPLICATION"
            ).format(sql.Identifier(role))
        )
    with psycopg.connect(database, autocommit=True) as conn:
        conn.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(role)))
        # Exactly the reviewed allow-list the worker checks at startup (grants.py).
        for table, privileges in worker_grants():
            conn.execute(
                sql.SQL("GRANT {} ON {} TO {}").format(
                    sql.SQL(", ").join(sql.SQL(privilege) for privilege in privileges),
                    sql.Identifier(table),
                    sql.Identifier(role),
                )
            )
    runtime_dsn = conninfo.make_conninfo(database, user=role)
    try:
        yield runtime_dsn
    finally:
        with psycopg.connect(database, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


@pytest.fixture()
def corpus(database: str) -> dict:
    owner_a = uuid.uuid4()
    owner_b = uuid.uuid4()
    book_a = uuid.uuid4()
    book_a2 = uuid.uuid4()
    book_b = uuid.uuid4()
    incarnation_a = uuid.uuid4()
    incarnation_a2 = uuid.uuid4()
    incarnation_b = uuid.uuid4()
    with psycopg.connect(database) as conn:
        conn.cursor().executemany(
            "INSERT INTO users (id,display_name) VALUES (%s,%s)",
            [(owner_a, "Owner A"), (owner_b, "Owner B")],
        )
        conn.cursor().executemany(
            """
            INSERT INTO books (owner_id,id,incarnation,title,schema_version)
            VALUES (%s,%s,%s,%s,1)
            """,
            [
                (owner_a, book_a, incarnation_a, "A book"),
                (owner_a, book_a2, incarnation_a2, "A second book"),
                (owner_b, book_b, incarnation_b, "B book"),
            ],
        )
    return {
        "owner_a": owner_a,
        "owner_b": owner_b,
        "book_a": book_a,
        "book_a2": book_a2,
        "book_b": book_b,
        "incarnation_a": incarnation_a,
        "incarnation_a2": incarnation_a2,
        "incarnation_b": incarnation_b,
    }


def _enqueue(
    database: str,
    corpus: dict,
    *,
    owner: str = "a",
    book: str | None = None,
    max_attempts: int = 3,
    suffix: str | None = None,
) -> uuid.UUID:
    job_id = uuid.uuid4()
    suffix = suffix or job_id.hex
    book = book or owner
    with psycopg.connect(database) as conn:
        conn.execute(
            """
            INSERT INTO jobs
              (owner_id,id,book_id,book_incarnation,kind,idempotency_key,max_attempts)
            VALUES (%s,%s,%s,%s,'chapter_notes',%s,%s)
            """,
            (
                corpus[f"owner_{owner}"],
                job_id,
                corpus[f"book_{book}"],
                corpus[f"incarnation_{book}"],
                f"chapter-notes:{suffix}",
                max_attempts,
            ),
        )
    return job_id


def test_concurrent_claim_is_single_winner_and_lease_token_is_only_digest(
    database: str, corpus: dict
) -> None:
    job_id = _enqueue(database, corpus)
    repository = PostgresWorkerRepository(database)

    def claim(worker: str):
        return repository.claim_next(worker_id=worker, lease_for=timedelta(seconds=30))

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(claim, ("worker-a", "worker-b")))
    winners = [claim for claim in claims if claim is not None]
    assert len(winners) == 1
    claimed = winners[0]
    assert claimed.job_id == job_id
    assert claimed.owner_id == corpus["owner_a"]
    assert claimed.attempt_no == 1
    assert "lease_token" not in repr(claimed)

    with psycopg.connect(database) as conn:
        row = conn.execute(
            """
            SELECT job.state,job.attempt_count,octet_length(attempt.lease_token_digest),
                   attempt.worker_id
            FROM jobs AS job JOIN job_attempts AS attempt
              ON (attempt.owner_id,attempt.job_id)=(job.owner_id,job.id)
            WHERE job.owner_id=%s AND job.id=%s
            """,
            (corpus["owner_a"], job_id),
        ).fetchone()
    assert row == ("leased", 1, 32, claimed.worker_id)


def test_worker_claim_is_owner_and_book_fenced_against_foreign_identifiers(
    worker_dsn: str, database: str, corpus: dict
) -> None:
    job_id = _enqueue(database, corpus, suffix="owner-fence")
    repository = PostgresWorkerRepository(worker_dsn)
    claim = repository.claim_next(worker_id="fence-worker", lease_for=timedelta(seconds=30))
    assert claim is not None and claim.job_id == job_id

    foreign_owner_claim = replace(claim, owner_id=corpus["owner_b"])
    for operation in (
        lambda: repository.start(foreign_owner_claim),
        lambda: repository.heartbeat(
            foreign_owner_claim, lease_for=timedelta(seconds=30)
        ),
        lambda: repository.fail(
            foreign_owner_claim,
            failure=SanitizedFailure("internal_error", retryable=False),
            retry_after=timedelta(0),
        ),
        lambda: repository.succeed(foreign_owner_claim),
    ):
        with pytest.raises(LostLeaseError):
            operation()

    # A claim turned on another reader's book is refused at the publish too:
    # test_story_memory_postgres.py::test_one_readers_chapter_never_reads_or_writes_anothers_rows.
    assert repository.start(claim)

    with psycopg.connect(database) as conn:
        assert conn.execute(
            "SELECT state FROM jobs WHERE owner_id=%s AND id=%s",
            (corpus["owner_a"], job_id),
        ).fetchone() == ("running",)
        assert conn.execute(
            "SELECT count(*) FROM ingested_chapters WHERE owner_id=%s AND book_id=%s",
            (corpus["owner_b"], corpus["book_b"]),
        ).fetchone() == (0,)


def test_worker_role_is_bypassrls_and_exactly_privileged(
    worker_dsn: str, database: str, corpus: dict
) -> None:
    repository = PostgresWorkerRepository(worker_dsn)
    repository.check_runtime_role()
    job_id = _enqueue(database, corpus)
    claim = repository.claim_next(worker_id="isolated-worker", lease_for=timedelta(seconds=30))
    assert claim is not None and claim.job_id == job_id
    assert repository.start(claim)
    assert repository.succeed(claim)

    role = conninfo.conninfo_to_dict(worker_dsn)["user"]
    with psycopg.connect(database, autocommit=True) as conn:
        # One privilege more than its list (ADR 0044 step 3 gave it SELECT on books, so the
        # probe is a table it must never read: a reader's stored file).
        conn.execute(sql.SQL("GRANT SELECT ON source_objects TO {}").format(
            sql.Identifier(role)))
    try:
        with pytest.raises(WorkerConfigurationError, match="allow-list"):
            repository.check_runtime_role()
    finally:
        with psycopg.connect(database, autocommit=True) as conn:
            conn.execute(sql.SQL("REVOKE SELECT ON source_objects FROM {}").format(
                sql.Identifier(role)))


def test_provider_concurrency_is_atomic_across_worker_instances(
    worker_dsn: str, database: str, corpus: dict
) -> None:
    update_limits(database, corpus["owner_a"], {"max_provider_concurrency": 1})
    # On two books, so that only the owner's concurrency (not one job per owner, kind and book)
    # can hold the second back.
    jobs_a = [_enqueue(database, corpus, book=book, suffix=f"limit-{book}")
              for book in ("a", "a2")]
    job_b = _enqueue(database, corpus, owner="b", suffix="limit-b")

    def claim(worker: str):
        return PostgresWorkerRepository(worker_dsn).claim_next(
            worker_id=worker, lease_for=timedelta(seconds=30)
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        claims = list(pool.map(claim, ("limit-worker-1", "limit-worker-2", "limit-worker-3")))
    claimed = [value for value in claims if value is not None]
    assert len(claimed) == 2
    assert {value.owner_id for value in claimed} == {corpus["owner_a"], corpus["owner_b"]}
    assert sum(value.job_id in jobs_a for value in claimed) == 1
    assert any(value.job_id == job_b for value in claimed)


def test_job_payload_rejects_credentials(database: str, corpus: dict) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        with psycopg.connect(database) as conn:
            conn.execute(
                """
                INSERT INTO jobs
                  (owner_id,id,book_id,book_incarnation,kind,idempotency_key,payload_metadata)
                VALUES (%s,%s,%s,%s,'chapter_notes',%s,%s::jsonb)
                """,
                (
                    corpus["owner_a"],
                    uuid.uuid4(),
                    corpus["book_a"],
                    corpus["incarnation_a"],
                    f"chapter-notes:{uuid.uuid4().hex}",
                    psycopg.types.json.Jsonb({"chapter_count": 1, "token": "must-not-persist"}),
                ),
            )


def test_database_rejects_unreviewed_failure_codes(database: str, corpus: dict) -> None:
    job_id = _enqueue(database, corpus)
    with pytest.raises(psycopg.errors.CheckViolation):
        with psycopg.connect(database) as conn:
            conn.execute(
                """
                UPDATE jobs SET sanitized_error=
                  '{"code":"private_chapter_text","retryable":false}'::jsonb
                WHERE owner_id=%s AND id=%s
                """,
                (corpus["owner_a"], job_id),
            )


def test_expired_lease_is_recovered_and_attempt_budget_is_bounded(
    database: str, corpus: dict
) -> None:
    job_id = _enqueue(database, corpus, max_attempts=2)
    repository = PostgresWorkerRepository(database)
    first = repository.claim_next(worker_id="worker-one", lease_for=timedelta(seconds=30))
    assert first is not None
    with psycopg.connect(database) as conn:
        conn.execute(
            "UPDATE job_attempts SET leased_at=now()-interval '2 seconds', "
            "lease_expires_at=now()-interval '1 second' "
            "WHERE owner_id=%s AND job_id=%s",
            (corpus["owner_a"], job_id),
        )
    second = repository.claim_next(worker_id="worker-two", lease_for=timedelta(seconds=30))
    assert second is not None
    assert second.job_id == job_id and second.attempt_no == 2
    with pytest.raises(LostLeaseError):
        repository.start(first)
    with psycopg.connect(database) as conn:
        assert conn.execute(
            "SELECT outcome FROM job_attempts WHERE owner_id=%s AND job_id=%s ORDER BY attempt_no",
            (corpus["owner_a"], job_id),
        ).fetchall() == [("expired",), (None,)]

    with psycopg.connect(database) as conn:
        conn.execute(
            "UPDATE job_attempts SET leased_at=now()-interval '2 seconds', "
            "lease_expires_at=now()-interval '1 second' "
            "WHERE owner_id=%s AND job_id=%s AND attempt_no=2",
            (corpus["owner_a"], job_id),
        )
    assert repository.claim_next(
        worker_id="worker-three", lease_for=timedelta(seconds=30)
    ) is None
    with psycopg.connect(database) as conn:
        state, attempts, error = conn.execute(
            "SELECT state,attempt_count,sanitized_error FROM jobs WHERE owner_id=%s AND id=%s",
            (corpus["owner_a"], job_id),
        ).fetchone()
    assert state == "failed" and attempts == 2
    assert error == {"code": "attempts_exhausted", "retryable": False}


def test_start_heartbeat_failure_retry_and_sanitized_terminal_error(
    database: str, corpus: dict
) -> None:
    job_id = _enqueue(database, corpus, max_attempts=2)
    repository = PostgresWorkerRepository(database)
    claim = repository.claim_next(worker_id="worker-one", lease_for=timedelta(seconds=30))
    assert claim is not None
    assert repository.start(claim)
    assert not repository.heartbeat(claim, lease_for=timedelta(seconds=30))
    secret = "private chapter text and provider credential"
    repository.fail(
        claim,
        failure=SanitizedFailure("provider_unavailable", retryable=True),
        retry_after=timedelta(0),
    )

    retry = repository.claim_next(worker_id="worker-two", lease_for=timedelta(seconds=30))
    assert retry is not None and retry.attempt_no == 2
    assert repository.start(retry)
    repository.fail(
        retry,
        failure=SanitizedFailure("internal_error", retryable=True),
        retry_after=timedelta(0),
    )
    with psycopg.connect(database) as conn:
        state, error = conn.execute(
            "SELECT state,sanitized_error FROM jobs WHERE owner_id=%s AND id=%s",
            (corpus["owner_a"], job_id),
        ).fetchone()
        rendered = str(conn.execute("SELECT sanitized_error FROM jobs").fetchall())
        audit = conn.execute(
            "SELECT action,result,metadata FROM audit_events WHERE owner_id=%s AND target_id=%s "
            "ORDER BY occurred_at,id",
            (corpus["owner_a"], job_id),
        ).fetchall()
    assert state == "failed"
    assert error == {"code": "internal_error", "retryable": True}
    assert secret not in rendered
    assert [row[0] for row in audit] == [
        "job.claim",
        "job.start",
        "job.fail",
        "job.claim",
        "job.start",
        "job.fail",
    ]
    assert audit[-1][1:] == ("failed", {"reason_code": "internal_error"})


def test_pending_and_running_cancellation_are_cooperative(database: str, corpus: dict) -> None:
    pending_id = _enqueue(database, corpus, suffix="pending")
    running_id = _enqueue(database, corpus, book="a2", suffix="running")
    repository = PostgresWorkerRepository(database)
    running = repository.claim_next(worker_id="worker", lease_for=timedelta(seconds=30))
    assert running is not None and running.job_id in {pending_id, running_id}
    other = running_id if running.job_id == pending_id else pending_id

    with psycopg.connect(database) as conn:
        conn.execute(
            """
            UPDATE jobs SET state='cancelled',cancellation_requested_at=now(),completed_at=now()
            WHERE owner_id=%s AND id=%s AND state='pending'
            """,
            (corpus["owner_a"], other),
        )
        conn.execute(
            "UPDATE jobs SET cancellation_requested_at=now() WHERE owner_id=%s AND id=%s",
            (corpus["owner_a"], running.job_id),
        )
    assert not repository.start(running)
    assert repository.claim_next(
        worker_id="worker-next", lease_for=timedelta(seconds=30)
    ) is None
    with psycopg.connect(database) as conn:
        assert conn.execute(
            "SELECT state FROM jobs WHERE owner_id=%s ORDER BY id",
            (corpus["owner_a"],),
        ).fetchall() == [("cancelled",), ("cancelled",)]
