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
            # With the library command's role, which a test below makes as an old deployment had.
            for role in [*(roles.role_name(prefix, role) for role in roles.ALL_ROLES),
                         f"{prefix}_library"]:
                admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


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
    billing = roles.role_name(prefix, next(r for r in roles.RUNTIME_ROLES if r.key == "billing"))

    def check_purge() -> None:
        PostgresAccountPurge(dsn["purge"]).check_runtime_role()
    cases = [
        ("GRANT SELECT ON platform_spend_days TO {}", tenant,
         lambda: asyncio.run(PostgresTenantRepository(dsn["tenant"]).check_runtime_role()),
         AuthConfigurationError),
        # (ADR 0044 step 7: the tenant role holds nothing on a story memory table.)
        ("GRANT SELECT ON themes TO {}", tenant,
         lambda: asyncio.run(PostgresTenantRepository(dsn["tenant"]).check_runtime_role()),
         AuthConfigurationError),
        # (ADR 0044 step 3 gave the worker SELECT on books; it must never read a stored file.)
        ("GRANT SELECT ON source_objects TO {}", worker,
         lambda: PostgresWorkerRepository(dsn["worker"]).check_runtime_role(),
         WorkerConfigurationError),
        # The worker only reads the money records; every job's spend is written as the tenant.
        ("GRANT INSERT ON cost_ledger TO {}", worker,
         lambda: PostgresWorkerRepository(dsn["worker"]).check_runtime_role(),
         WorkerConfigurationError),
        ("GRANT UPDATE ON cost_reservations TO {}", worker,
         lambda: PostgresWorkerRepository(dsn["worker"]).check_runtime_role(),
         WorkerConfigurationError),
        ("GRANT SELECT ON books TO {}", auth,
         lambda: asyncio.run(PostgresAuthRepository(dsn["auth"]).check_runtime_role()),
         AuthConfigurationError),
        # (ADR 0044 step 8 gave the purge UPDATE on books; it never adds a book.)
        ("GRANT INSERT ON books TO {}", purge, check_purge, PurgeConfigurationError),
        ("GRANT SELECT ON platform_spend_days TO {}", purge, check_purge, PurgeConfigurationError),
        ("REVOKE DELETE ON chapter_notes FROM {}", purge, check_purge, PurgeConfigurationError),
        ("REVOKE EXECUTE ON FUNCTION companion_platform_reserve(numeric,numeric) FROM {}", tenant,
         lambda: roles.verify(database, env, prefix), roles.ProvisioningError),
        # ADR 0045 step 3: the billing role, which only Paddle's message route uses, has no
        # startup check of its own; the deployment's check holds it to its list both ways.
        ("GRANT SELECT ON users TO {}", billing,
         lambda: roles.verify(database, env, prefix), roles.ProvisioningError),
        ("GRANT DELETE ON paddle_events TO {}", billing,
         lambda: roles.verify(database, env, prefix), roles.ProvisioningError),
        ("REVOKE UPDATE ON reader_plans FROM {}", billing,
         lambda: roles.verify(database, env, prefix), roles.ProvisioningError),
        ("REVOKE EXECUTE ON FUNCTION paddle_reader(text,uuid) FROM {}", billing,
         lambda: roles.verify(database, env, prefix), roles.ProvisioningError),
        # Step 4: its checkouts, never deleted by it, and the regular check's list.
        ("GRANT DELETE ON paddle_checkouts TO {}", billing,
         lambda: roles.verify(database, env, prefix), roles.ProvisioningError),
        ("REVOKE UPDATE ON paddle_checkouts FROM {}", billing,
         lambda: roles.verify(database, env, prefix), roles.ProvisioningError),
        ("REVOKE EXECUTE ON FUNCTION paddle_plans_to_check(integer) FROM {}", billing,
         lambda: roles.verify(database, env, prefix), roles.ProvisioningError),
    ]
    for statement, role, check, refusal in cases:
        run(database, statement, role)
        with pytest.raises(refusal):
            check()
        roles.grant(database, prefix)
        roles.verify(database, env, prefix)


def test_a_deployment_makes_no_library_role_drops_an_old_one_and_has_its_notes_account(
        provisioned) -> None:
    """ADR 0044 step 6: the library command and its database role are gone; the role script, not
    a migration, drops the role where an earlier deployment made it. The break it catches: a
    fresh deployment that still makes the role, or a server deployed before this step keeping it,
    with its sign-in and its grants on ``users`` and ``notes_account``, after the next
    deployment; or a fresh deployment whose notes account (the only account notes are built for,
    made by the migrations) is missing, or would need the removed command to exist."""
    roles, prefix, env, database, dsn, _marker = provisioned
    old = f"{prefix}_library"

    def exists() -> bool:
        with psycopg.connect(database) as conn:
            return conn.execute("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = %s)",
                                (old,)).fetchone()[0]

    assert not exists()
    with psycopg.connect(database) as conn:
        [(account,)] = conn.execute("SELECT account_id FROM notes_account").fetchall()
        assert conn.execute("SELECT status FROM users WHERE id = %s",
                            (account,)).fetchone() == ("suspended",)
        assert conn.execute("SELECT count(*) FROM owner_limits WHERE owner_id = %s",
                            (account,)).fetchone() == (1,)

    # A server deployed before this step: the role, signed in to, with what it was granted.
    with psycopg.connect(database, autocommit=True) as conn:
        role = sql.Identifier(old)
        conn.execute(sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOINHERIT NOBYPASSRLS "
                             "PASSWORD {}").format(role, sql.Literal(secrets.token_hex(32))))
        conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
            sql.Identifier(env["MOTHLI_DB_NAME"]), role))
        conn.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(role))
        conn.execute(sql.SQL("GRANT INSERT ON users TO {}").format(role))
        conn.execute(sql.SQL("GRANT SELECT, INSERT ON notes_account TO {}").format(role))
    assert exists()
    # The next deployment, step by step as `deploy` runs it.
    roles.prepare(database, env, prefix)
    roles.migrate(roles.role_dsn(env, prefix, roles.OWNER))
    roles.grant(database, prefix)
    roles.mark(database)
    roles.verify(database, env, prefix)
    assert not exists()
    with psycopg.connect(database) as conn:
        assert conn.execute("SELECT account_id FROM notes_account").fetchall() == [(account,)]


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
