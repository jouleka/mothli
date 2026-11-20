"""The streamed chat inside a book in hosted mode (ADR 0041 part 5), against real PostgreSQL.

``POST /api/books/{id}/ask/stream`` runs the unchanged ``stream_read_prefix`` over the hosted
adapters, the platform's clients and do-nothing background stand-ins. The local stream tests'
spoiler probes are run here through the hosted HTTP app on the RLS-enforced tenant role, with an
empty story memory and with a seeded one; then two readers, what is stored, the caps end to end, a
failed memory load, the longest reply there can be, and the database connections a reply holds
while a model answers. Stand-in writer and Jev only: no model is called and nothing is paid.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import threading
import time
import uuid
from decimal import Decimal
from types import SimpleNamespace

import psycopg
import pytest
from psycopg import conninfo

from app import stream_conversation
from app.hosted.auth.api import CSRF_HEADER
from app.hosted.companion.memory import HostedBookStore, StoryMemoryUnavailable
from app.hosted.companion.source import HostedBookSource
from app.hosted.companion.spend import SpendStore, set_spend_limits, spend_limits
from app.hosted.companion.store import HostedCompanionCatalog
from app.hosted.limits import update_limits
from app.hosted.tenant.models import OwnerId
from app.llm.client import LLMClient
from app.llm.jev import JevUnavailable
from app.read_map import read_snapshot
from app.stream_conversation import CHECK_INPUT_LIMIT, MAX_SENTENCE_CHARS, MAX_SENTENCES
from tests.api._epub import epub_ncx
from tests.hosted import test_companion_read_map_postgres as read_map_gate
from tests.hosted.test_tenant_postgres import _become
from tests.parity._adapters import serve_through_the_door
from tests.test_check_size import worst_check

pytestmark = pytest.mark.postgres

# The tenant gate's database and RLS-enforced role, and part 1's two readers and their book.
admin_dsn = read_map_gate.admin_dsn
database = read_map_gate.database
tenant_dsn = read_map_gate.tenant_dsn
readers = read_map_gate.readers
lamp_book = read_map_gate.lamp_book
line = read_map_gate.line
page = read_map_gate.page
upload = read_map_gate.upload

# The platform's settings for the chat: its own (fake) keys, the read map, explicit caps, and
# chapter notes off until part 6 (recollections already default off in hosted mode).
CHAT = {"reader_conversation_jev": True, "jev_enabled": True,
        "typesafe_api_key": "test-typesafe-not-real", "openai_api_key": "sk-test-not-real",
        "reader_story_notes": False, "hosted_reader_usd_per_day": 1.0,
        "hosted_reader_usd_per_month": 5.0, "hosted_platform_usd_per_day": 100.0}
QUESTION = "What did Mara do?"


@pytest.fixture(autouse=True)
def restore_caps():
    """The app's lifespan sets this process's caps from its settings; put them back after."""
    before = spend_limits()
    yield
    set_spend_limits(**dataclasses.asdict(before))


@pytest.fixture()
def chat_app(tenant_dsn: str, readers: dict, tmp_path):
    from datetime import UTC, datetime

    from fastapi.testclient import TestClient

    from app.main import create_app
    from tests.hosted.test_tenant_postgres import UnusedOIDCClient

    tokens, sessions = read_map_gate.signed_in(readers)
    app = create_app(
        read_map_gate.hosted_settings(tenant_dsn, tmp_path / "objects", **CHAT),
        auth_repository=sessions, oidc_client=UnusedOIDCClient(),
        auth_clock=lambda: datetime(2026, 7, 16, 12, tzinfo=UTC))
    with TestClient(app, base_url="https://reader.example") as client:
        yield client, tokens, app


# ---- stand-in models: the local stream tests' writer and checker, on any client --------------

class StandIns:
    """The local tests' stand-ins (``stream_models`` and ``models``) installed on one client.

    The writer returns ``reply`` (or the second look's verdict when asked with a schema). Jev
    doubts a sentence holding an ``unsupported`` word, is sure of a dropped condition for a
    ``sure`` word, finds a sentence ending in "?" free of story content, and raises an outage
    when ``down``. ``second`` maps a word to the second look's verdict (default: it agrees with
    the doubt). Everything the models are given is kept, and every call counted.
    """

    def __init__(self, client, monkeypatch, *, reply="Hello.", unsupported=(), second=None,
                 sure=(), down=False, jev=None, write_hook=None, judge_hook=None):
        self.reply, self.unsupported, self.second = reply, tuple(unsupported), second or {}
        self.sure, self.down = tuple(sure), down
        self.write_hook, self.judge_hook = write_hook, judge_hook
        self.prompts, self.systems, self.checked, self.looked = [], [], [], []
        self.writes = self.judged = 0
        self._lock = threading.Lock()
        monkeypatch.setattr(client, "complete", self.complete)
        monkeypatch.setattr(client, "jev", jev or SimpleNamespace(
            model="jev-1.14.0", evaluate=self.evaluate, close=lambda: None), raising=False)

    def complete(self, system, user, tier="cheap", schema=None):
        if schema is not None:
            assert schema.__name__ == "SentenceVerdict" and "SOURCE PASSAGES" not in user
            sentence = json.loads(user)["sentence"]
            with self._lock:
                self.looked.append(sentence)
            if self.judge_hook:
                self.judge_hook("second-look")
            return {"supported": any(ok and word in sentence
                                     for word, ok in self.second.items())}, {"in": 90, "out": 5}
        assert "SOURCE PASSAGES" in user
        with self._lock:
            self.writes += 1
            self.prompts.append(user)
            self.systems.append(system)
        if self.write_hook:
            self.write_hook()
        return self.reply, {"in": 100, "out": 40}

    def evaluate(self, state, questions, **_options):
        with self._lock:
            self.judged += 1
            if "sentence" in state:
                self.checked.append(state)
        if self.judge_hook:
            self.judge_hook("check" if "sentence" in state else "intent")
        if self.down:
            raise JevUnavailable("down")
        sentence = state.get("sentence", "")
        bad = any(word in sentence for word in self.unsupported)
        chat = "?" in sentence
        return {name: {"noul": .01 if chat else .9} if name == "book_assertion"
                else {"choice": "unestablished" if bad else "supported",
                      "probabilities": {"supported": .1 if bad else .95}} if q["type"] == "choice"
                else {"noul": .95 if name == "dropped_condition"
                      and any(word in sentence for word in self.sure) else .01}
                for name, q in questions.items()}, {"in": 50, "out": 5}

    def everything(self) -> str:
        """Every word the writer and Jev were given."""
        return "\n".join(self.prompts + [json.dumps(state, ensure_ascii=False)
                                         for state in self.checked])


# ---- one reader's side of the HTTP app ---------------------------------------------------------

class Reader:
    def __init__(self, client, tokens, label: str, owner: uuid.UUID) -> None:
        self.client, self.tokens, self.label, self.owner = client, tokens, label, owner

    def become(self) -> str:
        return _become(self.client, self.tokens, self.label)

    def call(self, method: str, path: str, body=None):
        return self.client.request(method, path, json=body,
                                   headers={CSRF_HEADER: self.become()})

    def mark(self, book: str, atoms: list[int], read: bool = True) -> None:
        response = self.call("PUT", f"/api/books/{book}/read-chapters",
                             {"atoms": atoms, "read": read})
        assert response.status_code == 200, response.text

    def read(self, book: str, chapter: int, first: int, last: int, skimmed: bool = False):
        response = self.call("PUT", f"/api/books/{book}/read-page",
                             page(chapter, first, last, skimmed=skimmed))
        assert response.status_code == 200, response.text
        return response.json()

    def point(self, book: str) -> dict:
        self.become()
        response = self.client.get(f"/api/books/{book}/read-frontier")
        assert response.status_code == 200, response.text
        return response.json()

    def stream(self, book: str, question: str = QUESTION, point: dict | None = None):
        point = point or self.point(book)
        return self.call("POST", f"/api/books/{book}/ask/stream",
                         {"question": question, "read_frontier": point})

    def ask(self, book: str, question: str = QUESTION) -> list[dict]:
        response = self.stream(book, question)
        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith("text/event-stream")
        return events_of(response)

    def shown(self, book: str) -> list[str]:
        self.become()
        response = self.client.get(f"/api/books/{book}/conversation")
        assert response.status_code == 200, response.text
        return [turn["content"] for turn in response.json()["turns"]]


def events_of(response) -> list[dict]:
    return [json.loads(row[6:]) for row in response.text.splitlines() if row.startswith("data: ")]


def sentences(events) -> list[str]:
    return [event["text"] for event in events if event["type"] == "sentence"]


def reader(chat_app, readers, label="a") -> Reader:
    client, tokens, _app = chat_app
    return Reader(client, tokens, label, readers[label])


def adapters(app, owner: uuid.UUID):
    catalog = HostedCompanionCatalog(app.state.companion_dsn, OwnerId(owner))
    return catalog, HostedBookSource(app.state.object_storage, catalog,
                                     app.state.companion_sources)


def turns_of(dsn: str, owner: uuid.UUID) -> list[dict]:
    with psycopg.connect(dsn, row_factory=psycopg.rows.dict_row) as conn:
        return conn.execute("SELECT * FROM conversation_turns WHERE owner_id=%s ORDER BY turn_id",
                            (owner,)).fetchall()


def ledger_of(dsn: str, owner: uuid.UUID) -> list[dict]:
    with psycopg.connect(dsn, row_factory=psycopg.rows.dict_row) as conn:
        return conn.execute("SELECT * FROM cost_ledger WHERE owner_id=%s ORDER BY recorded_at,id",
                            (owner,)).fetchall()


def reservations_of(dsn: str, owner: uuid.UUID) -> list[dict]:
    with psycopg.connect(dsn, row_factory=psycopg.rows.dict_row) as conn:
        return conn.execute("SELECT * FROM cost_reservations WHERE owner_id=%s ORDER BY created_at",
                            (owner,)).fetchall()


# ---- the story memory: empty (every hosted book until extraction) or seeded --------------------

# (name, the chapter it is first met in); the lamp book names Mara in part 1, Corvus in part 2
# and Vex in part 3. "the Lamplighter" is a late alias of Vex, never written in the book.
PEOPLE = [("Mara", 1), ("Corvus", 2), ("Vex", 3)]
ALIASES = [("Vex", "the Lamplighter", 3)]


def seed_memory(dsn: str, owner: uuid.UUID, book: str, *,
                keys: dict[int, str] | None = None) -> None:
    """The memory rows extraction would publish, written past row-level security as the parity
    suite writes them: three chapters, the first with its completion receipt (every person and
    alias is recorded against a completed chapter, which the schema requires), and the people
    and aliases with the chapter each is first met in. Keyed as the book's own chapters when
    ``keys`` is given, so the chat's frontier counts the receipt; otherwise it never does.

    Written into the edition of the reader's file (ADR 0044: a reader owns no story memory row;
    the door hands them the edition's up to their place, so a person first met in a chapter they
    have not finished is never handed to the chat). The reader's place is left as their reading
    made it."""
    with psycopg.connect(dsn) as conn:
        [scope] = conn.execute("SELECT owner_id,id,incarnation FROM books WHERE owner_id=%s"
                               " AND id=%s", (owner, book)).fetchall()
    _seed_rows(dsn, owner, book, keys=keys)
    with psycopg.connect(dsn) as conn:
        serve_through_the_door(conn, tuple(scope), place=None)


def _seed_rows(dsn: str, owner: uuid.UUID, book: str, *, keys: dict[int, str] | None) -> None:
    with psycopg.connect(dsn) as conn, conn.transaction():
        incarnation = conn.execute("SELECT incarnation FROM books WHERE owner_id=%s AND id=%s",
                                   (owner, book)).fetchone()[0]
        scope = (owner, uuid.UUID(book), incarnation)
        chapter_ids, entity_ids = {}, {}
        for ordinal in (1, 2, 3):
            chapter_id = chapter_ids[ordinal] = uuid.uuid4()
            digest = hashlib.sha256(f"chapter {ordinal}".encode()).hexdigest()
            key = (keys or {}).get(ordinal, f"seed:{ordinal}")
            conn.execute("INSERT INTO chapters (owner_id,book_id,book_incarnation,id,chapter_key,"
                         "revealed_at,content_hash) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                         (*scope, chapter_id, key, ordinal, digest))
            if ordinal == 1:
                conn.execute("INSERT INTO ingested_chapters (owner_id,book_id,book_incarnation,"
                             "chapter_id,content_hash,completed_at) VALUES (%s,%s,%s,%s,%s,now())",
                             (*scope, chapter_id, digest))
        for name, ordinal in PEOPLE:
            entity_ids[name] = uuid.uuid4()
            conn.execute("INSERT INTO entities (owner_id,book_id,book_incarnation,id,"
                         "source_chapter_id,canonical_name,entity_type,revealed_at) "
                         "VALUES (%s,%s,%s,%s,%s,%s,'character',%s)",
                         (*scope, entity_ids[name], chapter_ids[1], name, ordinal))
        for name, surface, ordinal in ALIASES:
            conn.execute("INSERT INTO aliases (owner_id,book_id,book_incarnation,id,entity_id,"
                         "source_chapter_id,surface_form,revealed_at) "
                         "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                         (*scope, uuid.uuid4(), entity_ids[name], chapter_ids[1], surface,
                          ordinal))


def seeded_with_receipts(app, database: str, owner: uuid.UUID, book: str) -> None:
    """A memory built for chapter 1, as extraction would leave it once chapter 1 is read: its
    receipt keyed to the book's own chapter, so the chat's frontier and name groups are used."""
    catalog, source = adapters(app, owner)
    manifest, _chapters = source.verified_source(book, catalog.get_book(book))
    seed_memory(database, owner, book,
                keys={atom["ordinal"]: atom["key"] for atom in manifest["atoms"]})
    assert catalog.get_state(book)["ingest_progress"] == 1


MEMORIES = ["empty", "seeded"]


def opened(chat_app, readers, database, memory: str) -> tuple[Reader, str]:
    """Reader A with the lamp book: part 1 read in full, the first five lines of part 2 read."""
    _client, _tokens, app = chat_app
    a = reader(chat_app, readers)
    book = upload(a.client, a.tokens, "a", lamp_book())
    a.mark(book, [1])
    a.read(book, 2, 1, 5)
    if memory == "seeded":
        seeded_with_receipts(app, database, a.owner, book)
    return a, book


# ---- the local stream tests' spoiler probes, against the hosted adapters ----------------------

@pytest.mark.parametrize("memory", MEMORIES)
def test_a_doubted_sentence_is_withheld_and_the_rest_shown_in_order(chat_app, readers, database,
                                                                    monkeypatch, memory):
    """``test_stream_shows_checked_sentences_in_order_and_withholds_the_doubted_one``, hosted."""
    a, book = opened(chat_app, readers, database, memory)
    models = StandIns(chat_app[2].state.platform_client, monkeypatch,
                      reply=f"{line(1, 2)} She later sold it abroad. What did you make of that?",
                      unsupported=["abroad"])
    response = a.stream(book)
    events = events_of(response)
    assert sentences(events) == [line(1, 2), "What did you make of that?"]
    answer = events[-1]["answer"]
    assert events[-1]["type"] == "done" and "abroad" not in response.text
    assert answer["dropped_sentences"] == 1 and answer["style"] == "prose"
    assert not answer["insufficient_evidence"]
    assert models.writes == 1


@pytest.mark.parametrize("memory", MEMORIES)
def test_a_sentence_naming_someone_not_yet_met_is_removed(chat_app, readers, database,
                                                          monkeypatch, memory):
    """A person of part 3 (Vex) when parts 1 and the start of 2 are read. The checker doubts the
    sentence, as it does any sentence its passages do not hold; with a seeded memory the fixed
    unread-name check removes it even when the checker clears it, and the late alias with it.

    Seeded, the edition of the file holds Vex and his late alias, and the door hands the chat
    neither: nothing past the reader's place reaches the web process (ADR 0044 section 4). The
    fixed check asks the database instead (step 10), which holds them and answers only with the
    sentence's own words; so it removes both sentences, as it did when the reader's own rows held
    them (before step 5)."""
    a, book = opened(chat_app, readers, database, memory)
    client = chat_app[2].state.platform_client
    StandIns(client, monkeypatch, reply=f"{line(1, 2)} Vex moved lamp 50 in part 3.",
             unsupported=["Vex"])
    events = a.ask(book)
    assert sentences(events) == [line(1, 2)]
    assert "Vex" not in json.dumps(events)
    if memory == "seeded":
        with HostedBookStore(chat_app[2].state.companion_dsn, OwnerId(a.owner)).book(book) as mem:
            handed = json.dumps([mem._audit_all(table) for table in ("entities", "aliases")],
                                default=str)
            assert "Mara" in handed
            for later in ("Vex", "Corvus", "Lamplighter"):
                assert later not in handed, later
        for name in ("Vex moved lamp 50 in part 3.", "The Lamplighter moved lamp 50."):
            models = StandIns(client, monkeypatch, reply=f"{line(1, 2)} {name}")
            events = a.ask(book)
            assert sentences(events) == [line(1, 2)], name
            assert events[-1]["answer"]["dropped_sentences"] == 1
            assert len(models.checked) >= 1        # the checker was asked and cleared it


@pytest.mark.parametrize("memory", MEMORIES)
def test_foreshadowing_is_removed_even_when_the_checker_clears_it(chat_app, readers, database,
                                                                  monkeypatch, memory):
    """The fixed check against foreshadowing runs on every sentence, with no memory at all."""
    a, book = opened(chat_app, readers, database, memory)
    StandIns(chat_app[2].state.platform_client, monkeypatch,
             reply=f"{line(1, 2)} Mara will lose lamp 2 later.")
    events = a.ask(book)
    assert sentences(events) == [line(1, 2)] and "will lose" not in json.dumps(events)


@pytest.mark.parametrize("memory", MEMORIES)
def test_the_second_look_alone_decides_and_cannot_overrule_a_sure_dropped_condition(
        chat_app, readers, database, monkeypatch, memory):
    """``test_a_doubted_sentence_gets_one_second_look_which_alone_decides``, hosted."""
    a, book = opened(chat_app, readers, database, memory)
    models = StandIns(chat_app[2].state.platform_client, monkeypatch,
                      reply=f"{line(1, 2)} She seems careful with it. She later sold it abroad. "
                      "She must return it at dawn.",
                      unsupported=["careful", "abroad"], second={"careful": True, "abroad": False},
                      sure=["dawn"])
    response = a.stream(book)
    events = events_of(response)
    assert sentences(events) == [line(1, 2), "She seems careful with it."]
    assert sorted(models.looked) == ["She later sold it abroad.", "She seems careful with it."]
    assert events[-1]["answer"]["dropped_sentences"] == 2
    assert "abroad" not in response.text and "dawn" not in response.text


@pytest.mark.parametrize("memory", MEMORIES)
def test_nothing_cleared_ends_empty_and_a_checker_outage_shows_nothing(
        chat_app, readers, database, monkeypatch, memory):
    """``test_stream_with_nothing_cleared_ends_empty_and_an_outage_shows_nothing``, hosted; and
    nothing of a failed reply is stored."""
    a, book = opened(chat_app, readers, database, memory)
    client = chat_app[2].state.platform_client
    StandIns(client, monkeypatch, reply="Mara sold it abroad.", unsupported=["abroad"])
    events = a.ask(book)
    assert [e["type"] for e in events] == ["done"] and events[0]["answer"]["insufficient_evidence"]
    stored = turns_of(database, a.owner)
    StandIns(client, monkeypatch, reply=line(1, 2), down=True)
    response = a.stream(book)
    assert [(e["type"], e["status"], e["reason"]) for e in events_of(response)] == [
        ("error", 503, "unavailable")]
    assert "lamp 2" not in response.text
    assert turns_of(database, a.owner) == stored          # the failed reply stored nothing


def test_a_refused_check_shows_nothing_and_charges_nothing(chat_app, readers, database,
                                                           monkeypatch):
    """``test_a_refused_check_tells_the_owner_why_charges_nothing_and_shows_nothing``, hosted:
    the platform's checker account refused (402), every reservation released, no Jev spend."""
    from tests.test_jev import provider

    a, book = opened(chat_app, readers, database, "empty")
    refused = provider(402)
    StandIns(chat_app[2].state.platform_client, monkeypatch, reply=line(1, 2), jev=refused)
    try:
        response = a.stream(book)
    finally:
        refused.close()
    assert events_of(response) == [{"type": "error", "status": 503, "reason": "no_credits",
                                    "message": "Jev review unavailable; no unchecked reply was "
                                               "published"}]
    assert "lamp 2" not in response.text and "private-test-key" not in response.text
    assert not [row for row in ledger_of(database, a.owner) if row["phase"] == "judge"]
    assert not [row for row in reservations_of(database, a.owner) if row["state"] == "reserved"]
    assert turns_of(database, a.owner) == []


