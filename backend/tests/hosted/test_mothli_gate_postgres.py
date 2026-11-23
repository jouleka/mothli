"""The gate in front of Mothli (ADR 0045 step 5), on real PostgreSQL.

After their two free weeks a reader who does not pay keeps reading for free, and all of Mothli
(the chat in a book, the home chat, the cast, a person's card, "Story so far", what Mothli has
said) needs the plan. Where a reader stands is ``reader_plan_state``'s, from the plan's own row,
the clock and their own chat: ``new``, ``free``, ``paying`` (a gift or a Paddle plan) or
``ended`` with why (``time``, ``ceiling``, ``plan``). Proven here: each state's answer on each
gated route; an ended reader's request reaches no model, no search, no story memory and no book
text and reserves nothing; an ended reader can still do everything that is reading; the app's
own fetches never start the free weeks and the start route starts them once; day 15 without
paying is gated and with a gift or a Paddle plan is not; the gate's answer says nothing of any
other reader; and the session tells the screens where the reader stands. Which routes are gated
is checked without a database in ``test_mothli_gate.py``.

Through the hosted HTTP app on the RLS-enforced tenant role. Stand-in writer and Jev only: no
model is called and nothing is paid.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import psycopg
import pytest

from app import stream_conversation
from app.hosted.companion import gate, jobs, routes
from app.hosted.companion.memory import HostedBookStore
from app.hosted.companion.source import HostedBookSource
from app.hosted.companion.spend import SpendStore
from app.hosted.companion.store import HostedCompanionCatalog
from tests.hosted import test_companion_read_map_postgres as read_map_gate
from tests.hosted import test_free_weeks_postgres as free_weeks
from tests.hosted.test_background_by_state_postgres import end, pay
from tests.hosted.test_companion_recollections_postgres import HomeModels
from tests.hosted.test_companion_stream_postgres import (
    QUESTION,
    Reader,
    StandIns,
    events_of,
    restore_caps,  # noqa: F401  (autouse: the hosted app sets this process's caps)
    sentences,
)
from tests.hosted.test_free_weeks_postgres import chat_spent, move_start, stamp, started, state

pytestmark = pytest.mark.postgres

admin_dsn = read_map_gate.admin_dsn
database = read_map_gate.database
tenant_dsn = read_map_gate.tenant_dsn
readers = read_map_gate.readers
make_app = free_weeks.make_app
line, upload, lamp_book = read_map_gate.line, read_map_gate.upload, read_map_gate.lamp_book

# Chapter notes and story memory on, so a request that went past the gate would ask for both.
BOTH = {"reader_story_notes": True, "hosted_story_memory": True}
STATES = ["new", "free", "gift", "paddle", "time", "ceiling", "plan"]
ENDED = {"time", "ceiling", "plan"}
# What a reader's request could leave behind: none of it for an ended reader's gated request.
TRACES = ("cost_reservations", "cost_ledger", "jobs", "conversation_turns", "reader_plans",
          "read_ranges", "reading_state")


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


def stand(database: str, tenant: str, owner: uuid.UUID, standing: str) -> None:
    """Put the reader where ``standing`` says, as the plan tests do."""
    if standing == "free":
        stamp(tenant, owner)
    elif standing in ("gift", "paddle"):
        pay(database, owner, standing)
    elif standing in ENDED:
        end(database, tenant, owner, standing)
    expected = {"gift": "paying", "paddle": "paying"}.get(
        standing, "ended" if standing in ENDED else standing)
    assert state(database, owner)[0] == expected


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


def gate_of(why: str) -> dict:
    return {"detail": {"code": "mothli_plan", "why": why}}


def traces(dsn: str, owner: uuid.UUID) -> dict:
    with psycopg.connect(dsn) as conn:
        return {table: sorted(map(str, conn.execute(
            f"SELECT to_jsonb(t) FROM {table} AS t WHERE owner_id=%s", (owner,)).fetchall()))
            for table in TRACES}


class Recorded:
    """Everything behind a gated route that opens the reader's catalog, source, search or story
    memory, reserves, starts the free weeks or asks for background work: each call is kept and
    then made as usual, so a request that went past the gate shows here."""

    PLACES = [(HostedCompanionCatalog, "__init__"), (HostedBookStore, "__init__"),
              (HostedBookSource, "__init__"), (SpendStore, "reserve_cost"),
              (SpendStore, "start_free_weeks"), (jobs.ChapterNotesQueue, "ensure"),
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


def asked_for(monkeypatch) -> list:
    """Every background job asked for, as (kind, book)."""
    seen = []
    real = jobs.ChapterNotesQueue.ensure

    def ensure(self, book_id, **options):
        seen.append((self.KIND, str(book_id)))
        return real(self, book_id, **options)

    monkeypatch.setattr(jobs.ChapterNotesQueue, "ensure", ensure)
    return seen


# ---- (d) each state's answer on each route ----------------------------------------------------------

@pytest.mark.parametrize("standing", STATES)
def test_each_state_gets_its_answer_on_every_gated_route(
        make_app, readers, database, tenant_dsn, monkeypatch, standing):
    """The break it catches: an ended reader (by time, at the free ceiling, or after a plan)
    answered by any gated route, or answered anything but the gate's one shape; a new reader, one
    in the free weeks, a gifted one or one paying through Paddle refused by it; or the app's own
    fetches starting a new reader's free weeks."""
    app, [(a, book)] = shelved(make_app, readers)
    models = HomeModels(app.state.platform_client, monkeypatch, reply=line(1, 2))
    stand(database, tenant_dsn, a.owner, standing)
    before = started(database, a.owner)
    for (method, path, body), past in zip(requests_for(a, book), PAST_THE_GATE, strict=True):
        if path.endswith("/ask/stream") and path.startswith("/api/books/"):
            # Before the first question, the app's own fetches have started nothing.
            assert started(database, a.owner) == before
        response = a.call(method, path, body)
        if standing in ENDED:
            assert (response.status_code, response.json()) == (402, gate_of(standing)), path
            continue
        assert response.status_code == past, (path, response.text)
        if method == "POST":
            events = events_of(response)
            assert events and events[-1]["type"] == "done", (path, events[-1])
    if standing in ENDED:
        assert models.writes == models.judged == 0 and started(database, a.owner) == before
    else:
        assert models.writes >= 2 and started(database, a.owner) is not None


