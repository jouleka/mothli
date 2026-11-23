"""A backup's database dump restored, against a real PostgreSQL (runbook steps 4 and 5).

The database is provisioned as on the server (roles, migrations by the migration owner, grants,
forced row-level security), two readers sign in and fill every corner their data lives in, story
memory and chapter notes included. It is then dumped as deploy/backup.sh dumps it (pg_dump as the
superuser, pg_restore's format), and restored as deploy/restore.sh restores it (roles first, then
pg_restore in one transaction, then the deployment step). Made-up data only.

pg_dump and pg_restore run from ``TEST_PG_TOOLS`` when set (for example
``docker exec -i <container>``, so they are the server's own version), else from the PATH.
"""

from __future__ import annotations

import base64
import io
import os
import shlex
import shutil
import subprocess
import uuid
from pathlib import Path

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import conninfo, sql

from app.config import Settings
from app.hosted.backups import count_rows, format_counts
from app.main import create_app
from tests.hosted.test_account_deletion_postgres import World, _seed
from tests.hosted.test_auth_postgres import (
    CLIENT_ID,
    CLIENT_SECRET,
    ISSUER,
    REDIRECT_URI,
    Clock,
    SignedOIDCProvider,
)
from tests.hosted.test_deploy import load_roles

pytestmark = pytest.mark.postgres

COUNTS_SQL = Path(__file__).resolve().parents[3] / "deploy" / "counts.sql"
# Expensive to build again (story memory is paid for per chapter) or a reader's own: each must be
# in the test's data, so an empty table cannot pass for a restored one.
MUST_HOLD_ROWS = ("users", "external_identities", "sessions", "books", "source_objects",
                  "highlights", "annotations", "bookmarks", "reader_preferences", "chapters",
                  "entities", "aliases", "edges", "events", "themes", "chapter_summaries",
                  "chapter_notes", "conversation_turns", "read_ranges", "read_position", "jobs",
                  "cost_ledger", "recollections", "audit_events", "app_schema_migrations")


def _uri(dsn: str) -> str:
    """A space-free address for the command-line tools (they may run through ``docker exec``)."""
    parts = conninfo.conninfo_to_dict(dsn)
    password = f":{parts['password']}" if parts.get("password") else ""
    return (f"postgresql://{parts.get('user', 'postgres')}{password}@"
            f"{parts.get('host', '127.0.0.1')}:{parts.get('port', '5432')}/{parts['dbname']}")


def _tool(name: str, *args: str, stdin: bytes = b"") -> subprocess.CompletedProcess:
    prefix = shlex.split(os.environ.get("TEST_PG_TOOLS", ""))
    if not prefix and shutil.which(name) is None:
        pytest.fail(f"{name} is not on the PATH: install PostgreSQL 16's client tools or set "
                    "TEST_PG_TOOLS (for example: docker exec -i <container>)")
    return subprocess.run([*prefix, name, *args], input=stdin, capture_output=True,  # noqa: S603
                          check=False, timeout=300)


def _drop(admin_dsn: str, name: str) -> None:
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                      "WHERE datname = %s AND pid <> pg_backend_pid()", (name,))
        admin.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name)))


def _app(dsn: dict[str, str], root: Path, provider: SignedOIDCProvider, clock: Clock):
    settings = Settings(
        _env_file=None, deployment_mode="hosted", hosted_auth_dsn=dsn["auth"],
        hosted_tenant_dsn=dsn["tenant"], oidc_issuer=ISSUER, oidc_client_id=CLIENT_ID,
        oidc_client_secret=CLIENT_SECRET, oidc_redirect_uri=REDIRECT_URI,
        trusted_hosts="reader.example", hosted_storage_backend="filesystem",
        hosted_storage_filesystem_root=str(root),
        hosted_storage_filesystem_key=base64.b64encode(b"t" * 32).decode("ascii"),
        session_absolute_ttl_seconds=3600, session_idle_ttl_seconds=3000)
    return create_app(settings, oidc_http_transport=httpx.MockTransport(provider.handle),
                      auth_clock=clock)


