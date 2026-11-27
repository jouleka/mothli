"""The gate in front of Mothli (ADR 0045 step 5, ADR 0046), on real PostgreSQL.

Reading is free; all of Mothli (the chat in a book, the home chat, the cast, a person's card,
"Story so far", what Mothli has said) needs the plan from the first question. Where a reader
stands is ``reader_plan_state``'s, from the plan's own row and the clock: ``new`` (never had a
plan or a gift), ``paying`` (a gift or a live Paddle plan) or ``ended`` with why ``plan`` (a gift
or a plan they had stopped). Proven here: each state's answer on each gated route; a new or an
ended reader's request reaches no model, no search, no story memory and no book text, reserves
nothing and writes no plan; a reader who does not pay can still do everything that is reading; a
new reader's first question is refused and a gift or a Paddle plan lets them in; the gate's
answer says nothing of any other reader; what a reply reads, searches and sends is the same
however the reader pays; and the session tells the screens where the reader stands. Which
routes are gated is checked without a database in ``test_mothli_gate.py``.

Through the hosted HTTP app on the RLS-enforced tenant role, and at the spend adapter. Stand-in
writer and Jev only: no model is called and nothing is paid.
"""

from __future__ import annotations

import json
import uuid
from contextlib import ExitStack
from datetime import UTC, datetime
from decimal import Decimal

import psycopg
import pytest
from fastapi.testclient import TestClient

from app import stream_conversation
from app.hosted.companion import gate, jobs, routes
from app.hosted.companion.memory import HostedBookStore
from app.hosted.companion.source import HostedBookSource
from app.hosted.companion.spend import (
    PlanNeeded,
    ReaderAllowanceReached,
    SpendStore,
    month_line,
    next_month,
)
from app.hosted.companion.store import HostedCompanionCatalog
from app.hosted.limits import update_limits
from app.hosted.tenant.models import OwnerId
from app.main import create_app
from tests.hosted import test_companion_read_map_postgres as read_map_gate
from tests.hosted.test_background_by_state_postgres import end, pay
from tests.hosted.test_companion_recollections_postgres import HomeModels
from tests.hosted.test_companion_spend_postgres import reserve
from tests.hosted.test_companion_stream_postgres import (
    CHAT,
    QUESTION,
    Reader,
    StandIns,
    events_of,
    restore_caps,  # noqa: F401  (autouse: the hosted app sets this process's caps)
    sentences,
)
from tests.hosted.test_tenant_postgres import UnusedOIDCClient

pytestmark = pytest.mark.postgres

admin_dsn = read_map_gate.admin_dsn
database = read_map_gate.database
tenant_dsn = read_map_gate.tenant_dsn
# New readers: each test puts them where it means to.
readers = read_map_gate.new_readers
line, upload, lamp_book = read_map_gate.line, read_map_gate.upload, read_map_gate.lamp_book

# A paying reader's guards, low: $0.10 a UTC day, $2.00 a UTC month.
TODAY = {**CHAT, "hosted_reader_usd_per_day": 0.10, "hosted_reader_usd_per_month": 2.00}
# Chapter notes and story memory on, so a request that went past the gate would ask for both.
BOTH = {"reader_story_notes": True, "hosted_story_memory": True}
STATES = ["new", "gift", "paddle", "gift_ended", "paddle_ended"]
NOT_PAYING = {"new": None, "gift_ended": "plan", "paddle_ended": "plan"}
# What a reader's request could leave behind: none of it for a gated request.
TRACES = ("cost_reservations", "cost_ledger", "jobs", "conversation_turns", "reader_plans",
          "read_ranges", "reading_state")


@pytest.fixture()
def make_app(tenant_dsn: str, readers: dict, tmp_path):
    """The hosted app with low caps and any settings given; one per test."""
    with ExitStack() as stack:
        def make(**overrides):
            tokens, sessions = read_map_gate.signed_in(readers)
            app = create_app(
                read_map_gate.hosted_settings(tenant_dsn, tmp_path / "objects",
                                              **{**TODAY, **overrides}),
                auth_repository=sessions, oidc_client=UnusedOIDCClient(),
                auth_clock=lambda: datetime(2026, 7, 16, 12, tzinfo=UTC))
            client = stack.enter_context(TestClient(app, base_url="https://reader.example"))
            return client, tokens, app

        yield make