# ---- (b) an ended reader's request reaches nothing behind the gate ---------------------------------

@pytest.mark.parametrize("why", ["time", "ceiling", "plan"])
def test_an_ended_readers_gated_requests_reach_no_model_search_memory_or_text_and_reserve_nothing(
        make_app, readers, database, tenant_dsn, monkeypatch, why):
    """ADR 0045 section 9: the gate sits in front and only says no. The break it catches: an
    ended reader's request that opens their catalog, their book's source (its text) or their
    story memory, searches, reaches a model, reserves, stores a turn, starts the free weeks or
    asks for background work before it is refused. The instrument is shown to see such a request
    by the same requests from a reader in their free weeks."""
    app, [(a, book_a), (b, book_b)] = shelved(make_app, readers, ("a", "b"), **BOTH)
    models = HomeModels(app.state.platform_client, monkeypatch, reply=line(1, 2))
    end(database, tenant_dsn, a.owner, why)
    asked = requests_for(a, book_a)
    control = requests_for(b, book_b)
    before = traces(database, a.owner)
    seen = Recorded(monkeypatch)
    for method, path, body in asked:
        response = a.call(method, path, body)
        assert (response.status_code, response.json()) == (402, gate_of(why)), path
    assert seen.seen == []
    assert models.writes == models.judged == 0 and models.prompts == models.checked == []
    assert traces(database, a.owner) == before

    # The same requests from a reader in their free weeks pass the gate and are seen.
    stamp(tenant_dsn, b.owner)
    for method, path, body in control:
        assert b.call(method, path, body).status_code != 402, path
    assert {"HostedCompanionCatalog.__init__", "HostedBookStore.__init__",
            "HostedBookSource.__init__", "SpendStore.reserve_cost",
            "app.hosted.companion.routes.stream_read_prefix",
            "app.hosted.companion.routes.stream_home", "app.hosted.companion.routes.read_snapshot",
            "app.stream_conversation.compact_candidates"} <= set(seen.seen)
    assert models.writes >= 2


# ---- (c) an ended reader can still do everything that is reading -----------------------------------