@pytest.fixture()
def server(tmp_path):
    """The live database of a server, provisioned by the deployment's own role script."""
    admin_dsn = os.environ.get("TEST_POSTGRES_DSN")
    if not admin_dsn:
        pytest.skip("TEST_POSTGRES_DSN is required for the real PostgreSQL suite")
    roles = load_roles()
    prefix = f"t{uuid.uuid4().hex[:12]}"
    names = {"live": f"{prefix}_live", "restored": f"{prefix}_restored"}
    parts = conninfo.conninfo_to_dict(admin_dsn)
    env = {role.password_env: f"made-up-{uuid.uuid4().hex}{uuid.uuid4().hex}"
           for role in roles.ALL_ROLES}
    env.update(MOTHLI_DB_HOST=parts.get("host", "127.0.0.1"),
               MOTHLI_DB_PORT=str(parts.get("port", "5432")), MOTHLI_DB_NAME=names["live"],
               POSTGRES_USER=parts.get("user", "postgres"),
               POSTGRES_PASSWORD=parts.get("password") or "the-test-server-trusts-local")
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(names["live"])))
    try:
        roles.deploy(env, prefix)
        yield roles, prefix, env, names, admin_dsn
    finally:
        for name in names.values():
            _drop(admin_dsn, name)
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            for role in roles.ALL_ROLES:
                admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(
                    sql.Identifier(roles.role_name(prefix, role))))


def _tables(database: str) -> list[str]:
    with psycopg.connect(database) as conn:
        return [row[0] for row in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' "
            "AND table_type = 'BASE TABLE' ORDER BY table_name COLLATE \"C\"").fetchall()]


def _every_row(database: str) -> dict[str, list[str]]:
    """Every row of every table, as the superuser (whom row-level security never hides rows from)."""
    with psycopg.connect(database) as conn:
        return {table: sorted(row[0] for row in conn.execute(
            sql.SQL("SELECT to_jsonb(row_value)::text FROM {} AS row_value").format(
                sql.Identifier(table))).fetchall()) for table in _tables(database)}


def _rights(database: str) -> dict[str, list[tuple]]:
    """Who owns what and who may do what: tables, sequences and views with their privileges and
    row-level security switches, the policies, the functions, the schema and the extensions.
    A privilege list left at its default and the same list written out are the same rights
    (pg_dump leaves defaults unwritten), so both are compared as written out, in one order."""
    queries = {
        "relations": """SELECT relname, relkind, relowner::regrole::text,
                               ARRAY(SELECT unnest(coalesce(relacl, acldefault(
                                 (CASE relkind WHEN 'S' THEN 's' ELSE 'r' END)::"char",
                                 relowner)))::text ORDER BY 1),
                               relrowsecurity, relforcerowsecurity
                        FROM pg_class WHERE relnamespace = 'public'::regnamespace
                          AND relkind IN ('r', 'S', 'v', 'm', 'p')""",
        "policies": """SELECT polrelid::regclass::text, polname, polcmd, polpermissive,
                              polroles::regrole[]::text, pg_get_expr(polqual, polrelid),
                              pg_get_expr(polwithcheck, polrelid) FROM pg_policy""",
        "functions": """SELECT proname, pg_get_function_identity_arguments(oid),
                               proowner::regrole::text,
                               ARRAY(SELECT unnest(coalesce(
                                 proacl, acldefault('f'::"char", proowner)))::text ORDER BY 1),
                               prosecdef
                        FROM pg_proc WHERE pronamespace = 'public'::regnamespace""",
        "columns": """SELECT attrelid::regclass::text, attname,
                             ARRAY(SELECT unnest(attacl)::text ORDER BY 1) FROM pg_attribute
                      WHERE attacl IS NOT NULL AND attrelid IN (
                        SELECT oid FROM pg_class WHERE relnamespace = 'public'::regnamespace)""",
        "triggers": """SELECT tgrelid::regclass::text, tgname, tgenabled FROM pg_trigger
                       WHERE NOT tgisinternal""",
        "schema": "SELECT nspowner::regrole::text, ARRAY(SELECT unnest(coalesce("
                  "nspacl, acldefault('n'::\"char\", nspowner)))::text ORDER BY 1) "
                  "FROM pg_namespace WHERE nspname = 'public'",
        "extensions": "SELECT extname, extversion FROM pg_extension",
    }
    with psycopg.connect(database) as conn:
        return {label: sorted(conn.execute(query).fetchall(), key=repr)
                for label, query in queries.items()}


def _connect_rights(database: str, prefix: str, roles) -> dict[str, bool]:
    with psycopg.connect(database) as conn:
        name = conn.execute("SELECT current_database()").fetchone()[0]
        return {role.key: conn.execute("SELECT has_database_privilege(%s, %s, 'CONNECT')",
                                       (roles.role_name(prefix, role), name)).fetchone()[0]
                for role in roles.ALL_ROLES}


