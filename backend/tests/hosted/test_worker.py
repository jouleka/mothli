"""Deterministic durable-worker process behavior (LIT-44)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from app.hosted.jobs import ClaimedJob, SanitizedFailure
from app.hosted.worker import DurableWorker, HandledJobFailure


def _claim() -> ClaimedJob:
    return ClaimedJob(
        owner_id=uuid.uuid4(),
        job_id=uuid.uuid4(),
        book_id=uuid.uuid4(),
        book_incarnation=uuid.uuid4(),
        kind="chapter_notes",
        attempt_no=1,
        worker_id="worker",
        lease_token="private-token",
    )


@dataclass
class FakeRepository:
    claim: ClaimedJob | None
    started: bool = True
    calls: list[tuple] = field(default_factory=list)

    def claim_next(self, **kwargs):
        self.calls.append(("claim", kwargs))
        return self.claim

    def start(self, claim):
        self.calls.append(("start", claim.job_id))
        return self.started

    def succeed(self, claim):
        self.calls.append(("succeed", claim.job_id))

    def fail(self, claim, *, failure, retry_after):
        self.calls.append(("fail", failure, retry_after))

    def check_runtime_role(self):
        self.calls.append(("check_runtime_role",))


def test_the_worker_starts_and_runs_without_a_credential_key(monkeypatch) -> None:
    """The break it catches: the worker still asking for the reader-key decrypter at start, so it
    cannot run without HOSTED_CREDENTIAL_MASTER_KEY although no job of any kind uses a reader's
    key (ADR 0043 step 7 retired the only kind that did)."""
    from app.hosted import worker as worker_module

    for name in ("HOSTED_CREDENTIAL_MASTER_KEY", "HOSTED_CREDENTIAL_KEY_VERSION",
                 "HOSTED_CREDENTIAL_PREVIOUS_MASTER_KEYS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOSTED_WORKER_DSN", "postgresql://unused.invalid/db")
    built = []

    def repository(*args, **kwargs):
        built.append(FakeRepository(None))
        assert args == ("postgresql://unused.invalid/db",) and not kwargs
        return built[-1]

    monkeypatch.setattr(worker_module, "PostgresWorkerRepository", repository)
    assert worker_module.main(["--handler", "builtins:print", "--once"]) == 0
    assert [call[0] for call in built[0].calls] == ["check_runtime_role", "claim"]
    assert not [name for name in vars(worker_module) if "credential" in name.lower()]


def test_worker_runs_claimed_job_to_success() -> None:
    repository = FakeRepository(_claim())
    handled = []
    worker = DurableWorker(repository, lambda claim, repo: handled.append(claim.job_id), "worker")
    assert worker.run_once()
    assert handled == [repository.claim.job_id]
    assert [call[0] for call in repository.calls] == ["claim", "start", "succeed"]


def test_worker_persists_only_reviewed_failure_classification() -> None:
    secret = "provider-key-and-private-chapter-text"
    repository = FakeRepository(_claim())

    def fail(_claim, _repository):
        raise RuntimeError(secret)

    assert DurableWorker(repository, fail, "worker").run_once()
    call = repository.calls[-1]
    assert call[0] == "fail"
    assert call[1] == SanitizedFailure("internal_error", retryable=True)
    assert secret not in repr(repository.calls)


def test_worker_honors_reviewed_retryability_and_cancelled_start() -> None:
    repository = FakeRepository(_claim())

    def fail(_claim, _repository):
        raise HandledJobFailure(SanitizedFailure("source_missing", retryable=False))

    assert DurableWorker(repository, fail, "worker").run_once()
    assert repository.calls[-1][1] == SanitizedFailure("source_missing", retryable=False)

    cancelled = FakeRepository(_claim(), started=False)
    assert DurableWorker(cancelled, fail, "worker").run_once()
    assert [call[0] for call in cancelled.calls] == ["claim", "start"]


def test_worker_claims_only_the_kinds_its_handler_runs() -> None:
    """The break it catches: a companion-only worker claiming extraction jobs it cannot run (and
    failing them), or a handler without ``kinds`` losing the claim of every kind."""
    class NotesOnly:
        kinds = frozenset({"chapter_notes"})

        def __call__(self, claim, repository):
            return None

    limited = FakeRepository(None)
    assert DurableWorker(limited, NotesOnly(), "worker").run_once() is False
    assert limited.calls[0][1]["kinds"] == frozenset({"chapter_notes"})
    everything = FakeRepository(None)
    assert DurableWorker(everything, lambda claim, repo: None, "worker").run_once() is False
    assert "kinds" not in everything.calls[0][1]