def state(dsn: str, owner: uuid.UUID) -> tuple:
    with psycopg.connect(dsn) as conn:
        return conn.execute("SELECT state, why FROM reader_plan_state(%s)", (owner,)).fetchone()


def chat_spent(dsn: str, owner: uuid.UUID, usd: Decimal) -> None:
    """One settled row of the reader's own chat today, written past row-level security as a reply
    would leave it."""
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "INSERT INTO cost_ledger (owner_id, phase, model, usd, idempotency_key, purpose,"
            " background) VALUES (%s, 'synthesis', 'gpt-6.1-terra', %s, %s, 'stream-reply',"
            " false)", (owner, usd, f"companion:{uuid.uuid4().hex}"))


def opened(make_app, readers, label="a", **settings):
    """A reader with the lamp book, part 1 read: ready to ask."""
    client, tokens, app = make_app(**settings)
    someone = Reader(client, tokens, label, readers[label])
    book = upload(client, tokens, label, lamp_book())
    someone.mark(book, [1])
    return someone, book, app


def refused(adapter, book, limit: str, message: str) -> None:
    with pytest.raises(ReaderAllowanceReached) as raised:
        reserve(adapter, book, 0.001)
    assert (raised.value.limit, raised.value.reader_message) == (limit, message)


def month_wall(dsn: str) -> str:
    """The line at the month's ceiling today: the day named is when the next UTC month has
    begun everywhere, by the database's clock."""
    with psycopg.connect(dsn) as conn:
        now = conn.execute("SELECT now()").fetchone()[0]
    return month_line(next_month(now))


def gated(adapter, book, why: str | None) -> None:
    """The reader does not pay: their next reply's first call is refused with the gate's answer."""
    with pytest.raises(PlanNeeded) as raised:
        reserve(adapter, book, 0.001)
    assert (raised.value.limit, raised.value.code, raised.value.why) == ("plan", "mothli_plan", why)
    assert raised.value.extra == {"status": 402, "why": why}


def gate_of(why: str | None) -> dict:
    """What a gated route answers a reader who does not pay."""
    return {"detail": {"code": "mothli_plan", "why": why}}


def shelve(dsn: str, owner: uuid.UUID) -> str:
    book = uuid.uuid4()
    with psycopg.connect(dsn) as conn:
        conn.execute("INSERT INTO books (owner_id,id,incarnation,title,author,schema_version)"
                     " VALUES (%s,%s,%s,'A book','An author',1)", (owner, book, uuid.uuid4()))
    return str(book)


def shelved(make_app, readers, labels=("a",), **settings):
    """Readers on one app, each with the same lamp book file and its part 1 read."""
    client, tokens, app = make_app(**settings)
    found = []
    for label in labels:
        someone = Reader(client, tokens, label, readers[label])
        book = upload(client, tokens, label, lamp_book())
        someone.mark(book, [1])
        found.append((someone, book))
    return app, found


def stand(database: str, owner: uuid.UUID, standing: str) -> None:
    """Put the reader where ``standing`` says, as the plan tests do: new (nothing), paying by a
    gift or a Paddle plan, or ended after a gift or a Paddle plan."""
    if standing in ("gift", "paddle"):
        pay(database, owner, standing)
    elif standing in ("gift_ended", "paddle_ended"):
        end(database, owner, standing.removesuffix("_ended"))
    expected = ("new", None) if standing == "new" else (
        ("ended", "plan") if standing.endswith("_ended") else ("paying", None))
    assert state(database, owner) == expected


