"""Deleting a hosted account, against a real PostgreSQL (ADR 0041 part 8).

Two readers sign in through the signed stand-in sign-in service, store rows and book files
through the real routes and roles, and one of them asks to delete their account. The purge runs
as its own least-privileged role. Made-up data only.
"""

from __future__ import annotations

import base64
import json
import re
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import conninfo, sql

from app.config import Settings
from app.hosted.auth.api import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE
from app.hosted.migrations import apply_migrations
from app.hosted.purge import PostgresAccountPurge, owner_tables, purge_due_accounts
from app.hosted.storage import EPUB_MEDIA_TYPE, ObjectStorageUnavailableError
from app.hosted.tenant.grants import purge_grants, tenant_function_grants, tenant_grants
from app.main import create_app
from tests.api._epub import epub_ncx
from tests.hosted.test_auth_postgres import (
    CLIENT_ID,
    CLIENT_SECRET,
    ISSUER,
    REDIRECT_URI,
    Clock,
    SignedOIDCProvider,
    _login,
)
from tests.hosted.test_deploy import load_roles

pytestmark = pytest.mark.postgres

INVENTORY = Path(__file__).parents[2] / "app" / "hosted" / "tenant" / "endpoints.json"
PENDING = "account_pending_deletion"


@pytest.fixture(scope="module")
def admin_dsn() -> str:
    import os

    dsn = os.environ.get("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN is required for the real PostgreSQL suite")
    return dsn


@pytest.fixture()
def database(admin_dsn: str):
    name = f"part8_{uuid.uuid4().hex}"
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    dsn = conninfo.make_conninfo(admin_dsn, dbname=name)
    apply_migrations(dsn)
    try:
        yield dsn
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()", (name,))
            admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))


@contextmanager
def _role(admin_dsn: str, database: str, *, bypass: bool, tables, functions=()):
    """A login role holding exactly ``tables`` and ``functions``, as the deployment grants."""
    name = f"part8_{uuid.uuid4().hex}"
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOINHERIT " + (
            "BYPASSRLS" if bypass else "NOBYPASSRLS")).format(sql.Identifier(name)))
    with psycopg.connect(database, autocommit=True) as conn:
        conn.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(name)))
        for table, privileges in tables:
            conn.execute(sql.SQL("GRANT {} ON {} TO {}").format(
                sql.SQL(", ").join(sql.SQL(item) for item in privileges),
                sql.Identifier(table), sql.Identifier(name)))
        for function, privileges in functions:
            conn.execute(sql.SQL("GRANT {} ON FUNCTION {} TO {}").format(
                sql.SQL(", ").join(sql.SQL(item) for item in privileges),
                sql.SQL(function), sql.Identifier(name)))
    try:
        yield conninfo.make_conninfo(database, user=name)
    finally:
        with psycopg.connect(database, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(name)))
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(name)))


@dataclass
class World:
    app: object
    provider: SignedOIDCProvider
    database: str
    purge_dsn: str
    storage: object
    readers: dict  # label -> {"client", "owner", "subject"}

    def sign_in(self, label: str, client: TestClient | None = None):
        reader = self.readers[label]
        self.provider.subject = reader["subject"]
        self.provider.email = f"{label}@example.test"
        _query, callback = _login(client or reader["client"], self.provider)
        return callback

    def csrf(self, label: str, client: TestClient | None = None) -> dict[str, str]:
        return {CSRF_HEADER: (client or self.readers[label]["client"]).cookies.get(CSRF_COOKIE)}

    def client(self) -> TestClient:
        return TestClient(self.app, base_url="https://reader.example")


