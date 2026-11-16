"""The read map in hosted mode, through the seam (ADR 0041 part 1), against real PostgreSQL.

Two readers through the hosted HTTP app on a non-BYPASSRLS tenant role; the same adapter through a
superuser connection, where only its own owner predicates keep readers apart; the role's exact
grants; and the hosted versioned position beside the map's chapter counter.
"""

from __future__ import annotations

import asyncio
import base64
import uuid
from datetime import UTC, datetime

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import conninfo, sql

from app.config import Settings
from app.hosted.auth.api import CSRF_HEADER
from app.hosted.auth.models import Principal
from app.hosted.auth.repository import AuthConfigurationError
from app.hosted.auth.tokens import digest_token, new_token
from app.hosted.companion.store import HostedCompanionCatalog
from app.hosted.storage import EPUB_MEDIA_TYPE
from app.hosted.tenant.grants import TENANT_TABLE_PRIVILEGES
from app.hosted.tenant.models import OwnerId
from app.hosted.tenant.repository import PostgresTenantRepository
from app.main import create_app
from tests.api._epub import epub_ncx
from tests.hosted import test_tenant_postgres as tenant_gate
from tests.hosted.test_tenant_postgres import SessionRepository, UnusedOIDCClient, _become

pytestmark = pytest.mark.postgres

# The tenant gate's own database and RLS-enforced role, granted from the same allow-list.
admin_dsn = tenant_gate.admin_dsn
database = tenant_gate.database
tenant_dsn = tenant_gate.tenant_dsn

PEOPLE = {1: "Mara", 2: "Corvus", 3: "Vex"}
READ_MAP_TABLES = ("read_ranges", "skimmed_ranges", "read_position")


def line(chapter: int, number: int) -> str:
    return f"{PEOPLE[chapter]} moved lamp {number} in part {chapter}."


def lamp_book() -> bytes:
    """Three chapters of sixty distinct sentences, so any page can be named exactly."""
    return epub_ncx(
        [(f"c{n}.xhtml", f"Part {n}", f"Part {n}", " ".join(line(n, i) for i in range(1, 61)))
         for n in (1, 2, 3)],
        title="The Lamp Book", author="Ada Writer")


def anchor(chapter: int, number: int, *, after: bool, epoch: int = 0) -> dict:
    quote = line(chapter, number)
    return {"atom": chapter, "quote": quote, "prefix": quote if after else "",
            "cfi": "epubcfi(/6/2)", "position_epoch": epoch}


def page(chapter: int, first: int, last: int, *, to_chapter: int | None = None,
         skimmed: bool = False) -> dict:
    return {"start": anchor(chapter, first, after=False),
            "end": anchor(to_chapter or chapter, last, after=True), "skimmed": skimmed}


@pytest.fixture()
def readers(database: str) -> dict:
    owners = {"a": uuid.uuid4(), "b": uuid.uuid4()}
    with psycopg.connect(database) as conn:
        conn.cursor().executemany(
            "INSERT INTO users (id,display_name) VALUES (%s,%s)",
            [(owner, f"Reader {label.upper()}") for label, owner in owners.items()],
        )
    return owners


def hosted_settings(tenant: str, objects, **overrides) -> Settings:
    return Settings(
        _env_file=None,
        deployment_mode="hosted",
        hosted_auth_dsn="postgresql://unused.invalid/litlet",
        hosted_tenant_dsn=tenant,
        oidc_issuer="https://idp.example",
        oidc_client_id="litlet",
        oidc_client_secret="not-a-real-secret",
        oidc_redirect_uri="https://reader.example/api/auth/callback",
        trusted_hosts="reader.example",
        hosted_storage_backend="filesystem",
        hosted_storage_filesystem_root=str(objects),
        hosted_storage_filesystem_key=base64.b64encode(b"t" * 32).decode("ascii"),
        hosted_credential_master_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        hosted_credential_key_version="test-v1",
        reader_page_context=True,
        reader_read_map=True,
        **overrides,
    )