@pytest.mark.parametrize("memory", MEMORIES)
def test_a_name_not_yet_met_is_answered_as_not_there_yet(chat_app, readers, database,
                                                         monkeypatch, memory):
    """``test_a_name_not_in_the_pages_read_is_still_a_name_not_yet_met`` and its guard, hosted."""
    a, book = opened(chat_app, readers, database, memory)
    StandIns(chat_app[2].state.platform_client, monkeypatch, reply="Nothing true here.",
             unsupported=["Nothing"])
    assert a.ask(book, "who is Vex?")[-1]["answer"]["status"] == "ahead"
    answer = a.ask(book, "who is Mara?")[-1]["answer"]
    assert answer["insufficient_evidence"] and "status" not in answer


def accent_book() -> bytes:
    lines = [f"Adelaïda moved lamp {n} in part 1." for n in range(1, 61)]
    later = [f"Miüsov moved lamp {n} in part 2." for n in range(1, 61)]
    return epub_ncx([("c1.xhtml", "Part 1", "Part 1", " ".join(lines)),
                     ("c2.xhtml", "Part 2", "Part 2", " ".join(later))],
                    title="The Accent Book", author="Ada Writer")


def test_a_name_typed_without_its_accent_is_the_name_read(chat_app, readers, monkeypatch):
    """``test_names_without_accents``' probes, hosted: a read name typed without its accent is
    not a name not yet met, and a true sentence spelling it so is shown; an unread one still is."""
    a = reader(chat_app, readers)
    book = upload(a.client, a.tokens, "a", accent_book())
    a.mark(book, [1])
    client = chat_app[2].state.platform_client
    StandIns(client, monkeypatch, reply="Nothing true here.", unsupported=["Nothing"])
    answer = a.ask(book, "why did Adelaida move the lamp?")[-1]["answer"]
    assert answer["insufficient_evidence"] and "status" not in answer
    assert a.ask(book, "who is Miusov?")[-1]["answer"]["status"] == "ahead"
    StandIns(client, monkeypatch, reply="Adelaida moved lamp 2 in part 1.")
    assert sentences(a.ask(book, "why did Adelaida move the lamp?")) == [
        "Adelaida moved lamp 2 in part 1."]