def requests_for(someone: Reader, book: str, point: dict | None = None
                 ) -> list[tuple[str, str, dict | None]]:
    """One request to each gated route: the app's own fetches first, then the two questions
    (asked from the reader's place in ``book``, or ``point`` for a book that is not theirs)."""
    point = point or someone.point(book)
    return [
        ("GET", f"/api/books/{book}/conversation", None),
        ("GET", f"/api/books/{book}/graph", None),
        ("GET", f"/api/books/{book}/character/{uuid.uuid4()}", None),
        ("GET", f"/api/books/{book}/notes?bookmark=1", None),
        ("GET", f"/api/books/{book}/ingest", None),
        ("GET", "/api/reader/conversation", None),
        ("POST", f"/api/books/{book}/ask/stream", {"question": QUESTION, "read_frontier": point}),
        ("POST", "/api/reader/ask/stream", {"question": "Who is Mara?"}),
    ]


# What each gated route answers a reader past the gate (the stand-in story memory is empty, so a
# person's card is unknown).
PAST_THE_GATE = [200, 200, 404, 200, 200, 200, 200, 200]


def traces(dsn: str, owner: uuid.UUID) -> dict:
    with psycopg.connect(dsn) as conn:
        return {table: sorted(map(str, conn.execute(
            f"SELECT to_jsonb(t) FROM {table} AS t WHERE owner_id=%s", (owner,)).fetchall()))
            for table in TRACES}


class Recorded:
    """Everything behind a gated route that opens the reader's catalog, source, search or story
    memory, reserves or asks for background work: each call is kept and then made as usual, so a
    request that went past the gate shows here."""

    PLACES = [(HostedCompanionCatalog, "__init__"), (HostedBookStore, "__init__"),
              (HostedBookSource, "__init__"), (SpendStore, "reserve_cost"),
              (jobs.ChapterNotesQueue, "ensure"),
              (jobs.RecollectQueue, "ensure"), (routes, "stream_read_prefix"),
              (routes, "stream_home"), (routes, "read_snapshot"), (routes, "home_turns"),
              (routes, "visible_turns"), (stream_conversation, "compact_candidates")]

    def __init__(self, monkeypatch) -> None:
        self.seen = []
        for owner, name in self.PLACES:
            real = getattr(owner, name)

            def recorded(*args, _real=real, _where=f"{owner.__name__}.{name}", **kwargs):
                self.seen.append(_where)
                return _real(*args, **kwargs)

            monkeypatch.setattr(owner, name, recorded)


# ---- each state's answer on each route ---------------------------------------------------------------

@pytest.mark.parametrize("standing", STATES)
def test_each_state_gets_its_answer_on_every_gated_route(
        make_app, readers, database, monkeypatch, standing):
    """The break it catches: a new reader (who never had a plan or a gift) or an ended one
    answered by any gated route, or answered anything but the gate's one shape (why null for new,
    ``plan`` for ended); a gifted reader or one paying through Paddle refused by it; or a new
    reader's requests writing a plan (as the free weeks' start once did)."""
    app, [(a, book)] = shelved(make_app, readers)
    models = HomeModels(app.state.platform_client, monkeypatch, reply=line(1, 2))
    stand(database, a.owner, standing)
    plan_before = traces(database, a.owner)["reader_plans"]
    for (method, path, body), past in zip(requests_for(a, book), PAST_THE_GATE, strict=True):
        response = a.call(method, path, body)
        if standing in NOT_PAYING:
            assert (response.status_code, response.json()) == (
                402, gate_of(NOT_PAYING[standing])), path
            continue
        assert response.status_code == past, (path, response.text)
        if method == "POST":
            events = events_of(response)
            assert events and events[-1]["type"] == "done", (path, events[-1])
    if standing in NOT_PAYING:
        assert models.writes == models.judged == 0
    else:
        assert models.writes >= 2
    assert traces(database, a.owner)["reader_plans"] == plan_before


# ---- a refused request reaches nothing behind the gate -----------------------------------------------

