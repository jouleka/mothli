"""The hosted database roles and their grants, as one script (ADR 0041 part 9).

The tenant and worker roles' privileges are read from ``app/hosted/tenant/grants.py``, the lists
the app's own startup checks read, so they cannot drift. The auth list is the one its repository
checks at startup (``app/hosted/auth/repository.py``); the ``verify`` step runs those very checks,
so a difference stops the deployment.

Run as the database superuser, by the deployment's one-shot ``migrate`` service:

    python scripts/hosted_roles.py deploy     # prepare, migrate, grant, mark, verify
    python scripts/hosted_roles.py prepare    # roles, extensions, schema rights (before a restore)
    python scripts/hosted_roles.py sql        # print the grant statements, for review

Passwords come from the environment (see deploy/.env.example) and are never printed. Every
runtime role ends with exactly the privileges listed here: anything else it holds on a table is
revoked first, in the same transaction.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import psycopg
from psycopg import conninfo, sql

# Run as a file (python scripts/hosted_roles.py), the app package is one folder up.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.hosted.tenant.grants import (  # noqa: E402
    LIBRARY_TABLE_PRIVILEGES,
    PURGE_OTHER_TABLE_PRIVILEGES,
    TENANT_FUNCTION_PRIVILEGES,
    TENANT_TABLE_PRIVILEGES,
    WORKER_TABLE_PRIVILEGES,
    purge_table_privileges,
)

# ADR 0020: the sign-in role (app/hosted/auth/repository.py check_runtime_role).
AUTH_TABLE_PRIVILEGES: dict[str, frozenset[str]] = {
    "users": frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"}),
    "external_identities": frozenset({"SELECT", "INSERT", "UPDATE"}),
    "sessions": frozenset({"SELECT", "INSERT", "UPDATE"}),
    "oidc_login_attempts": frozenset({"SELECT", "INSERT", "DELETE"}),
    "audit_events": frozenset({"INSERT"}),
}

# ADR 0029: reads and expires the audit log (python -m app.hosted.audit). Never in web or worker.
AUDIT_TABLE_PRIVILEGES: dict[str, frozenset[str]] = {
    "audit_events": frozenset({"SELECT", "DELETE"}),
}

# ADR 0027: shows and sets a reader's limits (python -m app.hosted.limits). Never in web or worker.
OPERATOR_TABLE_PRIVILEGES: dict[str, frozenset[str]] = {
    "owner_limits": frozenset({"SELECT", "UPDATE"}),
    "books": frozenset({"SELECT"}),
    "source_objects": frozenset({"SELECT"}),
    "jobs": frozenset({"SELECT"}),
    "cost_ledger": frozenset({"SELECT"}),
    "cost_reservations": frozenset({"SELECT"}),
}


@dataclass(frozen=True)
class Role:
    key: str                      # the role's suffix: <prefix>_<key>
    password_env: str             # the environment variable holding its password
    bypass_rls: bool
    tables: dict[str, frozenset[str]]
    functions: dict[str, frozenset[str]]
    # The purge role (ADR 0041 part 8) also gets SELECT and DELETE on every table that has an
    # ``owner_id`` column, read from the database's catalogue when the grants are made.
    owner_tables: bool = False


# The migration owner owns every table and function and is a runtime role of nothing. It bypasses
# row-level security as the tests' superuser does, so a migration that changes existing rows sees
# them all (the tables FORCE row security, which would otherwise hide every row from it).
OWNER = Role("owner", "MOTHLI_DB_OWNER_PASSWORD", True, {}, {})
RUNTIME_ROLES: tuple[Role, ...] = (
    Role("auth", "MOTHLI_DB_AUTH_PASSWORD", True, AUTH_TABLE_PRIVILEGES, {}),
    Role("tenant", "MOTHLI_DB_TENANT_PASSWORD", False, TENANT_TABLE_PRIVILEGES,
         TENANT_FUNCTION_PRIVILEGES),
    Role("worker", "MOTHLI_DB_WORKER_PASSWORD", True, WORKER_TABLE_PRIVILEGES, {}),
    Role("audit", "MOTHLI_DB_AUDIT_PASSWORD", True, AUDIT_TABLE_PRIVILEGES, {}),
    Role("operator", "MOTHLI_DB_OPERATOR_PASSWORD", True, OPERATOR_TABLE_PRIVILEGES, {}),
    # ADR 0041 part 8: erases accounts 30 days after the reader asked (python -m app.hosted.purge).
    # Never in web or worker.
    Role("purge", "MOTHLI_DB_PURGE_PASSWORD", True, PURGE_OTHER_TABLE_PRIVILEGES, {},
         owner_tables=True),
    # ADR 0042 part 5: makes the library account and reads it (python -m
    # app.hosted.library_memory), with row security on. Never in web or worker.
    Role("library", "MOTHLI_DB_LIBRARY_PASSWORD", False, LIBRARY_TABLE_PRIVILEGES, {}),
)
ALL_ROLES: tuple[Role, ...] = (OWNER, *RUNTIME_ROLES)
EXTENSIONS = ("pgcrypto", "vector")
SCHEMA_MARKER_SETTING = "mothli.schema_version"

# Long, and safe inside a connection URL without escaping (the compose file builds DSNs from it).
_PASSWORD_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
_PREFIX_RE = re.compile(r"^[a-z][a-z0-9_]{0,40}$")


class ProvisioningError(RuntimeError):
    """A refusal with a message that names a setting or a step, never a value."""


def role_name(prefix: str, role: Role) -> str:
    return f"{prefix}_{role.key}"


def grant_statements(prefix: str, role: Role, owner_tables=()) -> list[sql.Composable]:
    """Exactly this role's privileges: revoke everything on the schema's tables, then grant.
    ``owner_tables`` are the tables with an ``owner_id`` column, for the purge role."""
    name = sql.Identifier(role_name(prefix, role))
    tables = purge_table_privileges(owner_tables) if role.owner_tables else role.tables
    statements: list[sql.Composable] = [
        sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {}").format(name),
        sql.SQL("REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {}").format(name),
        sql.SQL("REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM {}").format(name),
        sql.SQL("REVOKE CREATE ON SCHEMA public FROM {}").format(name),
        sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(name),
    ]
    for table, privileges in sorted(tables.items()):
        statements.append(sql.SQL("GRANT {} ON TABLE {} TO {}").format(
            sql.SQL(", ").join(sql.SQL(item) for item in sorted(privileges)),
            sql.Identifier("public", table), name))
    for function, privileges in sorted(role.functions.items()):
        statements.append(sql.SQL("GRANT {} ON FUNCTION {} TO {}").format(
            sql.SQL(", ").join(sql.SQL(item) for item in sorted(privileges)),
            sql.SQL("public.") + sql.SQL(function), name))
    return statements


def render(statements: list[sql.Composable]) -> str:
    return "\n".join(statement.as_string(None) + ";" for statement in statements)


def _password(env: dict[str, str], variable: str) -> str:
    value = env.get(variable, "")
    if not value:
        raise ProvisioningError(f"{variable} is not set")
    if not _PASSWORD_RE.fullmatch(value):
        raise ProvisioningError(
            f"{variable} must be 32 to 128 letters, digits, '-' or '_' (use: openssl rand -hex 32)")
    return value


def _admin_dsn(env: dict[str, str]) -> str:
    return conninfo.make_conninfo(
        host=env.get("MOTHLI_DB_HOST", "db"), port=env.get("MOTHLI_DB_PORT", "5432"),
        dbname=env.get("MOTHLI_DB_NAME", "mothli"), user=env.get("POSTGRES_USER", "postgres"),
        password=_required(env, "POSTGRES_PASSWORD"))


def _required(env: dict[str, str], variable: str) -> str:
    value = env.get(variable, "")
    if not value:
        raise ProvisioningError(f"{variable} is not set")
    return value


def role_dsn(env: dict[str, str], prefix: str, role: Role) -> str:
    return conninfo.make_conninfo(
        host=env.get("MOTHLI_DB_HOST", "db"), port=env.get("MOTHLI_DB_PORT", "5432"),
        dbname=env.get("MOTHLI_DB_NAME", "mothli"), user=role_name(prefix, role),
        password=_password(env, role.password_env))


def prepare(admin_dsn: str, env: dict[str, str], prefix: str) -> None:
    """Create or reset the login roles, the extensions and the migration owner's schema right."""
    passwords = {role.key: _password(env, role.password_env) for role in ALL_ROLES}
    with psycopg.connect(admin_dsn, autocommit=True) as conn, conn.transaction():
        database = conn.execute("SELECT current_database()").fetchone()[0]
        for role in ALL_ROLES:
            name = sql.Identifier(role_name(prefix, role))
            attributes = sql.SQL(
                "LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOINHERIT "
                + ("BYPASSRLS" if role.bypass_rls else "NOBYPASSRLS"))
            exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s",
                                  (role_name(prefix, role),)).fetchone()
            verb = sql.SQL("ALTER ROLE") if exists else sql.SQL("CREATE ROLE")
            conn.execute(sql.SQL("{} {} {} PASSWORD {}").format(
                verb, name, attributes, sql.Literal(passwords[role.key])))
            # The startup checks refuse a runtime role that is a member of any role.
            memberships = conn.execute(
                """SELECT parent.rolname FROM pg_auth_members AS membership
                   JOIN pg_roles AS parent ON parent.oid = membership.roleid
                   JOIN pg_roles AS member ON member.oid = membership.member
                   WHERE member.rolname = %s""", (role_name(prefix, role),)).fetchall()
            for (parent,) in memberships:
                conn.execute(sql.SQL("REVOKE {} FROM {}").format(sql.Identifier(parent), name))
        for extension in EXTENSIONS:
            conn.execute(sql.SQL("CREATE EXTENSION IF NOT EXISTS {}").format(
                sql.Identifier(extension)))
        owner = sql.Identifier(role_name(prefix, OWNER))
        conn.execute(sql.SQL("GRANT USAGE, CREATE ON SCHEMA public TO {}").format(owner))
        conn.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(
            sql.Identifier(database)))
        for role in ALL_ROLES:
            conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                sql.Identifier(database), sql.Identifier(role_name(prefix, role))))