@pytest.fixture()
def world(database: str, admin_dsn: str, tmp_path):
    auth_tables = sorted((table, tuple(sorted(privileges)))
                         for table, privileges in load_roles().AUTH_TABLE_PRIVILEGES.items())
    with psycopg.connect(database) as conn:
        purge_tables = purge_grants(owner_tables(conn))
    with (
        _role(admin_dsn, database, bypass=True, tables=auth_tables) as auth_dsn,
        _role(admin_dsn, database, bypass=False, tables=tenant_grants(),
              functions=tenant_function_grants()) as tenant_dsn,
        _role(admin_dsn, database, bypass=True, tables=purge_tables) as purge_dsn,
    ):
        settings = Settings(
            _env_file=None,
            deployment_mode="hosted",
            hosted_auth_dsn=auth_dsn,
            hosted_tenant_dsn=tenant_dsn,
            oidc_issuer=ISSUER,
            oidc_client_id=CLIENT_ID,
            oidc_client_secret=CLIENT_SECRET,
            oidc_redirect_uri=REDIRECT_URI,
            trusted_hosts="reader.example",
            hosted_storage_backend="filesystem",
            hosted_storage_filesystem_root=str(tmp_path / "objects"),
            hosted_storage_filesystem_key=base64.b64encode(b"t" * 32).decode("ascii"),
            hosted_credential_master_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
            hosted_credential_key_version="test-v1",
            # Every inventoried route mounted, the reader-key ones included.
            hosted_reader_keys=True,
            session_absolute_ttl_seconds=3600,
            session_idle_ttl_seconds=3000,
        )
        clock = Clock()
        provider = SignedOIDCProvider(clock)
        app = create_app(settings, oidc_http_transport=httpx.MockTransport(provider.handle),
                         auth_clock=clock)
        with TestClient(app, base_url="https://reader.example") as first:
            readers = {
                "a": {"client": first, "subject": "made-up-subject-a"},
                "b": {"client": TestClient(app, base_url="https://reader.example"),
                      "subject": "made-up-subject-b"},
            }
            world = World(app, provider, database, purge_dsn, app.state.object_storage, readers)
            for label, reader in readers.items():
                assert world.sign_in(label).status_code == 303
                reader["owner"] = uuid.UUID(reader["client"].get("/api/auth/session").json()["user"]["id"])
            yield world


