"""The story screens say the same for every book (ADR 0044 step 5), against real PostgreSQL.

Every book has notes in principle, built as readers reach its chapters and shared by everyone who
has the same file. So what a reader is shown about a book's story memory must not depend on how
the file came (free library or upload), on whether anyone else has the same file, or on whether
an edition of it exists yet (the owner's rule on acceptance: nothing shown may say anything about
another reader). Proven here, each written before the code it guards:

- before their first finished chapter, a reader gets the same status and the same bytes from
  every story route (the cast, a person's card, the story so far, progress) with no edition, with
  an edition others built far ahead, with an edition their own run built one ahead, and with a job
  of theirs waiting, now or until tomorrow: the empty cast every book shows, never "no story
  memory for this book";
- a reader at place *n* sees the same for an uploaded file and a free-library copy of it;
- a reader's own memory rows are never read: the door is the only source of story memory;
- "running" and "waiting" are told only while the reader has finished chapters whose notes the
  door does not hand them yet, so a reader the notes have caught up with sees the same whether
  their own run is building the next chapter (the first reader of a file) or not (a later one).

Stand-in writer, checker and Jev only: nothing is called and nothing paid.
"""

from __future__ import annotations

import hashlib
import json
import uuid

import httpx
import psycopg
import pytest

from app.hosted.companion.jobs import StoryMemoryQueue
from app.hosted.companion.memory import HostedBookStore
from app.hosted.companion.source import HostedBookSource
from app.hosted.companion.store import HostedCompanionCatalog
from app.hosted.tenant import classics as hosted_classics
from app.hosted.tenant.models import OwnerId
from tests.hosted import test_companion_read_map_postgres as read_map_gate
from tests.hosted import test_jobs_postgres as jobs_gate
from tests.hosted.test_companion_stream_postgres import (  # noqa: F401  (autouse fixture)
    Reader,
    restore_caps,
)
from tests.hosted.test_story_memory_jobs_postgres import (
    MEMORY,
    Lamps,
    built_for,
    drain,
    lamps,
    memory_app_for,
    rows,
    worker,
)
from tests.hosted.test_story_memory_postgres import edition_of
from tests.parity.test_story_memory_parity import extraction_client

pytestmark = pytest.mark.postgres

admin_dsn = read_map_gate.admin_dsn
database = read_map_gate.database
tenant_dsn = read_map_gate.tenant_dsn
worker_dsn = jobs_gate.worker_dsn

LABELS = ("a", "b", "c", "d", "e", "f", "g")
NOBODY = "00000000-0000-4000-8000-000000000000"
PRIDE = 1342          # a free-library entry; its file here is the lamp book

# What every story route answers a reader before their first finished chapter, for every book.
EMPTY = {
    "graph": (200, {"as_of_chapter": 0, "characters": [], "relationships": []}),
    "notes": (200, {"as_of_chapter": 0, "cast": [], "chapters": []}),
    "ingest": (200, {"ingest_progress": 0, "status": "idle", "flags": [], "error": None}),
    "character": (404, {"detail": "unknown character"}),
}


@pytest.fixture()
def people(database: str) -> dict:
    owners = {label: uuid.uuid4() for label in LABELS}
    with psycopg.connect(database) as conn:
        conn.cursor().executemany("INSERT INTO users (id,display_name) VALUES (%s,%s)",
                                  [(owner, f"Reader {label}") for label, owner in owners.items()])
    return owners


@pytest.fixture()
def shelf(tenant_dsn: str, people: dict, tmp_path, monkeypatch):
    """The hosted app with story memory on, and the free library's source in memory: every
    entry's file is the lamp book."""
    monkeypatch.setattr(hosted_classics, "_transport", lambda: httpx.MockTransport(
        lambda request: httpx.Response(200, content=lamps())
        if request.url.host == "gutenberg.pglaf.org" else httpx.Response(404)))
    client, tokens, app = memory_app_for(tenant_dsn, people, tmp_path, **MEMORY)
    with client:
        yield client, tokens, app


def reader(shelf, people, label: str) -> Reader:
    client, tokens, _app = shelf
    return Reader(client, tokens, label, people[label])


def upload(shelf, someone: Reader, blob: bytes) -> str:
    client, tokens, _app = shelf
    return read_map_gate.upload(client, tokens, someone.label, blob)