def migrate(owner_dsn: str) -> None:
    from app.hosted.migrations import apply_migrations

    apply_migrations(owner_dsn)


def grant(admin_dsn: str, prefix: str) -> None:
    from app.hosted.purge import owner_tables

    with psycopg.connect(admin_dsn, autocommit=True) as conn, conn.transaction():
        tables = owner_tables(conn)
        for role in RUNTIME_ROLES:
            for statement in grant_statements(prefix, role, tables):
                conn.execute(statement)


def mark(admin_dsn: str) -> str:
    """Record, from the database's own migration table, the schema the readiness check expects."""
    from app.main import schema_marker

    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT version, name, checksum FROM app_schema_migrations ORDER BY version"
        ).fetchall()
        marker = schema_marker(rows)
        database = conn.execute("SELECT current_database()").fetchone()[0]
        conn.execute(sql.SQL("ALTER DATABASE {} SET {} = {}").format(
            sql.Identifier(database), sql.SQL(SCHEMA_MARKER_SETTING), sql.Literal(marker)))
    return marker


def verify(admin_dsn: str, env: dict[str, str], prefix: str) -> None:
    """Run the app's own startup checks against the roles just granted."""
    from app.hosted.auth.repository import PostgresAuthRepository
    from app.hosted.jobs import PostgresWorkerRepository
    from app.hosted.library_memory import PostgresLibraryAccount
    from app.hosted.purge import PostgresAccountPurge
    from app.hosted.tenant.repository import PostgresTenantRepository

    roles = {role.key: role for role in RUNTIME_ROLES}
    asyncio.run(PostgresAuthRepository(role_dsn(env, prefix, roles["auth"])).check_runtime_role())
    asyncio.run(
        PostgresTenantRepository(role_dsn(env, prefix, roles["tenant"])).check_runtime_role())
    PostgresWorkerRepository(role_dsn(env, prefix, roles["worker"])).check_runtime_role()
    PostgresAccountPurge(role_dsn(env, prefix, roles["purge"])).check_runtime_role()
    PostgresLibraryAccount(role_dsn(env, prefix, roles["library"])).check_runtime_role()
    # The tenant startup check reads tables only: the functions are checked here, both ways.
    tenant = role_name(prefix, roles["tenant"])
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        for function in TENANT_FUNCTION_PRIVILEGES:
            signature = f"public.{function}"
            held = conn.execute("SELECT has_function_privilege(%s, %s, 'EXECUTE')",
                                (tenant, signature)).fetchone()[0]
            public = conn.execute("SELECT has_function_privilege('public', %s, 'EXECUTE')",
                                  (signature,)).fetchone()[0]
            if not held or public:
                raise ProvisioningError(f"function privileges on {function} are not as reviewed")