def test_a_skimmed_page_never_reaches_a_model(chat_app, readers, monkeypatch):
    """``test_a_skimmed_page_is_not_read_and_never_reaches_a_model``, hosted."""
    a = reader(chat_app, readers)
    book = upload(a.client, a.tokens, "a", lamp_book())
    models = StandIns(chat_app[2].state.platform_client, monkeypatch,
                      reply="Corvus moved lamp 31 in part 2.")
    a.read(book, 1, 1, 5, skimmed=True)
    a.read(book, 1, 6, 10, skimmed=True)
    a.read(book, 2, 30, 34)
    a.ask(book, "What did Mara do with lamp 3?")
    assert line(2, 30) in models.prompts[-1] and "Mara moved" not in models.prompts[-1]
    assert models.checked and "Mara moved" not in models.everything()


def test_a_place_the_browser_claims_cannot_widen_what_a_reply_reads(chat_app, readers,
                                                                    monkeypatch):
    """The streamed chat's equal of the removed hosted ``/ask`` test's bound (ADR 0043 step 6),
    where a request for chapter 99 was answered as of the chapter read. The break it catches: a
    reply that reads further than the reader's own read map because the browser said so, by a
    later bookmark or by a read point that claims later chapters."""
    a = reader(chat_app, readers)
    book = upload(a.client, a.tokens, "a", lamp_book())
    a.mark(book, [1])
    honest = a.point(book)
    forged = {**honest, "atom": 3, "offset": 0, "available_chapter": 3}
    models = StandIns(chat_app[2].state.platform_client, monkeypatch, reply=line(1, 2))
    response = a.call("POST", f"/api/books/{book}/ask/stream",
                      {"question": "What happened to lamp 50?", "bookmark": 99,
                       "read_frontier": forged})
    assert response.status_code == 200, response.text
    events = events_of(response)
    answer = events[-1]["answer"]
    assert events[-1]["type"] == "done" and sentences(events) == [line(1, 2)]
    assert answer["as_of_chapter"] == honest["available_chapter"] <= 1
    assert answer["read_frontier"] == honest
    assert models.writes == 1 and models.checked
    assert "Corvus moved" not in models.everything() and "Vex moved" not in models.everything()


