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


# The removed settings of readers paying with their own key (ADR 0043 step 8).
REMOVED_KEY_SETTINGS = ("HOSTED_READER_KEYS", "HOSTED_CREDENTIAL_MASTER_KEY",
                        "HOSTED_CREDENTIAL_KEY_VERSION", "HOSTED_CREDENTIAL_PREVIOUS_MASTER_KEYS",
                        "HOSTED_PROVIDER_VALIDATION_TIMEOUT_SECONDS")


def test_the_worker_starts_and_runs_without_a_credential_key(monkeypatch) -> None:
    """The break it catches: the worker still asking for the reader-key decrypter at start, so it
    cannot run without HOSTED_CREDENTIAL_MASTER_KEY although no job of any kind uses a reader's
    key (ADR 0043 step 7 retired the only kind that did; step 8 removed the setting)."""
    from app.hosted import worker as worker_module

    for name in REMOVED_KEY_SETTINGS:
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


def test_the_companion_dispatcher_starts_without_the_reader_key_settings(monkeypatch,
                                                                          tmp_path) -> None:
    """The break it catches: the worker's companion handler loading hosted settings that still
    require HOSTED_CREDENTIAL_MASTER_KEY and its version, so a worker given only what the platform
    needs stops before its first claim. Only the role check is stood in for (no database)."""
    import base64

    from app.config import Settings
    from app.hosted.companion.jobs import CompanionJobs
    from app.hosted.tenant import repository as tenant_repository

    # Read nothing but this process's environment: never a settings file on this machine.
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    for name in REMOVED_KEY_SETTINGS:
        monkeypatch.delenv(name, raising=False)
    for name, value in {
        "DEPLOYMENT_MODE": "hosted", "TRUSTED_HOSTS": "reader.example",
        "HOSTED_AUTH_DSN": "postgresql://unused.invalid/db",
        "HOSTED_TENANT_DSN": "postgresql://unused.invalid/db",
        "OIDC_ISSUER": "https://idp.example", "OIDC_CLIENT_ID": "mothli",
        "OIDC_CLIENT_SECRET": "not-a-real-secret",
        "OIDC_REDIRECT_URI": "https://reader.example/api/auth/callback",
        "HOSTED_STORAGE_BACKEND": "filesystem",
        "HOSTED_STORAGE_FILESYSTEM_ROOT": str(tmp_path / "objects"),
        "HOSTED_STORAGE_FILESYSTEM_KEY": base64.b64encode(b"t" * 32).decode("ascii"),
        "READER_PAGE_CONTEXT": "true", "READER_CONVERSATION_JEV": "true", "JEV_ENABLED": "true",
        "OPENAI_API_KEY": "sk-not-a-real-key", "TYPESAFE_API_KEY": "ts-not-a-real-key",
        "HOSTED_READER_USD_PER_DAY": "0.10", "HOSTED_PLATFORM_USD_PER_DAY": "5.00",
        "DATA_DIR": str(tmp_path / "data"),
    }.items():
        monkeypatch.setenv(name, value)
    checked = []

    class _Tenant:
        def __init__(self, dsn):
            self.dsn = dsn

        async def check_runtime_role(self):
            checked.append(self.dsn)

    monkeypatch.setattr(tenant_repository, "PostgresTenantRepository", _Tenant)
    # The process-wide connection and spend limits it sets are left as they are for other tests.
    monkeypatch.setattr("app.hosted.companion.set_connection_limit", lambda limit: None)
    monkeypatch.setattr("app.hosted.companion.spend.set_spend_limits", lambda **caps: None)
    dispatcher = CompanionJobs.from_environment()
    try:
        assert checked == ["postgresql://unused.invalid/db"]
        assert dispatcher.kinds == {"chapter_notes", "recollect"}
    finally:
        dispatcher._client.jev.close()


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
