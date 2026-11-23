"""The gate in front of Mothli (ADR 0045 step 5), without a database.

Which hosted routes are behind the gate and which are reading (and so never gated), checked
against every route the hosted app has, so a new route has to be placed on one side; the gate's
answer to an ended reader on every gated route, given before anything of the reader's is opened;
the start route; and the session's ``mothli``. Where the reader stands is stood in for
(``gate.standing``), so no database and no model is needed. The same rules on real PostgreSQL,
from the plan's own row, are in ``test_mothli_gate_postgres.py``.
"""

from __future__ import annotations

import uuid
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime, timedelta

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
# conversation, the plan itself, and the start of the free weeks (which answers the state).
NOT_GATED = {
    ("GET", "/api/limits"), ("POST", "/api/books"), ("GET", "/api/books"),
    ("GET", "/api/books/{book_id}"), ("GET", "/api/books/{book_id}/manifest"),
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
    ("DELETE", "/api/reader/recollections"), ("POST", "/api/reader/mothli/start"),
    ("GET", "/api/classics"), ("GET", "/api/classics/front"),
    ("GET", "/api/classics/{classic_id}"), ("POST", "/api/classics/{classic_id}"),
    ("POST", "/api/account/delete"), ("POST", "/api/account/keep"), ("GET", "/api/account/export"),
    ("GET", "/api/billing/plan"),
    # The public pages' read (ADR 0045 step 8): signed out, nothing of any reader's.
    ("GET", "/api/site"),
    ("GET", "/api/auth/login"), ("GET", "/api/auth/callback"), ("GET", "/api/auth/session"),
    ("POST", "/api/auth/logout"),
}
_THE_GATE = (routes._mothli, routes._csrf_mothli)