def test_a_turn_goes_from_the_chat_when_its_chapter_is_marked_not_read(chat_app, readers,
                                                                        monkeypatch):
    """``test_a_turn_about_a_chapter_goes_when_that_chapter_is_marked_not_read``, hosted: the
    turn's own record (``drew_on``) decides, on the conversation and in what the models get."""
    a = reader(chat_app, readers)
    book = upload(a.client, a.tokens, "a", lamp_book())
    client = chat_app[2].state.platform_client
    a.read(book, 1, 1, 3)
    a.mark(book, [2])
    exchange = ("Corvus frightens me, honestly!", "Corvus was the one who carried lamp 9.")
    StandIns(client, monkeypatch, reply=exchange[1])
    a.ask(book, exchange[0])
    assert a.shown(book)[-2:] == list(exchange)
    models = StandIns(client, monkeypatch, reply="Hello.")
    a.ask(book, "And then?")
    assert all(text in models.everything() for text in exchange)       # still read: carried
    a.mark(book, [2], read=False)
    models = StandIns(client, monkeypatch, reply="Hello.")
    a.ask(book, "And then?")
    assert not [text for text in exchange if text in models.everything()]
    assert not [text for text in exchange if text in a.shown(book)]


def big_source(n: int) -> dict:
    return {"id": n, "ordinal": 1, "title": "Part 1", "source_offset": n * 2000,
            "text": f"Read passage number {n:02d} opens here. "
                    + "Mara moved a lamp across the long room. " * 37}