def _seed(world: World, label: str) -> uuid.UUID:
    """A book and something in every corner a reader's data lives in, through the routes where
    there is one and as the database owner where the route is not on in this test."""
    reader = world.readers[label]
    client, owner, csrf = reader["client"], reader["owner"], world.csrf(label)
    payload = epub_ncx([("one.xhtml", "Chapter I", "Chapter I", f"Made-up words {label}. " * 30)],
                       title=f"Made-up book {label}", author="Nobody")
    uploaded = client.post("/api/books", files={"file": ("book.epub", payload, EPUB_MEDIA_TYPE)},
                           headers=csrf)
    assert uploaded.status_code == 201, uploaded.text
    book = uuid.UUID(uploaded.json()["id"])
    anchor = {"cfi": "epubcfi(/6/2!/4/2,/1:0,/1:4)", "atom": 1}
    for path, body in (
        ("highlights", {"anchor": anchor, "selected_text": "Made"}),
        ("annotations", {"anchor": anchor, "body": "A made-up note."}),
        ("bookmarks", {"anchor": anchor, "label": "Here"}),
    ):
        assert client.post(f"/api/books/{book}/{path}", json=body, headers=csrf).status_code == 201
    assert client.put(f"/api/books/{book}/preferences", headers=csrf, json={
        "font_size": "book", "line_height": "comfortable", "measure": "balanced",
        "theme": "paper", "margins": "balanced", "typeface": "publisher"}).status_code == 200
    with psycopg.connect(world.database) as conn:
        incarnation = conn.execute("SELECT incarnation FROM books WHERE owner_id=%s AND id=%s",
                                   (owner, book)).fetchone()[0]
        scope = (owner, book, incarnation)
        chapter, entity, other, event = (uuid.uuid4() for _ in range(4))
        conn.execute("INSERT INTO chapters (owner_id,book_id,book_incarnation,id,chapter_key,"
                     "revealed_at,title,content_hash) VALUES (%s,%s,%s,%s,'ch1',1,'Chapter I',%s)",
                     (*scope, chapter, "a" * 64))
        conn.execute("INSERT INTO ingested_chapters (owner_id,book_id,book_incarnation,chapter_id,"
                     "content_hash,completed_at) VALUES (%s,%s,%s,%s,%s,now())",
                     (*scope, chapter, "a" * 64))
        for entity_id, name in ((entity, "Made-up One"), (other, "Made-up Two")):
            conn.execute("INSERT INTO entities (owner_id,book_id,book_incarnation,id,"
                         "source_chapter_id,canonical_name,entity_type,revealed_at) "
                         "VALUES (%s,%s,%s,%s,%s,%s,'character',1)",
                         (*scope, entity_id, chapter, name))
        conn.execute("INSERT INTO aliases (owner_id,book_id,book_incarnation,id,entity_id,"
                     "source_chapter_id,surface_form,revealed_at) "
                     "VALUES (%s,%s,%s,%s,%s,%s,'One',1)", (*scope, uuid.uuid4(), entity, chapter))
        conn.execute("INSERT INTO edges (owner_id,book_id,book_incarnation,id,source_chapter_id,"
                     "src_entity_id,dst_entity_id,relationship_type,label,revealed_at) "
                     "VALUES (%s,%s,%s,%s,%s,%s,%s,'knows','made up',1)",
                     (*scope, uuid.uuid4(), chapter, entity, other))
        conn.execute("INSERT INTO events (owner_id,book_id,book_incarnation,id,source_chapter_id,"
                     "order_idx,summary,kind,revealed_at) VALUES (%s,%s,%s,%s,%s,0,'made up',"
                     "'meeting',1)", (*scope, event, chapter))
        conn.execute("INSERT INTO themes (owner_id,book_id,book_incarnation,id,source_chapter_id,"
                     "name,description,revealed_at) VALUES (%s,%s,%s,%s,%s,'made up','made up',1)",
                     (*scope, uuid.uuid4(), chapter))
        conn.execute("INSERT INTO chapter_summaries (owner_id,book_id,book_incarnation,id,"
                     "source_chapter_id,kind,summary,revealed_at) "
                     "VALUES (%s,%s,%s,%s,%s,'chapter','made up',1)",
                     (*scope, uuid.uuid4(), chapter))
        conn.execute("INSERT INTO read_ranges (owner_id,book_id,book_incarnation,atom,start_offset,"
                     "end_offset,source) VALUES (%s,%s,%s,1,0,40,'read')", scope)
        conn.execute("INSERT INTO skimmed_ranges (owner_id,book_id,book_incarnation,atom,"
                     "start_offset,end_offset) VALUES (%s,%s,%s,1,40,80)", scope)
        conn.execute("INSERT INTO read_position (owner_id,book_id,book_incarnation,atom,"
                     "source_offset) VALUES (%s,%s,%s,1,40)", scope)
        conn.execute("INSERT INTO chapter_notes (owner_id,book_id,book_incarnation,atom,"
                     "source_hash,version,sentences,dropped) "
                     "VALUES (%s,%s,%s,1,'0123456789abcdef','v1','[\"Made up.\"]',0)", scope)
        for book_id, incarnation_id, content in ((book, incarnation, "A made-up question."),
                                                 (None, None, "A made-up hello.")):
            conn.execute("INSERT INTO conversation_turns (owner_id,book_id,book_incarnation,role,"
                         "content) VALUES (%s,%s,%s,'user',%s)",
                         (owner, book_id, incarnation_id, content))
        job = conn.execute("INSERT INTO jobs (owner_id,book_id,book_incarnation,kind,"
                           "idempotency_key) VALUES (%s,%s,%s,'chapter_notes',%s) RETURNING id",
                           (*scope, f"chapter-notes:{uuid.uuid4().hex}")).fetchone()[0]
        conn.execute("INSERT INTO cost_ledger (owner_id,id,book_id,book_incarnation,job_id,phase,"
                     "provider,model,input_tokens,output_tokens,usd,idempotency_key) "
                     "VALUES (%s,%s,%s,%s,%s,'extraction','made-up','model',10,2,0.01,%s)",
                     (*scope[:1], uuid.uuid4(), book, incarnation, job, f"cost-{label}"))
    # A file no row names (an upload whose row was never written): it must go with the reader.
    folder = Path(world.storage._root) / owner.hex
    (folder / f"{uuid.uuid4().hex}.epub.enc").write_bytes(b"made-up stray bytes")
    return book


