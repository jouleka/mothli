"""Hosted readiness, /api/health/ready (ADR 0041 part 9), with stand-in database reads."""

from __future__ import annotations

import logging
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app import main
from app.config import Settings
from app.main import create_app, expected_schema_marker, schema_marker

SECRET_DSN = "postgresql://tenant:a-password-that-must-not-show@db.internal:5432/mothli"


def settings() -> Settings:
    return Settings(
        _env_file=None,
        deployment_mode="hosted",
        hosted_auth_dsn=SECRET_DSN,
        hosted_tenant_dsn=SECRET_DSN,
        oidc_issuer="https://idp.example",
        oidc_client_id="mothli",
        oidc_client_secret="not-a-real-secret",
        oidc_redirect_uri="https://reader.example/api/auth/callback",
    )


class Repository:
    def __init__(self) -> None:
        self.failure: Exception | None = None
        self.calls = 0

    async def check_runtime_role(self) -> None:
        self.calls += 1
        if self.failure is not None:
            raise self.failure


def app_with(marker, auth=None, tenant=None):
    storage = SimpleNamespace(close=lambda: None)

    async def read_marker():
        if isinstance(marker, Exception):
            raise marker
        return marker

    return create_app(settings(), auth_repository=auth or Repository(),
                      tenant_repository=tenant or Repository(), oidc_client=object(),
                      object_storage=storage, schema_marker_reader=read_marker)


def test_ready_only_with_the_expected_migrations_and_roles(monkeypatch) -> None:
    """The break it catches: a server reported ready while its database is behind (or ahead of)
    the code, or while a role holds other privileges than reviewed."""
    monkeypatch.setattr(main, "_READINESS_CACHE_SECONDS", 0.0)
    auth, tenant = Repository(), Repository()
    with TestClient(app_with(expected_schema_marker(), auth, tenant)) as client:
        response = client.get("/api/health/ready")
        assert (response.status_code, response.json()) == (200, {"status": "ready"})
        assert response.headers["cache-control"] == "private, no-store"
        tenant.failure = RuntimeError("tenant runtime role privileges are not the allow-list")
        response = client.get("/api/health/ready")
        assert (response.status_code, response.json()) == (503, {"status": "not_ready"})
        tenant.failure = None
        assert client.get("/api/health/ready").status_code == 200
        assert client.get("/api/health/live").json() == {"status": "ok"}
    older = schema_marker([(1, "identity_and_accounts", "0" * 64)])
    with TestClient(app_with(older)) as client:
        assert client.get("/api/health/ready").status_code == 503
    with TestClient(app_with(None)) as client:
        assert client.get("/api/health/ready").status_code == 503


def test_not_ready_says_nothing_about_why(monkeypatch, caplog) -> None:
    """The break it catches: a database error (host, role or DSN) echoed to anyone who asks."""
    monkeypatch.setattr(main, "_READINESS_CACHE_SECONDS", 0.0)
    failure = OSError(f"connection to {SECRET_DSN} refused")
    caplog.set_level(logging.WARNING)
    with TestClient(app_with(failure)) as client:
        response = client.get("/api/health/ready")
    assert response.status_code == 503
    assert response.text == '{"status":"not_ready"}'
    assert "a-password-that-must-not-show" not in caplog.text


def test_one_check_serves_probes_for_a_few_seconds() -> None:
    """The break it catches: each probe running every startup check again, so a stream of
    requests to a public address becomes load on the database."""
    tenant = Repository()
    with TestClient(app_with(expected_schema_marker(), tenant=tenant)) as client:
        for _ in range(5):
            assert client.get("/api/health/ready").status_code == 200
    assert tenant.calls == 2  # once at startup, once for all five probes


def test_the_schema_marker_names_one_exact_migration_history() -> None:
    rows = [(1, "a", "1" * 64), (2, "b", "2" * 64)]
    assert schema_marker(rows).startswith("0002-")
    assert schema_marker(rows) != schema_marker([(1, "a", "1" * 64), (2, "b", "3" * 64)])
    assert schema_marker(rows) != schema_marker(rows[:1])
    from app.hosted.migrations import discover_migrations

    assert expected_schema_marker().startswith(f"{len(discover_migrations()):04d}-")
