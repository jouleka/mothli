"""Story memory for the checks gives the same answers locally and hosted (ADR 0041 part 4).

The parity corpus (``fixtures/spoiler_parity.json``), extended here with a person whose name is
an ordinary word, a late alias, a retracted person, a retracted alias and a retracted theme, is
loaded into a local ``MemoryDB`` and into the Postgres memory tables of one hosted reader. At
every chapter counter the unchanged ``score_recap``, ``same_person_names`` and
``completion_frontier`` must return exactly the same thing from both, on the tenant role with
row-level security and on a superuser connection where only the adapter's own predicates hold.
An empty memory must behave like a local book with extraction off. A memory that cannot be read
must stop the reply, and one reader's snapshot must never hold another's rows.
"""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest

import app.hosted.companion as companion
from app.eval.spoiler_gate import score_recap
from app.hosted.companion import CompanionDatabaseBusy
from app.hosted.companion.memory import (
    HostedBookStore,
    StoryMemorySnapshot,
    StoryMemoryUnavailable,
)
from app.hosted.tenant.models import OwnerId
from app.known_names import same_person_names
from app.memory.dal import _now
from app.memory.store import Store
from app.stream_conversation import gate_sentences, sentence_checks
from app.unicode_text import word_tokens
from tests.hosted import test_tenant_postgres as tenant_gate
from tests.parity._adapters import (
    PostgresParityAdapter,
    SQLiteParityAdapter,
    _uuid,
    load_corpus,
)

# The tenant gate's own database and RLS-enforced role, granted from the same allow-list.
admin_dsn = tenant_gate.admin_dsn
database = tenant_gate.database
tenant_dsn = tenant_gate.tenant_dsn

CORPUS = Path(__file__).parent / "fixtures" / "spoiler_parity.json"
BOOKMARKS = range(0, 8)  # before the first chapter, each chapter, past the retracted last one

# (key, name, type, revealed_at, retracted)
EXTRA_ENTITIES = [
    ("rose", "Rose", "character", 5, False),            # a person whose name is an ordinary word
    ("smerdyakov", "Smerdyakov", "character", 2, True),  # a retracted person
]
# (entity, surface, revealed_at, retracted)
EXTRA_ALIASES = [
    ("alyosha", "Lyosha", 4, False),   # a late alias of a person met in chapter 1
    ("dmitri", "Mitya", 3, True),      # a retracted alias
    ("rose", "Rosie", 6, False),       # a late alias of a late person
]
# (key, name, revealed_at, retracted)
EXTRA_THEMES = [("vengeance", "Vengeance", 5, True)]

SENTENCES = [
    "Alyosha prayed quietly in the monastery.",
    "Alexei smiled at his brother.",
    "Katerina laughed at the engagement.",
    "Alexander returned home.",
    "Alexandra spoke to Alyosha.",
    "Sasha left the room.",
    "The Shadow watched them from the gate.",
    "The hidden witness spoke at last.",
    "Lyosha smiled at Dmitri.",
    "Rose waited by the gate.",
    "The sun rose over the monastery.",
    "Rosie sang to herself.",
    "Smerdyakov lied to everyone.",
    "Mitya ran into the night.",
    "Dmitri will leave the town soon.",
    "Vengeance burned in him.",
    "Doubt crept into his prayers.",
]
FOCUS = [set(), {"alyosha"}, {"lyosha"}, {"alexei"}, {"sasha"}, {"dmitri"}, {"rose"}, {"rosie"}]


def _corpus() -> dict:
    return load_corpus(CORPUS)


def _atoms(corpus) -> list[dict]:
    return [{"key": chapter["key"], "ordinal": chapter["ordinal"]} for chapter in corpus["chapters"]]


def _read_text(corpus, bookmark) -> str:
    """The same read prefix for both modes: every live chapter's text up to the counter."""
    return " ".join(chapter["text"] for chapter in corpus["chapters"]
                    if chapter["ordinal"] <= bookmark and not chapter.get("retracted"))