def _owner_tables(conn) -> list[str]:
    """Every table with an owner_id column, found independently of the purge's own query."""
    return [row[0] for row in conn.execute(
        """SELECT c.table_name FROM information_schema.columns AS c
           JOIN information_schema.tables AS t USING (table_schema, table_name)
           WHERE c.table_schema = 'public' AND c.column_name = 'owner_id'
             AND t.table_type = 'BASE TABLE' ORDER BY 1""").fetchall()]


def _rows(conn, owner: uuid.UUID, *, leave_out=()) -> dict[str, list[str]]:
    return {
        table: sorted(row[0] for row in conn.execute(
            sql.SQL("SELECT to_jsonb(row_value)::text FROM {} AS row_value WHERE owner_id = %s")
            .format(sql.Identifier(table)), (owner,)).fetchall())
        for table in _owner_tables(conn) if table not in leave_out
    }


def _files(world: World, owner: uuid.UUID) -> dict[str, bytes]:
    folder = Path(world.storage._root) / owner.hex
    return {path.name: path.read_bytes() for path in folder.iterdir()} if folder.exists() else {}


def _user(world: World, owner: uuid.UUID) -> tuple:
    with psycopg.connect(world.database) as conn:
        return conn.execute("SELECT status, erase_after, deleted_at, display_name, email "
                            "FROM users WHERE id = %s", (owner,)).fetchone()


def _ask_to_delete(world: World, label: str):
    response = world.readers[label]["client"].post("/api/account/delete", headers=world.csrf(label))
    assert response.status_code == 200, response.text
    return response


def _past_due(world: World, owner: uuid.UUID) -> None:
    """The 30 days pass, in the database, never on the wall clock."""
    with psycopg.connect(world.database) as conn:
        conn.execute("UPDATE users SET erase_after = now() - interval '1 minute' WHERE id = %s",
                     (owner,))


def _purge(world: World, storage=None, repository=None):
    repository = repository or PostgresAccountPurge(world.purge_dsn)
    repository.check_runtime_role()
    return purge_due_accounts(repository, storage or world.storage)


def test_asking_to_delete_signs_out_everywhere_and_stops_background_work(world: World) -> None:
    """The break it catches: another device still signed in after the reader asked, background
    work of theirs still waiting or running, or the request taken without its CSRF token."""
    a, b = world.readers["a"], world.readers["b"]
    _seed(world, "a")
    _seed(world, "b")
    second_device = world.client()
    assert world.sign_in("a", second_device).status_code == 303
    with psycopg.connect(world.database) as conn:
        conn.execute("UPDATE jobs SET state='running' WHERE owner_id=%s AND kind='chapter_notes'",
                     (a["owner"],))
        b_jobs = conn.execute("SELECT id, state, cancellation_requested_at FROM jobs "
                              "WHERE owner_id=%s ORDER BY id", (b["owner"],)).fetchall()

    assert a["client"].post("/api/account/delete").status_code == 403
    assert _user(world, a["owner"])[0] == "active"
    old_a = a["client"].cookies.get(SESSION_COOKIE)
    old_second = second_device.cookies.get(SESSION_COOKIE)
    response = _ask_to_delete(world, "a")
    erase_after = response.json()["erase_after"]
    cleared = response.headers.get_list("set-cookie")
    assert any(item.startswith(f"{SESSION_COOKIE}=") and "Max-Age=0" in item for item in cleared)
    with psycopg.connect(world.database) as conn:
        assert conn.execute("SELECT count(*) FROM sessions WHERE owner_id=%s AND revoked_at IS NULL",
                            (a["owner"],)).fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM sessions WHERE owner_id=%s AND revoked_at IS NULL",
                            (b["owner"],)).fetchone()[0] == 1
        jobs = conn.execute("SELECT kind, state, cancellation_requested_at IS NOT NULL FROM jobs "
                            "WHERE owner_id=%s ORDER BY kind", (a["owner"],)).fetchall()
        assert jobs == [("chapter_notes", "running", True), ("ingest_book", "cancelled", True)]
        assert conn.execute("SELECT id, state, cancellation_requested_at FROM jobs "
                            "WHERE owner_id=%s ORDER BY id", (b["owner"],)).fetchall() == b_jobs
        status, stored, deleted_at, *_ = conn.execute(
            "SELECT status, erase_after, deleted_at FROM users WHERE id=%s",
            (a["owner"],)).fetchone()
        now = conn.execute("SELECT now()").fetchone()[0]
        events = conn.execute("SELECT actor_kind, action, target_kind, result, metadata "
                              "FROM audit_events WHERE owner_id=%s AND action LIKE 'account.%%'",
                              (a["owner"],)).fetchall()
    assert (status, deleted_at) == ("deleting", None)
    assert stored == datetime.fromisoformat(erase_after)
    # Until the start of a UTC day, the day after the one the reader is told: at least 31 days.
    assert stored.astimezone(UTC).time().isoformat() == "00:00:00"
    assert timedelta(days=31) < stored - now <= timedelta(days=32)
    assert events == [("owner", "account.delete", "account", "succeeded", {})]
    assert _user(world, b["owner"])[0] == "active"
    assert b["client"].get("/api/books").status_code == 200
    # The cookies both devices held no longer sign anyone in.
    for cookies in (old_a, old_second):
        stale = world.client()
        stale.cookies.set(SESSION_COOKIE, cookies, domain="reader.example", path="/")
        assert stale.get("/api/auth/session").status_code == 401