def signed_in(owners: dict) -> tuple[dict, SessionRepository]:
    tokens, sessions = {}, {}
    for label, owner in owners.items():
        session_token, csrf_token = new_token(), new_token()
        tokens[label] = (session_token, csrf_token)
        sessions[digest_token(session_token)] = Principal(
            owner_id=owner, session_id=uuid.uuid4(), display_name=f"Reader {label.upper()}",
            email=None, csrf_digest=digest_token(csrf_token),
            expires_at=datetime(2026, 7, 17, tzinfo=UTC),
        )
    return tokens, SessionRepository(sessions)


@pytest.fixture()
def reading_app(tenant_dsn: str, readers: dict, tmp_path):
    tokens, sessions = signed_in(readers)
    app = create_app(
        hosted_settings(tenant_dsn, tmp_path / "objects"),
        auth_repository=sessions,
        oidc_client=UnusedOIDCClient(),
        auth_clock=lambda: datetime(2026, 7, 16, 12, tzinfo=UTC),
    )
    with TestClient(app, base_url="https://reader.example") as client:
        yield client, tokens, app


def upload(client: TestClient, tokens: dict, label: str, blob: bytes) -> str:
    csrf = _become(client, tokens, label)
    posted = client.post("/api/books", files={"file": ("lamp.epub", blob, EPUB_MEDIA_TYPE)},
                         headers={CSRF_HEADER: csrf})
    assert posted.status_code == 201, posted.text
    return posted.json()["id"]


def rows_of(dsn: str, owner: uuid.UUID) -> dict:
    """Everything of one owner the read map and the counter hold, read past row-level security."""
    with psycopg.connect(dsn) as conn:
        held = {
            table: sorted(
                tuple(str(value) for value in row)
                for row in conn.execute(
                    sql.SQL("SELECT * FROM {} WHERE owner_id=%s").format(sql.Identifier(table)),
                    (owner,),
                ).fetchall()
            )
            for table in READ_MAP_TABLES
        }
        held["reading_state"] = conn.execute(
            """SELECT book_id,bookmark,current_cfi,position_epoch,position_version
               FROM reading_state WHERE owner_id=%s ORDER BY book_id""", (owner,)).fetchall()
    # updated_at moves with every write; the rows themselves are what must not change.
    return {table: [row[:-1] if table in READ_MAP_TABLES else row for row in rows]
            for table, rows in held.items()}


def test_second_reader_gets_first_readers_book_as_missing_on_every_read_map_route(
    reading_app, readers: dict, database: str
) -> None:
    """The break it catches: any read-map route that tells a second reader the first reader's book
    exists (a 409, a 422 or a body that differs from a random id's), shows them its map, or lets
    their page reports, chapter marks, skims or position reports change the first reader's map,
    place or chapter counter. Runs on the RLS-enforced tenant role, so both barriers are on.
    """
    client, tokens, _app = reading_app
    book_a = upload(client, tokens, "a", lamp_book())
    csrf_a = _become(client, tokens, "a")
    assert client.put(f"/api/books/{book_a}/read-page", json=page(2, 30, 34),
                      headers={CSRF_HEADER: csrf_a}).status_code == 200
    assert client.put(f"/api/books/{book_a}/read-chapters", json={"atoms": [1], "read": True},
                      headers={CSRF_HEADER: csrf_a}).status_code == 200
    assert client.put(f"/api/books/{book_a}/read-page", json=page(3, 10, 12, skimmed=True),
                      headers={CSRF_HEADER: csrf_a}).status_code == 200
    seen_by_a = (client.get(f"/api/books/{book_a}/read-map").json(),
                 client.get(f"/api/books/{book_a}/read-frontier").json())
    assert [chapter["read"] for chapter in seen_by_a[0]["chapters"]][0] == 1.0
    before = rows_of(database, readers["a"])
    assert before["read_ranges"] and before["skimmed_ranges"] and before["read_position"]

    # B has a copy of the very same file: nothing may be shared through its content.
    book_b = upload(client, tokens, "b", lamp_book())
    csrf_b = _become(client, tokens, "b")
    missing = uuid.uuid4()
    requests = [
        ("GET", "/read-map", None),
        ("PUT", "/read-page", page(1, 1, 5)),
        ("PUT", "/read-page", page(2, 30, 34, skimmed=True)),
        ("PUT", "/read-page", page(1, 58, 2, to_chapter=2)),
        ("PUT", "/read-chapters", {"atoms": [1, 2, 3], "read": False}),
        ("PUT", "/read-chapters", {"atoms": [3], "read": True, "source": "import"}),
        ("GET", "/read-frontier", None),
        ("PUT", "/read-frontier", anchor(2, 34, after=True)),
    ]
    for method, suffix, body in requests:
        hidden = client.request(method, f"/api/books/{book_a}{suffix}", json=body,
                                headers={CSRF_HEADER: csrf_b})
        absent = client.request(method, f"/api/books/{missing}{suffix}", json=body,
                                headers={CSRF_HEADER: csrf_b})
        assert (hidden.status_code, hidden.json()) == (absent.status_code, absent.json()), suffix
        assert (hidden.status_code, hidden.json()) == (404, {"detail": "unknown book"}), suffix
        assert hidden.headers["cache-control"] == "private, no-store"

    # B's own copy holds only what B read.
    assert client.put(f"/api/books/{book_b}/read-page", json=page(3, 50, 52),
                      headers={CSRF_HEADER: csrf_b}).status_code == 200
    own = client.get(f"/api/books/{book_b}/read-map").json()["chapters"]
    assert [chapter["read"] > 0 for chapter in own] == [False, False, True]
    assert client.get(f"/api/books/{book_b}/read-frontier").json()["atom"] == 3

    assert rows_of(database, readers["a"]) == before
    _become(client, tokens, "a")
    assert (client.get(f"/api/books/{book_a}/read-map").json(),
            client.get(f"/api/books/{book_a}/read-frontier").json()) == seen_by_a
    assert rows_of(database, readers["a"]) == before