def _read_texts(corpus, bookmark):
    prefix = _read_text(corpus, bookmark)
    # None is the eval-only mode; the third prefix has read the late alias mid-clause, so
    # reader-parity may drop it (the same in both modes or not at all).
    return [None, prefix, prefix + " Everyone called him Lyosha, and Rose was a name in town."]


# ---- seeding: the corpus plus the extra rows, in the same insertion order on both sides ----

def _seed_local_extras(local: SQLiteParityAdapter) -> None:
    with local.store.book(local.book_key) as mem:
        for key, name, kind, revealed_at, retracted in EXTRA_ENTITIES:
            entity_id = mem.add_entity(name, kind, revealed_at)
            local._entity_ids[key] = entity_id
            if retracted:
                with mem._writer():
                    mem._conn.execute("UPDATE entities SET retracted_at=? WHERE entity_id=?",
                                      (_now(), entity_id))
        for entity, surface, revealed_at, retracted in EXTRA_ALIASES:
            alias_id = mem.add_alias(local._entity_ids[entity], surface, revealed_at)
            if retracted:
                with mem._writer():
                    mem._conn.execute("UPDATE aliases SET retracted_at=? WHERE alias_id=?",
                                      (_now(), alias_id))
        for _key, name, revealed_at, retracted in EXTRA_THEMES:
            theme_id = mem.add_theme(name, f"{name}, as a theme.", revealed_at)
            if retracted:
                with mem._writer():
                    mem._conn.execute("UPDATE themes SET retracted_at=? WHERE theme_id=?",
                                      (_now(), theme_id))


def _seed_hosted_extras(remote: PostgresParityAdapter, corpus: dict) -> None:
    scope = remote._scope()
    chapter_at = {chapter["ordinal"]: remote._chapter_ids[chapter["key"]]
                  for chapter in corpus["chapters"]}
    stamp = "now()"
    with remote.conn.transaction():
        for key, name, kind, revealed_at, retracted in EXTRA_ENTITIES:
            entity_id = _uuid("entity", key)
            remote._entity_ids[key] = entity_id
            remote.conn.execute(
                "INSERT INTO entities (owner_id,book_id,book_incarnation,id,source_chapter_id,"
                "canonical_name,entity_type,revealed_at,retracted_at) "
                f"VALUES (%s,%s,%s,%s,%s,%s,%s,%s,{stamp if retracted else 'NULL'})",
                (*scope, entity_id, chapter_at[revealed_at], name, kind, revealed_at))
        for index, (entity, surface, revealed_at, retracted) in enumerate(EXTRA_ALIASES):
            remote.conn.execute(
                "INSERT INTO aliases (owner_id,book_id,book_incarnation,id,entity_id,"
                "source_chapter_id,surface_form,revealed_at,retracted_at) "
                f"VALUES (%s,%s,%s,%s,%s,%s,%s,%s,{stamp if retracted else 'NULL'})",
                (*scope, _uuid("alias", f"extra:{index}"), remote._entity_ids[entity],
                 chapter_at[revealed_at], surface, revealed_at))
        for key, name, revealed_at, retracted in EXTRA_THEMES:
            remote.conn.execute(
                "INSERT INTO themes (owner_id,book_id,book_incarnation,id,source_chapter_id,name,"
                "description,revealed_at,retracted_at) "
                f"VALUES (%s,%s,%s,%s,%s,%s,%s,%s,{stamp if retracted else 'NULL'})",
                (*scope, _uuid("theme", key), chapter_at[revealed_at], name,
                 f"{name}, as a theme.", revealed_at))
        # Rows inserted in one transaction share now(); give them the local insertion order, as
        # an extraction that publishes chapter by chapter would (the tie-break among rows first
        # revealed at the same chapter).
        base = datetime(2026, 1, 1, tzinfo=UTC)
        entity_ids = [_uuid("entity", item["key"]) for item in corpus["entities"]]
        entity_ids += [_uuid("entity", key) for key, *_rest in EXTRA_ENTITIES]
        alias_ids = [_uuid("alias", f'{item["entity"]}:{index}')
                     for index, item in enumerate(corpus["aliases"])]
        alias_ids += [_uuid("alias", f"extra:{index}") for index in range(len(EXTRA_ALIASES))]
        for table, ids in (("entities", entity_ids), ("aliases", alias_ids)):
            for position, row_id in enumerate(ids):
                remote.conn.execute(
                    f"UPDATE {table} SET recorded_at=%s "
                    "WHERE owner_id=%s AND book_id=%s AND book_incarnation=%s AND id=%s",
                    (base + timedelta(milliseconds=position), *scope, row_id))