def test_the_largest_check_stays_inside_the_checkers_limit(chat_app, readers, monkeypatch):
    """``test_book_chat_check_size``'s probes, hosted: a reader with more read text than one check
    can carry; read text gives way, and the writer is given exactly what its checker is given."""
    a = reader(chat_app, readers)
    book = upload(a.client, a.tokens, "a", lamp_book())
    a.read(book, 1, 1, 3)
    pool = [big_source(n) for n in range(1, 13)]
    monkeypatch.setattr(stream_conversation, "compact_candidates",
                        lambda *args, **more: [dict(s) for s in pool])
    monkeypatch.setattr(stream_conversation, "complete_ask_context",
                        lambda query, selected, *args, **more: list(selected))
    models = StandIns(chat_app[2].state.platform_client, monkeypatch,
                      reply="Mara moved lamp 2 in part 1.")
    assert sentences(a.ask(book)) == ["Mara moved lamp 2 in part 1."]
    largest = max(models.checked, key=lambda state: len(str(state["passages"])))
    assert worst_check(largest["passages"], largest["reader_message"]) <= CHECK_INPUT_LIMIT
    passages = models.checked[0]["passages"]
    texts = [" ".join(s["text"].split()) for s in pool[:10]]
    assert [text in models.prompts[-1] for text in texts] == [text in passages for text in texts]
    assert all(state["passages"] == passages for state in models.checked)


