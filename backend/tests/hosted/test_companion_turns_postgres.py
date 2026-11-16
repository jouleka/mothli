"""Conversations inside a book in hosted mode (ADR 0041 part 2), against real PostgreSQL.

Two readers through the hosted HTTP app on a non-BYPASSRLS tenant role; the table under row-level
security alone; the same adapter through a superuser connection, where only its own owner
predicates keep readers apart; the role's exact grants; and what a response may carry. Turns are
stored through the adapter, as the streamed chat (part 5) will store them.
"""

from __future__ import annotations

import asyncio
import uuid

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import conninfo, sql

from app.hosted.auth.api import CSRF_HEADER
from app.hosted.auth.repository import AuthConfigurationError
from app.hosted.companion.source import HostedBookSource
from app.hosted.companion.store import HostedCompanionCatalog
from app.hosted.tenant.grants import TENANT_TABLE_PRIVILEGES
from app.hosted.tenant.models import OwnerId
from app.hosted.tenant.repository import PostgresTenantRepository
from app.read_map import book_record, read_snapshot
from tests.hosted import test_companion_read_map_postgres as read_map_gate
from tests.hosted.test_tenant_postgres import _become

pytestmark = pytest.mark.postgres

# The tenant gate's database and RLS-enforced role, and part 1's two signed-in readers.
admin_dsn = read_map_gate.admin_dsn
database = read_map_gate.database
tenant_dsn = read_map_gate.tenant_dsn
readers = read_map_gate.readers
reading_app = read_map_gate.reading_app
lamp_book = read_map_gate.lamp_book
upload = read_map_gate.upload

TURN_FIELDS = {"role", "content", "atom", "reaction", "created_at"}


def mark(client: TestClient, csrf: str, book: str, atoms: list[int], read: bool = True) -> None:
    response = client.put(f"/api/books/{book}/read-chapters", json={"atoms": atoms, "read": read},
                          headers={CSRF_HEADER: csrf})
    assert response.status_code == 200, response.text


def say(app, owner: uuid.UUID, book: str, *lines: tuple[str, str], reaction: bool = False,
        drew_on: bool = True) -> list[int]:
    """Store turns as the chat does: with what is read of the book now as their record."""
    catalog = HostedCompanionCatalog(app.state.companion_dsn, OwnerId(owner))
    source = HostedBookSource(app.state.object_storage, catalog, app.state.companion_sources)
    point, _chapters, _state, _manifest, record, held = read_snapshot(
        book, catalog, app.state.settings, source)
    return [catalog.add_turn(book, incarnation=record["incarnation"], position_epoch=0,
                             atom=point["atom"], offset=point["offset"], role=role,
                             content=content, reaction=reaction and role == "user",
                             request_id=uuid.uuid4().hex[:12],
                             drew_on=book_record(book, record, held) if drew_on else None)
            for role, content in lines]


def turns_of(dsn: str, owner: uuid.UUID) -> list[tuple]:
    """Every stored turn of one owner, read past row-level security."""
    with psycopg.connect(dsn) as conn:
        return conn.execute(
            "SELECT * FROM conversation_turns WHERE owner_id=%s ORDER BY turn_id",
            (owner,)).fetchall()


def incarnation_of(dsn: str, book: str) -> str:
    with psycopg.connect(dsn) as conn:
        return str(conn.execute("SELECT incarnation FROM books WHERE id=%s",
                                (book,)).fetchone()[0])


def shown(client: TestClient, book: str) -> list[str]:
    response = client.get(f"/api/books/{book}/conversation")
    assert response.status_code == 200, response.text
    return [turn["content"] for turn in response.json()["turns"]]