def test_an_ended_reader_can_still_do_everything_that_is_reading(
        make_app, readers, database, tenant_dsn, monkeypatch):
    """ADR 0045 decision 1: reading is free forever. The break it catches: the gate (or anything
    else of the plan) shutting an ended reader out of their books, their place, page reports and
    the read map, highlights, notes and bookmarks, the library and the free library, their
    account and the copy of their data, what Mothli remembers, or clearing a conversation; or any
    of those answering an ended reader otherwise than in their free weeks."""
    app, [(a, book)] = shelved(make_app, readers)
    StandIns(app.state.platform_client, monkeypatch, reply=line(1, 2))
    stamp(tenant_dsn, a.owner)
    assert sentences(a.ask(book)) == [line(1, 2)]
    reads = [f"/api/books/{book}", f"/api/books/{book}/manifest", f"/api/books/{book}/epub",
             f"/api/books/{book}/position", f"/api/books/{book}/preferences",
             f"/api/books/{book}/search?q=lamp", f"/api/books/{book}/marks",
             f"/api/books/{book}/marks/export", f"/api/books/{book}/read-map",
             f"/api/books/{book}/read-frontier", "/api/books", "/api/reader/profile",
             "/api/reader/recollections", "/api/limits", "/api/costs", "/api/classics",
             "/api/billing/plan", "/api/auth/session"]
    # The same answer as in the free weeks, word for word where nothing else moves it (the
    # place's ``last_opened_at`` moves with every look at it, so it is compared by status).
    same_words = {f"/api/books/{book}", f"/api/books/{book}/manifest",
                  f"/api/books/{book}/preferences",
                  f"/api/books/{book}/search?q=lamp", f"/api/books/{book}/marks",
                  f"/api/books/{book}/read-map", f"/api/books/{book}/read-frontier",
                  "/api/reader/profile", "/api/reader/recollections"}
    in_the_free_weeks = {path: a.call("GET", path) for path in reads}
    end(database, tenant_dsn, a.owner, "time")
    for path in reads:
        now, then = a.call("GET", path), in_the_free_weeks[path]
        assert now.status_code == then.status_code != 402, (path, now.text)
        if path in same_words:
            assert now.content == then.content, path

    # Reading and keeping one's own things: as a reader in their free weeks does, and never the
    # gate's answer. Page reports are taken in every state.
    b = Reader(a.client, a.tokens, "b", readers["b"])
    b_book = upload(b.client, b.tokens, "b", lamp_book())
    b.mark(b_book, [1])
    stamp(tenant_dsn, b.owner)
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

    free = [b.call(method, path, body).status_code for method, path, body in writes(b_book)]
    ranges = traces(database, a.owner)["read_ranges"]
    ended = [a.call(method, path, body).status_code for method, path, body in writes(book)]
    assert ended == free and all(status < 400 for status in ended), (ended, free)
    assert traces(database, a.owner)["read_ranges"] != ranges
    copy = a.call("GET", "/api/account/export")
    assert copy.status_code == 200 and copy.headers["content-type"] == "application/zip"


# ---- (e) the app's own fetches never start the clock; the start route starts it once ---------------