def add_from_the_library(someone: Reader, entry: int = PRIDE) -> str:
    added = someone.call("POST", f"/api/classics/{entry}")
    assert added.status_code == 201, added.text
    return added.json()["book_id"]


def paths(people_ids) -> dict[str, list[str]]:
    """Every story route, with the bookmarks a panel or a scrubber may ask for, and a card for
    each person the edition knows and for an id of nobody's."""
    return {
        "graph": ["graph", "graph?bookmark=0", "graph?bookmark=1", "graph?bookmark=99"],
        "notes": ["notes", "notes?bookmark=0", "notes?bookmark=99"],
        "ingest": ["ingest"],
        "character": [f"character/{person}{query}" for person in (*people_ids, NOBODY)
                      for query in ("", "?bookmark=99")],
    }


def seen(someone: Reader, book: str, people_ids) -> dict:
    """What every story route sends this reader: the status, the caching header and the body's
    bytes, with the reader's own book id (in chapter keys) put out of the way."""
    answers = {}
    for route, asked in paths(people_ids).items():
        for path in asked:
            response = someone.call("GET", f"/api/books/{book}/{path}")
            answers[path] = (response.status_code, response.headers.get("cache-control"),
                             response.content.replace(book.encode(), b"BOOK"))
    return answers


def expected_empty(people_ids) -> dict:
    """``EMPTY`` for every path, as the JSON the app writes."""
    answers = {}
    for route, asked in paths(people_ids).items():
        status, body = EMPTY[route]
        for path in asked:
            answers[path] = (status, body)
    return answers


def decoded(answers: dict) -> dict:
    return {path: (status, json.loads(body)) for path, (status, _cache, body) in answers.items()}


def edition_people(dsn: str, scope: tuple) -> dict[str, str]:
    with psycopg.connect(dsn) as conn:
        return dict(conn.execute("SELECT canonical_name,id::text FROM entities"
                                 " WHERE owner_id=%s AND book_id=%s", scope).fetchall())


def store_of(someone: Reader, book: str, atoms) -> tuple:
    """What the chat's and the screens' story memory holds for this reader: whether it hands any
    chapter, how far it is complete and every name its checks would read."""
    with HostedBookStore(tenant_dsn_value["dsn"], OwnerId(someone.owner)).book(book) as mem:
        return (mem.has_rows(), mem.completion_frontier(atoms),
                [sorted(str(row) for row in mem._audit_all(table))
                 for table in ("entities", "aliases", "themes")])


tenant_dsn_value: dict = {}


@pytest.fixture(autouse=True)
def _remember_tenant(tenant_dsn):
    tenant_dsn_value["dsn"] = tenant_dsn
    yield
    tenant_dsn_value.clear()


def atoms_of(shelf, someone: Reader, book: str) -> list[dict]:
    _client, _tokens, app = shelf
    catalog = HostedCompanionCatalog(app.state.companion_dsn, OwnerId(someone.owner))
    source = HostedBookSource(app.state.object_storage, catalog, app.state.companion_sources)
    manifest, _chapters = source.verified_source(book, catalog.get_book(book))
    return [{"key": atom["key"], "ordinal": atom["ordinal"]} for atom in manifest["atoms"]]


def place_of(dsn: str, owner, book) -> int:
    [row] = rows(dsn, "SELECT LEAST(memory_through,bookmark) AS at FROM reading_state"
                      " WHERE owner_id=%s AND book_id=%s", owner, book)
    return row["at"]


# ---- 1: before the first finished chapter, the same answer whatever exists -----------------------