def test_signing_in_while_pending_answers_only_the_pending_refusal_on_every_inventoried_route(
    world: World,
) -> None:
    """The break it catches: any reader route, now or added later to the inventory, that serves
    a reader waiting to be erased, or reads their data, instead of the one refusal."""
    a = world.readers["a"]
    book = _seed(world, "a")
    _ask_to_delete(world, "a")
    a["client"].cookies.clear()
    assert world.sign_in("a").status_code == 303
    session = a["client"].get("/api/auth/session")
    assert session.status_code == 200
    erase_after = session.json()["erase_after"]
    with psycopg.connect(world.database) as conn:
        before = _rows(conn, a["owner"], leave_out={"sessions", "audit_events",
                                                    "external_identities"})

    inventory = json.loads(INVENTORY.read_text(encoding="utf-8"))
    walked = 0
    for item in inventory["enabled"]:
        if item["path"] == "/api/account/keep":
            continue
        path = re.sub(r"\{(\w+)\}", lambda match: "extraction" if match[1] == "capability"
                      else str(book) if match[1] == "book_id" else str(uuid.uuid4()), item["path"])
        body = {} if item["method"] in {"POST", "PUT", "PATCH"} else None
        response = a["client"].request(item["method"], path, json=body, headers=world.csrf("a"))
        assert response.status_code == 403, (item["method"], path, response.text)
        assert response.json()["detail"] == {"code": PENDING, "erase_after": erase_after}, path
        walked += 1
    assert walked == len(inventory["enabled"]) - 1
    with psycopg.connect(world.database) as conn:
        assert _rows(conn, a["owner"], leave_out={"sessions", "audit_events",
                                                  "external_identities"}) == before
    # Signing out still works.
    assert a["client"].post("/api/auth/logout", headers=world.csrf("a")).status_code == 204
    assert a["client"].get("/api/auth/session").status_code == 401


def test_keep_my_account_restores_everything_exactly(world: World) -> None:
    """The break it catches: "Keep my account" that leaves the account refused, loses a row or a
    file, or that a stray request without the CSRF token can trigger."""
    a = world.readers["a"]
    book = _seed(world, "a")
    leave_out = {"sessions", "audit_events", "external_identities", "jobs", "job_attempts",
                 "owner_request_windows"}
    with psycopg.connect(world.database) as conn:
        before = _rows(conn, a["owner"], leave_out=leave_out)
    files = _files(world, a["owner"])
    _ask_to_delete(world, "a")
    a["client"].cookies.clear()
    assert world.sign_in("a").status_code == 303
    assert a["client"].post("/api/account/keep").status_code == 403
    assert _user(world, a["owner"])[0] == "deleting"
    kept = a["client"].post("/api/account/keep", headers=world.csrf("a"))
    assert kept.status_code == 204
    assert _user(world, a["owner"])[:3] == ("active", None, None)
    assert "erase_after" not in a["client"].get("/api/auth/session").json()
    assert [item["id"] for item in a["client"].get("/api/books").json()] == [str(book)]
    with psycopg.connect(world.database) as conn:
        assert _rows(conn, a["owner"], leave_out=leave_out) == before
        actions = [row[0] for row in conn.execute(
            "SELECT action FROM audit_events WHERE owner_id=%s AND action LIKE 'account.%%' "
            "ORDER BY occurred_at, action", (a["owner"],)).fetchall()]
    assert actions == ["account.delete", "account.restore"]
    assert _files(world, a["owner"]) == files
    # Keeping an ordinary account changes nothing.
    assert a["client"].post("/api/account/keep", headers=world.csrf("a")).status_code == 204


