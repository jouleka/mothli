"""The home chat, what Mothli remembers, and suggestions in hosted mode (ADR 0041 part 7), on real
PostgreSQL.

``POST /api/reader/ask/stream`` runs the unchanged ``stream_home`` over the hosted adapters and the
platform's clients; the reader's notes ("recollections") are distilled by durable ``recollect``
jobs that run the unchanged ``pending`` and ``distil`` through the RLS-enforced tenant role, and are
listed and forgotten through the unchanged ``listed`` at the local paths. Proven here: the local
home chat's spoiler probes, across books, with an empty and a seeded story memory; suggestions
spoken only from their reviewed line, and offered to add through the free library (part 11) to
the asking reader's shelf only; two readers (the shelf, the
conversation, the profile, the notes, the cap); the 301st note; one waiting job; a lost lease, two
workers and a moved cursor never storing a batch twice; clearing that no later job undoes; a
capped reader's job waiting; the role's exact grants. Stand-in writer and Jev only.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from datetime import timedelta
from decimal import Decimal

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import conninfo, sql

from app.home_conversation import HOME, SUGGESTABLE, read_maps, shelf_books
from app.home_suggestions import suggested_in
from app.hosted.auth.repository import AuthConfigurationError
from app.hosted.companion.jobs import CompanionJobs, RecollectQueue
from app.hosted.companion.memory import HostedBookStore
from app.hosted.companion.store import HostedCompanionCatalog
from app.hosted.companion.store_recollections import RecollectionCursorMoved
from app.hosted.jobs import PostgresWorkerRepository, WorkerConfigurationError
from app.hosted.limits import update_limits
from app.hosted.tenant.grants import TENANT_TABLE_PRIVILEGES, WORKER_TABLE_PRIVILEGES
from app.hosted.tenant.models import OwnerId
from app.hosted.tenant.repository import PostgresTenantRepository
from app.hosted.worker import DurableWorker
from app.read_map import book_record, read_snapshot
from app.recollections import BLOCK_HEAD, STORE_CAP
from tests.api._epub import epub_ncx
from tests.hosted import test_companion_read_map_postgres as read_map_gate
from tests.hosted import test_jobs_postgres as jobs_gate
from tests.hosted.test_companion_notes_postgres import expire_lease, held_privileges, rows
from tests.hosted.test_free_library_postgres import MIRROR, Source
from tests.hosted.test_companion_stream_postgres import (
    CHAT,
    Reader,
    StandIns,
    adapters,
    events_of,
    restore_caps,  # noqa: F401  (autouse: the hosted app sets this process's caps)
    seeded_with_receipts,
    sentences,
)

pytestmark = pytest.mark.postgres

worker_dsn = jobs_gate.worker_dsn
admin_dsn = read_map_gate.admin_dsn
database = read_map_gate.database
tenant_dsn = read_map_gate.tenant_dsn
readers = read_map_gate.readers
lamp_book = read_map_gate.lamp_book
lamp = read_map_gate.line
page = read_map_gate.page
upload = read_map_gate.upload

# The chat's settings; recollections follow it (on), chapter notes are left off here.
HOME_CHAT = {**CHAT}
CARTS = {1: "Jonas", 2: "Petra", 3: "Ilse"}
NIGHT = "I always read at night, on the train home."
AT_NIGHT = "The reader reads at night, on the train home."


def cart(chapter: int, number: int) -> str:
    return f"{CARTS[chapter]} pushed cart {number} in section {chapter}."


def cart_book() -> bytes:
    return epub_ncx(
        [(f"c{n}.xhtml", f"Section {n}", f"Section {n}", " ".join(cart(n, i) for i in range(1, 41)))
         for n in (1, 2, 3)], title="The Cart Book", author="Ben Author")


def unread_book() -> bytes:
    return epub_ncx(
        [(f"c{n}.xhtml", f"Book {n}", f"Book {n}",
          " ".join(f"UNREAD_SHELF_CANARY {n} {i}." for i in range(1, 41))) for n in (1, 2)],
        title="Middlemarch", author="George Eliot")


@pytest.fixture()
def home_app(tenant_dsn: str, readers: dict, tmp_path):
    from datetime import UTC, datetime

    from app.main import create_app
    from tests.hosted.test_tenant_postgres import UnusedOIDCClient

    tokens, sessions = read_map_gate.signed_in(readers)
    app = create_app(
        read_map_gate.hosted_settings(tenant_dsn, tmp_path / "objects", **HOME_CHAT),
        auth_repository=sessions, oidc_client=UnusedOIDCClient(),
        auth_clock=lambda: datetime(2026, 7, 16, 12, tzinfo=UTC))
    with TestClient(app, base_url="https://reader.example") as client:
        assert app.state.settings.reader_recollections is True
        yield client, tokens, app


# ---- stand-in models ---------------------------------------------------------------------------

def change(text: str, message: int = 1, kind: str = "about", replaces=None) -> dict:
    return {"kind": kind, "text": text, "message": message, "replaces": replaces}


class HomeModels(StandIns):
    """The stream tests' stand-ins, also answering the home writer (whose prompt has no SOURCE
    PASSAGES heading) and the distilling call: it returns ``changes`` (a list, or a function of
    its prompt). Jev finds a note stated by the reader unless it holds a ``doubts`` word, and free
    of story content unless it holds a ``story`` word. ``distil_hook`` runs during a distilling
    call, before it returns."""

    def __init__(self, client, monkeypatch, *, changes=(), doubts=(), story=(),
                 distil_hook=None, **options):
        super().__init__(client, monkeypatch, **options)
        self.changes, self.doubts, self.story = changes, tuple(doubts), tuple(story)
        self.distil_hook, self.distils, self.notes_judged = distil_hook, [], []

    def complete(self, system, user, tier="cheap", schema=None):
        if schema is not None and schema.__name__ == "RecollectionChanges":
            with self._lock:
                self.distils.append(user)
            if self.distil_hook:
                self.distil_hook()
            value = self.changes(user) if callable(self.changes) else self.changes
            return {"changes": list(value)}, {"in": 100, "out": 40}
        if schema is not None or "SOURCE PASSAGES" in user:
            return super().complete(system, user, tier=tier, schema=schema)
        with self._lock:
            self.writes += 1
            self.prompts.append(user)
            self.systems.append(system)
        if self.write_hook:
            self.write_hook()
        return self.reply, {"in": 100, "out": 40}

    def evaluate(self, state, questions, **options):
        if "note" not in state:
            return super().evaluate(state, questions, **options)
        with self._lock:
            self.notes_judged.append(state)
        stated = not any(word in state["note"] for word in self.doubts)
        story = any(word in state["note"] for word in self.story)
        return {"stated": {"choice": "supported" if stated else "unestablished",
                           "probabilities": {"supported": .95 if stated else .1}},
                "book_assertion": {"noul": .9 if story else .01}}, {"in": 50, "out": 5}


# ---- one reader's home side ------------------------------------------------------------------------

class HomeReader(Reader):
    def ask_home(self, question: str) -> list[dict]:
        response = self.call("POST", "/api/reader/ask/stream", {"question": question})
        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith("text/event-stream")
        return events_of(response)

    def get(self, path: str):
        self.become()
        return self.client.get(path)

    def home_shown(self) -> list[str]:
        response = self.get("/api/reader/conversation")
        assert response.status_code == 200, response.text
        return [turn["content"] for turn in response.json()["turns"]]

    def notes_listed(self) -> dict:
        response = self.get("/api/reader/recollections")
        assert response.status_code == 200, response.text
        return response.json()


def reader(home_app, readers, label="a") -> HomeReader:
    client, tokens, _app = home_app
    return HomeReader(client, tokens, label, readers[label])


def shelf(home_app, readers, label="a") -> tuple[HomeReader, str, str, str]:
    """Three books: the lamp book with part 1 read and five lines of part 2, the cart book with
    section 1 read, and one not started."""
    someone = reader(home_app, readers, label)
    lamps = upload(someone.client, someone.tokens, label, lamp_book())
    carts = upload(someone.client, someone.tokens, label, cart_book())
    unread = upload(someone.client, someone.tokens, label, unread_book())
    someone.mark(lamps, [1])
    someone.read(lamps, 2, 1, 5)
    someone.mark(carts, [1])
    return someone, lamps, carts, unread


def said(app, owner: uuid.UUID, book: str | None, *messages: str, reply: str = "Mm.") -> list[int]:
    """Store reader messages (each with Mothli's line after it) as the chats store them, with
    what is read now, without running a reply."""
    catalog, source = adapters(app, owner)
    if book is None:
        drew_on = read_maps(shelf_books(catalog, app.state.settings, source))
        incarnation = HOME
    else:
        held = read_snapshot(book, catalog, app.state.settings, source)[5]
        record = catalog.get_book(book)
        drew_on, incarnation = book_record(book, record, held), record["incarnation"]
    where = dict(incarnation=incarnation, position_epoch=0, atom=0, offset=0, drew_on=drew_on)
    ids = []
    for message in messages:
        ids.append(catalog.add_turn(book, role="user", content=message, **where))
        catalog.add_turn(book, role="assistant", content=reply, **where)
    return ids


def chatter(count: int, start: int = 1) -> list[str]:
    return [f"So what about lamp number {n}, then, and where did it end up?"
            for n in range(start, start + count)]


def dispatcher(app, *, lease_for=timedelta(seconds=60)) -> CompanionJobs:
    return CompanionJobs(tenant_dsn=app.state.companion_dsn, settings=app.state.settings,
                         client=app.state.platform_client, storage=app.state.object_storage,
                         sources=app.state.companion_sources, lease_for=lease_for)


def worker(app, dsn: str, worker_id: str = "recollect-worker") -> DurableWorker:
    return DurableWorker(PostgresWorkerRepository(dsn), dispatcher(app), worker_id,
                         lease_for=timedelta(seconds=60), retry_after=timedelta(seconds=15))


def drain(work: DurableWorker, most: int = 10) -> int:
    ran = 0
    while ran < most and work.run_once():
        ran += 1
    return ran


def jobs_of(dsn: str, owner: uuid.UUID) -> list[dict]:
    return rows(dsn, "SELECT * FROM jobs WHERE owner_id=%s AND kind='recollect' "
                     + jobs_gate.JOB_ORDER, owner)


def notes_of(dsn: str, owner: uuid.UUID) -> list[dict]:
    return rows(dsn, "SELECT * FROM recollections WHERE owner_id=%s ORDER BY recollection_id",
                owner)


def cursors_of(dsn: str, owner: uuid.UUID) -> list[dict]:
    return rows(dsn, "SELECT conversation,incarnation,turn_id FROM recollection_cursors "
                     "WHERE owner_id=%s ORDER BY conversation", owner)


def home_turns_of(dsn: str, owner: uuid.UUID) -> list[dict]:
    return rows(dsn, "SELECT * FROM conversation_turns WHERE owner_id=%s AND book_id IS NULL "
                     "ORDER BY turn_id", owner)


def kept(changes) -> list[dict]:
    """Changes as ``distil`` returns them, for storing straight through the adapter."""
    return [{"kind": kind, "text": text, "tied": False, "book_id": None, "drew_on": {},
             "said": text, "said_at": "2026-11-17T10:00:00+00:00", "replaces": None}
            for kind, text in changes]


# ---- the home chat: what it is given, what it shows, what it keeps ------------------------------

def test_a_home_reply_holds_each_books_read_text_only_and_is_kept_and_cleared(
        home_app, readers, database, monkeypatch):
    """``test_a_home_reply_has_each_books_read_text_under_its_title_and_nothing_unread`` and
    ``test_the_home_conversation_is_kept_and_can_be_cleared``, hosted: each book's read text
    under its title and nothing unread (the rest of either book, or the book not started), the
    same passages for the checker, a doubted sentence withheld, the turns kept with no book and
    shown again, cleared on request."""
    _client, _tokens, app = home_app
    a, lamps, carts, _unread = shelf(home_app, readers)
    models = HomeModels(app.state.platform_client, monkeypatch,
                        reply=f"{lamp(2, 3)} She later sold it abroad.", unsupported=["abroad"])
    events = a.ask_home("What did Corvus do with lamp 3, and what did Jonas do with cart 7?")
    prompt = models.prompts[-1]
    assert lamp(2, 3) in prompt and cart(1, 7) in prompt
    assert "From “The Lamp Book”" in prompt and "From “The Cart Book”" in prompt
    assert lamp(2, 6) not in prompt and "Vex" not in prompt and "Petra" not in prompt
    assert "UNREAD_SHELF_CANARY" not in models.everything()
    assert "“Middlemarch” by George Eliot: not started" in prompt
    assert sentences(events) == [lamp(2, 3)]
    assert events[-1]["type"] == "done" and events[-1]["answer"]["dropped_sentences"] == 1
    passages = " ".join(models.checked[-1]["passages"])
    assert "From “The Lamp Book”" in passages and "Petra" not in passages

    stored = home_turns_of(database, a.owner)
    assert [(turn["role"], turn["book_id"], turn["book_incarnation"]) for turn in stored] == [
        ("user", None, None), ("assistant", None, None)]
    assert set(stored[0]["drew_on"]) == {lamps, carts}
    assert a.home_shown() == ["What did Corvus do with lamp 3, and what did Jonas do with cart 7?",
                              lamp(2, 3)]
    a.ask_home("And then?")
    assert "What did Corvus do with lamp 3" in models.prompts[-1]
    assert "abroad" not in models.prompts[-1]
    # Read fresh: "I haven't read this" for the lamp book's part 2 hides what was written from it.
    a.mark(lamps, [2], read=False)
    assert a.home_shown() == []
    a.mark(lamps, [2])
    assert len(a.home_shown()) == 4
    assert a.call("DELETE", "/api/reader/conversation").status_code == 204
    assert a.home_shown() == [] and home_turns_of(database, a.owner) == []
    # Costs: the reader's, against the first book with read text, each with its purpose.
    ledger = rows(database, "SELECT book_id,purpose FROM cost_ledger WHERE owner_id=%s", a.owner)
    assert ledger and {row["book_id"] for row in ledger} == {uuid.UUID(lamps)}
    assert {"home-reply", "home-sentence-check"} <= {row["purpose"] for row in ledger}


MEMORIES = ["empty", "seeded"]


@pytest.mark.parametrize("memory", MEMORIES)
def test_a_sentence_past_any_books_place_is_removed_across_books(home_app, readers, database,
                                                                 monkeypatch, memory):
    """The local home chat's spoiler probes, hosted: a sentence naming someone met only past the
    reader's place (in its own book or put in another book's mouth), a late alias, or
    foreshadowing is removed; the checker doubts what its passages do not hold.

    Seeded, the edition of the lamp book's file holds Vex and his late alias, and the door hands
    the home chat neither (ADR 0044 section 4): the fixed name check cannot name them, and a later
    name the checker clears is left to the second look, as for every book through the door
    (``test_library_story_memory_postgres.py::
    test_a_later_name_the_writer_remembers_is_left_to_jev_and_the_second_look``). Until ADR 0044
    step 5 the reader's own rows held them, unclamped, and the seeded case proved the fixed check
    removed those sentences even when the checker cleared them."""
    _client, _tokens, app = home_app
    a, lamps, _carts, _unread = shelf(home_app, readers)
    if memory == "seeded":
        seeded_with_receipts(app, database, a.owner, lamps)
    client = app.state.platform_client
    probes = [
        (f"{lamp(1, 2)} Vex moved lamp 50 in part 3.", ["Vex"]),
        (f"{lamp(1, 2)} Jonas met Vex in section 1.", ["Vex"]),          # across books
        (f"{lamp(1, 2)} Petra pushed cart 9 in section 2.", ["Petra"]),
        (f"{lamp(1, 2)} Mara will lose lamp 2 later.", []),             # foreshadowing
        (f"{lamp(1, 2)} Jonas is going to lose cart 1.", []),
    ]
    if memory == "seeded":
        with HostedBookStore(app.state.companion_dsn, OwnerId(a.owner)).book(lamps) as mem:
            handed = json.dumps([mem._audit_all(table) for table in ("entities", "aliases")],
                                default=str)
        assert "Mara" in handed
        assert not [later for later in ("Vex", "Corvus", "Lamplighter") if later in handed]
    for reply, doubted in probes:
        models = HomeModels(client, monkeypatch, reply=reply, unsupported=doubted)
        events = a.ask_home("What happened in my books?")
        assert sentences(events) == [lamp(1, 2)], reply
        assert events[-1]["answer"]["dropped_sentences"] == 1, reply
        shown = json.dumps(events)
        assert "Vex" not in shown and "will lose" not in shown and "Petra" not in shown
        assert "Lamplighter" not in shown and "going to" not in shown
        if not doubted:
            assert models.checked, reply               # the checker cleared it; the gate did not
    assert not [text for text in a.home_shown() if "Vex" in text or "Petra" in text]


def test_an_outage_shows_nothing_and_keeps_nothing_and_an_empty_shelf_is_answered(
        home_app, readers, database, monkeypatch):
    """``test_an_outage_shows_nothing_and_keeps_nothing`` and the empty shelf (6al), hosted."""
    _client, _tokens, app = home_app
    a, _lamps, _carts, _unread = shelf(home_app, readers)
    HomeModels(app.state.platform_client, monkeypatch, reply=lamp(1, 2), down=True)
    response = a.call("POST", "/api/reader/ask/stream", {"question": "What did Mara do?"})
    events = events_of(response)
    assert [e["type"] for e in events] == ["error"] and "lamp" not in response.text
    assert home_turns_of(database, a.owner) == []

    b = reader(home_app, readers, "b")
    models = HomeModels(app.state.platform_client, monkeypatch,
                        reply="Hello, welcome. Something big will happen later.")
    events = b.ask_home("What should I read?")
    assert sentences(events) == ["Hello, welcome."] and events[-1]["type"] == "done"
    assert models.writes == 1 and "The shelf is empty" in models.prompts[-1]


def test_a_book_not_on_the_shelf_is_spoken_of_only_through_its_reviewed_line_and_added_on_asking(
        home_app, readers, database, monkeypatch):
    """The break it catches: a suggestion given to the writer or the checker with anything but
    its reviewed catalogue line; a reply that names a suggested book without offering to add it
    (part 11); the offer's request adding the book to anyone's shelf but the asking reader's; or
    any of the added book's text reaching a reply before the reader has read it, or the book
    suggested again once it is on the shelf."""
    _client, _tokens, app = home_app
    library = Source(monkeypatch)
    a, _lamps, _carts, _unread = shelf(home_app, readers)
    b = reader(home_app, readers, "b")
    b_books = b.get("/api/books").json()
    models = HomeModels(app.state.platform_client, monkeypatch, reply="Hello.")
    a.ask_home("Can you suggest something new to read?")
    prompt = models.prompts[-1]
    listed = prompt.split("BOOKS YOU CAN SUGGEST", 1)[1].split("\n\nWHAT YOU HAVE READ TOGETHER")[0]
    lines = [line[2:] for line in listed.splitlines() if line.startswith("- ")]
    assert lines, "the catalogue offered nothing to suggest"
    # The checker is given the same lines, each marked as all that is known of that book.
    passages = models.checked[-1]["passages"]
    assert [p for p in passages if p.startswith(SUGGESTABLE)] == [SUGGESTABLE + line for line in lines]
    title, author = re.match(r"“(.+?)” by (.+?) \(", lines[0]).groups()
    candidates = [{"classic_id": "0", "title": title, "author": author}]
    reply = f"You might like {title} by {author}."
    assert suggested_in([reply], candidates)          # locally this reply offers the book
    HomeModels(app.state.platform_client, monkeypatch, reply=reply)
    events = a.ask_home("Can you suggest something new to read?")
    assert sentences(events) == [reply]
    offered = events[-1]["answer"]["suggested"]
    assert [(book["title"], book["author"]) for book in offered] == [(title, author)]
    classic = offered[0]["classic_id"]

    # The app's "Add … to my shelf": the free library's add, for the asking reader only.
    added = a.call("POST", f"/api/classics/{classic}")
    assert added.status_code == 201, added.text
    assert library.seen == [MIRROR.format(pg=classic)]
    assert added.json()["book_id"] in {book["id"] for book in a.get("/api/books").json()}
    assert b.get("/api/books").json() == b_books
    assert rows(database, "SELECT count(*) AS n FROM books WHERE source_kind=%s",
                f"gutenberg:{classic}")[0]["n"] == 1

    # Added, not read: nothing of its text reaches the writer or the checker, and it is not
    # suggested again now that it is on the shelf.
    models = HomeModels(app.state.platform_client, monkeypatch, reply="Hello.")
    a.ask_home("Can you suggest something new to read? What is in my new book?")
    assert "“A Made-Up Classic” by Nobody Real: not started" in models.prompts[-1]
    assert "LIBRARY_CANARY" not in models.everything()
    listed = models.prompts[-1].split("BOOKS YOU CAN SUGGEST", 1)[1].split(
        "\n\nWHAT YOU HAVE READ TOGETHER")[0]
    assert lines[0] not in listed


# ---- two readers: the shelf, the home conversation, the profile -----------------------------------

def test_second_readers_home_chat_and_profile_hold_only_their_own_shelf(home_app, readers,
                                                                        database, tenant_dsn,
                                                                        monkeypatch):
    """The break it catches: ``list_books`` in the home chat (or the profile) reaching another
    reader's shelf, so a second reader's reply is written from the first reader's books or shows
    their conversation; or a second reader's clearing touching the first reader's turns. Through
    the routes on the RLS-enforced role, and through the adapter on a superuser connection."""
    _client, _tokens, app = home_app
    a, lamps, carts, _unread = shelf(home_app, readers, "a")
    client = app.state.platform_client
    HomeModels(client, monkeypatch, reply=lamp(1, 2))
    a.ask_home("A's question about Mara.")
    turns_a = home_turns_of(database, a.owner)
    assert len(turns_a) == 2

    b = reader(home_app, readers, "b")
    own = upload(b.client, b.tokens, "b", epub_ncx(
        [("c1.xhtml", "One", "One", " ".join(f"Wren rowed boat {i} in leg 1." for i in range(1, 41)))],
        title="The Boat Book", author="Cy Rower"))
    b.mark(own, [1])
    models = HomeModels(client, monkeypatch, reply="Wren rowed boat 2 in leg 1.")
    assert sentences(b.ask_home("What did Wren do with boat 2?")) == ["Wren rowed boat 2 in leg 1."]
    everything = models.everything()
    assert "The Boat Book" in everything
    for foreign in ("The Lamp Book", "The Cart Book", "Middlemarch", "Mara", "Jonas",
                    "A's question", "UNREAD_SHELF_CANARY"):
        assert foreign not in everything, foreign
    assert b.home_shown() == ["What did Wren do with boat 2?", "Wren rowed boat 2 in leg 1."]
    profile = b.get("/api/reader/profile").json()
    assert [book["title"] for book in profile["books"]] == ["The Boat Book"]
    assert profile["elsewhere"] == []
    assert {book["title"] for book in a.get("/api/reader/profile").json()["books"]} == {
        "The Lamp Book", "The Cart Book", "Middlemarch"}
    assert b.call("DELETE", "/api/reader/conversation").status_code == 204
    assert home_turns_of(database, a.owner) == turns_a

    # Row-level security alone: B's role setting, no predicate naming any owner.
    with psycopg.connect(tenant_dsn, autocommit=True) as conn:
        conn.execute("SELECT set_config('app.owner_id', %s, false)", (str(b.owner),))
        assert conn.execute("SELECT count(*) FROM conversation_turns WHERE book_id IS NULL"
                            ).fetchone()[0] == 0
        assert conn.execute("DELETE FROM conversation_turns").rowcount == 0
    # The adapter's own predicates alone: bound to B on a superuser connection.
    other = HostedCompanionCatalog(database, OwnerId(b.owner))
    assert [book["title"] for book in other.list_books()] == ["The Boat Book"]
    assert other.get_turns(None, incarnation=HOME) == []
    assert other.get_book(lamps) is None and other.get_book(carts) is None
    other.clear_turns(None)
    assert home_turns_of(database, a.owner) == turns_a


# ---- recollections: written by jobs, listed, forgotten ---------------------------------------------

def test_a_reply_asks_for_its_conversation_to_be_distilled_and_the_job_keeps_checked_notes(
        home_app, readers, database, worker_dsn, monkeypatch):
    """The break it catches: hosted notes never asked for (the do-nothing stand-in part 5 left),
    asked for a job per reply, written from anything but the reader's own messages, kept without
    their checks, charged to another reader, or not listed and used as locally."""
    _client, _tokens, app = home_app
    a, lamps, _carts, _unread = shelf(home_app, readers)
    models = HomeModels(app.state.platform_client, monkeypatch, reply=lamp(1, 2),
                        changes=lambda prompt: [change(AT_NIGHT, message=6),
                                                change("The reader dislikes Ilse.", message=1)]
                        if NIGHT in prompt else [])
    said(app, a.owner, lamps, *chatter(4))
    a.ask(lamps, "So what about lamp number 5, then, and where did it end up?")
    assert jobs_of(database, a.owner) == []                  # five messages: not enough yet
    a.ask(lamps, NIGHT)
    [job] = jobs_of(database, a.owner)
    assert (job["state"], str(job["book_id"]), job["priority"]) == ("pending", lamps, 10)
    assert drain(worker(app, worker_dsn)) == 1
    assert [row["state"] for row in jobs_of(database, a.owner)] == ["succeeded"]
    [prompt] = models.distils
    assert NIGHT in prompt and lamp(1, 2) not in prompt and "“The Lamp Book”" in prompt
    # The note naming someone the reader did not write is never kept; the other is, untied.
    [note] = notes_of(database, a.owner)
    assert (note["kind"], note["text"], note["tied"], note["book_id"], note["said"]) == (
        "about", AT_NIGHT, False, None, NIGHT)
    assert [state["reader_message"] for state in models.notes_judged] == [NIGHT]
    [cursor] = cursors_of(database, a.owner)
    assert (cursor["conversation"], cursor["turn_id"]) == (lamps, max(
        row["turn_id"] for row in rows(database, "SELECT turn_id FROM conversation_turns "
                                                 "WHERE owner_id=%s AND role='user'", a.owner)))
    ledger = rows(database, "SELECT purpose,book_id FROM cost_ledger WHERE owner_id=%s "
                            "AND purpose LIKE 'recollect%%'", a.owner)
    assert {row["purpose"] for row in ledger} == {"recollect-write", "recollect-check"}
    assert {row["book_id"] for row in ledger} == {uuid.UUID(lamps)}

    # Listed with its public id, and used by the next reply, in the book and on the home screen.
    listed = a.notes_listed()
    assert listed["hidden"] == 0
    [shown] = listed["recollections"]
    assert shown["id"] == str(note["public_id"]) and shown["text"] == AT_NIGHT
    assert shown["message"] == NIGHT and shown["book"] is None
    a.ask(lamps, "What now?")
    assert BLOCK_HEAD in models.prompts[-1] and AT_NIGHT in models.prompts[-1]
    a.ask_home("What should I read tonight?")
    assert BLOCK_HEAD in models.prompts[-1] and AT_NIGHT in models.prompts[-1]

    # Forgotten by its public id; a second time it is unknown.
    assert a.call("DELETE", f"/api/reader/recollections/{shown['id']}").status_code == 204
    assert a.notes_listed() == {"hidden": 0, "recollections": []}
    gone = a.call("DELETE", f"/api/reader/recollections/{shown['id']}")
    assert (gone.status_code, gone.json()) == (404, {"detail": "unknown recollection"})


def test_the_home_conversation_is_distilled_by_its_own_job(home_app, readers, database, worker_dsn,
                                                           monkeypatch):
    """The home chat asks for its conversation (no book) as a book's chat does; a feeling said
    on the home screen is tied to what was read then, and hidden while that is not read."""
    _client, _tokens, app = home_app
    a, lamps, _carts, _unread = shelf(home_app, readers)
    models = HomeModels(app.state.platform_client, monkeypatch, reply=lamp(1, 2), story=["Mara"],
                        changes=lambda prompt: [change("Part 1 got to the reader: Mara and that "
                                                       "lamp.", message=6, kind="feeling")]
                        if "Mara and that lamp" in prompt else [])
    said(app, a.owner, None, *chatter(5))
    a.ask_home("Part 1 wrecked me, honestly: Mara and that lamp.")
    [job] = jobs_of(database, a.owner)
    assert (job["book_id"], job["book_incarnation"]) == (None, None)
    assert drain(worker(app, worker_dsn)) == 1
    assert "written on the home screen" in models.distils[0]
    [note] = notes_of(database, a.owner)
    assert note["tied"] and note["book_id"] is None and lamps in note["drew_on"]
    assert cursors_of(database, a.owner)[0]["conversation"] == ""
    assert [n["text"] for n in a.notes_listed()["recollections"]] == [note["text"]]
    a.mark(lamps, [1], read=False)
    assert a.notes_listed() == {"hidden": 1, "recollections": []}
    a.mark(lamps, [1])
    assert a.notes_listed()["hidden"] == 0


def test_a_second_ensure_while_a_recollect_job_waits_adds_none(home_app, readers, database,
                                                               worker_dsn, monkeypatch):
    """The break it catches: every reply adding a job (a queue that grows with talking), or a
    second job for the same conversation running beside the first and paying twice."""
    _client, _tokens, app = home_app
    a, lamps, _carts, _unread = shelf(home_app, readers)
    HomeModels(app.state.platform_client, monkeypatch)
    queue = RecollectQueue(app.state.companion_dsn, OwnerId(a.owner))
    said(app, a.owner, lamps, *chatter(5))
    assert queue.ensure(lamps) is False                       # five: not enough
    said(app, a.owner, lamps, *chatter(1, start=6))
    assert queue.ensure(lamps) is True
    assert queue.ensure(lamps) is False
    said(app, a.owner, None, *chatter(6))
    assert queue.ensure(None) is True and queue.ensure(None) is False
    assert {str(row["book_id"]) for row in jobs_of(database, a.owner)} == {"None", lamps}
    # While one runs, one more may wait, and only one; the waiting one is not claimed meanwhile.
    repository = PostgresWorkerRepository(worker_dsn)
    claim = repository.claim_next(worker_id="first", lease_for=timedelta(seconds=60),
                                  kinds=frozenset({"recollect"}))
    assert claim is not None and repository.start(claim)
    book = None if claim.book_id is None else str(claim.book_id)
    assert queue.ensure(book) is True and queue.ensure(book) is False
    states = sorted(row["state"] for row in jobs_of(database, a.owner))
    assert states == ["pending", "pending", "running"]
    # The next claim takes the other conversation's job, never the one beside the running job.
    second = repository.claim_next(worker_id="second", lease_for=timedelta(seconds=60),
                                   kinds=frozenset({"recollect"}))
    assert second is not None and second.book_id != claim.book_id
    # Another reader's book, a malformed id, and nothing waiting add nothing.
    b = reader(home_app, readers, "b")
    other = RecollectQueue(app.state.companion_dsn, OwnerId(b.owner))
    assert other.ensure(lamps) is False and other.ensure("not-a-book") is False
    assert other.ensure(None) is False and jobs_of(database, b.owner) == []


# ---- never twice, never after clearing --------------------------------------------------------------

def test_a_moved_cursor_refuses_the_whole_batch(home_app, readers, database):
    """The adapter's compare-and-set: two runs that read the same batch store it once; a batch
    whose messages were cleared, or whose cursor was moved by "forget everything", stores nothing."""
    _client, _tokens, app = home_app
    a, lamps, _carts, _unread = shelf(home_app, readers)
    catalog = HostedCompanionCatalog(app.state.companion_dsn, OwnerId(a.owner))
    incarnation = catalog.get_book(lamps)["incarnation"]
    ids = said(app, a.owner, lamps, *chatter(6))
    batch = kept([("about", AT_NIGHT)])
    assert catalog.keep_recollections(batch, cap=STORE_CAP, expected=0,
                                      cursor=(lamps, incarnation, ids[-1])) == 1
    with pytest.raises(RecollectionCursorMoved):
        catalog.keep_recollections(batch, cap=STORE_CAP, expected=0,
                                   cursor=(lamps, incarnation, ids[-1]))
    assert [n["text"] for n in notes_of(database, a.owner)] == [AT_NIGHT]
    assert catalog.get_recollection_cursor(lamps, incarnation=incarnation) == ids[-1]

    # Cleared since the batch was read: the cursor is 0 again, but the messages are gone.
    home = said(app, a.owner, None, *chatter(6))
    catalog.clear_turns(None)
    assert catalog.get_recollection_cursor(None, incarnation=HOME) == 0
    with pytest.raises(RecollectionCursorMoved):
        catalog.keep_recollections(kept([("taste", "The reader loves dry humour.")]),
                                   cap=STORE_CAP, expected=0, cursor=(None, HOME, home[-1]))
    # "Forget everything" since: every conversation counts as looked at.
    later = said(app, a.owner, lamps, *chatter(6, start=7))
    catalog.clear_recollections()
    assert catalog.get_recollection_cursor(lamps, incarnation=incarnation) == later[-1] + 1
    with pytest.raises(RecollectionCursorMoved):
        catalog.keep_recollections(kept([("plan", "The reader wants something lighter.")]),
                                   cap=STORE_CAP, expected=ids[-1],
                                   cursor=(lamps, incarnation, later[-1]))
    assert notes_of(database, a.owner) == []


def test_a_lease_lost_mid_run_or_a_second_worker_never_stores_a_batch_twice(
        home_app, readers, database, worker_dsn, monkeypatch):
    """The break it catches: a worker whose lease ran out while its distilling call was out (and
    whose job another worker has since finished) storing the same batch again, or a batch stored
    by another run meanwhile (the cursor moved) stored a second time. At worst a call repeats."""
    _client, _tokens, app = home_app
    a, lamps, _carts, _unread = shelf(home_app, readers)
    said(app, a.owner, lamps, *chatter(5), NIGHT)
    assert RecollectQueue(app.state.companion_dsn, OwnerId(a.owner)).ensure(lamps)
    first, second = worker(app, worker_dsn, "first"), worker(app, worker_dsn, "second")
    after = {}

    def lose_the_lease_and_let_another_worker_finish():
        if after:
            return
        after["started"] = True
        expire_lease(database, a.owner)
        after["ran"] = second.run_once()
        after["notes"] = notes_of(database, a.owner)

    models = HomeModels(app.state.platform_client, monkeypatch,
                        changes=[change(AT_NIGHT, message=6)],
                        distil_hook=lose_the_lease_and_let_another_worker_finish)
    assert first.run_once()
    assert after["ran"] is True and len(after["notes"]) == 1
    assert notes_of(database, a.owner) == after["notes"]
    assert len(models.distils) == 2                           # the repeated call, and no more
    [job] = jobs_of(database, a.owner)
    assert (job["state"], job["attempt_count"]) == ("succeeded", 2)
    assert not [row for row in rows(database, "SELECT state FROM cost_reservations "
                                              "WHERE owner_id=%s", a.owner)
                if row["state"] == "reserved"]

    # A run that keeps its lease but whose batch was stored by another run meanwhile.
    said(app, a.owner, lamps, *chatter(6, start=10))
    assert RecollectQueue(app.state.companion_dsn, OwnerId(a.owner)).ensure(lamps)
    catalog = HostedCompanionCatalog(app.state.companion_dsn, OwnerId(a.owner))
    incarnation = catalog.get_book(lamps)["incarnation"]

    def another_run_stores_this_batch():
        if len(models.distils) == 3:
            last = max(row["turn_id"] for row in rows(
                database, "SELECT turn_id FROM conversation_turns WHERE owner_id=%s "
                          "AND role='user'", a.owner))
            catalog.keep_recollections(kept([("plan", "The reader wants something lighter next.")]),
                                       cap=STORE_CAP, cursor=(lamps, incarnation, last))

    models.distil_hook = another_run_stores_this_batch
    models.changes = [change("The reader wants something lighter next.", message=1, kind="plan")]
    assert first.run_once()
    plans = [n for n in notes_of(database, a.owner) if n["kind"] == "plan"]
    assert len(plans) == 1
    assert [row["state"] for row in jobs_of(database, a.owner)] == ["succeeded", "succeeded"]


@pytest.mark.parametrize("where", ["book", "home"])
def test_clearing_a_conversation_deletes_it_and_no_later_job_brings_its_text_back(
        home_app, readers, database, worker_dsn, monkeypatch, where):
    """The break it catches: a cleared conversation's messages turned into notes afterwards, by
    a job that was waiting when it was cleared or by one whose distilling call was out at the time
    (the cursor alone cannot tell: it is back to nothing, as it was)."""
    _client, _tokens, app = home_app
    a, lamps, _carts, _unread = shelf(home_app, readers)
    book = lamps if where == "book" else None
    path = (f"/api/books/{lamps}/conversation" if book else "/api/reader/conversation")
    models = HomeModels(app.state.platform_client, monkeypatch,
                        changes=[change(AT_NIGHT, message=6)])
    said(app, a.owner, book, *chatter(5), NIGHT)
    queue = RecollectQueue(app.state.companion_dsn, OwnerId(a.owner))
    assert queue.ensure(book)
    assert a.call("DELETE", path).status_code == 204
    assert rows(database, "SELECT 1 FROM conversation_turns WHERE owner_id=%s", a.owner) == []
    assert cursors_of(database, a.owner) == []
    drain(worker(app, worker_dsn))
    assert models.distils == [] and notes_of(database, a.owner) == []

    # Cleared while the distilling call is out.
    said(app, a.owner, book, *chatter(5), NIGHT)
    assert queue.ensure(book)
    models.distil_hook = lambda: a.call("DELETE", path)
    assert drain(worker(app, worker_dsn)) == 1
    assert len(models.distils) == 1 and notes_of(database, a.owner) == []
    assert [row["state"] for row in jobs_of(database, a.owner)][-1] == "succeeded"
    assert rows(database, "SELECT 1 FROM conversation_turns WHERE owner_id=%s", a.owner) == []

    # "Forget everything" while the call is out: nothing said before it becomes a note.
    said(app, a.owner, book, *chatter(5), NIGHT)
    assert queue.ensure(book)
    models.distil_hook = lambda: a.call("DELETE", "/api/reader/recollections")
    assert drain(worker(app, worker_dsn)) == 1
    assert notes_of(database, a.owner) == []
    drain(worker(app, worker_dsn))
    assert queue.ensure(book) is False and notes_of(database, a.owner) == []


def test_a_capped_reader_waits_for_the_next_day_and_others_go_on(home_app, readers, database,
                                                                  worker_dsn, monkeypatch):
    """The break it catches: a reader at their day's allowance whose notes job is claimed again
    and again, or who holds up another reader's."""
    _client, _tokens, app = home_app
    a, lamps_a, _c, _u = shelf(home_app, readers, "a")
    b, lamps_b, _c, _u = shelf(home_app, readers, "b")
    for owner, book in ((a.owner, lamps_a), (b.owner, lamps_b)):
        said(app, owner, book, *chatter(5), NIGHT)
        assert RecollectQueue(app.state.companion_dsn, OwnerId(owner)).ensure(book)
    update_limits(database, a.owner, {"max_spend_usd_per_day": Decimal("0.05")})
    catalog = HostedCompanionCatalog(database, OwnerId(a.owner))
    held = catalog.reserve_cost(lamps_a, phase="stream-reply", model="gpt-6.1-terra",
                                input_tokens=10, output_tokens=10, usd=0.05, max_input_tokens=1,
                                max_output_tokens=1, max_usd=0.0)
    catalog.settle_cost(lamps_a, held)                      # exactly at the cap
    models = HomeModels(app.state.platform_client, monkeypatch,
                        changes=[change(AT_NIGHT, message=6)])
    assert drain(worker(app, worker_dsn)) == 1
    [job_a] = jobs_of(database, a.owner)
    tomorrow = rows(database, "SELECT (date_trunc('day', now() AT TIME ZONE 'UTC') + interval "
                              "'1 day') AT TIME ZONE 'UTC' AS at")[0]["at"]
    assert (job_a["state"], job_a["attempt_count"], job_a["run_after"]) == ("pending", 0, tomorrow)
    assert notes_of(database, a.owner) == [] and len(models.distils) == 1
    assert [row["state"] for row in jobs_of(database, b.owner)] == ["succeeded"]
    assert [n["text"] for n in notes_of(database, b.owner)] == [AT_NIGHT]