def test_read_map_rows_are_isolated_by_row_level_security_alone(
    reading_app, readers: dict, tenant_dsn: str, database: str
) -> None:
    """The break it catches: a read-map table without a working owner policy, so a query that
    forgot its owner predicate (or a hand-written one) could read, delete or plant another reader's
    rows. Here no predicate names the owner: only the policy does."""
    client, tokens, _app = reading_app
    book_a = upload(client, tokens, "a", lamp_book())
    csrf_a = _become(client, tokens, "a")
    client.put(f"/api/books/{book_a}/read-page", json=page(1, 1, 5), headers={CSRF_HEADER: csrf_a})
    client.put(f"/api/books/{book_a}/read-page", json=page(2, 1, 5, skimmed=True),
               headers={CSRF_HEADER: csrf_a})
    before = rows_of(database, readers["a"])
    assert all(before[table] for table in READ_MAP_TABLES)
    with psycopg.connect(database) as conn:
        incarnation = conn.execute("SELECT incarnation FROM books WHERE id=%s",
                                   (book_a,)).fetchone()[0]

    with psycopg.connect(tenant_dsn, autocommit=True) as conn:
        conn.execute("SELECT set_config('app.owner_id', %s, false)", (str(readers["b"]),))
        assert conn.execute("SELECT count(*) FROM books").fetchone()[0] == 0
        for table in READ_MAP_TABLES:
            name = sql.Identifier(table)
            assert conn.execute(sql.SQL("SELECT count(*) FROM {}").format(name)).fetchone()[0] == 0
            assert conn.execute(sql.SQL("DELETE FROM {}").format(name)).rowcount == 0
        assert conn.execute("UPDATE read_position SET atom=0").rowcount == 0
        # A row naming A's real book: only the policy's WITH CHECK can refuse it.
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                """INSERT INTO read_ranges
                     (owner_id,book_id,book_incarnation,atom,start_offset,end_offset,source)
                   VALUES (%s,%s,%s,3,0,10,'told')""",
                (readers["a"], book_a, incarnation),
            )
    assert rows_of(database, readers["a"]) == before