@pytest.fixture()
def seeded(tmp_path, database):
    corpus = _corpus()
    local = SQLiteParityAdapter(tmp_path / "local", corpus)
    _seed_local_extras(local)
    remote = PostgresParityAdapter(database, corpus)
    _seed_hosted_extras(remote, corpus)
    try:
        yield corpus, local, remote
    finally:
        local.close()
        remote.close()


# ---- what the companion modules see, from either memory ----

def _audit(mem) -> dict:
    entities = mem._audit_all("entities")
    names = {row["entity_id"]: row["canonical_name"] for row in entities}
    return {
        "entities": sorted((row["canonical_name"], row["type"], row["revealed_at"],
                            row["invalid_at"], row["retracted_at"] is None) for row in entities),
        "aliases": sorted((names[row["entity_id"]], row["surface_form"], row["revealed_at"],
                           row["retracted_at"] is None) for row in mem._audit_all("aliases")),
        "themes": sorted((row["name"], row["revealed_at"], row["invalid_at"],
                          row["retracted_at"] is None) for row in mem._audit_all("themes")),
    }


def _view(mem, bookmark) -> dict:
    view = mem.view(bookmark)
    by_type = {kind: [(row["canonical_name"], row["type"], row["revealed_at"])
                      for row in view.entities_of_type(kind)]
               for kind in ("character", "place", "faction", "object")}
    return {
        "characters": [(row["canonical_name"], row["revealed_at"]) for row in view.characters()],
        "by_type": by_type,
        "aliases": {row["canonical_name"]: [(alias["surface_form"], alias["revealed_at"])
                                            for alias in view.aliases_of(row["entity_id"])]
                    for kind in by_type for row in view.entities_of_type(kind)},
        "summaries": [(row["chapter_key"], row["revealed_at"], row["summary"])
                      for row in view.chapter_summaries()],
        "timeline": [(row["revealed_at"], row["order_idx"], row["summary"], row["kind"])
                     for row in view.timeline()],
        "chapters": [(row["chapter_key"], row["revealed_at"], row["title"],
                      row["part_label"] or None) for row in view.chapters()],
    }


def _answers(mem, corpus) -> dict:
    """Every answer the modules can get from this memory, at every counter, for every sentence."""
    atoms = _atoms(corpus)
    read_words = set(word_tokens(" ".join(
        [_read_text(corpus, 7), *SENTENCES, "Lyosha Rosie Mitya Smerdyakov"])))
    answers = {
        "audit": _audit(mem),
        "frontier": {
            "all": mem.completion_frontier(atoms),
            "first-three": mem.completion_frontier(atoms[:3]),
            "none": mem.completion_frontier([]),
            "wrong-key": mem.completion_frontier([{"key": "ch0", "ordinal": 1}, *atoms]),
            "wrong-ordinal": mem.completion_frontier([{"key": "ch1", "ordinal": 2}]),
            "gap": mem.completion_frontier([atoms[0], atoms[2]]),
        },
    }
    for bookmark in BOOKMARKS:
        answers[bookmark] = {
            "view": _view(mem, bookmark),
            "names": [same_person_names(mem, bookmark, read_words, focus) for focus in FOCUS],
            "recap": {(sentence, index): score_recap(mem, bookmark, sentence, read_text=text)
                      for sentence in SENTENCES
                      for index, text in enumerate(_read_texts(corpus, bookmark))},
        }
    return answers