@pytest.mark.parametrize("standing", sorted(NOT_PAYING))
def test_a_refused_readers_requests_reach_no_model_search_memory_or_text_and_reserve_nothing(
        make_app, readers, database, monkeypatch, standing):
    """ADR 0045 section 9: the gate sits in front and only says no. The break it catches: a new
    or an ended reader's request that opens their catalog, their book's source (its text) or
    their story memory, searches, reaches a model, reserves, stores a turn, writes a plan or asks
    for background work before it is refused. The instrument is shown to see such a request by
    the same requests from a paying reader."""
    app, [(a, book_a), (b, book_b)] = shelved(make_app, readers, ("a", "b"), **BOTH)
    models = HomeModels(app.state.platform_client, monkeypatch, reply=line(1, 2))
    stand(database, a.owner, standing)
    asked = requests_for(a, book_a)
    control = requests_for(b, book_b)
    before = traces(database, a.owner)
    seen = Recorded(monkeypatch)
    for method, path, body in asked:
        response = a.call(method, path, body)
        assert (response.status_code, response.json()) == (
            402, gate_of(NOT_PAYING[standing])), path
    assert seen.seen == []
    assert models.writes == models.judged == 0 and models.prompts == models.checked == []
    assert traces(database, a.owner) == before

    # The same requests from a paying reader pass the gate and are seen.
    pay(database, b.owner, "gift")
    for method, path, body in control:
        assert b.call(method, path, body).status_code != 402, path
    assert {"HostedCompanionCatalog.__init__", "HostedBookStore.__init__",
            "HostedBookSource.__init__", "SpendStore.reserve_cost",
            "app.hosted.companion.routes.stream_read_prefix",
            "app.hosted.companion.routes.stream_home", "app.hosted.companion.routes.read_snapshot",
            "app.stream_conversation.compact_candidates"} <= set(seen.seen)
    assert models.writes >= 2


# ---- a reader who does not pay can still do everything that is reading ----------------------------

@pytest.mark.parametrize("standing", ["new", "ended"])
def test_a_reader_who_does_not_pay_can_still_do_everything_that_is_reading(
        make_app, readers, database, monkeypatch, standing):
    """ADR 0045 decision 1: reading is free forever. The break it catches: the gate (or anything
    else of the plan) shutting a new or an ended reader out of their books, their place, page
    reports and the read map, highlights, notes and bookmarks, the library and the free library,
    their account and the copy of their data, what Mothli remembers, or clearing a conversation;
    or any of those answering them otherwise than a paying reader."""
    app, [(a, book)] = shelved(make_app, readers)
    StandIns(app.state.platform_client, monkeypatch, reply=line(1, 2))
    pay(database, a.owner, "gift")
    assert sentences(a.ask(book)) == [line(1, 2)]
    reads = [f"/api/books/{book}", f"/api/books/{book}/manifest", f"/api/books/{book}/epub",
             f"/api/books/{book}/position", f"/api/books/{book}/preferences",
             f"/api/books/{book}/search?q=lamp", f"/api/books/{book}/marks",
             f"/api/books/{book}/marks/export", f"/api/books/{book}/read-map",
             f"/api/books/{book}/read-frontier", "/api/books", "/api/reader/profile",
             "/api/reader/recollections", "/api/limits", "/api/costs", "/api/classics",
             "/api/billing/plan", "/api/auth/session"]
    # The same answer as while paying, word for word where nothing else moves it (the place's
    # ``last_opened_at`` moves with every look at it, so it is compared by status).
    same_words = {f"/api/books/{book}", f"/api/books/{book}/manifest",
                  f"/api/books/{book}/preferences",
                  f"/api/books/{book}/search?q=lamp", f"/api/books/{book}/marks",
                  f"/api/books/{book}/read-map", f"/api/books/{book}/read-frontier",
                  "/api/reader/profile", "/api/reader/recollections"}
    while_paying = {path: a.call("GET", path) for path in reads}
    if standing == "new":                                   # the gift taken back: never had one
        with psycopg.connect(database) as conn:
            conn.execute("SELECT set_gift(%s, NULL::date)", (a.owner,))
    else:
        end(database, a.owner, "gift")
    assert state(database, a.owner)[0] == standing
    for path in reads:
        now, then = a.call("GET", path), while_paying[path]
        assert now.status_code == then.status_code != 402, (path, now.text)
        if path in same_words:
            assert now.content == then.content, path

    # Reading and keeping one's own things: as a paying reader does, and never the gate's answer.
    # Page reports are taken in every state.
    b = Reader(a.client, a.tokens, "b", readers["b"])
    b_book = upload(b.client, b.tokens, "b", lamp_book())
    b.mark(b_book, [1])
    pay(database, b.owner, "gift")
    assert sentences(b.ask(b_book)) == [line(1, 2)]
    placed = {"cfi": "epubcfi(/6/2!/4/2,/1:0,/1:4)", "atom": 1}

    def writes(book):
        return [
            ("POST", f"/api/books/{book}/highlights",
             {"anchor": placed, "color": "yellow", "selected_text": "Mara"}),
            ("POST", f"/api/books/{book}/annotations", {"anchor": placed, "body": "A note."}),
            ("POST", f"/api/books/{book}/bookmarks", {"anchor": placed, "label": "place"}),
            ("PUT", f"/api/books/{book}/preferences", {
                "font_size": "book", "line_height": "comfortable", "measure": "balanced",
                "theme": "paper", "margins": "balanced", "typeface": "publisher"}),
            ("PUT", f"/api/books/{book}/read-page", read_map_gate.page(2, 1, 5)),
            ("PUT", f"/api/books/{book}/read-chapters", {"atoms": [2], "read": True}),
            ("PUT", f"/api/books/{book}/read-frontier", read_map_gate.anchor(2, 3, after=True)),
            ("DELETE", f"/api/books/{book}/conversation", None),
            ("DELETE", "/api/reader/conversation", None),
        ]

    paying = [b.call(method, path, body).status_code for method, path, body in writes(b_book)]
    ranges = traces(database, a.owner)["read_ranges"]
    not_paying = [a.call(method, path, body).status_code for method, path, body in writes(book)]
    assert not_paying == paying and all(status < 400 for status in not_paying), (
        not_paying, paying)
    assert traces(database, a.owner)["read_ranges"] != ranges
    copy = a.call("GET", "/api/account/export")
    assert copy.status_code == 200 and copy.headers["content-type"] == "application/zip"