def test_the_start_route_starts_once_and_only_the_signed_in_readers_free_weeks(
        make_app, readers, database, tenant_dsn, monkeypatch):
    """ADR 0045 section 2: the free weeks start at first use, a question or the reader's own tap
    on the cast or "Story so far". The break it catches: the app's own fetches (the cast for
    names in the page, the notes after a chapter, progress, the stored conversation) starting
    the clock; the start route not starting it, starting it twice, starting another reader's, or
    stamping a reader who is ended; or the start not asking for the book's background work (or
    asking for an ended reader's)."""
    app, [(a, book)] = shelved(make_app, readers, **BOTH)
    b = Reader(a.client, a.tokens, "b", readers["b"])
    for path in (f"/api/books/{book}/graph", f"/api/books/{book}/notes?bookmark=1",
                 f"/api/books/{book}/ingest", f"/api/books/{book}/conversation",
                 "/api/reader/conversation"):
        assert a.call("GET", path).status_code == 200, path
    assert started(database, a.owner) is None and state(database, a.owner) == ("new", None)

    work = asked_for(monkeypatch)
    answer = a.call("POST", "/api/reader/mothli/start", {"book_id": book})
    assert (answer.status_code, answer.json()) == (200, {"state": "free", "why": None,
                                                         "ending_note": False})
    first = started(database, a.owner)
    assert first is not None and started(database, b.owner) is None
    assert sorted(work) == [("chapter_notes", book), ("story_memory", book)]

    move_start(database, a.owner, "3 days")                 # so a second stamp would show
    first = started(database, a.owner)
    assert a.call("POST", "/api/reader/mothli/start", {}).json()["state"] == "free"
    a.call("POST", "/api/reader/mothli/start", {"book_id": str(uuid.uuid4())})
    assert started(database, a.owner) == first
    with psycopg.connect(database) as conn:                 # a book not theirs adds nothing
        assert conn.execute("SELECT count(*) FROM jobs WHERE owner_id=%s AND book_id <> %s",
                            (a.owner, book)).fetchone()[0] == 0

    # Ended: told so, and nothing is stamped or asked for.
    move_start(database, a.owner, "15 days")
    moved, work[:] = started(database, a.owner), []
    answer = a.call("POST", "/api/reader/mothli/start", {"book_id": book})
    assert answer.json() == {"state": "ended", "why": "time", "ending_note": False}
    assert started(database, a.owner) == moved and work == []
    end(database, tenant_dsn, b.owner, "plan")              # paid, stopped, never started
    assert b.call("POST", "/api/reader/mothli/start", {}).json() == {
        "state": "ended", "why": "plan", "ending_note": False}
    assert started(database, b.owner) is None


# ---- (f) the free ceiling: the reply under way finishes, the next request is gated -----------------

def test_a_reader_who_ends_between_the_gate_and_the_reply_gets_the_gates_answer_in_the_stream(
        make_app, readers, database, tenant_dsn, monkeypatch):
    """The break it catches: a reader whose free ceiling is reached by another reply after the
    gate let their question through, answered anyway (the reply's first call admitted), or
    refused with anything but the gate's code and why in the stream's error event."""
    app, [(a, book)] = shelved(make_app, readers)
    models = StandIns(app.state.platform_client, monkeypatch, reply=line(1, 2))
    stamp(tenant_dsn, a.owner)
    chat_spent(database, a.owner, Decimal("2.10"))
    assert state(database, a.owner) == ("ended", "ceiling")
    # The gate read the reader a moment earlier, while they were still in their free weeks.
    monkeypatch.setattr(gate, "standing",
                        lambda dsn, owner: {"state": "free", "why": None, "ending_note": False})
    refusal = {"type": "error", "status": 402, "code": "mothli_plan", "why": "ceiling",
               "message": "Mothli needs the plan"}
    assert a.ask(book) == [refusal]
    response = a.call("POST", "/api/reader/ask/stream", {"question": "Who is Mara?"})
    assert events_of(response) == [refusal]
    assert models.writes == 0
    with psycopg.connect(database) as conn:
        assert conn.execute("SELECT count(*) FROM cost_reservations WHERE owner_id=%s"
                            " AND state='reserved'", (a.owner,)).fetchone()[0] == 0


# ---- (g) day 15 ------------------------------------------------------------------------------------

def test_day_15_without_paying_is_gated_and_with_a_gift_or_a_paddle_plan_is_not(
        make_app, readers, database, tenant_dsn, monkeypatch):
    """The break it catches: a reader past their free weeks answered (the stopgap of step 1, the
    paying readers' day and month in place of the gate), or a reader who pays (a gift, a Paddle
    plan) refused by the gate on day 15. Their question, in a book and at home."""
    app, [(a, book)] = shelved(make_app, readers)
    HomeModels(app.state.platform_client, monkeypatch, reply=line(1, 2))
    assert sentences(a.ask(book)) == [line(1, 2)]           # the first question starts them
    move_start(database, a.owner, "13 days 23 hours 59 minutes")
    assert sentences(a.ask(book)) == [line(1, 2)]           # a minute before the end of day 14
    move_start(database, a.owner, "14 days 1 minute")
    assert state(database, a.owner) == ("ended", "time")
    assert (a.stream(book).status_code, a.stream(book).json()) == (402, gate_of("time"))
    home = a.call("POST", "/api/reader/ask/stream", {"question": "Who is Mara?"})
    assert (home.status_code, home.json()) == (402, gate_of("time"))

    for how in ("gift", "paddle"):
        if how == "paddle":
            with psycopg.connect(database) as conn:
                conn.execute("SELECT set_gift(%s, NULL::date)", (a.owner,))
        pay(database, a.owner, how)
        assert sentences(a.ask(book)) == [line(1, 2)], how
        home = a.call("POST", "/api/reader/ask/stream", {"question": "Who is Mara?"})
        assert home.status_code == 200 and events_of(home)[-1]["type"] == "done", how
    end(database, tenant_dsn, a.owner, "plan")              # the Paddle plan stopped
    assert (a.stream(book).status_code, a.stream(book).json()) == (402, gate_of("plan"))


