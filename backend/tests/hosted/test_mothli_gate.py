"""The gate in front of Mothli (ADR 0045 step 5, ADR 0046), without a database.

Which hosted routes are behind the gate and which are reading (and so never gated), checked
against every route the hosted app has, so a new route has to be placed on one side; the gate's
answer to a new or an ended reader on every gated route, given before anything of the reader's
is opened; and the session's ``mothli``. Where the reader stands is stood in for
(``gate.standing``), so no database and no model is needed. The same rules on real PostgreSQL,
from the plan's own row, are in ``test_mothli_gate_postgres.py``.
"""

from __future__ import annotations

import uuid
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime

import psycopg
import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app import stream_conversation
from app.hosted.auth.api import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE
from app.hosted.auth.models import Principal
from app.hosted.auth.tokens import digest_token, new_token
from app.hosted.companion import gate, jobs, routes
from app.hosted.companion.memory import HostedBookStore
from app.hosted.companion.source import HostedBookSource
from app.hosted.companion.spend import SpendStore
from app.hosted.companion.store import HostedCompanionCatalog
from app.main import create_app
from tests.hosted.test_companion_stream_settings import CHAT, hosted_settings

BOOK = str(uuid.uuid4())

# Every route behind the gate: what Mothli wrote or knows.
GATED = {
    ("POST", "/api/books/{book_id}/ask/stream"),
    ("GET", "/api/books/{book_id}/conversation"),
    ("GET", "/api/books/{book_id}/graph"),
    ("GET", "/api/books/{book_id}/character/{entity_id}"),
    ("GET", "/api/books/{book_id}/notes"),
    ("GET", "/api/books/{book_id}/ingest"),
    ("POST", "/api/reader/ask/stream"),
    ("GET", "/api/reader/conversation"),
}
# Every other route a signed-in reader has, deliberately not behind it: reading, the place,
# highlights, notes, bookmarks, the library and the free library, the account and the copy of
# one's data, what Mothli remembers (the reader's own data, to read and to forget), clearing a
# conversation, and the plan itself.
NOT_GATED = {
    ("GET", "/api/limits"), ("POST", "/api/books"), ("GET", "/api/books"),
    ("GET", "/api/books/{book_id}"), ("GET", "/api/books/{book_id}/manifest"),
    ("GET", "/api/books/{book_id}/cover"),
    ("GET", "/api/books/{book_id}/epub"), ("DELETE", "/api/books/{book_id}"),
    ("GET", "/api/books/{book_id}/position"), ("PUT", "/api/books/{book_id}/position"),
    ("POST", "/api/books/{book_id}/position/reset"),
    ("GET", "/api/books/{book_id}/preferences"), ("PUT", "/api/books/{book_id}/preferences"),
    ("GET", "/api/books/{book_id}/search"), ("GET", "/api/books/{book_id}/marks"),
    ("GET", "/api/books/{book_id}/marks/export"),
    ("POST", "/api/books/{book_id}/highlights"), ("POST", "/api/books/{book_id}/annotations"),
    ("POST", "/api/books/{book_id}/bookmarks"),
    ("PATCH", "/api/books/{book_id}/highlights/{mark_id}"),
    ("PATCH", "/api/books/{book_id}/annotations/{mark_id}"),
    ("PATCH", "/api/books/{book_id}/bookmarks/{mark_id}"),
    ("DELETE", "/api/books/{book_id}/highlights/{mark_id}"),
    ("DELETE", "/api/books/{book_id}/annotations/{mark_id}"),
    ("DELETE", "/api/books/{book_id}/bookmarks/{mark_id}"),
    ("GET", "/api/costs"),
    ("GET", "/api/books/{book_id}/read-map"), ("PUT", "/api/books/{book_id}/read-page"),
    ("PUT", "/api/books/{book_id}/read-chapters"), ("GET", "/api/books/{book_id}/read-frontier"),
    ("PUT", "/api/books/{book_id}/read-frontier"), ("DELETE", "/api/books/{book_id}/conversation"),
    ("DELETE", "/api/reader/conversation"), ("GET", "/api/reader/profile"),
    ("GET", "/api/reader/recollections"), ("DELETE", "/api/reader/recollections/{public_id}"),
    ("DELETE", "/api/reader/recollections"),
    ("GET", "/api/classics"), ("GET", "/api/classics/front"),
    ("GET", "/api/classics/{classic_id}/cover"),
    ("GET", "/api/classics/{classic_id}"), ("POST", "/api/classics/{classic_id}"),
    ("POST", "/api/account/delete"), ("POST", "/api/account/keep"), ("GET", "/api/account/export"),
    ("GET", "/api/billing/plan"),
    # The public pages' read (ADR 0045 step 8): signed out, nothing of any reader's.
    ("GET", "/api/site"),
    ("GET", "/api/auth/login"), ("GET", "/api/auth/callback"), ("GET", "/api/auth/session"),
    ("POST", "/api/auth/logout"),
}
_THE_GATE = (routes._mothli, routes._csrf_mothli)