# ---- a reader whose plan stops between the gate and the reply ---------------------------------------

@pytest.mark.parametrize("standing", sorted(NOT_PAYING))
def test_a_reader_who_stops_paying_between_the_gate_and_the_reply_gets_the_gates_answer(
        make_app, readers, database, monkeypatch, standing):
    """The break it catches: a reader whose plan stopped (or whose gift was taken back) after the
    gate let their question through, answered anyway (the reply's first call admitted), or
    refused with anything but the gate's code and why in the stream's error event."""
    app, [(a, book)] = shelved(make_app, readers)
    models = StandIns(app.state.platform_client, monkeypatch, reply=line(1, 2))
    stand(database, a.owner, standing)
    # The gate read the reader a moment earlier, while they were still paying.
    monkeypatch.setattr(gate, "standing", lambda dsn, owner: {"state": "paying", "why": None})
    why = NOT_PAYING[standing]
    refusal = {"type": "error", "status": 402, "code": "mothli_plan", "why": why,
               "message": "Mothli needs the plan"}
    assert a.ask(book) == [refusal]
    response = a.call("POST", "/api/reader/ask/stream", {"question": "Who is Mara?"})
    assert events_of(response) == [refusal]
    assert models.writes == 0
    with psycopg.connect(database) as conn:
        assert conn.execute("SELECT count(*) FROM cost_reservations WHERE owner_id=%s"
                            " AND state='reserved'", (a.owner,)).fetchone()[0] == 0


# ---- the first question ----------------------------------------------------------------------------