def test_a_dump_restores_every_row_and_every_right_and_the_app_starts_on_it(server, tmp_path):
    """The break it catches: a backup that leaves rows out (row-level security hiding them from
    the role that dumps), loses who may read what (a reader seeing another's rows, or the app
    refusing to start), drops story memory or notes, or cannot be restored in one go."""
    roles, prefix, env, names, admin_dsn = server
    live = conninfo.make_conninfo(admin_dsn, dbname=names["live"])
    live_roles = {role.key: roles.role_dsn(env, prefix, role) for role in roles.RUNTIME_ROLES}
    clock = Clock()
    provider = SignedOIDCProvider(clock)
    books_root = tmp_path / "books"
    app = _app(live_roles, books_root, provider, clock)
    with TestClient(app, base_url="https://reader.example") as first:
        readers = {"a": {"client": first, "subject": "made-up-subject-a"},
                   "b": {"client": TestClient(app, base_url="https://reader.example"),
                         "subject": "made-up-subject-b"}}
        world = World(app, provider, live, live_roles["purge"], app.state.object_storage, readers)
        for label, reader in readers.items():
            assert world.sign_in(label).status_code == 303
            reader["owner"] = uuid.UUID(
                reader["client"].get("/api/auth/session").json()["user"]["id"])
        for label in readers:
            _seed(world, label)
            with psycopg.connect(live) as conn:  # what Mothli remembers of them
                conn.execute("INSERT INTO recollections (owner_id, kind, text, tied, said, said_at)"
                             " VALUES (%s, 'taste', 'Likes made-up books.', false, 'Made up.', "
                             "now())", (readers[label]["owner"],))
        shelves = {label: reader["client"].get("/api/books").json()
                   for label, reader in readers.items()}
        cookies = {label: dict(reader["client"].cookies) for label, reader in readers.items()}
    before = _every_row(live)
    assert not [table for table in MUST_HOLD_ROWS if not before.get(table)], before
    rights = _rights(live)

    # A reader's role cannot take the dump: pg_dump refuses rather than leave rows out.
    refused = _tool("pg_dump", "--format=custom",
                    f"--dbname={_uri(live_roles['tenant'])}")
    assert refused.returncode != 0 and refused.stdout == b""

    # deploy/backup.sh: pg_dump as the superuser, in pg_restore's format.
    dumped = _tool("pg_dump", "--format=custom", f"--dbname={_uri(live)}")
    assert dumped.returncode == 0, dumped.stderr.decode(errors="replace")
    dump = dumped.stdout
    assert dump.startswith(b"PGDMP")
    # counts.txt, read back from the dump.
    text = _tool("pg_restore", "--data-only", "--file=-", stdin=dump)
    assert text.returncode == 0, text.stderr.decode(errors="replace")
    counts = list(format_counts(count_rows(io.StringIO(text.stdout.decode("utf-8")))))
    assert counts == [f"{table} {len(rows)}" for table, rows in before.items()]

    # deploy/restore.sh: an empty database, the roles, then the dump in one transaction.
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(names["restored"])))
    restored = conninfo.make_conninfo(admin_dsn, dbname=names["restored"])
    restored_env = {**env, "MOTHLI_DB_NAME": names["restored"]}
    roles.prepare(restored, restored_env, prefix)
    loaded = _tool("pg_restore", "--exit-on-error", "--single-transaction",
                   f"--dbname={_uri(restored)}", stdin=dump)
    assert loaded.returncode == 0, loaded.stderr.decode(errors="replace")

    assert _every_row(restored) == before
    assert _rights(restored) == rights
    with psycopg.connect(restored) as conn:
        printed = [row[0] for row in conn.execute(COUNTS_SQL.read_text(encoding="utf-8"))]
    assert printed == counts  # what deploy/restore.sh compares

    # Then `docker compose up`: the deployment step, whose last part is the app's own checks.
    roles.deploy(restored_env, prefix)
    assert _rights(restored) == rights
    assert _connect_rights(restored, prefix, roles) == _connect_rights(live, prefix, roles)
    assert _every_row(restored) == before
    # Numbering goes on where it stopped: a new row takes a number no restored row has.
    with psycopg.connect(restored) as conn:
        highest = conn.execute("SELECT max(recollection_id) FROM recollections").fetchone()[0]
        added = conn.execute(
            "INSERT INTO recollections (owner_id, kind, text, tied, said, said_at) VALUES "
            "(%s, 'plan', 'Made up.', false, 'Made up.', now()) RETURNING recollection_id",
            (readers["a"]["owner"],)).fetchone()[0]
        assert added > highest
        conn.rollback()

    # The app on the restored database: ready, each reader still signed in to their own shelf.
    restored_roles = {role.key: roles.role_dsn(restored_env, prefix, role)
                      for role in roles.RUNTIME_ROLES}
    app = _app(restored_roles, books_root, provider, clock)
    with TestClient(app, base_url="https://reader.example") as probe:
        ready = probe.get("/api/health/ready")
        assert (ready.status_code, ready.json()) == (200, {"status": "ready"})
        for label in ("a", "b"):
            client = TestClient(app, base_url="https://reader.example", cookies=cookies[label])
            assert client.get("/api/books").json() == shelves[label]
        assert shelves["a"] != shelves["b"]