ENDED = {"state": "ended", "why": "plan"}
NEW = {"state": "new", "why": None}


class _Reader:
    """A sign-in service that knows one reader, with a CSRF token."""

    def __init__(self) -> None:
        self.session, self.csrf, self.owner = new_token(), new_token(), uuid.uuid4()

    async def check_runtime_role(self) -> None:
        return None

    async def authenticate_session(self, *, session_digest, now, idle_ttl):
        if session_digest != digest_token(self.session):
            return None
        return Principal(owner_id=self.owner, session_id=uuid.uuid4(), display_name="Reader",
                         email=None, csrf_digest=digest_token(self.csrf),
                         expires_at=datetime(2026, 7, 17, tzinfo=UTC))


class _Tenant:
    async def check_runtime_role(self) -> None:
        return None

    async def consume_request_limit(self, owner_id) -> None:
        return None


@pytest.fixture()
def signed_in(tmp_path):
    """(client, reader) on the hosted app with the chat on, signed in, with no database."""
    with ExitStack() as stack:
        yield lambda **values: stack.enter_context(_signed_in(tmp_path, **values))


@contextmanager
def _signed_in(tmp_path, **values):
    reader = _Reader()
    app = create_app(hosted_settings(tmp_path, **{**CHAT, **values}),
                     auth_repository=reader, tenant_repository=_Tenant(), oidc_client=object(),
                     auth_clock=lambda: datetime(2026, 7, 16, tzinfo=UTC))
    with TestClient(app, base_url="https://reader.example") as client:
        client.cookies.set(SESSION_COOKIE, reader.session, domain="reader.example", path="/")
        client.cookies.set(CSRF_COOKIE, reader.csrf, domain="reader.example", path="/")
        yield client, reader


def stand(monkeypatch, *answers) -> list:
    """Where the reader stands, as the database would answer, one answer per read (the last
    repeated); the owners it was asked for are kept."""
    asked, left = [], list(answers)

    def standing(dsn, owner_id):
        asked.append(owner_id.value)
        return dict(left.pop(0) if len(left) > 1 else left[0])

    monkeypatch.setattr(gate, "standing", standing)
    return asked


class Touched:
    """Everything behind a gated route that opens the reader's catalog, source, search or story
    memory, reserves or asks for background work: each call is kept, then
    refused, so a route that went past the gate would fail here even if nothing else noticed."""

    PLACES = [(HostedCompanionCatalog, "__init__"), (HostedBookStore, "__init__"),
              (HostedBookSource, "__init__"), (SpendStore, "reserve_cost"),
              (jobs.ChapterNotesQueue, "ensure"),
              (jobs.RecollectQueue, "ensure"), (routes, "stream_read_prefix"),
              (routes, "stream_home"), (routes, "read_snapshot"), (routes, "home_turns"),
              (routes, "visible_turns"), (stream_conversation, "compact_candidates")]

    def __init__(self, monkeypatch) -> None:
        self.seen = []
        for owner, name in self.PLACES:
            def touched(*_args, _where=f"{owner.__name__}.{name}", **_kwargs):
                self.seen.append(_where)
                raise AssertionError(f"{_where} reached behind the gate")

            monkeypatch.setattr(owner, name, touched)


def _api_routes(app):
    """Every route the app serves, including those of the routers it includes."""
    for route in app.routes:
        if isinstance(route, APIRoute):
            yield route
        for inner in getattr(getattr(route, "original_router", None), "routes", ()):
            if isinstance(inner, APIRoute):
                yield inner


def _gated_on(route: APIRoute) -> bool:
    stack = [route.dependant]
    while stack:
        dependant = stack.pop()
        if dependant.call in _THE_GATE:
            return True
        stack.extend(dependant.dependencies)
    return False


def requests_for(book: str = BOOK) -> list[tuple[str, str, dict | None]]:
    point = {"id": "a" * 64, "atom": 1, "offset": 0, "position_epoch": 0,
             "available_chapter": 1}
    return [
        ("POST", f"/api/books/{book}/ask/stream", {"question": "Who?", "read_frontier": point}),
        ("GET", f"/api/books/{book}/conversation", None),
        ("GET", f"/api/books/{book}/graph", None),
        ("GET", f"/api/books/{book}/character/{uuid.uuid4()}", None),
        ("GET", f"/api/books/{book}/notes?bookmark=3", None),
        ("GET", f"/api/books/{book}/ingest", None),
        ("POST", "/api/reader/ask/stream", {"question": "What next?"}),
        ("GET", "/api/reader/conversation", None),
    ]


# ---- which routes ----------------------------------------------------------------------------------

def test_every_hosted_route_is_placed_and_the_gate_stands_in_front_of_exactly_the_gated(
        signed_in) -> None:
    """The break it catches: a route that serves what Mothli wrote or knows left open to a
    reader who does not pay (or a new route added without a decision), or a reading route (the place, a page
    report, highlights, what Mothli remembers) shut by the gate."""
    client, _reader = signed_in(paddle_api_key=None)
    every = list(_api_routes(client.app))
    found = {(method, route.path) for route in every if route.path.startswith("/api/")
             and not route.path.startswith("/api/health") for method in route.methods}
    assert not GATED & NOT_GATED
    assert found == GATED | NOT_GATED, sorted(found ^ (GATED | NOT_GATED))
    behind = {(method, route.path) for route in every if _gated_on(route)
              for method in route.methods}
    assert behind == GATED
    assert len(requests_for()) == len(GATED)