def _local_answers(local, corpus) -> dict:
    with local.store.book(local.book_key) as mem:
        return _answers(mem, corpus)


def _hosted_answers(dsn, owner, book_id, corpus) -> dict:
    with HostedBookStore(dsn, OwnerId(owner)).book(str(book_id)) as mem:
        return _answers(mem, corpus)


def _leaks(answers, bookmark, sentence) -> list:
    return answers[bookmark]["recap"][(sentence, 1)]["future_entity_leaks"]


@pytest.mark.postgres
def test_the_checks_get_the_same_story_memory_locally_and_hosted(seeded, tenant_dsn, database):
    """The break it catches: a hosted snapshot that hides a future person or alias from the
    forbidden-name check (so a sentence naming someone the reader has not met would be shown in
    hosted mode and dropped locally), keeps a retracted or superseded row local mode ignores,
    groups names differently, or counts a different completion frontier."""
    corpus, local, remote = seeded
    expected = _local_answers(local, corpus)
    for dsn in (tenant_dsn, database):  # with row-level security, and with the predicates alone
        hosted = _hosted_answers(dsn, remote.owner_id, remote.book_id, corpus)
        assert hosted["audit"] == expected["audit"]
        assert hosted["frontier"] == expected["frontier"]
        for bookmark in BOOKMARKS:
            assert hosted[bookmark]["view"] == expected[bookmark]["view"], bookmark
            assert hosted[bookmark]["names"] == expected[bookmark]["names"], bookmark
            for key, result in expected[bookmark]["recap"].items():
                assert hosted[bookmark]["recap"][key] == result, (bookmark, key)

    # A floor under the comparison: the fixture exercises what it claims to.
    assert expected["frontier"]["all"] == 5   # chapter 6 is retracted
    assert _leaks(expected, 4, "The Shadow watched them from the gate.") == ["The Shadow"]
    assert _leaks(expected, 5, "The Shadow watched them from the gate.") == []
    assert _leaks(expected, 4, "Rose waited by the gate.") == ["Rose"]
    assert _leaks(expected, 4, "The sun rose over the monastery.") == ["Rose"]
    assert _leaks(expected, 5, "Rose waited by the gate.") == []
    assert _leaks(expected, 3, "Lyosha smiled at Dmitri.") == ["Alyosha"]
    assert _leaks(expected, 4, "Lyosha smiled at Dmitri.") == []
    assert _leaks(expected, 5, "Rosie sang to herself.") == ["Rose"]
    for bookmark in BOOKMARKS:
        assert _leaks(expected, bookmark, "Smerdyakov lied to everyone.") == []
        assert _leaks(expected, bookmark, "Mitya ran into the night.") == []
    assert expected[3]["names"][1] == [["Alyosha", "Alexei"]]
    assert expected[4]["names"][2] == [["Alyosha", "Alexei", "Lyosha"]]
    assert expected[4]["recap"][("Dmitri will leave the town soon.", 1)]["prolepsis_hits"]