def test_before_the_first_finished_chapter_every_story_route_answers_the_same_whatever_exists(
        shelf, people, database, tenant_dsn, worker_dsn, monkeypatch):
    """ADR 0044 section 3 (what a reader sees while a chapter is not ready), section 4 (what must
    answer identically) and the owner's rule on acceptance. The break it catches: any story route
    answering a reader who has not finished a chapter differently (a status, a header or one byte)
    depending on whether an edition of their file exists, how far others built it, whether their
    own run built the chapter ahead of them, or whether a job of theirs waits; in particular the
    404 "no story memory for this book" that a book with no notes and no job answered, which told
    a reader nobody had built anything (and the panels "I don't keep a list of who's who for this
    book yet"), or a "running" or "waiting" while there is nothing the reader could be shown."""
    _client, _tokens, app = shelf
    extraction = extraction_client()
    models = Lamps(extraction, monkeypatch)
    work = worker(app, extraction, worker_dsn)

    # Someone else reads the lamp book through part 2: its edition is built to part 3.
    builder = reader(shelf, people, "a")
    built_book = upload(shelf, builder, lamps())
    builder.read(built_book, 1, 1, 2)
    builder.mark(built_book, [1, 2])
    drain(work)
    edition = edition_of(database, builder.owner, built_book)
    assert built_for(database, builder.owner, built_book) == [(n, "body") for n in (1, 2, 3)]
    cast = edition_people(database, edition)
    assert set(cast) == {"Mara", "Corvus", "Vex"}

    situations = {}
    # No edition: a file nobody else has, nothing read.
    alone = reader(shelf, people, "b")
    situations["no edition"] = (alone, upload(shelf, alone, lamps("A Lamp Book Nobody Has")))
    # An edition others built far ahead: the very same file, nothing read yet; and the same
    # after a page read, whose run finds part 1 built and builds nothing.
    late = reader(shelf, people, "c")
    situations["built far ahead by others"] = (late, upload(shelf, late, lamps()))
    paged = reader(shelf, people, "d")
    paged_book = upload(shelf, paged, lamps())
    paged.read(paged_book, 1, 1, 2)
    situations["built by others, a page read"] = (paged, paged_book)
    # An edition their own run built one ahead: a file only they have, a page read, part 1 built.
    own = reader(shelf, people, "e")
    own_book = upload(shelf, own, lamps("The Lamp Book, Own Printing"))
    own.read(own_book, 1, 1, 2)
    drain(work)
    assert built_for(database, own.owner, own_book) == [(1, "body")]
    assert built_for(database, paged.owner, paged_book) == [(n, "body") for n in (1, 2, 3)]
    situations["built one ahead by their own run"] = (own, own_book)
    # A job of theirs waiting, to run now, and one waiting for tomorrow (a guard or the notes
    # ceiling held it): a page read, its run not taken yet.
    waits = reader(shelf, people, "f")
    waits_book = upload(shelf, waits, lamps("The Lamp Book, Waiting Printing"))
    waits.read(waits_book, 1, 1, 2)
    assert [row["state"] for row in rows(database, "SELECT state FROM jobs WHERE owner_id=%s"
                                         " AND kind='story_memory'", waits.owner)] == ["pending"]
    situations["a job of theirs waiting"] = (waits, waits_book)
    tomorrow = reader(shelf, people, "g")
    tomorrow_book = upload(shelf, tomorrow, lamps())
    assert StoryMemoryQueue(tenant_dsn, OwnerId(tomorrow.owner)).ensure(tomorrow_book,
                                                                         tomorrow=True)
    situations["a job of theirs waiting for tomorrow"] = (tomorrow, tomorrow_book)
    assert models.parts() == [1, 2, 3, 1]          # nothing else was built for anyone

    people_ids = sorted(cast.values())
    answers = {}
    for name, (someone, book) in situations.items():
        assert place_of(database, someone.owner, book) == 0, name
        answers[name] = seen(someone, book, people_ids)
        assert store_of(someone, book, atoms_of(shelf, someone, book)) == (
            False, 0, [[], [], []]), name
    first = answers["no edition"]
    assert decoded(first) == expected_empty(people_ids)
    for name, answer in answers.items():
        assert answer == first, name


# ---- 2: an upload and a free-library copy of one file ---------------------------------------------