def test_read_map_adapter_isolates_owners_by_its_own_predicates_with_rls_bypassed(
    reading_app, readers: dict, database: str
) -> None:
    """The break it catches: an adapter query that leans on row-level security alone. Through a
    superuser connection, where RLS does not apply, an adapter bound to reader B must still see
    nothing of reader A's book and change nothing of it (ADR 0021 Verification)."""
    client, tokens, _app = reading_app
    book_a = upload(client, tokens, "a", lamp_book())
    csrf_a = _become(client, tokens, "a")
    client.put(f"/api/books/{book_a}/read-page", json=page(2, 30, 34), headers={CSRF_HEADER: csrf_a})
    client.put(f"/api/books/{book_a}/read-page", json=page(3, 1, 4, skimmed=True),
               headers={CSRF_HEADER: csrf_a})
    client.put(f"/api/books/{book_a}/read-chapters", json={"atoms": [1], "read": True},
               headers={CSRF_HEADER: csrf_a})
    before = rows_of(database, readers["a"])
    with psycopg.connect(database) as conn:
        assert conn.execute("SELECT rolsuper FROM pg_roles WHERE rolname=current_user").fetchone()[0]
        incarnation = str(conn.execute("SELECT incarnation FROM books WHERE id=%s",
                                        (book_a,)).fetchone()[0])

    owner_a = HostedCompanionCatalog(database, OwnerId(readers["a"]))
    assert owner_a.get_read_map(book_a, incarnation=incarnation)          # the rows are there

    other = HostedCompanionCatalog(database, OwnerId(readers["b"]))
    assert other.get_book(book_a) is None
    assert other.get_state(book_a) is None
    assert other.list_books() == []
    assert other.source_record(book_a) is None
    assert other.get_conversation_frontier(book_a) is None
    assert other.get_read_map(book_a, incarnation=incarnation) == {}
    assert other.get_skimmed(book_a, incarnation=incarnation) == {}
    assert other.get_read_position(book_a, incarnation=incarnation) is None
    other.clear_read_map(book_a, atom=2)
    other.clear_read_map(book_a)
    other.set_bookmark(book_a, 0)
    for write in (
        lambda: other.add_read_range(book_a, incarnation=incarnation, atom=3, start=0, end=50,
                                     source="read"),
        lambda: other.add_skimmed_range(book_a, incarnation=incarnation, atom=2, start=0, end=50),
        lambda: other.set_read_position(book_a, incarnation=incarnation, atom=3, offset=9),
    ):
        # B's rows can only name B, and B has no such book: the write is refused outright.
        with pytest.raises(psycopg.Error):
            write()
    assert rows_of(database, readers["a"]) == before


def test_tenant_role_holds_exactly_the_read_map_grants(tenant_dsn: str, database: str) -> None:
    """The break it catches: a read-map table the tenant role cannot use (every route fails), or
    one with a privilege nobody reviewed (TRUNCATE, an UPDATE on ranges). The startup check reads
    the same allow-list the fixture granted, so one grant more or one fewer refuses startup."""
    assert TENANT_TABLE_PRIVILEGES["read_ranges"] == {"SELECT", "INSERT", "DELETE"}
    assert TENANT_TABLE_PRIVILEGES["skimmed_ranges"] == {"SELECT", "INSERT", "DELETE"}
    assert TENANT_TABLE_PRIVILEGES["read_position"] == {"SELECT", "INSERT", "UPDATE", "DELETE"}
    repository = PostgresTenantRepository(tenant_dsn)
    asyncio.run(repository.check_runtime_role())
    role = conninfo.conninfo_to_dict(tenant_dsn)["user"]
    with psycopg.connect(database, autocommit=True) as conn:
        for table in READ_MAP_TABLES:
            held = {
                privilege
                for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE",
                                  "REFERENCES", "TRIGGER")
                if conn.execute("SELECT has_table_privilege(%s, %s, %s)",
                                (role, f"public.{table}", privilege)).fetchone()[0]
            }
            assert held == TENANT_TABLE_PRIVILEGES[table], table

    for grant, undo in (
        ("GRANT UPDATE ON read_ranges TO {}", "REVOKE UPDATE ON read_ranges FROM {}"),
        ("GRANT TRUNCATE ON skimmed_ranges TO {}", "REVOKE TRUNCATE ON skimmed_ranges FROM {}"),
        ("REVOKE DELETE ON read_position FROM {}", "GRANT DELETE ON read_position TO {}"),
    ):
        with psycopg.connect(database, autocommit=True) as conn:
            conn.execute(sql.SQL(grant).format(sql.Identifier(role)))
        try:
            with pytest.raises(AuthConfigurationError, match="allow-list"):
                asyncio.run(repository.check_runtime_role())
        finally:
            with psycopg.connect(database, autocommit=True) as conn:
                conn.execute(sql.SQL(undo).format(sql.Identifier(role)))
    asyncio.run(repository.check_runtime_role())