def deploy(env: dict[str, str], prefix: str) -> None:
    admin = _admin_dsn(env)
    steps = (
        ("roles", lambda: prepare(admin, env, prefix)),
        ("migrations", lambda: migrate(role_dsn(env, prefix, OWNER))),
        ("grants", lambda: grant(admin, prefix)),
        ("schema marker", lambda: mark(admin)),
        ("startup role checks", lambda: verify(admin, env, prefix)),
    )
    for label, step in steps:
        step()
        print(f"ok: {label}", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hosted database roles and grants")
    parser.add_argument("command", choices=("deploy", "prepare", "sql"))
    parser.add_argument("--prefix", default=os.environ.get("MOTHLI_DB_ROLE_PREFIX", "mothli"))
    args = parser.parse_args(argv)
    if not _PREFIX_RE.fullmatch(args.prefix):
        parser.error("the role prefix must be lowercase letters, digits and '_'")
    env = dict(os.environ)
    try:
        if args.command == "sql":
            for role in RUNTIME_ROLES:
                print(f"-- {role_name(args.prefix, role)}")
                if role.owner_tables:
                    print("-- and SELECT, DELETE on every table with an owner_id column")
                print(render(grant_statements(args.prefix, role)))
        elif args.command == "prepare":
            prepare(_admin_dsn(env), env, args.prefix)
            print("ok: roles", flush=True)
        else:
            deploy(env, args.prefix)
    except ProvisioningError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - name the failure, never print a DSN or a password
        print(f"failed: {type(exc).__name__}: {_safe(exc, env)}", file=sys.stderr)
        return 1
    return 0


def _safe(exc: BaseException, env: dict[str, str]) -> str:
    """The exception's message with every configured password removed."""
    text = str(exc).splitlines()[0] if str(exc) else ""
    for role in ALL_ROLES:
        value = env.get(role.password_env)
        if value:
            text = text.replace(value, "***")
    admin = env.get("POSTGRES_PASSWORD")
    if admin:
        text = text.replace(admin, "***")
    return text[:300]


if __name__ == "__main__":
    raise SystemExit(main())