def test_a_reader_at_place_n_sees_the_same_for_an_uploaded_file_and_a_free_library_copy(
        shelf, people, database, worker_dsn, monkeypatch):
    """ADR 0044 section 1 (a free-library book and an imported copy of the same file are one
    edition) and step 5. The break it catches: a story route answering a reader of a free-library
    copy differently from a reader of the same bytes uploaded, at the same place (before any
    chapter, and at each chapter), by status, header or byte; or either being refused where the
    other is answered."""
    _client, _tokens, app = shelf
    extraction = extraction_client()
    Lamps(extraction, monkeypatch)
    work = worker(app, extraction, worker_dsn)
    builder = reader(shelf, people, "a")
    built_book = upload(shelf, builder, lamps())
    builder.read(built_book, 1, 1, 2)
    builder.mark(built_book, [1, 2])
    drain(work)
    cast = edition_people(database, edition_of(database, builder.owner, built_book))
    people_ids = sorted(cast.values())

    library, uploaded = reader(shelf, people, "b"), reader(shelf, people, "c")
    books = {"b": add_from_the_library(library), "c": upload(shelf, uploaded, lamps())}
    assert edition_of(database, library.owner, books["b"]) \
        == edition_of(database, uploaded.owner, books["c"])
    shown = {}
    for at in (0, 1, 2, 3):
        for someone in (library, uploaded):
            if at:
                someone.mark(books[someone.label], list(range(1, at + 1)))
        drain(work)
        for someone in (library, uploaded):
            assert place_of(database, someone.owner, books[someone.label]) == at
        shown[at] = [seen(someone, books[someone.label], people_ids)
                     for someone in (library, uploaded)]
        assert shown[at][0] == shown[at][1], at
    assert decoded(shown[0][0]) == expected_empty(people_ids)
    # And it is the edition, to the reader's place: Corvus from part 2, Vex from part 3.
    for at, names in ((1, ["Mara"]), (2, ["Mara", "Corvus"]), (3, ["Mara", "Corvus", "Vex"])):
        graph = decoded(shown[at][0])["graph"][1]
        assert [person["canonical_name"] for person in graph["characters"]] == names, at


# ---- 3: the door is the only source ----------------------------------------------------------------

def test_a_readers_own_memory_rows_are_never_read_the_door_is_the_only_source(
        shelf, people, database, tenant_dsn):
    """ADR 0044 section 4: under one edition per file no reader owns memory rows, so the web
    process reads story memory through the door alone. The break it catches: rows under a
    reader's own account for their book (made by the per-reader path before ADR 0044 step 3, or
    written by any bug) reaching that reader's screens, progress or the chat's story memory; the
    reader with own rows and no edition must be told exactly what a reader with neither is."""
    with_rows, without = reader(shelf, people, "a"), reader(shelf, people, "b")
    book = upload(shelf, with_rows, lamps("A Private Lamp Book"))
    other = upload(shelf, without, lamps("Another Private Lamp Book"))
    atoms = atoms_of(shelf, with_rows, book)
    with psycopg.connect(database) as conn:
        [(incarnation,)] = conn.execute("SELECT incarnation FROM books WHERE id=%s",
                                        (book,)).fetchall()
        scope = (with_rows.owner, uuid.UUID(book), incarnation)
        for atom in atoms:
            chapter = uuid.uuid4()
            digest = hashlib.sha256(f"chapter {atom['ordinal']}".encode()).hexdigest()
            conn.execute("INSERT INTO chapters (owner_id,book_id,book_incarnation,id,chapter_key,"
                         "revealed_at,title,content_hash) VALUES (%s,%s,%s,%s,%s,%s,'Own',%s)",
                         (*scope, chapter, atom["key"], atom["ordinal"], digest))
            conn.execute("INSERT INTO ingested_chapters (owner_id,book_id,book_incarnation,"
                         "chapter_id,content_hash,completed_at) VALUES (%s,%s,%s,%s,%s,now())",
                         (*scope, chapter, digest))
            conn.execute("INSERT INTO entities (owner_id,book_id,book_incarnation,"
                         "source_chapter_id,canonical_name,entity_type,revealed_at)"
                         " VALUES (%s,%s,%s,%s,%s,'character',%s)",
                         (*scope, chapter, f"Ownname{atom['ordinal']}", atom["ordinal"]))
    for someone, own_book in ((with_rows, book), (without, other)):
        someone.mark(own_book, [1, 2])
    with psycopg.connect(database) as conn:
        conn.execute("DELETE FROM jobs WHERE owner_id = ANY(%s)",
                     ([with_rows.owner, without.owner],))
    assert place_of(database, with_rows.owner, book) == 2

    answers = seen(with_rows, book, [])
    assert answers == seen(without, other, [])
    assert b"Ownname" not in b"".join(body for _status, _cache, body in answers.values())
    graph = decoded(answers)["graph"][1]
    assert graph == {"as_of_chapter": 0, "characters": [], "relationships": []}
    assert decoded(answers)["ingest"][1]["ingest_progress"] == 0
    assert store_of(with_rows, book, atoms) == (False, 0, [[], [], []])
    _client, _tokens, app = shelf
    catalog = HostedCompanionCatalog(app.state.companion_dsn, OwnerId(with_rows.owner))
    assert catalog.get_state(book)["ingest_progress"] == 0