def test_second_reader_gets_first_readers_book_as_missing_on_conversation_routes(
    reading_app, readers: dict, database: str
) -> None:
    """The break it catches: a conversation route that tells a second reader the first reader's
    book exists (a status or body that differs from a random id's), shows them its turns, or lets
    their clearing delete the first reader's conversation. Runs on the RLS-enforced tenant role,
    so both barriers are on; the database is checked afterwards."""
    client, tokens, app = reading_app
    book_a = upload(client, tokens, "a", lamp_book())
    csrf_a = _become(client, tokens, "a")
    mark(client, csrf_a, book_a, [1])
    say(app, readers["a"], book_a, ("user", "Poor Mara!"), ("assistant", "Mara kept lamp 2."),
        reaction=True)
    HostedCompanionCatalog(app.state.companion_dsn, OwnerId(readers["a"])).add_turn(
        None, incarnation="home", position_epoch=0, atom=0, offset=0, role="user",
        content="A's home words.", drew_on={})
    seen_by_a = client.get(f"/api/books/{book_a}/conversation").json()
    assert [turn["content"] for turn in seen_by_a["turns"]] == ["Poor Mara!", "Mara kept lamp 2."]
    before = turns_of(database, readers["a"])
    assert len(before) == 3

    # B has a copy of the very same file: nothing may be shared through its content.
    book_b = upload(client, tokens, "b", lamp_book())
    csrf_b = _become(client, tokens, "b")
    missing = uuid.uuid4()
    for method in ("GET", "DELETE"):
        hidden = client.request(method, f"/api/books/{book_a}/conversation",
                                headers={CSRF_HEADER: csrf_b})
        absent = client.request(method, f"/api/books/{missing}/conversation",
                                headers={CSRF_HEADER: csrf_b})
        assert (hidden.status_code, hidden.json()) == (absent.status_code, absent.json()), method
        assert (hidden.status_code, hidden.json()) == (404, {"detail": "unknown book"}), method
        assert hidden.headers["cache-control"] == "private, no-store"

    # B's own copy holds only what B said, and B's clearing clears only B's.
    mark(client, csrf_b, book_b, [1])
    say(app, readers["b"], book_b, ("user", "B's question."))
    assert shown(client, book_b) == ["B's question."]
    cleared = client.delete(f"/api/books/{book_b}/conversation", headers={CSRF_HEADER: csrf_b})
    assert cleared.status_code == 204 and cleared.content == b""
    assert shown(client, book_b) == []
    assert turns_of(database, readers["b"]) == []

    assert turns_of(database, readers["a"]) == before
    _become(client, tokens, "a")
    assert client.get(f"/api/books/{book_a}/conversation").json() == seen_by_a
    assert turns_of(database, readers["a"]) == before


def test_conversation_turns_are_isolated_by_row_level_security_alone(
    reading_app, readers: dict, tenant_dsn: str, database: str
) -> None:
    """The break it catches: a conversation table without a working owner policy, so a query that
    forgot its owner predicate could read, delete or plant another reader's turns. Here no
    predicate names the owner: only the policy does."""
    client, tokens, app = reading_app
    book_a = upload(client, tokens, "a", lamp_book())
    mark(client, _become(client, tokens, "a"), book_a, [1])
    say(app, readers["a"], book_a, ("user", "Poor Mara!"))
    HostedCompanionCatalog(app.state.companion_dsn, OwnerId(readers["a"])).add_turn(
        None, incarnation="home", position_epoch=0, atom=0, offset=0, role="user",
        content="A's home words.", drew_on={})
    before = turns_of(database, readers["a"])
    assert len(before) == 2
    incarnation = incarnation_of(database, book_a)

    with psycopg.connect(tenant_dsn, autocommit=True) as conn:
        conn.execute("SELECT set_config('app.owner_id', %s, false)", (str(readers["b"]),))
        assert conn.execute("SELECT count(*) FROM conversation_turns").fetchone()[0] == 0
        assert conn.execute("DELETE FROM conversation_turns").rowcount == 0
        # Rows naming A, with or without A's real book: only WITH CHECK can refuse them.
        for book, book_incarnation in ((book_a, incarnation), (None, None)):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(
                    """INSERT INTO conversation_turns
                         (owner_id,book_id,book_incarnation,role,content)
                       VALUES (%s,%s,%s,'user','planted')""",
                    (readers["a"], book, book_incarnation))
        # A row of B's own that names A's book: B holds no such book.
        with pytest.raises(psycopg.Error):
            conn.execute(
                """INSERT INTO conversation_turns (owner_id,book_id,book_incarnation,role,content)
                   VALUES (%s,%s,%s,'user','planted')""",
                (readers["b"], book_a, incarnation))
    assert turns_of(database, readers["a"]) == before
    assert turns_of(database, readers["b"]) == []


def test_turn_adapter_isolates_owners_by_its_own_predicates_with_rls_bypassed(
    reading_app, readers: dict, database: str
) -> None:
    """The break it catches: an adapter query that leans on row-level security alone. Through a
    superuser connection, where RLS does not apply, an adapter bound to reader B must see none of
    reader A's turns (a book's or the home conversation's), clear none of them, and store nothing
    against A's book (ADR 0021 Verification)."""
    client, tokens, app = reading_app
    book_a = upload(client, tokens, "a", lamp_book())
    mark(client, _become(client, tokens, "a"), book_a, [1])
    say(app, readers["a"], book_a, ("user", "Poor Mara!"), ("assistant", "Mara kept lamp 2."),
        reaction=True)
    with psycopg.connect(database) as conn:
        assert conn.execute("SELECT rolsuper FROM pg_roles WHERE rolname=current_user").fetchone()[0]
    incarnation = incarnation_of(database, book_a)

    owner_a = HostedCompanionCatalog(database, OwnerId(readers["a"]))
    owner_a.add_turn(None, incarnation="home", position_epoch=0, atom=0, offset=0, role="user",
                     content="A's home words.", drew_on={})
    assert len(owner_a.get_turns(book_a, incarnation=incarnation)) == 2      # the rows are there
    assert [t["content"] for t in owner_a.get_turns(book_a, incarnation=incarnation,
                                                     reactions=True)] == ["Poor Mara!"]
    assert [t["content"] for t in owner_a.get_turns(None, incarnation="home")] == [
        "A's home words."]
    before = turns_of(database, readers["a"])

    other = HostedCompanionCatalog(database, OwnerId(readers["b"]))
    assert other.get_turns(book_a, incarnation=incarnation) == []
    assert other.get_turns(book_a, incarnation=incarnation, reactions=True) == []
    assert other.get_turns(None, incarnation="home") == []
    other.clear_turns(book_a)
    other.clear_turns(None)
    with pytest.raises(LookupError):
        other.add_turn(book_a, incarnation=incarnation, position_epoch=0, atom=1, offset=0,
                       role="user", content="planted", drew_on=None)
    assert turns_of(database, readers["a"]) == before
    assert turns_of(database, readers["b"]) == []

    # A's own clearing of the book leaves A's home conversation, and the reverse.
    owner_a.clear_turns(book_a)
    assert owner_a.get_turns(book_a, incarnation=incarnation) == []
    assert len(owner_a.get_turns(None, incarnation="home")) == 1
    owner_a.clear_turns(None)
    assert turns_of(database, readers["a"]) == []