def test_the_purge_waits_for_the_thirty_days(world: World) -> None:
    """The break it catches: an account erased while the reader may still change their mind."""
    a, b = world.readers["a"], world.readers["b"]
    _seed(world, "a")
    _seed(world, "b")
    _ask_to_delete(world, "a")
    _ask_to_delete(world, "b")
    with psycopg.connect(world.database) as conn:
        within = _rows(conn, b["owner"])
    result = _purge(world)
    assert (result.erased, result.unfinished) == (0, 0)
    assert _user(world, a["owner"])[0] == _user(world, b["owner"])[0] == "deleting"

    _past_due(world, a["owner"])
    result = _purge(world)
    assert (result.erased, result.unfinished) == (1, 0)
    assert _user(world, a["owner"])[0] == "deleted"
    assert _user(world, b["owner"])[0] == "deleting"
    with psycopg.connect(world.database) as conn:
        assert _rows(conn, b["owner"]) == within
    assert _files(world, b["owner"])


def test_purge_erases_one_reader_entirely_and_leaves_the_other_untouched(world: World) -> None:
    """The break it catches: a row of the erased reader left in any table with an owner_id,
    a table added later included (the tables are read from the catalogue, never listed), a book
    file left in storage, or anything of the other reader's changed."""
    a, b = world.readers["a"], world.readers["b"]
    _seed(world, "a")
    _seed(world, "b")
    with psycopg.connect(world.database) as conn:
        tables = _owner_tables(conn)
        # The purge finds the same tables as this independent reading of the catalogue.
        assert owner_tables(conn) == tables
        seeded = {table for table, rows in _rows(conn, a["owner"]).items() if rows}
    assert {"books", "source_objects", "highlights", "conversation_turns", "chapter_notes",
            "read_ranges", "entities", "jobs", "cost_ledger", "sessions",
            "external_identities", "owner_limits", "audit_events"} <= seeded
    _ask_to_delete(world, "a")
    _past_due(world, a["owner"])
    with psycopg.connect(world.database) as conn:
        other = _rows(conn, b["owner"])
    other_files = _files(world, b["owner"])
    assert len(_files(world, a["owner"])) == 2 and len(other_files) == 2

    assert (_purge(world).erased) == 1

    with psycopg.connect(world.database) as conn:
        left = _rows(conn, a["owner"])
        assert _rows(conn, b["owner"]) == other
        events = conn.execute("SELECT actor_kind, action, target_kind, target_id, result, "
                              "request_id, metadata FROM audit_events WHERE owner_id = %s "
                              "ORDER BY occurred_at, action", (a["owner"],)).fetchall()
    assert {table: rows for table, rows in left.items() if rows and table != "audit_events"} == {}
    assert events == [("owner", "account.delete", "account", None, "succeeded", None, {}),
                      ("system", "account.purge", "account", None, "succeeded", None, {})]
    status, erase_after, deleted_at, display_name, email = _user(world, a["owner"])
    assert (status, erase_after, display_name, email) == ("deleted", None, "", None)
    assert deleted_at is not None
    assert not (Path(world.storage._root) / a["owner"].hex).exists()
    assert _files(world, b["owner"]) == other_files

    # The same sign-in later finds nothing of theirs: a new, empty account.
    a["client"].cookies.clear()
    assert world.sign_in("a").status_code == 303
    fresh = uuid.UUID(a["client"].get("/api/auth/session").json()["user"]["id"])
    assert fresh != a["owner"]
    assert a["client"].get("/api/books").json() == []