# ---- the answer ------------------------------------------------------------------------------------

@pytest.mark.parametrize("standing", [NEW, ENDED])
def test_a_reader_who_does_not_pay_gets_the_gates_answer_on_every_gated_route_and_nothing_runs(
        signed_in, monkeypatch, standing) -> None:
    """The break it catches: a new reader (who has never had a plan or a gift, so who would have
    started free weeks before ADR 0046) or an ended one opening their catalog, source, search or
    story memory, reserving or asking for background work before they are refused; a refusal of
    another shape (a 429, a stream that opens and then fails); or one whose body differs by route
    or by book."""
    client, reader = signed_in()
    why = standing["why"]
    asked = stand(monkeypatch, standing)
    touched = Touched(monkeypatch)
    answers = []
    for book in (BOOK, str(uuid.uuid4())):
        for method, path, body in requests_for(book):
            response = client.request(method, path, json=body, headers={CSRF_HEADER: reader.csrf})
            assert response.status_code == 402, (path, response.text)
            assert response.headers["content-type"].startswith("application/json")
            answers.append(response.json())
    assert answers == [{"detail": {"code": "mothli_plan", "why": why}}] * len(answers)
    assert touched.seen == []
    assert asked == [reader.owner] * len(answers)


def test_a_gated_write_checks_the_csrf_token_before_the_gate(signed_in, monkeypatch) -> None:
    """The break it catches: the gate answering a request that carries no CSRF token (telling a
    cross-site page where a reader stands)."""
    client, _reader = signed_in()
    for standing in (NEW, ENDED):
        asked = stand(monkeypatch, standing)
        for path, body in (("/api/reader/ask/stream", {"question": "Hi"}),
                           (f"/api/books/{BOOK}/ask/stream", {"question": "Hi"})):
            assert client.post(path, json=body).status_code == 403
        assert asked == []


def test_a_paying_reader_goes_past_the_gate_to_the_route_as_before(
        signed_in, monkeypatch) -> None:
    """The break it catches: the gate refusing a paying reader (a gift or a live Paddle plan).
    Past it, the route's own body runs (here it reaches the catalog, which the stand-in stops)."""
    client, reader = signed_in()
    stand(monkeypatch, {"state": "paying", "why": None})
    touched = Touched(monkeypatch)
    for method, path, body in requests_for():
        with pytest.raises(AssertionError, match="reached behind the gate"):
            client.request(method, path, json=body, headers={CSRF_HEADER: reader.csrf})
    assert touched.seen and all(where in {"HostedCompanionCatalog.__init__",
                                          "HostedBookStore.__init__"} for where in touched.seen)


def test_a_plan_that_cannot_be_read_serves_nothing(signed_in, monkeypatch) -> None:
    """The break it catches: a database that cannot be reached letting a request past the gate."""
    client, reader = signed_in()

    def down(dsn, owner_id):
        raise psycopg.OperationalError("down")

    monkeypatch.setattr(gate, "standing", down)
    touched = Touched(monkeypatch)
    for method, path, body in requests_for():
        response = client.request(method, path, json=body, headers={CSRF_HEADER: reader.csrf})
        assert response.status_code == 503, path
    assert touched.seen == []


# ---- the session ------------------------------------------------------------------------------------

def test_the_session_says_where_the_reader_stands_and_nothing_more(signed_in, monkeypatch) -> None:
    """The break it catches: the screens learning a reader is ended only from a refused request;
    the session carrying a date, an amount or a count; or a session that fails because the plan
    cannot be read (the gate still answers every route)."""
    client, reader = signed_in()
    for standing in (NEW, {"state": "paying", "why": None}, ENDED):
        asked = stand(monkeypatch, standing)
        session = client.get("/api/auth/session")
        assert session.status_code == 200
        assert session.json()["mothli"] == standing and asked == [reader.owner]
        assert set(session.json()) == {"user", "expires_at", "chat", "mothli"}

    def down(dsn, owner_id):
        raise psycopg.OperationalError("down")

    monkeypatch.setattr(gate, "standing", down)
    session = client.get("/api/auth/session")
    assert session.status_code == 200 and session.json()["mothli"] is None


def test_without_the_chat_there_is_no_mothli_in_the_session(signed_in, monkeypatch) -> None:
    """The break it catches: a server with no chat telling the app about a plan for a Mothli it
    does not have."""
    client, _reader = signed_in(reader_conversation_jev=False, jev_enabled=False,
                                typesafe_api_key=None, openai_api_key=None)
    asked = stand(monkeypatch, ENDED)
    session = client.get("/api/auth/session").json()
    assert session["chat"] is False and session["mothli"] is None
    assert asked == []