# ---- two readers -------------------------------------------------------------------------------

def test_second_reader_cannot_stream_from_first_readers_book(chat_app, readers, database,
                                                             monkeypatch):
    """The break it catches: a chat that tells a second reader the first reader's book exists,
    calls a model or reserves money for it, writes B's reply from A's reading, or shows B any of
    A's turns. Both readers hold the very same file. Runs on the RLS-enforced tenant role."""
    _client, _tokens, app = chat_app
    a = reader(chat_app, readers, "a")
    book_a = upload(a.client, a.tokens, "a", lamp_book())
    a.mark(book_a, [1])
    client = app.state.platform_client
    StandIns(client, monkeypatch, reply=line(1, 2))
    assert sentences(a.ask(book_a, "A's question about Mara.")) == [line(1, 2)]
    turns_a, ledger_a = turns_of(database, a.owner), ledger_of(database, a.owner)
    reservations_a = reservations_of(database, a.owner)
    assert len(turns_a) == 2 and ledger_a

    b = reader(chat_app, readers, "b")
    book_b = upload(b.client, b.tokens, "b", lamp_book())
    b.read(book_b, 2, 30, 34)
    point_b = b.point(book_b)
    models = StandIns(client, monkeypatch, reply=line(2, 31))
    missing = uuid.uuid4()
    hidden = b.stream(book_a, point=point_b)
    absent = b.stream(str(missing), point=point_b)
    assert (hidden.status_code, hidden.json()) == (absent.status_code, absent.json())
    assert (hidden.status_code, hidden.json()) == (404, {"detail": "unknown book"})
    assert models.writes == 0 and models.judged == 0                  # no model was called
    assert reservations_of(database, b.owner) == [] and ledger_of(database, b.owner) == []
    assert turns_of(database, a.owner) == turns_a and ledger_of(database, a.owner) == ledger_a
    assert reservations_of(database, a.owner) == reservations_a

    # B's own copy: written only from what B read, and B's conversation is B's alone.
    events = b.ask(book_b, "What did Corvus do?")
    assert sentences(events) == [line(2, 31)]
    assert line(2, 30) in models.prompts[-1]
    assert "Mara moved" not in models.everything() and "A's question" not in models.everything()
    assert b.shown(book_b) == ["What did Corvus do?", line(2, 31)]
    b.become()
    assert b.client.get(f"/api/books/{book_a}/conversation").status_code == 404
    assert turns_of(database, a.owner) == turns_a


def test_a_stored_turn_and_its_cost_belong_to_the_asking_reader(chat_app, readers, database,
                                                                monkeypatch):
    """The break it catches: a stored turn recording another book (or another reader's book)
    in what it drew on, or a reply's cost landing on the wrong reader or without its purpose."""
    _client, _tokens, app = chat_app
    a = reader(chat_app, readers, "a")
    book_a = upload(a.client, a.tokens, "a", lamp_book())
    a.mark(book_a, [1])
    b = reader(chat_app, readers, "b")
    book_b = upload(b.client, b.tokens, "b", lamp_book())
    b.read(book_b, 2, 30, 34)
    StandIns(app.state.platform_client, monkeypatch,
             reply=f"{line(2, 31)} She later sold it abroad.", unsupported=["abroad"])
    assert sentences(b.ask(book_b, "What did Corvus do?")) == [line(2, 31)]

    stored = turns_of(database, b.owner)
    assert [(turn["role"], turn["book_id"]) for turn in stored] == [
        ("user", uuid.UUID(book_b)), ("assistant", uuid.UUID(book_b))]
    catalog_b, source_b = adapters(app, b.owner)
    incarnation = catalog_b.get_book(book_b)["incarnation"]
    held = read_snapshot(book_b, catalog_b, app.state.settings, source_b)[5]
    for turn in stored:
        assert set(turn["drew_on"]) == {book_b}
        assert turn["drew_on"][book_b][0] == incarnation
        assert {int(atom): [list(span) for span in spans]
                for atom, spans in turn["drew_on"][book_b][1].items()} == {
            atom: [list(span) for span in spans] for atom, spans in held.items()}
    assert book_a not in json.dumps([turn["drew_on"] for turn in stored])
    assert turns_of(database, a.owner) == []

    costs = ledger_of(database, b.owner)
    assert costs and all(row["book_id"] == uuid.UUID(book_b) and row["purpose"] for row in costs)
    purposes = {row["purpose"] for row in costs}
    assert {"stream-reply", "stream-sentence-check", "stream-second-look"} <= purposes
    assert {row["phase"] for row in costs} <= {"synthesis", "judge"}
    assert {row["model"] for row in costs if row["phase"] == "synthesis"} == {"gpt-6.1-terra"}
    assert not [row for row in reservations_of(database, b.owner) if row["state"] == "reserved"]
    assert ledger_of(database, a.owner) == [] and reservations_of(database, a.owner) == []


# ---- the caps, end to end ------------------------------------------------------------------------