def test_an_interrupted_purge_finishes_when_run_again(world: World) -> None:
    """The break it catches: a crash between storage and database that leaves files nobody will
    delete, rows that are served while their files are gone, or a reader stuck half-erased; and
    one reader's failure holding up another's erasure."""
    a, b = world.readers["a"], world.readers["b"]
    _seed(world, "a")
    _seed(world, "b")
    _ask_to_delete(world, "a")
    _ask_to_delete(world, "b")
    _past_due(world, a["owner"])
    _past_due(world, b["owner"])

    class AwayForA:
        def __init__(self, storage) -> None:
            self.storage = storage

        def delete_owner(self, owner_id) -> None:
            if owner_id.value == a["owner"]:
                raise ObjectStorageUnavailableError("made-up outage")
            self.storage.delete_owner(owner_id)

    result = _purge(world, storage=AwayForA(world.storage))
    assert (result.erased, result.unfinished) == (1, 1)  # B is not held up by A
    assert _user(world, b["owner"])[:2] == ("deleted", None)
    assert _user(world, a["owner"])[0] == "deleted" and _user(world, a["owner"])[1] is not None
    assert _files(world, a["owner"])
    # Being erased: it can no longer be signed into, so nothing of it is served, nor kept.
    a["client"].cookies.clear()
    assert world.sign_in("a").status_code == 400

    class CrashAfterFiles(PostgresAccountPurge):
        def erase_rows(self, owner_id):
            raise psycopg.OperationalError("made-up lost connection")

    result = _purge(world, repository=CrashAfterFiles(world.purge_dsn))
    assert (result.erased, result.unfinished) == (0, 1)
    assert _files(world, a["owner"]) == {}
    with psycopg.connect(world.database) as conn:
        assert _rows(conn, a["owner"])["books"]  # rows outlive the files...
    assert world.sign_in("a").status_code == 400  # ...and are never served

    result = _purge(world)
    assert (result.erased, result.unfinished) == (1, 0)
    with psycopg.connect(world.database) as conn:
        left = _rows(conn, a["owner"])
    assert {table: rows for table, rows in left.items() if rows and table != "audit_events"} == {}
    assert _user(world, a["owner"])[:2] == ("deleted", None)
    assert (_purge(world).erased) == 0  # nothing left to do


def test_one_reader_cannot_delete_keep_or_erase_another(world: World) -> None:
    """The break it catches: one reader's request deleting, keeping or erasing another reader's
    account, or an erasure of one reader touching or delaying the other's."""
    a, b = world.readers["a"], world.readers["b"]
    _seed(world, "a")
    _seed(world, "b")
    _ask_to_delete(world, "a")
    before_a = _user(world, a["owner"])
    with psycopg.connect(world.database) as conn:
        rows_a = _rows(conn, a["owner"], leave_out={"audit_events"})

    # B keeping their own (ordinary) account does not keep A's.
    assert b["client"].post("/api/account/keep", headers=world.csrf("b")).status_code == 204
    assert _user(world, a["owner"]) == before_a
    # An owner in the request is refused, never followed.
    for name in ("owner_id", "user_id"):
        refused = b["client"].post(f"/api/account/delete?{name}={a['owner']}",
                                   headers=world.csrf("b"))
        assert refused.status_code == 422
        assert b["client"].post(f"/api/account/keep?{name}={a['owner']}",
                                headers=world.csrf("b")).status_code == 422
    assert _user(world, b["owner"])[0] == "active"
    # A body naming A is ignored: B's request deletes B only.
    deleted = b["client"].post("/api/account/delete", json={"owner_id": str(a["owner"])},
                               headers=world.csrf("b"))
    assert deleted.status_code == 200
    assert _user(world, b["owner"])[0] == "deleting"
    assert _user(world, a["owner"]) == before_a

    _past_due(world, b["owner"])
    assert _purge(world).erased == 1
    assert _user(world, a["owner"]) == before_a
    with psycopg.connect(world.database) as conn:
        assert _rows(conn, a["owner"], leave_out={"audit_events"}) == rows_a
    assert len(_files(world, a["owner"])) == 2

    # A can still come back and keep everything.
    a["client"].cookies.clear()
    assert world.sign_in("a").status_code == 303
    assert a["client"].post("/api/account/keep", headers=world.csrf("a")).status_code == 204
    assert len(a["client"].get("/api/books").json()) == 1