def test_a_new_readers_first_question_is_refused_and_a_gift_or_a_paddle_plan_lets_them_in(
        make_app, readers, database, monkeypatch):
    """ADR 0046: talking with Mothli is the plan from the first question. The break it catches:
    a new reader's first question (in a book or at home) answered, or starting anything; a reader
    who pays (a gift, a Paddle plan) refused; or a reader whose plan stopped answered."""
    app, [(a, book)] = shelved(make_app, readers)
    models = HomeModels(app.state.platform_client, monkeypatch, reply=line(1, 2))
    refused_now = a.stream(book)
    assert (refused_now.status_code, refused_now.json()) == (402, gate_of(None))
    home = a.call("POST", "/api/reader/ask/stream", {"question": "Who is Mara?"})
    assert (home.status_code, home.json()) == (402, gate_of(None))
    assert models.writes == 0 and state(database, a.owner) == ("new", None)
    assert traces(database, a.owner)["reader_plans"] == []

    for how in ("gift", "paddle"):
        if how == "paddle":
            with psycopg.connect(database) as conn:
                conn.execute("SELECT set_gift(%s, NULL::date)", (a.owner,))
        pay(database, a.owner, how)
        assert sentences(a.ask(book)) == [line(1, 2)], how
        home = a.call("POST", "/api/reader/ask/stream", {"question": "Who is Mara?"})
        assert home.status_code == 200 and events_of(home)[-1]["type"] == "done", how
    end(database, a.owner, "paddle")                        # the Paddle plan stopped
    assert (a.stream(book).status_code, a.stream(book).json()) == (402, gate_of("plan"))


def test_at_the_adapter_a_reader_who_does_not_pay_is_gated_whatever_their_own_caps(
        tenant_dsn, database, readers):
    """The gate at the spend adapter, where a reader whose plan stops between the route's gate and
    the reply's first call is met. The break it catches: a new reader admitted (a free start
    by another name), or a new or ended reader held by the paying readers' day and month (or let
    through by an operator's raised caps) in place of the gate's one answer."""
    a = readers["a"]
    book = shelve(database, a)
    chat = HostedCompanionCatalog(tenant_dsn, OwnerId(a))
    gated(chat, book, None)
    update_limits(database, a, {"max_spend_usd_per_day": Decimal("5"),
                                "max_spend_usd_per_month": Decimal("50")})
    gated(chat, book, None)
    pay(database, a, "gift")
    reserve(chat, book, 0.001)
    end(database, a, "gift")
    gated(chat, book, "plan")
    chat_spent(database, a, Decimal("0.10"))
    gated(chat, book, "plan")                               # past the day: the gate, not the day


# ---- nothing in the gate's answer says anything of another reader ----------------------------------

@pytest.mark.parametrize("standing", ["new", "paddle_ended"])
def test_two_readers_of_one_file_the_gate_says_nothing_of_the_other(
        make_app, readers, database, monkeypatch, standing):
    """The owner's rule (ADR 0044, ADR 0045): nothing shown may imply anything about another
    reader. A and B have the same book file; A does not pay. The break it catches: A's gated
    answers differing with B's state (B paying, new, ended) or with whether a book id is A's,
    B's or nobody's; or B's answers held because A does not pay."""
    app, [(a, book_a), (b, book_b)] = shelved(make_app, readers, ("a", "b"))
    HomeModels(app.state.platform_client, monkeypatch, reply=line(1, 2))
    stand(database, a.owner, standing)
    pay(database, b.owner, "gift")
    stranger = str(uuid.uuid4())

    def a_is_told():
        told, point = [], a.point(book_a)
        for book in (book_a, book_b, stranger):
            for method, path, body in requests_for(a, book, point):
                response = a.call(method, path, body)
                told.append((response.status_code, response.headers["content-type"],
                             response.content))
        return told

    while_b_pays = a_is_told()
    assert {answer for answer in while_b_pays} == {
        (402, "application/json",
         json.dumps(gate_of(NOT_PAYING[standing]), separators=(",", ":")).encode())}
    for method, path, body in requests_for(b, book_b):
        assert b.call(method, path, body).status_code != 402, path
    with psycopg.connect(database) as conn:
        conn.execute("SELECT set_gift(%s, NULL::date)", (b.owner,))
    assert state(database, b.owner) == ("new", None)
    assert a_is_told() == while_b_pays                      # B new
    end(database, b.owner, "paddle")
    assert a_is_told() == while_b_pays                      # B ended too


# ---- what is read, searched and sent ----------------------------------------------------------------