# ---- two readers' notes and the cap ----------------------------------------------------------------

def test_second_reader_cannot_list_forget_or_evict_first_readers_notes(home_app, readers, database,
                                                                       tenant_dsn):
    """The break it catches: a second reader seeing, forgetting (one by its id, or all) or
    pushing out under the cap the first reader's notes, or moving their cursors; through the
    routes on the RLS-enforced role, through that role alone, and through an adapter bound to
    the second reader on a connection where row-level security does not apply."""
    _client, _tokens, app = home_app
    a, lamps, _carts, _unread = shelf(home_app, readers, "a")
    mine = HostedCompanionCatalog(app.state.companion_dsn, OwnerId(a.owner))
    ids = said(app, a.owner, None, *chatter(6))
    mine.keep_recollections(kept([("about", AT_NIGHT), ("taste", "The reader loves dry humour.")]),
                            cap=STORE_CAP, cursor=(None, HOME, ids[-1]))
    notes_a, cursors_a = notes_of(database, a.owner), cursors_of(database, a.owner)
    assert len(notes_a) == 2 and cursors_a
    public = [str(note["public_id"]) for note in notes_a]
    assert [n["id"] for n in a.notes_listed()["recollections"]] == public[::-1]

    b = reader(home_app, readers, "b")
    assert b.notes_listed() == {"hidden": 0, "recollections": []}
    for target in public:
        hidden = b.call("DELETE", f"/api/reader/recollections/{target}")
        absent = b.call("DELETE", f"/api/reader/recollections/{uuid.uuid4()}")
        malformed = b.call("DELETE", "/api/reader/recollections/1")
        assert (hidden.status_code, hidden.json()) == (absent.status_code, absent.json()) == (
            malformed.status_code, malformed.json()) == (404, {"detail": "unknown recollection"})
    assert b.call("DELETE", "/api/reader/recollections").status_code == 204
    assert b.call("DELETE", "/api/reader/conversation").status_code == 204
    # B's own notes over the cap push out only B's.
    other = HostedCompanionCatalog(app.state.companion_dsn, OwnerId(b.owner))
    other.keep_recollections(kept([("plan", f"The reader plans book {n}.") for n in range(5)]),
                             cap=3)
    assert len(notes_of(database, b.owner)) == 3
    assert notes_of(database, a.owner) == notes_a and cursors_of(database, a.owner) == cursors_a

    # Row-level security alone.
    with psycopg.connect(tenant_dsn, autocommit=True) as conn:
        conn.execute("SELECT set_config('app.owner_id', %s, false)", (str(b.owner),))
        assert conn.execute("SELECT count(*) FROM recollections WHERE owner_id=%s",
                            (a.owner,)).fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM recollection_cursors WHERE owner_id=%s",
                            (a.owner,)).fetchone()[0] == 0
        assert conn.execute("DELETE FROM recollections WHERE owner_id=%s",
                            (a.owner,)).rowcount == 0
        assert conn.execute("UPDATE recollections SET text='Planted.' WHERE owner_id=%s",
                            (a.owner,)).rowcount == 0
        assert conn.execute("UPDATE recollection_cursors SET turn_id=0").rowcount == 0
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("INSERT INTO recollections (owner_id,kind,text,tied,said,said_at) "
                         "VALUES (%s,'about','Planted.',false,'x',now())", (a.owner,))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("INSERT INTO recollection_cursors (owner_id,conversation,incarnation,"
                         "turn_id) VALUES (%s,'','home',999999)", (a.owner,))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("INSERT INTO jobs (owner_id,kind,idempotency_key) "
                         "VALUES (%s,'recollect','recollect:planted')", (a.owner,))

    # The adapter's own predicates alone: bound to B on a superuser connection.
    with psycopg.connect(database) as conn:
        assert conn.execute("SELECT rolsuper FROM pg_roles WHERE rolname=current_user").fetchone()[0]
    bound = HostedCompanionCatalog(database, OwnerId(b.owner))
    assert {n["text"] for n in bound.get_recollections()} <= {
        f"The reader plans book {n}." for n in range(5)}
    assert all(bound.delete_recollection(target) is False for target in public)
    bound.touch_recollections([note["recollection_id"] for note in notes_a])
    replacing = kept([("about", "The reader reads in the bath.")])
    replacing[0]["replaces"] = notes_a[0]["recollection_id"]
    assert bound.keep_recollections(replacing, cap=STORE_CAP) == 0
    assert bound.get_recollection_cursor(None, incarnation=HOME) == 0
    bound.keep_recollections(kept([("plan", f"The reader plans more {n}.") for n in range(5)]),
                             cap=1)
    bound.clear_recollections()
    bound.clear_turns(None)
    assert notes_of(database, a.owner) == notes_a and cursors_of(database, a.owner) == cursors_a
    assert RecollectQueue(database, OwnerId(b.owner)).ensure(None) is False
    assert jobs_of(database, b.owner) == []