ENDED = {"state": "ended", "why": "time", "ending_note": False}


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
    memory, reserves, starts the free weeks or asks for background work: each call is kept, then
    refused, so a route that went past the gate would fail here even if nothing else noticed."""

    PLACES = [(HostedCompanionCatalog, "__init__"), (HostedBookStore, "__init__"),
              (HostedBookSource, "__init__"), (SpendStore, "reserve_cost"),
              (SpendStore, "start_free_weeks"), (jobs.ChapterNotesQueue, "ensure"),
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
    """The break it catches: a route that serves what Mothli wrote or knows left open to an
    ended reader (or a new one added without a decision), or a reading route (the place, a page
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

@pytest.mark.parametrize("why", ["time", "ceiling", "plan"])
def test_an_ended_reader_gets_the_gates_answer_on_every_gated_route_and_nothing_behind_it_runs(
        signed_in, monkeypatch, why) -> None:
    """The break it catches: an ended reader's request opening their catalog, source, search or
    story memory, reserving, starting the free weeks or asking for background work before it is
    refused; a refusal of another shape (a 429, a stream that opens and then fails); or one whose
    body differs by route or by book."""
    client, reader = signed_in()
    asked = stand(monkeypatch, {"state": "ended", "why": why, "ending_note": False})
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
    asked = stand(monkeypatch, ENDED)
    for path, body in (("/api/reader/ask/stream", {"question": "Hi"}),
                       ("/api/reader/mothli/start", {})):
        assert client.post(path, json=body).status_code == 403
    assert asked == []


@pytest.mark.parametrize("state", ["new", "free", "paying"])
def test_anyone_not_ended_goes_past_the_gate_to_the_route_as_before(
        signed_in, monkeypatch, state) -> None:
    """The break it catches: the gate refusing a reader who is new, in the free weeks or paying.
    Past it, the route's own body runs (here it reaches the catalog, which the stand-in stops)."""
    client, reader = signed_in()
    stand(monkeypatch, {"state": state, "why": None, "ending_note": False})
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


# ---- the gentle note -------------------------------------------------------------------------------

@pytest.mark.parametrize("state, left, note", [
    ("free", timedelta(days=2), True), ("free", timedelta(hours=1), True),
    ("free", timedelta(days=2, seconds=1), False), ("free", timedelta(days=10), False),
    ("paying", timedelta(hours=1), False), ("ended", timedelta(hours=1), False),
])
def test_the_ending_note_is_for_the_free_weeks_last_two_days_only(monkeypatch, state, left,
                                                                  note) -> None:
    """The break it catches: the note shown before the last two days, after the free weeks, to
    a paying reader, or with anything beyond yes or no (a date, a count)."""
    now = datetime(2026, 11, 23, 12, tzinfo=UTC)

    def plan(self):
        return {"state": state, "why": "time" if state == "ended" else None,
                "free_ends_at": now + left, "now": now}

    monkeypatch.setattr(SpendStore, "plan", plan)
    from app.hosted.tenant.models import OwnerId
    assert gate.standing("postgresql://unused.invalid/x", OwnerId(uuid.uuid4())) == {
        "state": state, "why": "time" if state == "ended" else None, "ending_note": note}


# ---- the start route ------------------------------------------------------------------------------

def test_the_start_route_starts_the_free_weeks_and_asks_for_the_books_work_unless_ended(
        signed_in, monkeypatch) -> None:
    """The break it catches: the start stamping an ended reader (a paid plan that stopped could
    otherwise reach the free weeks), not stamping a new one, or not asking for the book's
    background work; or an answer other than where the reader stands."""
    client, reader = signed_in()
    stamped, finished = [], []
    monkeypatch.setattr(SpendStore, "start_free_weeks", lambda self: stamped.append(self))
    monkeypatch.setattr(routes, "_finished",
                        lambda request, owner_id, book: finished.append((owner_id.value, book)))
    free = {"state": "free", "why": None, "ending_note": False}
    stand(monkeypatch, {"state": "new", "why": None, "ending_note": False}, free)
    answer = client.post("/api/reader/mothli/start", json={"book_id": BOOK},
                         headers={CSRF_HEADER: reader.csrf})
    assert (answer.status_code, answer.json()) == (200, free)
    assert answer.headers["cache-control"] == "private, no-store"
    assert len(stamped) == 1 and finished == [(reader.owner, BOOK)]

    stand(monkeypatch, free)                       # no book: the free weeks only
    assert client.post("/api/reader/mothli/start", json={},
                       headers={CSRF_HEADER: reader.csrf}).json() == free
    assert len(stamped) == 2 and len(finished) == 1

    stand(monkeypatch, {"state": "ended", "why": "plan", "ending_note": False})
    answer = client.post("/api/reader/mothli/start", json={"book_id": BOOK},
                         headers={CSRF_HEADER: reader.csrf})
    assert (answer.status_code, answer.json()) == (200, {"state": "ended", "why": "plan",
                                                         "ending_note": False})
    assert len(stamped) == 2 and len(finished) == 1

    for body in ({"book_id": "not-a-book"}, {"owner_id": str(uuid.uuid4())}):
        assert client.post("/api/reader/mothli/start", json=body,
                           headers={CSRF_HEADER: reader.csrf}).status_code == 422
    assert len(stamped) == 2


def test_without_the_chat_there_is_no_start_and_no_mothli_in_the_session(
        signed_in, monkeypatch) -> None:
    """The break it catches: a server with no chat starting anyone's free weeks, or telling the
    app about a plan for a Mothli it does not have."""
    client, reader = signed_in(reader_conversation_jev=False, jev_enabled=False,
                               typesafe_api_key=None, openai_api_key=None)
    asked = stand(monkeypatch, ENDED)
    assert client.post("/api/reader/mothli/start", json={},
                       headers={CSRF_HEADER: reader.csrf}).status_code == 404
    session = client.get("/api/auth/session").json()
    assert session["chat"] is False and session["mothli"] is None
    assert asked == []


# ---- the session ------------------------------------------------------------------------------------

def test_the_session_says_where_the_reader_stands_and_nothing_more(signed_in, monkeypatch) -> None:
    """The break it catches: the screens learning a reader is ended only from a refused request;
    the session carrying a date, an amount or a count; or a session that fails because the plan
    cannot be read (the gate still answers every route)."""
    client, reader = signed_in()
    for standing in ({"state": "new", "why": None, "ending_note": False},
                     {"state": "free", "why": None, "ending_note": True},
                     {"state": "paying", "why": None, "ending_note": False},
                     {"state": "ended", "why": "ceiling", "ending_note": False}):
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