def test_how_a_reader_pays_changes_nothing_a_reply_reads_searches_or_sends(
        make_app, readers, database, monkeypatch):
    """Spoiler safety (ADR 0045 section 9): the plan check only admits or refuses a model call.
    The break it catches: anything a reply reads, searches or sends that differs because the
    reader is gifted (ADR 0045 step 2) or paying through Paddle (step 3); or anything the gate's
    routes serve differing behind it (step 5). The same reader, book, place and question, with a
    gift and on a Paddle plan: the passages retrieved, the writers' prompts and systems in a book
    and at home, everything Jev is given, and the cast and "Story so far" served, are identical.
    (A reader who does not pay meets the gate, so nothing behind it runs: the tests above.)"""
    a, book, app = opened(make_app, readers)
    a.read(book, 2, 1, 5)
    retrieved = []
    real = stream_conversation.compact_candidates

    def compact_candidates(*args, **options):
        found = real(*args, **options)
        retrieved.append(json.dumps(found, sort_keys=True, default=str))
        return found

    monkeypatch.setattr(stream_conversation, "compact_candidates", compact_candidates)

    def asked():
        retrieved.clear()
        models = HomeModels(app.state.platform_client, monkeypatch, reply=line(1, 2))
        events = a.ask(book, QUESTION)
        assert events[-1]["type"] == "done" and sentences(events) == [line(1, 2)]
        home = a.call("POST", "/api/reader/ask/stream", {"question": QUESTION})
        assert home.status_code == 200 and events_of(home)[-1]["type"] == "done"
        served = [a.call("GET", f"/api/books/{book}/{path}").text
                  for path in ("graph", "graph?bookmark=99", "notes?bookmark=99", "ingest")]
        assert a.call("DELETE", f"/api/books/{book}/conversation").status_code == 204
        assert a.call("DELETE", "/api/reader/conversation").status_code == 204
        return (list(retrieved), models.systems, models.prompts,
                [json.dumps(checked, sort_keys=True) for checked in models.checked], served)

    pay(database, a.owner, "gift")
    gifted = asked()
    # ADR 0045 step 3: paying through Paddle, as a checked message leaves the row.
    with psycopg.connect(database) as conn:
        conn.execute("SELECT set_gift(%s, NULL::date)", (a.owner,))
        conn.execute("UPDATE reader_plans SET paddle_customer_id = %s,"
                     " paddle_subscription_id = %s, paddle_status = 'active',"
                     " paddle_occurred_at = now() WHERE owner_id = %s",
                     ("ctm_" + "c" * 26, "sub_" + "s" * 26, a.owner))
        assert conn.execute("SELECT state, paid_by FROM reader_plan_state(%s)",
                            (a.owner,)).fetchone() == ("paying", "paddle")
    paying = asked()
    assert gifted[0] and gifted[2] and gifted[3]
    assert len(gifted[2]) >= 2                              # a book's writer and the home's
    assert paying == gifted
    assert "Corvus moved lamp 6" not in "".join(gifted[2])


# ---- the session ------------------------------------------------------------------------------------

def test_the_session_tells_the_screens_where_the_reader_stands(
        make_app, readers, database, monkeypatch):
    """The break it catches: the screens unable to tell, at sign-in, a new reader from a paying
    one or an ended one; or the session carrying a date, an amount, a count or anything of the
    removed free weeks (their note)."""
    app, [(a, _book)] = shelved(make_app, readers)

    def mothli(someone):
        someone.become()
        answer = someone.client.get("/api/auth/session")
        assert answer.status_code == 200
        assert set(answer.json()) == {"user", "expires_at", "chat", "mothli"}
        return answer.json()["mothli"]

    assert mothli(a) == {"state": "new", "why": None}
    pay(database, a.owner, "gift")
    assert mothli(a) == {"state": "paying", "why": None}
    end(database, a.owner, "gift")
    assert mothli(a) == {"state": "ended", "why": "plan"}
    b = Reader(a.client, a.tokens, "b", readers["b"])
    assert mothli(b) == {"state": "new", "why": None}
    end(database, b.owner, "paddle")
    assert mothli(b) == {"state": "ended", "why": "plan"}