# ---- (h) nothing in the gate's answer says anything of another reader ------------------------------

def test_two_readers_of_one_file_one_ended_one_paying_the_gate_says_nothing_of_the_other(
        make_app, readers, database, tenant_dsn, monkeypatch):
    """The owner's rule (ADR 0044, ADR 0045): nothing shown may imply anything about another
    reader. A and B have the same book file. The break it catches: A's gated answers differing
    with B's state (B paying, in the free weeks, ended) or with whether a book id is A's, B's or
    nobody's; or B's answers held because A is ended."""
    app, [(a, book_a), (b, book_b)] = shelved(make_app, readers, ("a", "b"))
    HomeModels(app.state.platform_client, monkeypatch, reply=line(1, 2))
    end(database, tenant_dsn, a.owner, "time")
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
        (402, "application/json", b'{"detail":{"code":"mothli_plan","why":"time"}}')}
    for method, path, body in requests_for(b, book_b):
        assert b.call(method, path, body).status_code != 402, path
    with psycopg.connect(database) as conn:
        conn.execute("SELECT set_gift(%s, NULL::date)", (b.owner,))
    stamp(tenant_dsn, b.owner)
    assert a_is_told() == while_b_pays                      # B in the free weeks
    move_start(database, b.owner, "15 days")
    assert a_is_told() == while_b_pays                      # B ended too


# ---- the session ------------------------------------------------------------------------------------

def test_the_session_tells_the_screens_where_the_reader_stands_and_when_to_show_the_note(
        make_app, readers, database, tenant_dsn, monkeypatch):
    """The break it catches: the screens unable to tell, at sign-in, a new reader from one in the
    free weeks, a paying one or an ended one and why; the gentle note shown before the free
    weeks' last two days or after them; or the session carrying a date, an amount or a count."""
    app, [(a, _book)] = shelved(make_app, readers)

    def mothli():
        a.become()
        answer = a.client.get("/api/auth/session")
        assert answer.status_code == 200
        assert set(answer.json()) == {"user", "expires_at", "chat", "mothli"}
        return answer.json()["mothli"]

    assert mothli() == {"state": "new", "why": None, "ending_note": False}
    stamp(tenant_dsn, a.owner)
    assert mothli() == {"state": "free", "why": None, "ending_note": False}
    move_start(database, a.owner, "11 days 23 hours")          # three days and an hour left
    assert mothli()["ending_note"] is False
    move_start(database, a.owner, "12 days 1 hour")            # under two days left
    assert mothli() == {"state": "free", "why": None, "ending_note": True}
    move_start(database, a.owner, "14 days 1 minute")
    assert mothli() == {"state": "ended", "why": "time", "ending_note": False}
    pay(database, a.owner, "gift")
    assert mothli() == {"state": "paying", "why": None, "ending_note": False}
    with psycopg.connect(database) as conn:
        conn.execute("SELECT set_gift(%s, NULL::date)", (a.owner,))
    move_start(database, a.owner, "1 day")
    chat_spent(database, a.owner, Decimal("2.10"))
    assert mothli() == {"state": "ended", "why": "ceiling", "ending_note": False}
    b = Reader(a.client, a.tokens, "b", readers["b"])
    end(database, tenant_dsn, b.owner, "plan")
    b.become()
    assert b.client.get("/api/auth/session").json()["mothli"] == {
        "state": "ended", "why": "plan", "ending_note": False}