def test_the_301st_note_evicts_the_readers_own_least_used_and_who_they_are_last(
        home_app, readers, database):
    """The break it catches: the cap counted or applied across every reader's notes (one
    reader's 301st note pushing out another reader's), or the local eviction order lost: the
    least recently used go first, the notes about who the reader is last, and here a note tied to
    a book no longer on the shelf before any other."""
    _client, _tokens, app = home_app
    a, lamps, _carts, _unread = shelf(home_app, readers, "a")
    b = reader(home_app, readers, "b")
    theirs = HostedCompanionCatalog(app.state.companion_dsn, OwnerId(b.owner))
    theirs.keep_recollections(kept([("plan", "The reader plans to read slowly.")]), cap=STORE_CAP)
    notes_b = notes_of(database, b.owner)

    mine = HostedCompanionCatalog(app.state.companion_dsn, OwnerId(a.owner))
    first = kept([("about", "The reader is a nurse.")]) + kept(
        [("plan", f"The reader plans book {n}.") for n in range(STORE_CAP - 1)])
    assert mine.keep_recollections(first, cap=STORE_CAP) == STORE_CAP
    oldest_plan = notes_of(database, a.owner)[1]
    mine.touch_recollections([oldest_plan["recollection_id"]])
    assert mine.keep_recollections(kept([("plan", "The reader plans book 300.")]),
                                   cap=STORE_CAP) == 1
    texts = [n["text"] for n in notes_of(database, a.owner)]
    assert len(texts) == STORE_CAP
    # Gone: the least recently used plan ("book 1"), not "book 0" (just used), not the nurse.
    assert "The reader plans book 1." not in texts
    assert {"The reader is a nurse.", "The reader plans book 0.",
            "The reader plans book 300."} <= set(texts)
    assert notes_of(database, b.owner) == notes_b

    # A note tied to a book taken off the shelf is not shown, and goes first under the cap.
    tied = kept([("feeling", "The reader loved part 1.")])
    tied[0].update(tied=True, book_id=lamps)
    mine.keep_recollections(tied, cap=STORE_CAP)
    assert "The reader loved part 1." in [n["text"] for n in mine.get_recollections()]
    assert a.call("DELETE", f"/api/books/{lamps}").status_code == 204
    assert "The reader loved part 1." not in [n["text"] for n in mine.get_recollections()]
    mine.keep_recollections(kept([("plan", "The reader plans book 301.")]), cap=STORE_CAP)
    texts = [n["text"] for n in notes_of(database, a.owner)]
    # (Keeping the tied note had pushed out "book 2"; now the tied note goes, not "book 3".)
    assert "The reader loved part 1." not in texts and "The reader plans book 3." in texts
    assert len(texts) == STORE_CAP
    # A change tied to a book no longer on the shelf is not kept at all.
    assert mine.keep_recollections(tied, cap=STORE_CAP) == 0
    assert notes_of(database, b.owner) == notes_b