def test_every_read_map_route_requires_a_session_and_writes_require_csrf(reading_app) -> None:
    """The break it catches: a read-map route reachable without a signed-in reader, or a write
    reachable from another site without the CSRF token."""
    client, tokens, _app = reading_app
    book = uuid.uuid4()
    routes = [("GET", "/read-map", None), ("PUT", "/read-page", page(1, 1, 2)),
              ("PUT", "/read-chapters", {"atoms": [1], "read": True}),
              ("GET", "/read-frontier", None), ("PUT", "/read-frontier", anchor(1, 1, after=True))]
    client.cookies.clear()
    for method, suffix, body in routes:
        assert client.request(method, f"/api/books/{book}{suffix}", json=body).status_code == 401
    _become(client, tokens, "a")
    for method, suffix, body in routes:
        if method == "PUT":
            assert client.request(method, f"/api/books/{book}{suffix}",
                                  json=body).status_code == 403


def test_chapter_counter_follows_the_map_beside_the_versioned_position(
    reading_app, readers: dict, database: str
) -> None:
    """The break it catches (ADR 0041, "least sure" 3): the map lowering the chapter counter in a
    way that breaks cross-device sync (its version clock or saved place), or a device's stale
    report raising the counter for good past what the map says was read.

    What holds today, and is recorded as open: between a stale device report and the reader's
    next read-map call, the counter can stand ahead of the map; the next call takes it back.
    """
    client, tokens, _app = reading_app
    book = upload(client, tokens, "a", lamp_book())
    with psycopg.connect(database) as conn:
        incarnation = conn.execute("SELECT incarnation FROM books WHERE id=%s",
                                   (book,)).fetchone()[0]
        for ordinal in (1, 2, 3):        # extraction would record these; positions are checked
            conn.execute(                # against them
                """INSERT INTO chapters
                     (owner_id,book_id,book_incarnation,chapter_key,revealed_at,content_hash)
                   VALUES (%s,%s,%s,%s,%s,%s)""",
                (readers["a"], book, incarnation, f"c{ordinal}", ordinal, "a" * 64))
    csrf = _become(client, tokens, "a")
    headers = {CSRF_HEADER: csrf}

    def device(version, sequence, client_id):
        return client.put(f"/api/books/{book}/position", headers=headers, json={
            "cfi": "epubcfi(/6/6)", "offset": 500, "completed_chapter": 2, "position_epoch": 0,
            "base_version": version, "client_id": str(client_id), "client_sequence": sequence})

    assert client.put(f"/api/books/{book}/read-chapters", json={"atoms": [1, 2], "read": True},
                      headers=headers).status_code == 200
    start = client.get(f"/api/books/{book}/position").json()
    assert start["bookmark"] == 2
    reported = device(start["position_version"], 1, uuid.uuid4())
    assert reported.status_code == 200 and reported.json()["applied"]
    synced = client.get(f"/api/books/{book}/position").json()

    # "I haven't read chapter 2": the map takes the counter back, and nothing else.
    assert client.put(f"/api/books/{book}/read-chapters", json={"atoms": [2], "read": False},
                      headers=headers).status_code == 200
    lowered = client.get(f"/api/books/{book}/position").json()
    assert lowered["bookmark"] == 1
    assert {key: lowered[key] for key in ("cfi", "position_version", "current_offset",
                                          "high_water_offset", "position_epoch")} == {
        key: synced[key] for key in ("cfi", "position_version", "current_offset",
                                     "high_water_offset", "position_epoch")}

    # A second device that never saw that reports chapter 2 as completed from an old version.
    stale = device(start["position_version"], 1, uuid.uuid4())
    assert stale.status_code == 200 and stale.json()["bookmark"] == 2      # ahead of the map
    assert [c["read"] for c in client.get(f"/api/books/{book}/read-map").json()["chapters"]] == [
        1.0, 0, 0]
    # The map's next word takes it back; the device report added nothing to the map.
    assert client.get(f"/api/books/{book}/read-frontier").json()["available_chapter"] == 0
    assert client.get(f"/api/books/{book}/position").json()["bookmark"] == 1