def _hosted_reader(conn, *, chapters, people, title="The Parity Book") -> tuple[uuid.UUID, ...]:
    """A reader with their own copy of a book: chapters with receipts and the given people."""
    owner, book, incarnation = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    scope = (owner, book, incarnation)
    with conn.transaction():
        conn.execute("INSERT INTO users (id,display_name) VALUES (%s,'Reader')", (owner,))
        conn.execute("INSERT INTO books (owner_id,id,incarnation,title,schema_version) "
                     "VALUES (%s,%s,%s,%s,1)", (*scope, title))
        chapter_ids = {}
        for chapter in chapters:
            chapter_id = chapter_ids[chapter["ordinal"]] = uuid.uuid4()
            digest = "a" * 63 + str(chapter["ordinal"] % 10)
            conn.execute("INSERT INTO chapters (owner_id,book_id,book_incarnation,id,chapter_key,"
                         "revealed_at,content_hash) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                         (*scope, chapter_id, chapter["key"], chapter["ordinal"], digest))
            conn.execute("INSERT INTO ingested_chapters (owner_id,book_id,book_incarnation,"
                         "chapter_id,content_hash,completed_at) VALUES (%s,%s,%s,%s,%s,now())",
                         (*scope, chapter_id, digest))
        for name, revealed_at in people:
            conn.execute("INSERT INTO entities (owner_id,book_id,book_incarnation,id,"
                         "source_chapter_id,canonical_name,entity_type,revealed_at) "
                         "VALUES (%s,%s,%s,%s,%s,%s,'character',%s)",
                         (*scope, uuid.uuid4(), chapter_ids[revealed_at], name, revealed_at))
    return scope


@pytest.mark.postgres
def test_one_readers_snapshot_never_holds_another_readers_rows(seeded, tenant_dsn, database):
    """The break it catches: a snapshot query that misses an owner predicate or the owner's row
    security, so reader B's checks (or prompts) see reader A's names, or B can load A's book."""
    corpus, _local, a = seeded
    with psycopg.connect(database) as conn:
        b_owner, b_book, _b_incarnation = _hosted_reader(
            conn, chapters=corpus["chapters"][:2], people=[("Zinaida", 1), ("Kolya", 2)])
        # A's names as the superuser reads them, without the adapter.
        a_names = {row[0] for row in conn.execute(
            "SELECT canonical_name FROM entities WHERE owner_id=%s AND book_id=%s",
            (a.owner_id, a.book_id)).fetchall()}
    assert {"Alyosha", "The Shadow", "Rose", "Smerdyakov"} <= a_names

    for dsn in (tenant_dsn, database):
        b_store = HostedBookStore(dsn, OwnerId(b_owner))
        with b_store.book(str(b_book)) as mem:
            audit = _audit(mem)
            assert [row[0] for row in audit["entities"]] == ["Kolya", "Zinaida"]
            assert audit["aliases"] == [] and audit["themes"] == []
            assert mem.completion_frontier(_atoms(corpus)) == 2
            assert [row["canonical_name"] for row in mem.view(99).characters()] == [
                "Zinaida", "Kolya"]
            assert mem.view(99).timeline() == [] and mem.view(99).chapter_summaries() == []
        with pytest.raises(LookupError):
            with b_store.book(str(a.book_id)):
                pass
        # And A's snapshot holds nothing of B's.
        with HostedBookStore(dsn, OwnerId(a.owner_id)).book(str(a.book_id)) as mem:
            names = {row["canonical_name"] for row in mem._audit_all("entities")}
            assert names == a_names and not names & {"Zinaida", "Kolya"}

    # Row-level security is really on for the tenant role: with B's owner set, a query with no
    # owner predicate at all still sees none of A's rows.
    with psycopg.connect(tenant_dsn) as conn, conn.transaction():
        conn.execute("SELECT set_config('app.owner_id', %s, true)", (str(b_owner),))
        seen = {row[0] for row in conn.execute("SELECT canonical_name FROM entities").fetchall()}
    assert seen == {"Zinaida", "Kolya"}


@pytest.mark.postgres
def test_an_empty_story_memory_behaves_like_local_with_extraction_off(tmp_path, database):
    """The break it catches: a hosted book with no memory yet (every hosted book until extraction
    exists) answering the checks differently from a local book with extraction off, for example
    a non-zero frontier or a different verdict on a sentence."""
    corpus = _corpus()
    store = Store(str(tmp_path / "data"), vector_backend="bruteforce")
    with psycopg.connect(database) as conn, conn.transaction():
        owner, book = uuid.uuid4(), uuid.uuid4()
        conn.execute("INSERT INTO users (id,display_name) VALUES (%s,'Empty reader')", (owner,))
        conn.execute("INSERT INTO books (owner_id,id,title,schema_version) VALUES (%s,%s,'B',1)",
                     (owner, book))
    try:
        with store.book("empty-book") as local:
            expected = _answers(local, corpus)
        hosted = _hosted_answers(database, owner, book, corpus)
    finally:
        store.close()
    assert hosted == expected
    assert set(expected["frontier"].values()) == {0}
    assert expected["audit"] == {"entities": [], "aliases": [], "themes": []}
    assert all(result["future_entity_leaks"] == [] for bookmark in BOOKMARKS
               for result in expected[bookmark]["recap"].values())
    assert expected[4]["recap"][("Dmitri will leave the town soon.", 1)]["prolepsis_hits"]


@pytest.mark.postgres
def test_one_snapshot_per_store_shared_by_threads_and_never_reused_by_the_next(seeded, database):
    """The break it catches: a snapshot cached past its request (a reader's next reply checked
    against stale memory), or threads of one reply each loading a different memory."""
    _corpus_, _local, remote = seeded
    store = HostedBookStore(database, OwnerId(remote.owner_id))
    seen = []
    barrier = threading.Barrier(4)

    def take():
        barrier.wait()
        with store.book(str(remote.book_id)) as mem:
            seen.append(mem)

    threads = [threading.Thread(target=take) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(seen) == 4 and all(mem is seen[0] for mem in seen)

    scope = remote._scope()
    with remote.conn.transaction():
        remote.conn.execute(
            "INSERT INTO entities (owner_id,book_id,book_incarnation,id,source_chapter_id,"
            "canonical_name,entity_type,revealed_at) VALUES (%s,%s,%s,%s,%s,'Newcomer',"
            "'character',6)", (*scope, uuid.uuid4(), remote._chapter_ids["ch5"]))
    with store.book(str(remote.book_id)) as mem:
        assert "Newcomer" not in {row["canonical_name"] for row in mem._audit_all("entities")}
    with HostedBookStore(database, OwnerId(remote.owner_id)).book(str(remote.book_id)) as mem:
        assert "Newcomer" in {row["canonical_name"] for row in mem._audit_all("entities")}
        with pytest.raises(AttributeError):
            mem._entities = ()
        rows = mem._audit_all("entities")
        rows[0]["canonical_name"] = "Changed"
        assert "Changed" not in {row["canonical_name"] for row in mem._audit_all("entities")}


# ---- failing closed: none of these need a database ----

@contextmanager
def _owner_store(dsn="postgresql://reader@127.0.0.1:1/none?connect_timeout=2"):
    yield HostedBookStore(dsn, OwnerId(uuid.uuid4()))


def test_an_unreachable_database_stops_the_check_instead_of_passing_it():
    """The break it catches: a load failure turned into an empty memory, which would let every
    unread name through the check."""
    with _owner_store() as store:
        with pytest.raises(StoryMemoryUnavailable) as caught:
            with store.book(str(uuid.uuid4())):
                pass
    assert isinstance(caught.value.__cause__, psycopg.Error)


def test_exhausted_connections_stop_the_check(monkeypatch):
    held = threading.BoundedSemaphore(1)
    held.acquire()
    monkeypatch.setattr(companion, "_connections", held)
    monkeypatch.setattr(companion, "_CONNECT_WAIT_SECONDS", 0.01)
    with _owner_store() as store:
        with pytest.raises(StoryMemoryUnavailable) as caught:
            with store.book(str(uuid.uuid4())):
                pass
    assert isinstance(caught.value.__cause__, CompanionDatabaseBusy)


def _payload(**changes) -> dict:
    payload = {
        "content_language": "en",
        "chapters": [{"key": "ch1", "ordinal": 1, "title": "One", "part_label": None,
                      "live": True}],
        "completed": [{"key": "ch1", "ordinal": 1}],
        "entities": [{"id": "e1", "name": "Rose", "type": "character", "revealed_at": 1,
                      "invalid_at": None, "recorded_at": "2026-01-01T00:00:00+00:00",
                      "retracted_at": None}],
        "aliases": [{"id": "a1", "entity_id": "e1", "surface_form": "Rosie", "revealed_at": 1,
                     "recorded_at": "2026-01-01T00:00:00+00:00", "retracted_at": None}],
        "themes": [],
        "events": [],
        "summaries": [],
        "edges": [],
        "states": [],
    }
    payload.update(changes)
    return payload


class _FakeConnection:
    def __init__(self, row):
        self._row = row

    def execute(self, *_args):
        return self

    def fetchone(self):
        return self._row


MALFORMED = [
    {"entities": [{**_payload()["entities"][0], "revealed_at": 0}]},
    {"entities": [{**_payload()["entities"][0], "revealed_at": "3"}]},
    {"entities": [{**_payload()["entities"][0], "revealed_at": True}]},
    {"entities": [{**_payload()["entities"][0], "type": "ghost"}]},
    {"entities": [{**_payload()["entities"][0], "name": None}]},
    {"aliases": [{"id": "a1", "entity_id": "e1", "revealed_at": 1}]},
    {"chapters": [{"key": "ch1", "ordinal": 1, "title": None, "part_label": None, "live": 1}]},
    {"completed": [{"key": "ch1", "ordinal": -1}]},
    {"themes": None},
    {"events": ["not a row"]},
]


@pytest.mark.parametrize("changes", MALFORMED)
def test_a_malformed_row_stops_the_check(monkeypatch, changes):
    """The break it catches: a row the snapshot cannot read safely (a text ordinal would compare
    as 'revealed' with every counter) skipped or loaded instead of refused."""
    with pytest.raises((KeyError, TypeError, ValueError)):
        StoryMemorySnapshot(_payload(**changes))

    @contextmanager
    def transaction(self):
        yield _FakeConnection(_payload(**changes))

    monkeypatch.setattr(HostedBookStore, "_transaction", transaction)
    with _owner_store() as store:
        with pytest.raises(StoryMemoryUnavailable):
            with store.book(str(uuid.uuid4())):
                pass


def test_a_book_that_is_not_the_readers_is_unknown_not_empty(monkeypatch):
    @contextmanager
    def transaction(self):
        yield _FakeConnection(None)

    monkeypatch.setattr(HostedBookStore, "_transaction", transaction)
    with _owner_store() as store:
        for book_id in (str(uuid.uuid4()), "not-a-book"):
            with pytest.raises(LookupError):
                with store.book(book_id):
                    pass


def test_a_well_formed_snapshot_answers_and_refuses_a_malformed_counter():
    memory = StoryMemorySnapshot(_payload())
    assert memory.completion_frontier([{"key": "ch1", "ordinal": 1}]) == 1
    assert memory.content_language() == "en"
    assert [row["canonical_name"] for row in memory.view(1).characters()] == ["Rose"]
    assert memory.view(0).characters() == []
    for bookmark in ("1", None, True, 1.0):
        with pytest.raises(ValueError):
            memory.view(bookmark)
    with pytest.raises(ValueError):
        memory._audit_all("raw_chapters")
    with pytest.raises(RuntimeError):
        StoryMemorySnapshot(_payload(content_language="EN_us")).content_language()
    with pytest.raises(AttributeError):
        memory._completed = {}


def test_a_failed_load_stops_the_reply_inside_the_unchanged_checks(monkeypatch):
    """The break it catches: the module-level gate passing a sentence when the memory behind its
    unread-name check cannot be read. Nothing is shown: the error ends the reply."""
    monkeypatch.setattr(companion, "_CONNECT_WAIT_SECONDS", 0.01)
    held = threading.BoundedSemaphore(1)
    held.acquire()
    monkeypatch.setattr(companion, "_connections", held)
    with _owner_store() as store:
        _check, safe, _outage = sentence_checks(
            book_id=str(uuid.uuid4()), catalog=None, store=store, settings=None, writer=None,
            jev_run=None, note=lambda *args, **kwargs: None, passages_for=lambda sentence: [],
            question="Who is Rose?", identities=(), read_text="Alyosha arrives.",
            durable=lambda mem: 0)
        shown = []
        with ThreadPoolExecutor(max_workers=1) as pool, pytest.raises(StoryMemoryUnavailable):
            for event in gate_sentences(iter(["Rose waited by the gate. "]),
                                        lambda sentence, earlier: True, safe, pool=pool):
                shown.append(event)
        assert shown == []