# ---- grants -------------------------------------------------------------------------------------

def test_tenant_and_worker_roles_hold_exactly_the_part_7_grants(tenant_dsn: str, worker_dsn: str,
                                                                database: str) -> None:
    """The break it catches: the reader role unable to keep, forget or move the cursor of notes,
    or holding a privilege on them nobody reviewed; the worker role reaching them at all (its
    owner work goes through the tenant role). Both startup checks refuse one more or one fewer."""
    tenant = conninfo.conninfo_to_dict(tenant_dsn)["user"]
    worker_role = conninfo.conninfo_to_dict(worker_dsn)["user"]
    every = {"SELECT", "INSERT", "UPDATE", "DELETE"}
    for table in ("recollections", "recollection_cursors"):
        assert TENANT_TABLE_PRIVILEGES[table] == every
        assert table not in WORKER_TABLE_PRIVILEGES
        assert held_privileges(database, tenant, table) == every
        assert held_privileges(database, worker_role, table) == set()
    tenant_check = PostgresTenantRepository(tenant_dsn)
    worker_check = PostgresWorkerRepository(worker_dsn)
    asyncio.run(tenant_check.check_runtime_role())
    worker_check.check_runtime_role()

    def refused(grant: str, undo: str, role: str, check, error) -> None:
        with psycopg.connect(database, autocommit=True) as conn:
            conn.execute(sql.SQL(grant).format(sql.Identifier(role)))
        try:
            with pytest.raises(error, match="allow-list"):
                check()
        finally:
            with psycopg.connect(database, autocommit=True) as conn:
                conn.execute(sql.SQL(undo).format(sql.Identifier(role)))

    def tenant_runs():
        asyncio.run(tenant_check.check_runtime_role())

    for grant, undo in (
        ("GRANT TRUNCATE ON recollections TO {}", "REVOKE TRUNCATE ON recollections FROM {}"),
        ("REVOKE DELETE ON recollections FROM {}", "GRANT DELETE ON recollections TO {}"),
        ("GRANT TRIGGER ON recollection_cursors TO {}",
         "REVOKE TRIGGER ON recollection_cursors FROM {}"),
        ("REVOKE UPDATE ON recollection_cursors FROM {}",
         "GRANT UPDATE ON recollection_cursors TO {}"),
    ):
        refused(grant, undo, tenant, tenant_runs, AuthConfigurationError)
    refused("GRANT SELECT ON recollections TO {}", "REVOKE SELECT ON recollections FROM {}",
            worker_role, worker_check.check_runtime_role, WorkerConfigurationError)
    asyncio.run(tenant_check.check_runtime_role())
    worker_check.check_runtime_role()


def test_a_recollect_job_is_well_formed_or_refused_by_the_schema(home_app, readers, database):
    """The break it catches: a second waiting recollect job (none can name a credential: the
    column went in migration 0029) for the same conversation inserted around the queue."""
    _client, _tokens, app = home_app
    a, lamps, _carts, _unread = shelf(home_app, readers)
    said(app, a.owner, None, *chatter(6))
    assert RecollectQueue(app.state.companion_dsn, OwnerId(a.owner)).ensure(None)
    with psycopg.connect(database, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute("INSERT INTO jobs (owner_id,kind,idempotency_key) "
                         "VALUES (%s,'recollect','recollect:twin')", (a.owner,))
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("INSERT INTO recollection_cursors (owner_id,conversation,incarnation,"
                         "turn_id) VALUES (%s,'','not-home',1)", (a.owner,))
    assert len(jobs_of(database, a.owner)) == 1