def test_tenant_role_holds_exactly_the_conversation_grants(tenant_dsn: str, database: str) -> None:
    """The break it catches: a conversation table the tenant role cannot use (every route fails),
    or one with a privilege nobody reviewed (UPDATE would let a stored turn be rewritten after its
    record was taken; TRUNCATE ignores row-level security). The startup check reads the same
    allow-list the fixture granted, so one grant more or one fewer refuses startup."""
    assert TENANT_TABLE_PRIVILEGES["conversation_turns"] == {"SELECT", "INSERT", "DELETE"}
    repository = PostgresTenantRepository(tenant_dsn)
    asyncio.run(repository.check_runtime_role())
    role = conninfo.conninfo_to_dict(tenant_dsn)["user"]
    with psycopg.connect(database, autocommit=True) as conn:
        held = {
            privilege
            for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES",
                              "TRIGGER")
            if conn.execute("SELECT has_table_privilege(%s, 'public.conversation_turns', %s)",
                            (role, privilege)).fetchone()[0]
        }
    assert held == TENANT_TABLE_PRIVILEGES["conversation_turns"]

    for grant, undo in (
        ("GRANT UPDATE ON conversation_turns TO {}", "REVOKE UPDATE ON conversation_turns FROM {}"),
        ("REVOKE DELETE ON conversation_turns FROM {}", "GRANT DELETE ON conversation_turns TO {}"),
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


def test_conversation_routes_require_a_session_and_clearing_requires_csrf(reading_app) -> None:
    """The break it catches: a conversation readable without a signed-in reader, or one another
    site could clear without the CSRF token."""
    client, tokens, _app = reading_app
    book = uuid.uuid4()
    client.cookies.clear()
    assert client.get(f"/api/books/{book}/conversation").status_code == 401
    assert client.delete(f"/api/books/{book}/conversation").status_code == 401
    _become(client, tokens, "a")
    assert client.delete(f"/api/books/{book}/conversation").status_code == 403


def test_no_turn_id_or_other_guessable_id_reaches_a_response(
    reading_app, readers: dict, database: str
) -> None:
    """The break it catches: a conversation response that hands out a turn id (an increasing
    integer, global across readers, so it would count everyone's conversations and name turns to
    guess at) or any field the local response does not have. The read point is the read map's own,
    already served by the read-map routes."""
    client, tokens, app = reading_app
    book = upload(client, tokens, "a", lamp_book())
    csrf = _become(client, tokens, "a")
    mark(client, csrf, book, [1])
    ids = say(app, readers["a"], book, ("user", "Poor Mara!"), ("assistant", "Mara kept lamp 2."),
              reaction=True)
    assert ids == sorted(ids) and len(set(ids)) == 2

    response = client.get(f"/api/books/{book}/conversation")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"read_frontier", "turns"}
    assert [set(turn) for turn in body["turns"]] == [TURN_FIELDS, TURN_FIELDS]
    assert [(turn["role"], turn["reaction"]) for turn in body["turns"]] == [
        ("user", True), ("assistant", False)]
    assert "turn_id" not in response.text and "request_id" not in response.text
    assert "drew_on" not in response.text and str(readers["a"]) not in response.text

    catalog = HostedCompanionCatalog(app.state.companion_dsn, OwnerId(readers["a"]))
    source = HostedBookSource(app.state.object_storage, catalog, app.state.companion_sources)
    point = read_snapshot(book, catalog, app.state.settings, source)[0]
    assert body["read_frontier"] == point

    cleared = client.delete(f"/api/books/{book}/conversation", headers={CSRF_HEADER: csrf})
    assert cleared.status_code == 204 and cleared.content == b""
    assert turns_of(database, readers["a"]) == []
