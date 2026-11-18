"""The deployment's role script against a real PostgreSQL (ADR 0041 part 9).

A fresh database is provisioned exactly as on the server: roles, migrations applied by the
non-superuser migration owner, grants, the schema marker, then the app's own startup checks.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import uuid

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import conninfo, sql

from app.config import Settings
from app.hosted.auth.repository import AuthConfigurationError, PostgresAuthRepository
from app.hosted.jobs import PostgresWorkerRepository, WorkerConfigurationError
from app.hosted.purge import PostgresAccountPurge, PurgeConfigurationError
from app.hosted.tenant.repository import PostgresTenantRepository
from app.main import _read_schema_marker, create_app, expected_schema_marker
from tests.hosted.test_deploy import load_roles

pytestmark = pytest.mark.postgres


@pytest.fixture()
def provisioned():
    admin_dsn = os.environ.get("TEST_POSTGRES_DSN")
    if not admin_dsn:
        pytest.skip("TEST_POSTGRES_DSN is required for the real PostgreSQL suite")
    roles = load_roles()
    prefix = f"t{uuid.uuid4().hex[:12]}"
    name = f"{prefix}_deploy"
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    parts = conninfo.conninfo_to_dict(admin_dsn)
    env = {role.password_env: secrets.token_hex(32) for role in roles.ALL_ROLES}
    env.update(MOTHLI_DB_HOST=parts.get("host", "127.0.0.1"),
               MOTHLI_DB_PORT=str(parts.get("port", "5432")), MOTHLI_DB_NAME=name)
    database = conninfo.make_conninfo(admin_dsn, dbname=name)
    try:
        roles.prepare(database, env, prefix)
        roles.migrate(roles.role_dsn(env, prefix, roles.OWNER))
        roles.grant(database, prefix)
        marker = roles.mark(database)
        roles.verify(database, env, prefix)
        dsn = {role.key: roles.role_dsn(env, prefix, role) for role in roles.RUNTIME_ROLES}
        yield roles, prefix, env, database, dsn, marker
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                          "WHERE datname = %s AND pid <> pg_backend_pid()", (name,))
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name)))
            for role in roles.ALL_ROLES:
                admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(
                    sql.Identifier(roles.role_name(prefix, role))))


def run(database: str, statement: str, role: str) -> None:
    with psycopg.connect(database, autocommit=True) as conn:
        conn.execute(sql.SQL(statement).format(sql.Identifier(role)))


def test_the_provisioned_roles_pass_the_apps_own_startup_checks(provisioned) -> None:
    """The break it catches: a role script that grants what the code does not expect, so the
    server refuses to start, or a migration that needs the superuser to run."""
    roles, prefix, env, database, dsn, marker = provisioned
    asyncio.run(PostgresAuthRepository(dsn["auth"]).check_runtime_role())
    asyncio.run(PostgresTenantRepository(dsn["tenant"]).check_runtime_role())
    PostgresWorkerRepository(dsn["worker"]).check_runtime_role()
    PostgresAccountPurge(dsn["purge"]).check_runtime_role()
    # The tables belong to the migration owner, not the superuser.
    with psycopg.connect(database) as conn:
        owners = {row[0] for row in conn.execute(
            "SELECT DISTINCT tableowner FROM pg_tables WHERE schemaname = 'public'").fetchall()}
    assert owners == {roles.role_name(prefix, roles.OWNER)}
    # The readiness marker reads, as the tenant role, exactly what this code expects.
    assert marker == expected_schema_marker()
    assert asyncio.run(_read_schema_marker(dsn["tenant"])) == expected_schema_marker()


def test_one_grant_more_or_less_refuses_and_the_script_restores_exactly(provisioned) -> None:
    """The break it catches: a hand-made grant surviving a deployment, or a role check that
    would accept one privilege more (or fewer) than reviewed."""
    roles, prefix, env, database, dsn, _marker = provisioned
    tenant = roles.role_name(prefix, next(r for r in roles.RUNTIME_ROLES if r.key == "tenant"))
    worker = roles.role_name(prefix, next(r for r in roles.RUNTIME_ROLES if r.key == "worker"))
    auth = roles.role_name(prefix, next(r for r in roles.RUNTIME_ROLES if r.key == "auth"))
    purge = roles.role_name(prefix, next(r for r in roles.RUNTIME_ROLES if r.key == "purge"))

    def check_purge() -> None:
        PostgresAccountPurge(dsn["purge"]).check_runtime_role()
    cases = [
        ("GRANT SELECT ON platform_spend_days TO {}", tenant,
         lambda: asyncio.run(PostgresTenantRepository(dsn["tenant"]).check_runtime_role()),
         AuthConfigurationError),
        ("REVOKE SELECT ON themes FROM {}", tenant,
         lambda: asyncio.run(PostgresTenantRepository(dsn["tenant"]).check_runtime_role()),
         AuthConfigurationError),
        ("GRANT SELECT ON books TO {}", worker,
         lambda: PostgresWorkerRepository(dsn["worker"]).check_runtime_role(),
         WorkerConfigurationError),
        ("GRANT SELECT ON books TO {}", auth,
         lambda: asyncio.run(PostgresAuthRepository(dsn["auth"]).check_runtime_role()),
         AuthConfigurationError),
        ("GRANT UPDATE ON books TO {}", purge, check_purge, PurgeConfigurationError),
        ("GRANT SELECT ON platform_spend_days TO {}", purge, check_purge, PurgeConfigurationError),
        ("REVOKE DELETE ON chapter_notes FROM {}", purge, check_purge, PurgeConfigurationError),
        ("REVOKE EXECUTE ON FUNCTION companion_platform_reserve(numeric,numeric) FROM {}", tenant,
         lambda: roles.verify(database, env, prefix), roles.ProvisioningError),
    ]
    for statement, role, check, refusal in cases:
        run(database, statement, role)
        with pytest.raises(refusal):
            check()
        roles.grant(database, prefix)
        roles.verify(database, env, prefix)


def test_readiness_follows_the_database(provisioned) -> None:
    """The break it catches: ready reported while the migrations recorded in the database are not
    this code's (a restored older backup, a half-finished deployment)."""
    roles, prefix, env, database, dsn, _marker = provisioned
    settings = Settings(
        _env_file=None, deployment_mode="hosted",
        hosted_auth_dsn=dsn["auth"], hosted_tenant_dsn=dsn["tenant"],
        oidc_issuer="https://idp.example", oidc_client_id="mothli",
        oidc_client_secret="not-a-real-secret",
        oidc_redirect_uri="https://reader.example/api/auth/callback",
    )
    import app.main as main

    original = main._READINESS_CACHE_SECONDS
    main._READINESS_CACHE_SECONDS = 0.0
    try:
        storage = type("Storage", (), {"close": lambda self: None})()
        with TestClient(create_app(settings, oidc_client=object(), object_storage=storage)) as client:
            assert client.get("/api/health/ready").json() == {"status": "ready"}
            with psycopg.connect(database, autocommit=True) as conn:
                conn.execute(sql.SQL("ALTER DATABASE {} SET {} = {}").format(
                    sql.Identifier(env["MOTHLI_DB_NAME"]), sql.SQL(roles.SCHEMA_MARKER_SETTING),
                    sql.Literal("0019-0000000000000000")))
            response = client.get("/api/health/ready")
            assert (response.status_code, response.json()) == (503, {"status": "not_ready"})
            roles.mark(database)
            assert client.get("/api/health/ready").status_code == 200
    finally:
        main._READINESS_CACHE_SECONDS = original
