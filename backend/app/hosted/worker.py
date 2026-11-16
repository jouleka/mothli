"""Separately runnable durable hosted-job worker (LIT-44)."""

from __future__ import annotations

import argparse
import importlib
import math
import os
import socket
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

from app.hosted.jobs import ClaimedJob, LostLeaseError, PostgresWorkerRepository, SanitizedFailure
from app.hosted.credentials import (
    CredentialUnavailableError,
    build_credential_cipher_from_environment,
)
from app.hosted.limits import LimitExceededError


class JobHandler(Protocol):
    def __call__(self, claim: ClaimedJob, repository: PostgresWorkerRepository) -> None: ...


class HandledJobFailure(RuntimeError):
    """A reviewed failure classification; exception text is never persisted."""

    def __init__(self, failure: SanitizedFailure) -> None:
        super().__init__(failure.code)
        self.failure = failure


@dataclass(slots=True)
class DurableWorker:
    repository: PostgresWorkerRepository
    handler: JobHandler
    worker_id: str
    lease_for: timedelta = timedelta(minutes=2)
    retry_after: timedelta = timedelta(seconds=15)

    def run_once(self) -> bool:
        # A handler that runs only some kinds says which (``kinds``); this worker claims no other.
        kinds = getattr(self.handler, "kinds", None)
        if kinds is None:
            claim = self.repository.claim_next(worker_id=self.worker_id, lease_for=self.lease_for)
        else:
            claim = self.repository.claim_next(
                worker_id=self.worker_id, lease_for=self.lease_for, kinds=frozenset(kinds)
            )
        if claim is None:
            return False
        try:
            started = self.repository.start(claim)
        except LostLeaseError:
            return True
        if not started:
            return True
        try:
            self.handler(claim, self.repository)
        except LostLeaseError:
            return True
        except HandledJobFailure as exc:
            try:
                self.repository.fail(claim, failure=exc.failure, retry_after=self.retry_after)
            except LostLeaseError:
                pass
        except CredentialUnavailableError:
            try:
                self.repository.fail(
                    claim,
                    failure=SanitizedFailure("provider_rejected", retryable=False),
                    retry_after=self.retry_after,
                )
            except LostLeaseError:
                pass
        except LimitExceededError:
            try:
                self.repository.fail(
                    claim,
                    failure=SanitizedFailure("budget_exceeded", retryable=False),
                    retry_after=self.retry_after,
                )
            except LostLeaseError:
                pass
        except Exception:
            # Never persist or log raw exceptions: providers and parsers can embed source text or keys.
            try:
                self.repository.fail(
                    claim,
                    failure=SanitizedFailure("internal_error", retryable=True),
                    retry_after=self.retry_after,
                )
            except LostLeaseError:
                pass
        else:
            try:
                self.repository.succeed(claim)
            except LostLeaseError:
                pass
        return True

    def run_forever(self, *, idle_for: timedelta = timedelta(seconds=1)) -> None:
        idle_seconds = idle_for.total_seconds()
        if not math.isfinite(idle_seconds) or idle_seconds <= 0 or idle_seconds > 60:
            raise ValueError("idle duration must be between zero and sixty seconds")
        while True:
            if not self.run_once():
                time.sleep(idle_seconds)


def _load_handler(spec: str) -> JobHandler:
    module_name, separator, attribute = spec.partition(":")
    if not separator or not module_name or not attribute:
        raise ValueError("handler must use module.path:callable syntax")
    value = getattr(importlib.import_module(module_name), attribute)
    if not callable(value):
        raise TypeError("configured handler is not callable")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the durable hosted ingestion worker")
    parser.add_argument("--dsn-env", default="HOSTED_WORKER_DSN")
    parser.add_argument("--handler", default=os.environ.get("HOSTED_JOB_HANDLER"))
    parser.add_argument("--worker-id", default=f"{socket.gethostname()}:{os.getpid()}")
    parser.add_argument("--lease-seconds", type=int, default=120)
    parser.add_argument("--retry-seconds", type=int, default=15)
    parser.add_argument("--idle-seconds", type=float, default=1.0)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    dsn = os.environ.get(args.dsn_env)
    if not dsn:
        parser.error(f"environment variable {args.dsn_env!r} is not set")
    if not args.handler:
        parser.error("--handler or HOSTED_JOB_HANDLER is required")
    repository = PostgresWorkerRepository(
        dsn, build_credential_cipher_from_environment(os.environ)
    )
    repository.check_runtime_role()
    handler = _load_handler(args.handler)
    # A handler that works for owners through other settings builds itself from this process's
    # environment once, at startup, so a missing or refused setting stops the worker here. The
    # companion dispatcher (ADR 0041 part 6) reads HOSTED_TENANT_DSN and the platform's model
    # settings this way and checks the tenant role before any job is claimed.
    from_environment = getattr(handler, "from_environment", None)
    if from_environment is not None:
        handler = from_environment()
    worker = DurableWorker(
        repository=repository,
        handler=handler,
        worker_id=args.worker_id,
        lease_for=timedelta(seconds=args.lease_seconds),
        retry_after=timedelta(seconds=args.retry_seconds),
    )
    if args.once:
        worker.run_once()
    else:
        worker.run_forever(idle_for=timedelta(seconds=args.idle_seconds))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