# ---- 4: "running" and "waiting" only while notes are owed ------------------------------------------

def progress(someone: Reader, book: str) -> dict:
    response = someone.call("GET", f"/api/books/{book}/ingest")
    assert response.status_code == 200, response.text
    return response.json()


def only_job(dsn: str, owner, book, *, tomorrow: bool = False, state: str = "pending") -> None:
    """Exactly one story-memory job of this reader's book: waiting to run now, waiting for the
    next UTC day, or under way."""
    with psycopg.connect(dsn) as conn:
        conn.execute("DELETE FROM jobs WHERE owner_id=%s AND kind='story_memory'", (owner,))
    assert StoryMemoryQueue(tenant_dsn_value["dsn"], OwnerId(owner)).ensure(book,
                                                                             tomorrow=tomorrow)
    if state != "pending":
        with psycopg.connect(dsn) as conn:
            conn.execute("UPDATE jobs SET state=%s WHERE owner_id=%s AND kind='story_memory'",
                         (state, owner))


def test_running_and_waiting_are_told_only_while_the_reader_has_notes_to_come(
        shelf, people, database, worker_dsn, monkeypatch):
    """ADR 0044 section 4, "the honest limit", and the owner's rule. The break it catches:
    progress telling a reader whom the notes have caught up with that a run of theirs is under
    way or waits for tomorrow. The first reader of a file has a run building the chapter after
    theirs (one ahead) and a later reader has none, so that would tell them apart where nothing
    else does; and "waiting" there would make the panels say "I'll catch up on the rest tomorrow"
    to someone who is not behind. While the reader is owed notes, "running" and "waiting" are
    told as before (the timing hint the owner accepted), whoever is reading."""
    _client, _tokens, app = shelf
    extraction = extraction_client()
    Lamps(extraction, monkeypatch)
    work = worker(app, extraction, worker_dsn)
    first, later = reader(shelf, people, "a"), reader(shelf, people, "b")
    book = upload(shelf, first, lamps())
    first.read(book, 1, 1, 2)
    first.mark(book, [1])
    drain(work)
    assert built_for(database, first.owner, book) == [(1, "body"), (2, "body")]
    idle = {"ingest_progress": 1, "status": "idle", "flags": [], "error": None}

    # At part 1, with part 1's notes handed: nothing is owed, whatever their jobs.
    for tomorrow, state in ((False, "pending"), (True, "pending"), (False, "running")):
        only_job(database, first.owner, book, tomorrow=tomorrow, state=state)
        assert progress(first, book) == idle, (tomorrow, state)
    later_book = upload(shelf, later, lamps())
    later.mark(later_book, [1])
    assert progress(later, later_book) == idle
    drain(work)
    assert progress(later, later_book) == idle

    # Past what is built (part 3 is not): owed part 3, told how it goes.
    with psycopg.connect(database) as conn:
        conn.execute("DELETE FROM jobs WHERE owner_id=%s", (first.owner,))
    first.mark(book, [1, 2, 3])
    with psycopg.connect(database) as conn:
        conn.execute("DELETE FROM jobs WHERE owner_id=%s", (first.owner,))
    owed = {"ingest_progress": 2, "flags": [], "error": None}
    assert progress(first, book) == {**owed, "status": "idle"}            # no job: nothing said
    only_job(database, first.owner, book)
    assert progress(first, book) == {**owed, "status": "running"}
    only_job(database, first.owner, book, tomorrow=True)
    assert progress(first, book) == {**owed, "status": "waiting"}
    # The same for a later reader who is owed the same chapter.
    later.mark(later_book, [1, 2, 3])
    only_job(database, later.owner, later_book, tomorrow=True)
    assert progress(later, later_book) == {**owed, "status": "waiting"}