def test_at_the_day_cap_the_chat_ends_with_allowance_reached_before_the_writer(
        chat_app, readers, database, monkeypatch):
    """The break it catches: a reader at their day's allowance whose reply still reaches the
    writer, ends with an error the app cannot tell from a fault, stores a turn, or takes reading
    down with it."""
    from app.hosted.companion.spend import _DAY

    _client, _tokens, app = chat_app
    a, book = opened(chat_app, readers, database, "empty")
    update_limits(database, a.owner, {"max_spend_usd_per_day": Decimal("0.05")})
    catalog, _source = adapters(app, a.owner)
    held = catalog.reserve_cost(book, phase="stream-reply", model="gpt-6.1-terra",
                                input_tokens=10, output_tokens=10, usd=0.05, max_input_tokens=1,
                                max_output_tokens=1, max_usd=0.0)
    catalog.settle_cost(book, held)                    # exactly at the cap
    models = StandIns(app.state.platform_client, monkeypatch, reply=line(1, 2))
    events = a.ask(book)
    assert events == [{"type": "error", "status": 429, "code": "allowance_reached",
                       "message": _DAY}]
    assert models.writes == 0 and models.judged == 0
    assert turns_of(database, a.owner) == []
    assert not [row for row in reservations_of(database, a.owner) if row["state"] == "reserved"]
    # Reading takes no AI and keeps working.
    a.read(book, 2, 6, 9)
    a.become()
    assert a.client.get(f"/api/books/{book}/read-map").status_code == 200
    assert a.client.get("/api/books").status_code == 200


def test_a_reply_begun_under_the_day_cap_is_shown_whole_and_the_next_waits(
        chat_app, readers, database, monkeypatch):
    """The break it catches: a reader with a little allowance left whose reply is cut off, or
    never starts, because the reply's own calls pass the cap part-way; or a reader who, having
    passed the cap, is answered again."""
    from app.hosted.companion.spend import _DAY

    _client, _tokens, app = chat_app
    a, book = opened(chat_app, readers, database, "empty")
    update_limits(database, a.owner, {"max_spend_usd_per_day": Decimal("0.05")})
    catalog, _source = adapters(app, a.owner)
    held = catalog.reserve_cost(book, phase="stream-reply", model="gpt-6.1-terra",
                                input_tokens=10, output_tokens=10, usd=0.0499999,
                                max_input_tokens=1, max_output_tokens=1, max_usd=0.0)
    catalog.settle_cost(book, held)                    # a hair under the cap
    reserve, past_the_cap = SpendStore.reserve_cost, []

    def reserve_cost(adapter, book_id, **call):
        spent = sum(row["usd"] for row in ledger_of(database, a.owner))
        held = reserve(adapter, book_id, **call)
        past_the_cap.append(spent >= Decimal("0.05"))
        return held

    monkeypatch.setattr(SpendStore, "reserve_cost", reserve_cost)
    models = StandIns(app.state.platform_client, monkeypatch, reply=ORDINARY)
    events = a.ask(book)
    assert events[-1]["type"] == "done", events[-1]
    assert sentences(events) == [line(1, 2), "She seems careful with it.", line(1, 3),
                                 "It looks like she trusts Jonas less now.",
                                 "What did you make of that?"]
    # The reply's first call was admitted under the cap; later ones were made past it.
    assert past_the_cap[0] is False and any(past_the_cap)
    assert not [row for row in reservations_of(database, a.owner) if row["state"] == "reserved"]

    writes = models.writes
    assert a.ask(book) == [{"type": "error", "status": 429, "code": "allowance_reached",
                            "message": _DAY}]
    assert models.writes == writes


class OpenReservations:
    """Every companion reservation open at once, by wrapping the adapter's four spend calls."""

    def __init__(self, monkeypatch) -> None:
        self.open, self.lock = {}, threading.Lock()
        self.peak_count, self.peak_usd, self.at_peak = 0, Decimal(0), []
        self.reserved = []
        reserve, note = SpendStore.reserve_cost, SpendStore.note_reservation_actual
        settle, discard = SpendStore.settle_cost, SpendStore.discard_cost_reservation

        def reserve_cost(adapter, book_id, **call):
            held = reserve(adapter, book_id, **call)
            with self.lock:
                self.open[held] = (call["phase"], call["model"], Decimal(str(call["usd"])))
                self.reserved.append((call["phase"], call["model"], Decimal(str(call["usd"]))))
                self._measure()
            return held

        def note_actual(adapter, book_id, held, **call):
            note(adapter, book_id, held, **call)
            with self.lock:
                phase, model, _usd = self.open[held]
                self.open[held] = (phase, model, Decimal(str(call["usd"])))

        def closing(real):
            def close(adapter, book_id, held, **call):
                real(adapter, book_id, held, **call)
                with self.lock:
                    self.open.pop(held, None)
            return close

        monkeypatch.setattr(SpendStore, "reserve_cost", reserve_cost)
        monkeypatch.setattr(SpendStore, "note_reservation_actual", note_actual)
        monkeypatch.setattr(SpendStore, "settle_cost", closing(settle))
        monkeypatch.setattr(SpendStore, "discard_cost_reservation", closing(discard))

    def _measure(self) -> None:
        """The most reservations open at once, and separately the most dollars held at once."""
        total = sum(usd for _phase, _model, usd in self.open.values())
        self.peak_count = max(self.peak_count, len(self.open))
        if total > self.peak_usd:
            self.peak_usd, self.at_peak = total, sorted(self.open.values())


def streaming_writer(monkeypatch, models: StandIns, *, pause: float) -> None:
    """The writer streams its reply word by word, so checks run while it is still writing."""
    def complete(client, system, user, tier="cheap", schema=None, max_output_tokens=None):
        if schema is not None:
            return models.complete(system, user, tier=tier, schema=schema)
        value, usage = models.complete(system, user, tier=tier)
        for word in value.split(" "):
            client.on_text_delta(word + " ")
            time.sleep(pause)
        return value, usage

    monkeypatch.setattr(LLMClient, "complete", complete)


ORDINARY = (f"{line(1, 2)} She seems careful with it. {line(1, 3)} It looks like she trusts "
            "Jonas less now. What did you make of that?")


def test_an_ordinary_reply_is_not_refused_by_the_calls_in_flight_cap(chat_app, readers, database,
                                                                     monkeypatch):
    """The break it catches: the reader's calls-in-flight cap (8 by default) refusing a reply's
    own checks. A streamed reply of five sentences, two doubted and given a second look, with a
    slow checker so the checks overlap the writer and each other. Also records how many
    reservations one reply holds open at once (ADR 0041 part 5 asks for the numbers)."""
    _client, _tokens, app = chat_app
    a, book = opened(chat_app, readers, database, "empty")
    with psycopg.connect(database) as conn:
        assert conn.execute("SELECT max_companion_calls_in_flight FROM owner_limits"
                            " WHERE owner_id=%s", (a.owner,)).fetchone()[0] == 8
    client = app.state.platform_client
    models = StandIns(client, monkeypatch, reply=ORDINARY, unsupported=["careful", "trusts"],
                      second={"careful": True, "trusts": True},
                      judge_hook=lambda kind: time.sleep(.15))
    monkeypatch.delattr(client, "complete")         # the class's streaming writer below
    streaming_writer(monkeypatch, models, pause=.02)
    spend = OpenReservations(monkeypatch)
    events = a.ask(book)
    assert events[-1]["type"] == "done", events[-1]
    assert sentences(events) == [line(1, 2), "She seems careful with it.", line(1, 3),
                                 "It looks like she trusts Jonas less now.",
                                 "What did you make of that?"]
    assert len(models.looked) == 2
    # The writer, the four check threads, a sentence judged again on the reply's own thread and
    # the reply's intent check are the most that can be open at once: seven.
    assert 2 <= spend.peak_count <= 7
    assert spend.peak_count <= 8
    record = {"peak_open": spend.peak_count, "peak_usd": str(spend.peak_usd),
              "at_peak": [(phase, model, str(usd)) for phase, model, usd in spend.at_peak],
              "reserved": sorted({(phase, model, str(usd)) for phase, model, usd in spend.reserved})}
    print("RESERVATIONS", json.dumps(record))


# ---- failing closed ---------------------------------------------------------------------------

def test_a_story_memory_that_cannot_be_read_stops_the_reply(chat_app, readers, database,
                                                            monkeypatch):
    """The break it catches: a reply that goes on, unchecked by names, when the reader's story
    memory cannot be loaded. Nothing is shown and nothing stored."""
    from app.hosted.companion import memory

    a, book = opened(chat_app, readers, database, "seeded")

    def unavailable(self, book_id):
        raise StoryMemoryUnavailable("the story memory could not be read")

    monkeypatch.setattr(memory.HostedBookStore, "_load", unavailable)
    StandIns(chat_app[2].state.platform_client, monkeypatch, reply=line(1, 2))
    response = a.stream(book)
    assert events_of(response) == [{"type": "error", "status": 500, "message": "reply failed"}]
    assert "lamp 2" not in response.text
    assert turns_of(database, a.owner) == []


def test_the_longest_reply_there_can_be_is_stored(chat_app, readers, database, monkeypatch):
    """The stored turn is capped at 20,000 characters in PostgreSQL. A reply is at most
    ``MAX_SENTENCES`` sentences of at most ``MAX_SENTENCE_CHARS`` each, joined by spaces (4,811
    characters), and the reader's message at most 500: the cap cannot be reached. Checked with the
    longest of both, and a reply that runs past the sentence limit."""
    a, book = opened(chat_app, readers, database, "empty")
    longest = (MAX_SENTENCES * MAX_SENTENCE_CHARS) + (MAX_SENTENCES - 1)
    assert longest == 4811 and longest < 20_000
    sentence = "Mara " + "a" * (MAX_SENTENCE_CHARS - 6) + "."
    assert len(sentence) == MAX_SENTENCE_CHARS
    StandIns(chat_app[2].state.platform_client, monkeypatch,
             reply=" ".join([sentence] * (MAX_SENTENCES + 3)))
    question = "Mara? " + "q" * 494
    assert len(question) == 500
    events = a.ask(book, question)
    assert len(sentences(events)) == MAX_SENTENCES
    stored = turns_of(database, a.owner)
    assert [len(turn["content"]) for turn in stored] == [500, longest]


def test_a_reply_holds_no_database_connection_while_a_model_answers(chat_app, readers, database,
                                                                    tenant_dsn, monkeypatch):
    """ADR 0041 section 2: every adapter call is one short transaction, so while the writer and the
    checker are working, none of the tenant role's connections is open."""
    a, book = opened(chat_app, readers, database, "seeded")
    role = conninfo.conninfo_to_dict(tenant_dsn)["user"]
    counts = {"write": [], "check": []}

    def open_connections(kind):
        time.sleep(.3)                  # everything started before the model has finished
        with psycopg.connect(database) as conn:
            counts[kind].append(conn.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE usename=%s", (role,)).fetchone()[0])

    StandIns(chat_app[2].state.platform_client, monkeypatch, reply=line(1, 2),
             write_hook=lambda: open_connections("write"),
             judge_hook=lambda kind: open_connections(kind) if kind == "check" else None)
    assert sentences(a.ask(book)) == [line(1, 2)]
    assert counts["write"] == [0]
    assert counts["check"] and set(counts["check"]) == {0}
