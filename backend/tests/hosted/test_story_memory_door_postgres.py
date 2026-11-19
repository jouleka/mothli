"""Story memory through the door (ADR 0042 part 5, ADR 0044), against real PostgreSQL.

A book file's story memory (its edition) belongs to the notes account; a reader whose copy is
exactly that file gets it through one database function that takes the book and nothing else and
works the reader's place out in the database. The owner's test: readers at chapters 2, 5 and 10
of one built book; whatever answers the reader at chapter 2 holds nothing of chapters 5 or 10, on
the raw function result (the RLS-enforced tenant role, and a superuser connection with that
reader's owner setting), through ``HostedBookStore``, the four story screens, the chat and the
copy of one's data. Then: no way to choose another place; moving back shrinks it and reading on
grows it; parity with local mode; the chat's spoiler probes; a later name the writer "remembers";
nothing between readers; three unremembered chapters in a row; erasure, removal and grants with
edition rows present; and the place column kept in step by the read map. Stand-in writer, checker
and Jev only: nothing is called and nothing paid.

ADR 0044 step 2 (the door for every book): the door is ``story_memory_door`` and serves any
reader's book whose file matches an edition on both fingerprints, however it came (a free-library
add or an upload); a live book of the reader with no edition gets one row with empty lists, the
same answer as an edition read at place 0. The door tests run for a free-library add and for an
upload of the same file.

ADR 0044 step 6 (the operator's path deleted): every edition here is built as every edition is,
by a reader's own runs, chapter by chapter, one chapter past what that reader has finished: the
builder reader (``BUILDER``), who adds the file from the free library, has read a page of it and
has finished its chapters (all of them, unless a test says how far). Until step 6 the operator's
command built them, as the old library account, to the end. Also here: a freshly migrated
database needs no step for a reader's first page to build chapter 1; an edition the command made
before this step keeps working; and a free-library add and an upload of its file are one book to
the trigger and the target.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import conninfo, sql
from psycopg.rows import dict_row

from app.classics import find_classic
from app.eval.spoiler_gate import score_recap
from app.hosted.auth.repository import InactiveUserError, PostgresAuthRepository
from app.hosted.companion.jobs import StoryMemoryQueue, story_memory_target
from app.hosted.companion.memory import HostedBookStore
from app.hosted.companion.source import HostedBookSource
from app.hosted.companion.store import HostedCompanionCatalog
from app.hosted.purge import PostgresAccountPurge, owner_tables, purge_account
from app.hosted.runtime import HostedRuntimeRegistry
from app.hosted.tenant import api as hosted_api
from app.hosted.tenant import classics as hosted_classics
from app.hosted.tenant.grants import purge_grants
from app.hosted.tenant.models import OwnerId
from app.hosted.tenant.repository import PostgresTenantRepository
from app.ingest.extraction.chapter_text import segment_for_ingest, split_hash_of
from app.main import create_app
from app.memory_views import graph_payload, notes_payload
from tests.api._epub import epub_ncx
from tests.hosted import test_companion_read_map_postgres as read_map_gate
from tests.hosted import test_jobs_postgres as jobs_gate
from tests.hosted.test_account_deletion_postgres import _role
from tests.hosted.test_account_export_postgres import copy_of
from tests.hosted.test_companion_recollections_postgres import HomeModels
from tests.hosted.test_companion_stream_postgres import (  # noqa: F401  (autouse fixture)
    CHAT,
    Reader,
    StandIns,
    restore_caps,
    sentences,
)
from tests.hosted.test_story_memory_jobs_postgres import (
    MEMORY,
    PROBES,
    Lamps,
    built,
    built_for,
    built_in,
    drain,
    fixed_check_drops,
    jobs_of,
    lamps,
    memory_app_for,
    worker,
)
from tests.hosted.test_story_memory_postgres import memory_of
from tests.hosted.test_tenant_postgres import UnusedOIDCClient
from tests.ingest._grounding import canned_grounding_verdict
from tests.parity.test_story_memory_parity import (
    Extractor,
    _person,
    answers,
    extraction_client,
    harbour_book,
    hosted_atoms,
    local_book,
)

pytestmark = pytest.mark.postgres

admin_dsn = read_map_gate.admin_dsn
database = read_map_gate.database
tenant_dsn = read_map_gate.tenant_dsn
worker_dsn = jobs_gate.worker_dsn
line = read_map_gate.line

# The launch setting: story memory on, as the deployment template sets it (ADR 0044 step 6).
LAUNCH = {**CHAT, "hosted_story_memory": True}
PRIDE, ALICE = "1342", "11"          # two catalogue entries; their files here are made up

# ---- the made-up book: eleven parts, a new person in each ---------------------------------------

NAMES = ("Mara", "Corvus", "Vex", "Zephyrine", "Quillon", "Brannoch", "Odalys", "Nerith",
         "Pellam", "Sabriel", "Tamsk")
# Mara rests in part 6 (her first state ends there), is called "the Keeper" from part 7, and
# Corvus greets Vex in part 9.
EXTRA = {6: " Mara rested by the quay.", 7: " Mara, the Keeper, watched the harbour.",
         9: " Corvus greeted Vex at the quay."}
PARTS = len(NAMES)


def tline(part: int, number: int) -> str:
    return f"{NAMES[part - 1]} moved lamp {number} in part {part}."


def long_lamps(title: str = "The Long Lamp Book") -> bytes:
    return epub_ncx([(f"c{n}.xhtml", f"Part {n}", f"Part {n}",
                      " ".join(tline(n, i) for i in range(1, 41)) + EXTRA.get(n, ""))
                     for n in range(1, PARTS + 1)], title=title, author="Ada Writer")


LONG = long_lamps()


def extraction_of(part: int) -> dict:
    name = NAMES[part - 1]
    found = {"chapter_summary": f"{name} moved the lamps.",
             "entities": [_person(name, state="moving lamps")], "relationships": [],
             "events": [{"summary": f"{name} moved lamp 1.", "participants": [name]}],
             "themes": [{"name": "Light", "description": "Lamps are moved."}] if part == 1
             else []}
    if part == 6:
        found["entities"].append(_person("Mara", known=True, state="resting by the quay"))
    if part == 7:
        found["entities"].append(_person("Mara", aliases=["the Keeper"], known=True))
    if part == 9:
        found["entities"] += [_person("Corvus", known=True), _person("Vex", known=True)]
        found["relationships"] = [{"src": "Corvus", "dst": "Vex", "rel_type": "social",
                                   "label": "greets"}]
    return found


class Writer:
    """The stand-in writer and checker for extraction over the long lamp book: the scripted
    extraction of the part the prompt holds and an accepting review, except for the parts in
    ``reject``, which the review rejects every time. Every call is counted as (schema, part)."""

    def __init__(self, client, monkeypatch, *, reject=()):
        self.calls, self.reject = [], set(reject)
        monkeypatch.setattr(client, "complete", self.complete)

    def complete(self, system, user, *, tier, schema):
        part = int(re.search(r" in part (\d+)\.", user)[1])
        self.calls.append((schema.__name__, part))
        usage = {"in": 120, "out": 60}
        if schema.__name__ == "ExtractionVerdict":
            return canned_grounding_verdict(user, references_future=part in self.reject), usage
        assert schema.__name__ == "Extraction", schema.__name__
        return extraction_of(part), usage

    def parts(self) -> list[int]:
        return [part for name, part in self.calls if name == "Extraction"]


# ---- the app, the free library's source, the readers and the builder ----------------------------

PLACES = {"r2a": 2, "r2b": 2, "r2c": 2, "r5a": 5, "r5b": 5, "r5c": 5,
          "r10a": 10, "r10b": 10, "r10c": 10, "r10d": 10}
# The reader whose own runs build the edition (ADR 0044 step 6: there is no other builder).
BUILDER = "builder"


@pytest.fixture()
def people(database: str) -> dict:
    owners = {label: uuid.uuid4() for label in (*PLACES, "u", BUILDER)}
    with psycopg.connect(database) as conn:
        conn.cursor().executemany("INSERT INTO users (id,display_name) VALUES (%s,%s)",
                                  [(owner, f"Reader {label}") for label, owner in owners.items()])
    return owners


class Source:
    """The catalogue's source, in memory: ``files[pg]`` (the long lamp book by default)."""

    def __init__(self, monkeypatch):
        self.files: dict[int, bytes] = {}
        monkeypatch.setattr(hosted_classics, "_transport",
                            lambda: httpx.MockTransport(self.handle))

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.host != "gutenberg.pglaf.org":
            return httpx.Response(404)
        return httpx.Response(200, content=self.files.get(int(request.url.path.split("/")[3]),
                                                           LONG))


@pytest.fixture()
def source(monkeypatch) -> Source:
    return Source(monkeypatch)


def app_for(tenant_dsn, people, tmp_path, **settings):
    tokens, sessions = read_map_gate.signed_in(people)
    app = create_app(read_map_gate.hosted_settings(tenant_dsn, tmp_path / "objects", **settings),
                     auth_repository=sessions, oidc_client=UnusedOIDCClient(),
                     auth_clock=lambda: datetime(2026, 7, 16, 12, tzinfo=UTC))
    return TestClient(app, base_url="https://reader.example"), tokens, app


@pytest.fixture()
def shelf(tenant_dsn: str, database: str, people: dict, tmp_path, source: Source):
    client, tokens, app = app_for(tenant_dsn, people, tmp_path, **LAUNCH)
    with client:
        yield SimpleNamespace(client=client, tokens=tokens, app=app, people=people,
                              source=source, tenant_dsn=tenant_dsn, database=database)


def reader(shelf, label: str) -> Reader:
    return Reader(shelf.client, shelf.tokens, label, shelf.people[label])


def add(someone: Reader, entry: str = PRIDE) -> str:
    added = someone.call("POST", f"/api/classics/{entry}")
    assert added.status_code == 201, added.text
    return added.json()["book_id"]


def place_at(someone: Reader, book: str, at: int, parts: int = PARTS) -> None:
    if at:
        someone.mark(book, list(range(1, at + 1)))
    if at < parts:
        someone.mark(book, list(range(at + 1, parts + 1)), read=False)


def notes_account_of(dsn: str) -> uuid.UUID:
    """The one notes account, which the migrations make (ADR 0044 step 2) and own every edition."""
    with psycopg.connect(dsn) as conn:
        [(account,)] = conn.execute("SELECT account_id FROM notes_account").fetchall()
    return account


def has_read_a_page(dsn: str, owner, book) -> None:
    """``owner`` has read a page of ``book`` (part 1, not skimmed): the read map's row as the
    read-page route writes it, written here because some of these books (the harbour book) repeat
    their text, so no page of them can be named by a quote found once in its chapter."""
    with psycopg.connect(dsn) as conn:
        conn.execute("INSERT INTO read_ranges (owner_id,book_id,book_incarnation,atom,start_offset,"
                     "end_offset,source) SELECT owner_id,id,incarnation,1,0,1,'read' FROM books"
                     " WHERE owner_id=%s AND id=%s", (owner, book))


def started_edition(shelf, worker_dsn, monkeypatch, entry=PRIDE, *, writer=Writer, through=None,
                    **options):
    """The edition of the file the free library serves for ``entry``, as a reader's runs build
    it (the only way since ADR 0044 step 6), its worker not run yet. The builder reader adds the
    file, has read a page of it, and has finished chapters 1 to ``through`` (every chapter by
    default; with 0, none: the test marks them as it goes, ``shelf.builder``), so their runs
    build the edition one chapter a run (``drain(work, n)`` builds the next ``n``) up to
    ``through`` + 1, never past the book's last chapter. Returns the notes account, the writer,
    the extraction client and the worker."""
    extraction = extraction_client()
    models = writer(extraction, monkeypatch, **options)
    builder = reader(shelf, BUILDER)
    book = add(builder, entry)
    shelf.builder = (builder, book)
    has_read_a_page(shelf.database, builder.owner, book)
    last = len(hosted_atoms(shelf.app, builder.owner, book))
    through = last if through is None else through
    if through:
        builder.mark(book, list(range(1, through + 1)))
    work = worker(shelf.app, extraction, worker_dsn)
    return notes_account_of(shelf.database), models, extraction, work


def built_edition(shelf, worker_dsn, monkeypatch, entry=PRIDE, *, writer=Writer, **options):
    """``started_edition``, then the worker until it has nothing left."""
    account, models, extraction, work = started_edition(shelf, worker_dsn, monkeypatch, entry,
                                                        writer=writer, **options)
    drain(work, 60)
    return account, models, extraction, work


# The two ways a reader comes to hold a file (ADR 0044 section 1): the door treats them alike.
HOW = pytest.mark.parametrize("how", ["library", "upload"])


def acquire(shelf, someone: Reader, how: str, entry: str = PRIDE) -> str:
    """``someone``'s copy of the file the free library serves for ``entry``: added from the
    free library, or the very same bytes uploaded."""
    if how == "library":
        return add(someone, entry)
    return read_map_gate.upload(shelf.client, shelf.tokens, someone.label,
                                shelf.source.files.get(int(entry), LONG))


def door(dsn: str, owner, book) -> dict | None:
    """The function's raw answer for ``book``, with ``owner`` as the transaction's owner."""
    with psycopg.connect(dsn, row_factory=dict_row) as conn, conn.transaction():
        if owner is not None:
            conn.execute("SELECT set_config('app.owner_id', %s, true)", (str(owner),))
        return conn.execute("SELECT * FROM story_memory_door(%s)", (book,)).fetchone()


def door_bytes(dsn: str, owner, book) -> str | None:
    """The door's answer as the database writes it out, for a byte-for-byte comparison."""
    with psycopg.connect(dsn) as conn, conn.transaction():
        conn.execute("SELECT set_config('app.owner_id', %s, true)", (str(owner),))
        row = conn.execute("SELECT row_to_json(answer)::text FROM story_memory_door(%s) AS answer",
                           (book,)).fetchone()
    return None if row is None else row[0]


# What the cast answers a reader the door hands nothing (ADR 0044 step 5): every book alike.
EMPTY_GRAPH = {"as_of_chapter": 0, "characters": [], "relationships": []}

HANDED = ("chapters", "completed", "entities", "aliases", "themes", "events", "summaries",
          "edges", "states")


def nothing(row: dict | None) -> bool:
    """The door handed nothing: no row at all, or the one row with every list empty."""
    return row is None or not any(row[name] for name in HANDED)


def empty_row(row: dict | None) -> bool:
    """The door's answer for a live book of the reader with no edition: one row, every list
    empty."""
    return row is not None and nothing(row)


def stamps(row: dict) -> list[int]:
    """Every chapter number in the door's answer: the chapter stamps and every end."""
    found = [item["ordinal"] for name in ("chapters", "completed") for item in row[name]]
    for name in ("entities", "aliases", "themes", "events", "summaries", "edges", "states"):
        for item in row[name]:
            found.append(item["revealed_at"])
            found += [item[key] for key in ("invalid_at", "chapter_ordinal")
                      if item.get(key) is not None]
    return found


def handed_anything(dsn: str, owner, book) -> bool:
    """Whether the door hands this reader any chapter of an edition of their book's file,
    through ``HostedBookStore`` (ADR 0044 step 5: the door is its only source)."""
    with HostedBookStore(dsn, OwnerId(owner)).book(book) as mem:
        return mem.has_rows()


def through(dsn: str, owner, book) -> int | None:
    row = door(dsn, owner, book)
    return None if row is None else max(stamps(row), default=0)


def memory_through(dsn: str, owner, book) -> int:
    with psycopg.connect(dsn) as conn:
        return conn.execute("SELECT memory_through FROM reading_state WHERE owner_id=%s"
                            " AND book_id=%s", (owner, book)).fetchone()[0]


def everything_of(dsn: str, owner) -> dict:
    """Every row of one owner in every table that has an owner, read past row security."""
    with psycopg.connect(dsn) as conn:
        return {table: sorted(row[0] for row in conn.execute(
            sql.SQL("SELECT t::text FROM {} AS t WHERE owner_id=%s").format(
                sql.Identifier(table)), (owner,)).fetchall())
            for table in owner_tables(conn) if table not in ("audit_events",
                                                             "owner_request_windows")}


def later_words(at: int) -> list[str]:
    """What a reader at ``at`` must not be handed: the people first met later, and Mara's other
    name, first given in part 7."""
    return [*NAMES[at:], *(["Keeper"] if at < 7 else [])]


def get(someone: Reader, path: str):
    someone.become()
    return someone.client.get(path)


# ---- 1: the owner's test -------------------------------------------------------------------------

@HOW
def test_readers_at_2_5_and_10_are_each_handed_only_what_is_up_to_their_place(
        shelf, tenant_dsn, database, worker_dsn, monkeypatch, how):
    """The owner's test, for readers who added the file from the free library and for readers
    who uploaded the very same file. The break it catches: a row of a later chapter, or an end
    ("this state ends at chapter 6") past the reader's place, leaving the database for that
    reader's request, on the row-security role or past it, or reaching their story memory, the
    four screens, the chat's prompts and checks, or their copy of their data."""
    account, models, _extraction, work = built_edition(shelf, worker_dsn, monkeypatch)
    assert built(database, account) == [(n, "body") for n in range(1, PARTS + 1)]
    with psycopg.connect(database, row_factory=dict_row) as conn:
        ids = {row["canonical_name"]: str(row["id"]) for row in conn.execute(
            "SELECT id,canonical_name FROM entities WHERE owner_id=%s", (account,)).fetchall()}
        mara_first = str(conn.execute(
            "SELECT id FROM entity_state WHERE owner_id=%s AND status->>'note'='moving lamps'"
            " AND entity_id=%s", (account, ids["Mara"])).fetchone()["id"])
    books = {}
    for label, at in PLACES.items():
        someone = reader(shelf, label)
        books[label] = acquire(shelf, someone, how)
        someone.mark(books[label], list(range(1, at + 1)))
    # ADR 0044 step 4: "I've read this" asks each reader's story-memory job, for any book; with
    # no page read and the edition built, it builds and spends nothing.
    calls = list(models.calls)
    assert drain(work, 60) == len(PLACES)
    assert models.calls == calls

    platform = shelf.app.state.platform_client
    for label, at in PLACES.items():
        someone, book = reader(shelf, label), books[label]
        hidden = later_words(at)
        # The raw function result, on the row-security role and past it.
        raw = door(tenant_dsn, someone.owner, book)
        assert raw is not None and max(stamps(raw)) == at, label
        assert door(database, someone.owner, book) == raw, label
        assert sorted(item["ordinal"] for item in raw["completed"]) == list(range(1, at + 1))
        assert {item["name"] for item in raw["entities"]} == set(NAMES[:at])
        [first] = [item for item in raw["states"] if item["id"] == mara_first]
        assert first["invalid_at"] == (6 if at >= 6 else None), label
        text = json.dumps(raw, default=str)
        assert not [word for word in hidden if word in text], label

        # The reader's story memory, progress and counter.
        with HostedBookStore(tenant_dsn, OwnerId(someone.owner)).book(book) as mem:
            assert mem.has_rows()
            assert {row["canonical_name"] for row in mem._audit_all("entities")} \
                == set(NAMES[:at])
            assert mem.completion_frontier(hosted_atoms(shelf.app, someone.owner, book)) == at
        state = HostedCompanionCatalog(tenant_dsn, OwnerId(someone.owner)).get_state(book)
        assert (state["bookmark"], state["ingest_progress"]) == (at, at), label

        # The four screens.
        graph = get(someone, f"/api/books/{book}/graph?bookmark=99")
        notes = get(someone, f"/api/books/{book}/notes?bookmark=99")
        progress = get(someone, f"/api/books/{book}/ingest")
        assert graph.json()["as_of_chapter"] == at == notes.json()["as_of_chapter"]
        assert {row["canonical_name"] for row in graph.json()["characters"]} == set(NAMES[:at])
        assert [row["revealed_at"] for row in notes.json()["chapters"]] == list(range(1, at + 1))
        assert progress.json() == {"ingest_progress": at, "status": "idle", "flags": [],
                                   "error": None}
        assert get(someone, f"/api/books/{book}/character/{ids[NAMES[at - 1]]}").status_code \
            == 200
        for name in NAMES[at:]:
            assert get(someone, f"/api/books/{book}/character/{ids[name]}").status_code == 404
        screens = graph.text + notes.text + progress.text
        assert not [word for word in hidden if word in screens], label

        # The chat: what its writer and checks were given, and what it showed.
        models = StandIns(platform, monkeypatch, reply=f"{tline(1, 2)} {tline(at, 3)}")
        shown = sentences(someone.ask(book, "What has happened so far?"))
        assert tline(at, 3) in shown, label
        assert not [word for word in hidden if word in models.everything()], label

        # The copy of their data.
        files = copy_of(someone.call("GET", "/api/account/export"))
        [entry] = [json.loads(body) for name, body in files.items() if name.startswith("books/")]
        story = entry["what_mothli_keeps_about_the_story"]
        assert story["as_of_chapter"] == at
        assert [person["name"] for person in story["people_you_have_met"]] == list(NAMES[:at])
        assert [chapter["chapter"] for chapter in story["the_story_so_far"]] == list(
            range(1, at + 1))
        everything = "\n".join(files.values())
        assert not [word for word in hidden if word in everything], label


# ---- 2: no way to choose another place ----------------------------------------------------------

@HOW
def test_no_argument_device_tab_or_direct_write_moves_the_place_and_the_door_refuses_the_rest(
        shelf, tenant_dsn, database, worker_dsn, admin_dsn, monkeypatch, how):
    """The break it catches: a place taken from a caller (an argument, a requested chapter), from
    the counter a device's position report raises, from a stale tab, or from a column written
    past every chapter the reader has a read page in; or the door answering a caller with no
    owner setting, another reader's book, a removed book, a book whose file differs, or the
    notes account itself; or the very same file come the other way (an upload for a library add,
    a library add for an upload) handed anything but what the first is handed at the same place.
    Run for a reader who added the file from the free library and one who uploaded it."""
    account, _models, _extraction, work = built_edition(shelf, worker_dsn, monkeypatch)
    a, b = reader(shelf, "r2a"), reader(shelf, "r5a")
    book, other = acquire(shelf, a, how), acquire(shelf, b, how)
    a.mark(book, [1, 2])
    b.mark(other, [1, 2, 3, 4, 5])
    assert through(tenant_dsn, a.owner, book) == 2

    with psycopg.connect(database) as conn:
        assert conn.execute(
            "SELECT pg_get_function_identity_arguments('story_memory_door'::regproc)"
        ).fetchone()[0] == "book uuid"
    assert get(a, f"/api/books/{book}/graph?bookmark=11").json()["as_of_chapter"] == 2

    # A device's position report, and a server with the read map off moving the old counter.
    position = get(a, f"/api/books/{book}/position").json()
    pushed = a.call("PUT", f"/api/books/{book}/position", {
        "cfi": "epubcfi(/6/20)", "offset": 9000, "completed_chapter": 10, "position_epoch": 0,
        "base_version": position["position_version"], "client_id": str(uuid.uuid4()),
        "client_sequence": 1})
    assert pushed.status_code == 200, pushed.text
    with psycopg.connect(database) as conn:
        conn.execute("UPDATE reading_state SET bookmark=10 WHERE owner_id=%s AND book_id=%s",
                     (a.owner, book))
    catalog = HostedCompanionCatalog(tenant_dsn, OwnerId(a.owner))
    assert catalog.get_state(book)["bookmark"] == 2
    assert memory_through(database, a.owner, book) == 2 == through(tenant_dsn, a.owner, book)

    # A stale tab sending a page far ahead: it counts nothing and moves nothing.
    stale = a.call("PUT", f"/api/books/{book}/read-frontier", {
        "atom": 10, "quote": tline(10, 3), "prefix": tline(10, 3), "cfi": "epubcfi(/6/20)",
        "position_epoch": 0})
    assert stale.status_code in (200, 422), stale.text
    assert through(tenant_dsn, a.owner, book) == 2
    far = a.call("PUT", f"/api/books/{book}/read-page", {
        "start": {"atom": 10, "quote": tline(10, 1), "prefix": "", "cfi": "epubcfi(/6/20)",
                  "position_epoch": 0},
        "end": {"atom": 10, "quote": tline(10, 2), "prefix": tline(10, 2),
                "cfi": "epubcfi(/6/20)", "position_epoch": 0}, "skimmed": True})
    assert far.status_code == 200, far.text
    assert through(tenant_dsn, a.owner, book) == 2

    # The column written past every chapter with a read page in it: the backstop holds.
    with psycopg.connect(database) as conn:
        conn.execute("UPDATE reading_state SET memory_through=10 WHERE owner_id=%s"
                     " AND book_id=%s", (a.owner, book))
    assert through(tenant_dsn, a.owner, book) == 2 == through(database, a.owner, book)
    with HostedBookStore(tenant_dsn, OwnerId(a.owner)).book(book) as mem:
        assert {row["canonical_name"] for row in mem._audit_all("entities")} == {"Mara", "Corvus"}

    # Who and what the door refuses: nothing, the same as for a book that does not exist.
    assert door(tenant_dsn, None, book) is None
    assert door(database, None, book) is None
    assert door(tenant_dsn, b.owner, book) is None and door(database, b.owner, book) is None
    assert door(tenant_dsn, a.owner, uuid.uuid4()) is None
    with psycopg.connect(database) as conn:
        edition_book = conn.execute("SELECT id FROM books WHERE owner_id=%s",
                                    (account,)).fetchone()[0]
    assert door(tenant_dsn, account, edition_book) is None
    assert door(tenant_dsn, account, book) is None and door(database, account, book) is None
    # The very same file, come the other way: handed exactly what the first is at that place,
    # and before reading, nothing.
    twin_reader = reader(shelf, "u")
    twin = acquire(shelf, twin_reader, "upload" if how == "library" else "library")
    assert empty_row(door(tenant_dsn, twin_reader.owner, twin))
    twin_reader.mark(twin, [1, 2])
    assert stamps(door(tenant_dsn, twin_reader.owner, twin)) == stamps(door(tenant_dsn, a.owner,
                                                                            book))
    assert handed_anything(tenant_dsn, twin_reader.owner, twin)
    assert get(twin_reader, f"/api/books/{twin}/graph").json()["as_of_chapter"] == 2
    # Another file of the same title: handed nothing, and nothing of its own to show.
    shelf.source.files[int(PRIDE)] = long_lamps("The Long Lamp Book, Revised")
    differs = reader(shelf, "r10a")
    changed = acquire(shelf, differs, how)
    differs.mark(changed, [1, 2, 3])
    assert empty_row(door(tenant_dsn, differs.owner, changed))
    assert empty_row(door(database, differs.owner, changed))
    assert not handed_anything(tenant_dsn, differs.owner, changed)
    # ADR 0044 step 4: the marks asked for its job; run (no page read), it builds nothing.
    drain(work, 60)
    assert empty_row(door(tenant_dsn, differs.owner, changed))
    assert get(differs, f"/api/books/{changed}/graph").json() == EMPTY_GRAPH
    assert a.call("DELETE", f"/api/books/{book}").status_code == 204
    assert door(tenant_dsn, a.owner, book) is None and door(database, a.owner, book) is None

    # Executable by the tenant role alone: not by everyone, the worker or the purge. (Until ADR
    # 0044 step 6 the fourth role checked was the library command's, which is gone.)
    with psycopg.connect(database) as conn:
        tables = purge_grants(owner_tables(conn))
    with _role(admin_dsn, database, bypass=True, tables=tables) as purge_dsn, \
            psycopg.connect(database) as conn:
        roles = {name: conn.execute(
            "SELECT has_function_privilege(%s,'story_memory_door(uuid)','EXECUTE')",
            (name,)).fetchone()[0] for name in (
            conninfo.conninfo_to_dict(tenant_dsn)["user"],
            conninfo.conninfo_to_dict(worker_dsn)["user"],
            conninfo.conninfo_to_dict(purge_dsn)["user"], "public")}
    assert list(roles.values()) == [True, False, False, False]


# ---- 3: moving back shrinks, moving on grows ------------------------------------------------------

@HOW
def test_i_havent_read_this_shrinks_the_next_answer_and_reading_on_grows_it(
        shelf, tenant_dsn, database, worker_dsn, monkeypatch, how):
    """The break it catches: a per-reader copy kept in step (or not) beside the edition, so
    "I haven't read this" leaves later people in the reader's memory, or reading on does not
    bring them. For a free-library add and an upload alike."""
    account, *_ = built_edition(shelf, worker_dsn, monkeypatch)
    a = reader(shelf, "r5a")
    book = acquire(shelf, a, how)
    a.mark(book, [1, 2, 3, 4, 5])
    library = memory_of(database, account)
    assert through(tenant_dsn, a.owner, book) == 5
    a.mark(book, [3, 4, 5], read=False)
    assert through(tenant_dsn, a.owner, book) == 2
    with HostedBookStore(tenant_dsn, OwnerId(a.owner)).book(book) as mem:
        assert {row["canonical_name"] for row in mem._audit_all("entities")} == {"Mara", "Corvus"}
    assert {row["canonical_name"] for row in get(a, f"/api/books/{book}/graph").json()[
        "characters"]} == {"Mara", "Corvus"}
    a.mark(book, [3, 4, 5, 6, 7])
    assert through(tenant_dsn, a.owner, book) == 7
    assert "the Keeper" in get(a, f"/api/books/{book}/graph").text
    # No copy was made for the reader, and the edition is as it was.
    assert all(rows == [] for rows in memory_of(database, a.owner).values())
    assert memory_of(database, account) == library


# ---- 4: parity with local mode --------------------------------------------------------------------

def screens(mem, bookmark: int) -> dict:
    """The cast and the story so far as the screens give them, with each mode's ids as names."""
    graph, notes = graph_payload(mem, bookmark), notes_payload(mem, bookmark)
    names = {row["entity_id"]: row["canonical_name"] for row in graph["characters"]}
    return {"cast": [(row["canonical_name"], row["revealed_at"], row["aliases"])
                     for row in graph["characters"]],
            "ties": sorted((names[row["src_entity"]], names[row["dst_entity"]], row["rel_type"],
                            row["label"], row["revealed_at"]) for row in graph["relationships"]),
            "story": [(row["chapter_key"].split(":", 1)[1], row["revealed_at"], row["title"],
                       row["summary"], [person["name"] for person in row["new_characters"]],
                       row["events"]) for row in notes["chapters"]],
            "chips": [person["name"] for person in notes["cast"]]}


# What a local memory built only to the reader's place cannot know: a name of a later chapter. The
# later-name check asks the database, which holds the whole edition (ADR 0044 step 10), so hosted
# mode may find one where such a local memory finds none; never the other way.
LATER_NAME_PARTS = ("names_a_later_name", "names_a_later_theme", "dropped_by_the_chat",
                    "ungrounded_name_tokens", "grounded_rate")


def never_looser_on_later_names(got: dict, expected: dict, at: int) -> int:
    """Hosted finds every later name local mode finds, and drops every sentence it drops; then
    ``got``'s later-name parts are set to local mode's, so the rest compares whole. Returns how
    many sentences hosted mode drops that this local memory cannot."""
    stricter = 0
    for bookmark, answer in expected.items():
        if not isinstance(bookmark, int):
            continue
        for key, local in answer["recap"].items():
            hosted = got[bookmark]["recap"][key]
            for part in ("names_a_later_name", "names_a_later_theme", "dropped_by_the_chat"):
                assert hosted[part] >= local[part], (at, bookmark, key, part)
            stricter += hosted["dropped_by_the_chat"] > local["dropped_by_the_chat"]
            got[bookmark]["recap"][key] = {**hosted, **{
                part: local[part] for part in LATER_NAME_PARTS}}
    return stricter


def test_a_reader_of_the_library_edition_gets_what_local_mode_builds_to_their_place(
        shelf, tenant_dsn, worker_dsn, tmp_path, monkeypatch):
    """The break it catches: the door answering a check or a screen differently from a local
    memory built only to the reader's place: a row of a later chapter, an end past the place,
    a chapter key that is the edition's and not the reader's (the frontier then stays 0), a lost
    alias or state, or the people of one chapter in another order."""
    shelf.source.files[int(ALICE)] = harbour_book()
    built_edition(shelf, worker_dsn, monkeypatch, ALICE, writer=Extractor)
    someone = reader(shelf, "r2a")
    book = add(someone, ALICE)
    atoms = hosted_atoms(shelf.app, someone.owner, book)
    local_client = extraction_client()
    Extractor(local_client, monkeypatch)
    with local_book(tmp_path, harbour_book()) as (extract, memory, local_atoms, chapters):
        assert [atom["ordinal"] for atom in atoms] == [1, 2, 3, 4]
        stricter = 0
        for at in (0, 1, 2, 3, 4):
            if at:
                extract(local_client, at)
            place_at(someone, book, at, parts=4)
            with memory() as mem:
                expected = answers(mem, local_atoms, chapters)
                expected_screens = {bm: screens(mem, bm) for bm in range(0, 6)}
            with HostedBookStore(tenant_dsn, OwnerId(someone.owner)).book(book) as mem:
                # Served by the door once it hands anything; at place 0 it hands nothing, the
                # same as for a book with no edition (ADR 0044 section 4).
                assert mem.has_rows() == (at > 0)
                got = answers(mem, atoms, chapters)
                if at < 4:
                    stricter += never_looser_on_later_names(got, expected, at)
                assert got == expected, at
                assert {bm: screens(mem, bm) for bm in range(0, 6)} == expected_screens, at
            assert expected["frontier"] == at
    # A floor under the comparison: the book exercises what it claims to.
    assert stricter > 0          # a later name the reader's own local memory could not know
    assert expected[3]["names"][2] == [["Mara", "the Keeper"]]
    assert ("Corvus", "Vex", "rivalry", "distrusts", 3) in expected_screens[3]["ties"]


# ---- 5: the chat's spoiler probes through the door --------------------------------------------------

# What the stand-in checker doubts at each place in the stream tests' "empty memory" probes: a name
# its passages (the read text) do not hold.
UNREAD = {1: ("Corvus", "Vex", "Lamplighter"), 2: ("Vex", "Lamplighter"), 3: ("Lamplighter",)}


@HOW
@pytest.mark.parametrize("hole", [False, True])
def test_the_chats_spoiler_probes_through_the_door(
        shelf, tenant_dsn, database, worker_dsn, tmp_path, monkeypatch, hole, how):
    """The chat's spoiler probes (a person first met later, one first met in an unremembered
    chapter, a late alias, foreshadowing) for a reader of the library edition at places before,
    at and after each person's first chapter, with and without an unremembered chapter. The break
    it catches: the door's fixed checks dropping less than a memory built only to the reader's
    place (local mode's, the usual per-reader case, ADR 0042 section 3), or the hosted chat
    showing a probe its checks drop. It records plainly what version 1 means: with a checker that
    doubts every name its passages do not hold (the stream tests' probes with no memory) every
    probe goes; with a checker that clears everything, a person or name first met after the place
    is removed too, because the fixed check asks the database about later names (ADR 0044 step
    10; ``test_a_later_name_the_writer_remembers_is_removed_in_the_database_never_handed_out``).
    Run for a free-library add and an upload of the same file, with a second reader of the file
    at its end, so the one probed is behind them."""
    shelf.source.files[int(ALICE)] = lamps()
    account, *_ = built_edition(shelf, worker_dsn, monkeypatch, ALICE, writer=Lamps,
                                reject={2} if hole else ())
    assert built(database, account) == [(1, "body"), (2, "unremembered" if hole else "body"),
                                        (3, "body")]
    ahead = reader(shelf, "r10a")
    ahead.mark(acquire(shelf, ahead, how, ALICE), [1, 2, 3])
    someone = reader(shelf, "r2a")
    book = acquire(shelf, someone, how, ALICE)
    atoms = hosted_atoms(shelf.app, someone.owner, book)
    platform = shelf.app.state.platform_client
    local_client = extraction_client()
    Lamps(local_client, monkeypatch, reject={2} if hole else ())
    door_drops = {}
    with local_book(tmp_path, lamps()) as (extract, memory, local_atoms, chapters):
        for at in (1, 2, 3):
            if at == 1 or not hole:          # local mode stops at a rejected chapter
                extract(local_client, at)
            place_at(someone, book, at, parts=3)
            read_text = " ".join(ch["text"] for ch in chapters if ch["ordinal"] <= at)
            with memory() as mem:
                local = fixed_check_drops(mem, min(at, mem.completion_frontier(local_atoms)),
                                          read_text)
            with HostedBookStore(tenant_dsn, OwnerId(someone.owner)).book(book) as mem:
                assert mem.completion_frontier(atoms) == at
                door_drops[at] = fixed_check_drops(mem, at, read_text)
            assert door_drops[at] >= local, at
            for probe, sentence in PROBES.items():
                unread = [name for name in UNREAD[at] if name in sentence]
                StandIns(platform, monkeypatch, reply=f"{line(1, 2)} {sentence}",
                         unsupported=UNREAD[at])
                shown = sentences(someone.ask(book))
                assert line(1, 2) in shown, (at, probe)
                assert (sentence in shown) == (not unread and probe not in door_drops[at]), (
                    at, probe)
                StandIns(platform, monkeypatch, reply=f"{line(1, 2)} {sentence}")
                shown = sentences(someone.ask(book))
                assert (sentence not in shown) == (probe in door_drops[at]), (at, probe)
    # Every name not yet met at the place is dropped by the fixed check (ADR 0044 step 10), as a
    # local memory holding the whole book would; a local memory built only to the place drops
    # only foreshadowing (``local`` above), so the door is stricter, never looser.
    assert door_drops == {1: set(PROBES), 2: {"later", "late alias", "foreshadowing"},
                          3: {"foreshadowing"}}


# ---- 6: a later name the writer "remembers" ------------------------------------------------------

REMEMBERED = "Zephyrine lit the harbour lamps."   # first met in part 4


def test_a_later_name_the_writer_remembers_is_removed_in_the_database_never_handed_out(
        shelf, tenant_dsn, worker_dsn, monkeypatch):
    """ADR 0042 section 3's probe, with stand-in models (real models are for
    ``scripts/library_later_name_probe.py``). A reader at chapter 2 of an edition built to the
    end; the writer names Zephyrine, first met in part 4, as a model remembering the novel would.
    What it records, plainly:

    - Through the door nothing stamped after the place is handed out: the reader's snapshot holds
      no Zephyrine, and neither the writer nor the checker is told the name.
    - The fixed name check asks the database (ADR 0044 step 10), which holds the edition's later
      rows, and is answered with the sentence's own word: so the sentence is removed even with a
      checker that clears everything, as it was when the edition's rows were handed out unclamped
      (the design ADR 0042 section 3 rejected), without handing them out.
    Until step 10 the check could not know the name and the sentence was left to Jev and the
    second look. The break it catches: a later name let through by the fixed check, or the
    edition's later rows (or the name) reaching the reader's request."""
    built_edition(shelf, worker_dsn, monkeypatch)
    someone = reader(shelf, "r2a")
    book = add(someone)
    someone.mark(book, [1, 2])
    with HostedBookStore(tenant_dsn, OwnerId(someone.owner)).book(book) as mem:
        handed = json.dumps([mem._audit_all(table) for table in ("entities", "aliases", "themes")],
                            default=str)
        assert "Mara" in handed and "Zephyrine" not in handed
        assert score_recap(mem, 2, REMEMBERED)["future_entity_leaks"] == ["zephyrine"]
    # A reader who has read to the end has met her: no later name for them.
    finished = reader(shelf, "r10a")
    finished_book = add(finished)
    finished.mark(finished_book, list(range(1, PARTS + 1)))
    with HostedBookStore(tenant_dsn, OwnerId(finished.owner)).book(finished_book) as everything:
        assert score_recap(everything, 2, REMEMBERED)["future_entity_leaks"] == ["zephyrine"]
        assert score_recap(everything, PARTS, REMEMBERED)["future_entity_leaks"] == []

    platform = shelf.app.state.platform_client
    cleared = StandIns(platform, monkeypatch, reply=f"{tline(1, 2)} {REMEMBERED}")
    assert sentences(someone.ask(book)) == [tline(1, 2)]
    assert cleared.checked                  # the checker cleared it; the gate did not
    assert "Zephyrine" not in "\n".join(cleared.prompts)     # the writer was never told the name
    StandIns(platform, monkeypatch, reply=f"{tline(1, 2)} {REMEMBERED}",
             unsupported=("Zephyrine",))
    assert sentences(someone.ask(book)) == [tline(1, 2)]


def _edition_book(dsn: str, account) -> uuid.UUID:
    with psycopg.connect(dsn) as conn, conn.transaction():
        conn.execute("SELECT set_config('app.owner_id', %s, true)", (str(account),))
        return conn.execute("SELECT id FROM books WHERE owner_id=%s AND deleted_at IS NULL",
                            (account,)).fetchone()[0]


# ---- 7: nothing between readers ------------------------------------------------------------------

def test_the_edition_tells_no_reader_anything_of_another_and_its_account_is_nobodys(
        shelf, tenant_dsn, database, worker_dsn, monkeypatch):
    """The break it catches: story memory that appears for a title only once someone has read
    it, or differs between readers before anyone reads; a reader's request that changes the
    edition's rows or makes a job of theirs; one reader's place, chat or memory reaching another
    reader of the same edition; or the notes account signed into or listed to a reader."""
    account, models, _extraction, work = built_edition(shelf, worker_dsn, monkeypatch)
    a, b = reader(shelf, "r5a"), reader(shelf, "r2a")
    book_a, book_b = add(a), add(b)
    before = [get(someone, f"/api/books/{book}/graph") for someone, book in ((a, book_a),
                                                                              (b, book_b))]
    # A title nobody built, the same for everyone, too. Its file must differ: the door matches
    # files, not catalogue entries.
    shelf.source.files[int(ALICE)] = long_lamps("Another Lamp Book")
    unbuilt = add(a, ALICE)
    # Before reading, a built title answers as a title nobody built (ADR 0044 section 4): the
    # door hands both the same row, byte for byte, and the screen says the same of both.
    assert door_bytes(tenant_dsn, a.owner, book_a) == door_bytes(tenant_dsn, b.owner, book_b) \
        == door_bytes(tenant_dsn, a.owner, unbuilt)
    unbuilt_graph = get(a, f"/api/books/{unbuilt}/graph")
    assert [(response.status_code, response.content) for response in before] \
        == [(unbuilt_graph.status_code, unbuilt_graph.content)] * 2
    assert unbuilt_graph.json() == {"as_of_chapter": 0, "characters": [], "relationships": []}

    edition = everything_of(database, account)
    calls = list(models.calls)
    a.mark(book_a, [1, 2, 3, 4, 5])
    b.mark(book_b, [1, 2])
    platform = shelf.app.state.platform_client
    StandIns(platform, monkeypatch, reply=f"{tline(5, 1)} Reader A's own line.")
    assert sentences(a.ask(book_a)) == [tline(5, 1), "Reader A's own line."]
    for path in ("graph", "notes", "ingest", "conversation", "read-map"):
        assert get(a, f"/api/books/{book_a}/{path}").status_code == 200, path
    # No reader corrects notes, for any book (ADR 0044 step 7): the correction route and the
    # reader's own memory routes are gone, and answer as a route that never existed.
    refused = a.call("POST", f"/api/books/{book_a}/memory-corrections", {
        "source_entity_id": str(uuid.uuid4()), "canonical_name": "Someone Else",
        "reason": "a typo", "bookmark": 5})
    assert (refused.status_code, refused.json()) == (404, {"detail": "Not Found"})
    for own in ("memory", "memory-corrections"):
        shown = get(a, f"/api/books/{book_a}/{own}")
        assert (shown.status_code, shown.json()) == (404, {"detail": "Not Found"}), own
    copy_of(a.call("GET", "/api/account/export"))
    assert everything_of(database, account) == edition and models.calls == calls
    # ADR 0044 step 4: "I've read this" asks for each reader's job, for any book; with no page
    # read it builds, writes and spends nothing.
    assert [row["state"] for row in jobs_of(database, a.owner)] == ["pending"] \
        == [row["state"] for row in jobs_of(database, b.owner)]
    assert drain(work, 60) == 2
    assert everything_of(database, account) == edition and models.calls == calls

    # Reader B sees nothing of A: not A's place, chat or what A was handed.
    assert through(tenant_dsn, b.owner, book_b) == 2
    assert get(b, f"/api/books/{book_b}/graph").json()["as_of_chapter"] == 2
    assert get(b, f"/api/books/{book_b}/conversation").json()["turns"] == []
    b_copy = "\n".join(copy_of(b.call("GET", "/api/account/export")).values())
    assert "Reader A's own line." not in b_copy and "Zephyrine" not in b_copy
    assert book_a not in b_copy

    # The notes account: never signed into, listed to no reader.
    auth = PostgresAuthRepository(database)
    now = datetime.now(UTC)
    with pytest.raises(InactiveUserError):
        asyncio.run(auth.create_session(owner_id=account, session_digest=b"s" * 32,
                                        csrf_digest=b"c" * 32, issuer="https://idp.example",
                                        old_session_digest=None, now=now,
                                        expires_at=now + timedelta(hours=1)))
    with psycopg.connect(database) as conn:
        assert conn.execute("SELECT count(*) FROM external_identities WHERE owner_id=%s",
                            (account,)).fetchone()[0] == 0
        conn.execute("""INSERT INTO sessions (owner_id,id,session_digest,csrf_digest,oidc_issuer,
                                              created_at,last_seen_at,expires_at)
                        VALUES (%s,%s,%s,%s,'https://idp.example',now(),now(),
                                now()+interval '1 hour')""",
                     (account, uuid.uuid4(), b"p" * 32, b"q" * 32))
    assert asyncio.run(auth.authenticate_session(session_digest=b"p" * 32, now=now,
                                                 idle_ttl=timedelta(hours=1))) is None
    edition_book = str(_edition_book(tenant_dsn, account))
    for someone in (a, b):
        listed = get(someone, "/api/books").text + get(someone, f"/api/classics/{PRIDE}").text
        assert edition_book not in listed and str(account) not in listed


# ---- 8: three unremembered chapters in a row ---------------------------------------------------

def test_three_unremembered_chapters_in_a_row_stop_an_edition_for_every_reader_of_its_file(
        shelf, tenant_dsn, database, worker_dsn, people, tmp_path, monkeypatch):
    """The break it catches: a model failing every chapter, each chapter paid for twice and kept
    unremembered, chapter after chapter, quietly: for an edition of a free-library file and of an
    uploaded one alike; or a stopped book started again by the next run, or by a run asked for by
    hand. (Until ADR 0044 step 6 the first edition was the operator's command's, and its report
    and a second ``build`` were checked too; both are gone with the command.)"""
    account, models, extraction, work = built_edition(shelf, worker_dsn, monkeypatch,
                                                      reject={4, 5, 6})
    builder, built_book = shelf.builder
    assert built(database, account) == [*[(n, "body") for n in (1, 2, 3)],
                                        *[(n, "unremembered") for n in (4, 5, 6)]]
    assert models.parts() == [1, 2, 3, *[4] * 4, *[5] * 4, *[6] * 4]
    stopped = [row for row in jobs_of(database, builder.owner) if row["state"] == "failed"
               and row["sanitized_error"]["code"] == "too_many_unremembered"]
    assert [row["sanitized_error"] for row in stopped] == [
        {"code": "too_many_unremembered", "retryable": False}]
    assert not [row for row in jobs_of(database, builder.owner) if row["state"] == "pending"]
    with psycopg.connect(database) as conn:
        assert conn.execute("SELECT count(*) FROM audit_events WHERE owner_id=%s AND"
                            " action='job.fail' AND metadata->>'reason_code'="
                            "'too_many_unremembered'", (builder.owner,)).fetchone()[0] == 1
    calls = list(models.calls)
    assert drain(work, 60) == 0
    # Asked again by hand: the run stops before anything is spent.
    assert StoryMemoryQueue(shelf.app.state.companion_dsn, OwnerId(builder.owner)).ensure(
        str(built_book))
    assert drain(work, 60) == 1 and models.calls == calls
    assert built(database, account)[-1] == (6, "unremembered")

    # A reader's book, with readers' story memory on: the same rule, for the edition its runs
    # build (ADR 0044 step 3). Another file than the first edition's, so its own edition.
    client, tokens, app = memory_app_for(shelf.tenant_dsn, people, tmp_path / "own", **MEMORY)
    with client:
        own = extraction_client()
        writer = Writer(own, monkeypatch, reject={2, 3, 4})
        someone = Reader(client, tokens, "u", people["u"])
        book = read_map_gate.upload(client, tokens, "u", another_file_of_the_same_title())
        page = someone.call("PUT", f"/api/books/{book}/read-page", tpage(1, 1, 2))
        assert page.status_code == 200, page.text           # a page read (ADR 0044 step 4)
        someone.mark(book, list(range(1, 9)))
        drain(worker(app, own, worker_dsn), 60)
        assert built_for(database, someone.owner, book) == [
            (1, "body"), *[(n, "unremembered") for n in (2, 3, 4)]]
        assert writer.parts() == [1, *[2] * 4, *[3] * 4, *[4] * 4]
        assert {row["sanitized_error"]["code"] for row in jobs_of(database, someone.owner)
                if row["state"] == "failed"} >= {"too_many_unremembered"}
        calls = list(writer.calls)
        someone.mark(book, [9])
        drain(worker(app, own, worker_dsn), 60)
        assert writer.calls == calls and len(built_for(database, someone.owner, book)) == 4
        assert built(database, someone.owner) == []          # the reader owns none of it


# ---- 9: erasure, removal and the purge with edition rows present ---------------------------------

def test_removing_a_book_and_erasing_a_reader_leave_the_edition_and_take_all_of_theirs(
        shelf, tenant_dsn, database, worker_dsn, admin_dsn, monkeypatch):
    """The break it catches: a reader's removal or erasure reaching the edition's rows (every
    other reader of the title would lose its memory), leaving anything of the reader's behind,
    or the purge erasing the notes account."""
    account, *_ = built_edition(shelf, worker_dsn, monkeypatch)
    edition = everything_of(database, account)
    a, b = reader(shelf, "r5a"), reader(shelf, "r2a")
    book_a, book_b = add(a), add(b)
    a.mark(book_a, [1, 2, 3])
    b.mark(book_b, [1, 2])
    assert a.call("DELETE", f"/api/books/{book_a}").status_code == 204
    assert everything_of(database, account) == edition
    with psycopg.connect(database) as conn:
        for table in ("reading_state", "read_ranges", "read_position", "source_objects"):
            assert conn.execute(f"SELECT count(*) FROM {table} WHERE owner_id=%s AND book_id=%s",
                                (a.owner, book_a)).fetchone()[0] == 0, table
    assert through(tenant_dsn, b.owner, book_b) == 2

    with psycopg.connect(database) as conn:
        tables = purge_grants(owner_tables(conn))
        conn.execute("UPDATE users SET status='deleting',erase_after=now()-interval '1 day'"
                     " WHERE id=%s", (b.owner,))
    with _role(admin_dsn, database, bypass=True, tables=tables) as purge_dsn:
        purge = PostgresAccountPurge(purge_dsn)
        assert account not in purge.due()
        assert purge_account(purge, shelf.app.state.object_storage, b.owner)
        assert not purge_account(purge, shelf.app.state.object_storage, account)
    assert everything_of(database, account) == edition
    assert all(rows == [] for rows in everything_of(database, b.owner).values())


# ---- 10: the place column kept in step by the read map --------------------------------------------

def tpage(part: int, first: int, last: int) -> dict:
    def anchor(number: int, after: bool) -> dict:
        quote = tline(part, number)
        return {"atom": part, "quote": quote, "prefix": quote if after else "",
                "cfi": "epubcfi(/6/2)", "position_epoch": 0}
    return {"start": anchor(first, False), "end": anchor(last, True), "skimmed": False}


@HOW
def test_the_place_follows_the_read_map_in_every_path_and_nothing_else_writes_it(
        shelf, tenant_dsn, database, worker_dsn, monkeypatch, how):
    """The break it catches: ``memory_through`` left behind by a read-map path (a page that
    finishes a chapter, chapters marked read or not read, a book added again), so the door hands
    out less, or more, than the map says was read; or a second writer of the column. For a
    free-library add and an upload alike."""
    built_edition(shelf, worker_dsn, monkeypatch)
    a = reader(shelf, "r5a")
    book = acquire(shelf, a, how)
    assert memory_through(database, a.owner, book) == 0
    finished = a.call("PUT", f"/api/books/{book}/read-page", tpage(1, 1, 40))
    assert finished.status_code == 200, finished.text
    assert memory_through(database, a.owner, book) == 1 == through(tenant_dsn, a.owner, book)
    half = a.call("PUT", f"/api/books/{book}/read-page", tpage(2, 1, 10))
    assert half.status_code == 200 and memory_through(database, a.owner, book) == 1
    a.mark(book, [2, 3, 4])
    assert memory_through(database, a.owner, book) == 4 == through(tenant_dsn, a.owner, book)
    a.mark(book, [3], read=False)
    assert memory_through(database, a.owner, book) == 2 == through(tenant_dsn, a.owner, book)
    a.mark(book, [3])
    assert memory_through(database, a.owner, book) == 4
    # Added again after removal: a new place, which follows the new map.
    assert a.call("DELETE", f"/api/books/{book}").status_code == 204
    again = acquire(shelf, a, how)
    assert again != book and memory_through(database, a.owner, again) == 0
    assert through(tenant_dsn, a.owner, again) == 0
    a.mark(again, [1, 2])
    assert memory_through(database, a.owner, again) == 2 == through(tenant_dsn, a.owner, again)

    # The one writer: the hosted adapter's set_bookmark, which only sync_counter calls.
    app_dir = Path(__file__).resolve().parents[2] / "app"
    writers = sorted(str(path.relative_to(app_dir)).replace("\\", "/")
                     for path in app_dir.rglob("*.py")
                     if re.search(r"memory_through\s*=(?!=)", path.read_text(encoding="utf-8")))
    assert writers == ["hosted/companion/store.py"]


# ---- 11: the split fingerprint (ADR 0044 step 1) ---------------------------------------------------

def fingerprints(dsn: str, owner, book) -> tuple:
    with psycopg.connect(dsn) as conn:
        return conn.execute("SELECT file_hash,split_hash FROM books WHERE owner_id=%s AND id=%s",
                            (owner, book)).fetchone()


def test_two_imports_of_one_file_have_one_split_fingerprint_and_another_split_another(
        shelf, database):
    """The break it catches: an import that keeps no split fingerprint, or one that depends on
    the reader, the book id or the way the file came (a free-library add or an upload), so two
    copies of one file would never match; one that stays the same for another split; or a removed
    book that keeps it, still tying the reader to the file."""
    a, b, uploader = reader(shelf, "r2a"), reader(shelf, "r5a"), reader(shelf, "u")
    book_a, book_b = add(a), add(b)
    upload = read_map_gate.upload(shelf.client, shelf.tokens, "u", LONG)
    found = {fingerprints(database, a.owner, book_a), fingerprints(database, b.owner, book_b),
             fingerprints(database, uploader.owner, upload)}
    [(file_hash, split_hash)] = found
    _result, chapters = segment_for_ingest(LONG, "any-book")
    assert len(chapters) == PARTS
    assert split_hash == split_hash_of(chapters, "any-book") and file_hash != split_hash

    # Another file whose chapters are the same (only its title differs): another file hash, the
    # same split, so it is still another book (the door needs both equal). A file whose last
    # two parts are one chapter: another split.
    shelf.source.files[int(ALICE)] = long_lamps("The Long Lamp Book, Revised")
    retitled = add(a, ALICE)
    assert fingerprints(database, a.owner, retitled)[0] not in (file_hash, None)
    assert fingerprints(database, a.owner, retitled)[1] == split_hash
    parts = [" ".join(tline(n, i) for i in range(1, 41)) + EXTRA.get(n, "")
             for n in range(1, PARTS + 1)]
    joined = epub_ncx([*((f"c{n}.xhtml", f"Part {n}", f"Part {n}", parts[n - 1])
                         for n in range(1, PARTS - 1)),
                       (f"c{PARTS - 1}.xhtml", f"Part {PARTS - 1}", f"Part {PARTS - 1}",
                        parts[-2] + " " + parts[-1])], title="The Long Lamp Book",
                      author="Ada Writer")
    other = read_map_gate.upload(shelf.client, shelf.tokens, "u", joined)
    other_file, other_split = fingerprints(database, uploader.owner, other)
    assert other_file != file_hash and other_split not in (split_hash, None)

    assert a.call("DELETE", f"/api/books/{book_a}").status_code == 204
    assert fingerprints(database, a.owner, book_a) == (None, None)
    assert fingerprints(database, b.owner, book_b) == (file_hash, split_hash)


@HOW
def test_the_door_refuses_a_reader_whose_split_differs_from_the_editions_or_is_unknown(
        shelf, tenant_dsn, database, worker_dsn, monkeypatch, how):
    """The break it catches: the door matching on the file alone, so a copy of the very same file
    split another way (numbered one on, as another version of the splitter could number it) is
    handed the edition's chapter 4 as its chapter 4; or a reader's book, or the edition, whose
    split is not known being matched rather than refused. Each refusal is shown to be the split's
    alone: putting the fingerprint back hands the reader exactly what it did before. Refused
    means handed nothing: no row for a reader's book with no fingerprint, else the empty row of a
    book with no edition (ADR 0044 step 2). For a free-library add and an upload alike."""
    account, _models, _extraction, work = built_edition(shelf, worker_dsn, monkeypatch)
    edition = _edition_book(tenant_dsn, account)
    a = reader(shelf, "r5a")
    book = acquire(shelf, a, how)
    a.mark(book, [1, 2, 3])
    # ADR 0044 step 4: the marks asked for the reader's job; run (no page read), it builds
    # nothing, and the screens again have nothing to show.
    drain(work, 60)
    file_hash, split_hash = fingerprints(database, a.owner, book)
    assert fingerprints(database, account, edition) == (file_hash, split_hash)
    before = door(tenant_dsn, a.owner, book)
    assert before is not None and max(stamps(before)) == 3

    def set_split(owner, which, value) -> None:
        with psycopg.connect(database) as conn:
            conn.execute("UPDATE books SET split_hash=%s WHERE owner_id=%s AND id=%s",
                         (value, owner, which))

    other = hashlib.sha256(b"another split of the same file").hexdigest()
    for owner, which in ((a.owner, book), (account, edition)):
        for value in (other, None):
            if owner == account and value is None:
                # ADR 0044 step 3 (migration 0033): an edition without a split fingerprint is
                # refused by the database itself, so it cannot be there to be matched.
                with pytest.raises(psycopg.errors.CheckViolation):
                    set_split(owner, which, value)
                continue
            set_split(owner, which, value)
            unknown = owner == a.owner and value is None
            for dsn in (tenant_dsn, database):
                answer = door(dsn, a.owner, book)
                assert (answer is None) == unknown and nothing(answer), (owner, value)
            graph = get(a, f"/api/books/{book}/graph")
            if unknown:
                # No row from the door, the only source of story memory (ADR 0044 step 5): the
                # book is unknown to it, as a book not theirs (no reader's book can be made
                # without both fingerprints, step 1).
                with pytest.raises(LookupError):
                    handed_anything(tenant_dsn, a.owner, book)
                assert (graph.status_code, graph.json()) == (404, {"detail": "unknown book"})
            else:
                assert not handed_anything(tenant_dsn, a.owner, book)
                assert (graph.status_code, graph.json()) == (200, EMPTY_GRAPH)
        set_split(owner, which, split_hash)
        assert door(tenant_dsn, a.owner, book) == before

    # An import of the same bytes whose splitter numbers every chapter one on: same file,
    # another split, refused; the reader keeps nothing of the edition's.
    real = hosted_api.split_hash_of
    monkeypatch.setattr(hosted_api, "split_hash_of", lambda chapters, book_id: real(
        [{**chapter, "ordinal": chapter["ordinal"] + 1} for chapter in chapters], book_id))
    later = reader(shelf, "r10a")
    shifted = acquire(shelf, later, how)
    assert fingerprints(database, later.owner, shifted)[0] == file_hash
    assert fingerprints(database, later.owner, shifted)[1] not in (split_hash, None)
    later.mark(shifted, [1, 2, 3, 4])
    # ADR 0044 step 4: the marks asked for the reader's job; run (no page read), it builds
    # nothing, and the screens again have nothing to show.
    drain(work, 60)
    assert empty_row(door(tenant_dsn, later.owner, shifted))
    assert empty_row(door(database, later.owner, shifted))
    assert get(later, f"/api/books/{shifted}/graph").json() == EMPTY_GRAPH
    assert door(tenant_dsn, a.owner, book) == before


# ---- 12: the door for every book (ADR 0044 step 2) --------------------------------------------------
#
# Uploads, against editions of the notes account built by the builder reader's runs (a free-library
# add of the same file), chapter by chapter, as any reader's runs build them.

def another_file_of_the_same_title() -> bytes:
    """The long lamp book's title, author and parts, with one sentence more in part 1: another
    file (and another split) of the same novel."""
    return epub_ncx([(f"c{n}.xhtml", f"Part {n}", f"Part {n}",
                      " ".join(tline(n, i) for i in range(1, 41)) + EXTRA.get(n, "")
                      + (" A gull cried over the quay." if n == 1 else ""))
                     for n in range(1, PARTS + 1)], title="The Long Lamp Book", author="Ada Writer")


def upload(shelf, someone: Reader, blob: bytes = LONG) -> str:
    return read_map_gate.upload(shelf.client, shelf.tokens, someone.label, blob)


def only_up_to(shelf, tenant_dsn, database, account, someone: Reader, book: str, at: int,
               monkeypatch) -> None:
    """What ``someone``, at ``at``, is handed of the edition: on the raw function (the
    row-security role and past it), through ``HostedBookStore``, on the four screens and in the
    chat's prompts and checks: everything up to ``at``, nothing of a later chapter, no end past
    ``at``."""
    hidden = later_words(at)
    for dsn in (tenant_dsn, database):
        raw = door(dsn, someone.owner, book)
        assert raw is not None and max(stamps(raw)) == at, (someone.label, dsn)
        assert sorted(item["ordinal"] for item in raw["completed"]) == list(range(1, at + 1))
        assert {item["name"] for item in raw["entities"]} == set(NAMES[:at])
        assert not [word for word in hidden if word in json.dumps(raw, default=str)]
    with HostedBookStore(tenant_dsn, OwnerId(someone.owner)).book(book) as mem:
        assert mem.has_rows()
        assert {row["canonical_name"] for row in mem._audit_all("entities")} == set(NAMES[:at])
        assert mem.completion_frontier(hosted_atoms(shelf.app, someone.owner, book)) == at

    with psycopg.connect(database) as conn:
        ids = dict(conn.execute("SELECT canonical_name,id::text FROM entities WHERE owner_id=%s",
                                (account,)).fetchall())
    graph = get(someone, f"/api/books/{book}/graph?bookmark=99")
    notes = get(someone, f"/api/books/{book}/notes?bookmark=99")
    progress = get(someone, f"/api/books/{book}/ingest")
    assert graph.json()["as_of_chapter"] == at == notes.json()["as_of_chapter"]
    assert {row["canonical_name"] for row in graph.json()["characters"]} == set(NAMES[:at])
    assert [row["revealed_at"] for row in notes.json()["chapters"]] == list(range(1, at + 1))
    assert progress.json()["ingest_progress"] == at
    assert get(someone, f"/api/books/{book}/character/{ids[NAMES[at - 1]]}").status_code == 200
    for name in NAMES[at:]:
        if name in ids:
            assert get(someone, f"/api/books/{book}/character/{ids[name]}").status_code == 404
    screens = graph.text + notes.text + progress.text
    assert not [word for word in hidden if word in screens], someone.label

    models = StandIns(shelf.app.state.platform_client, monkeypatch,
                      reply=f"{tline(1, 2)} {tline(at, 3)}")
    assert tline(at, 3) in sentences(someone.ask(book, "What has happened so far?"))
    assert not [word for word in hidden if word in models.everything()], someone.label


def test_two_readers_at_2_and_5_of_one_imported_file_are_each_handed_only_their_own_part(
        shelf, tenant_dsn, database, worker_dsn, monkeypatch):
    """ADR 0044 section 4's first new test. The break it catches: the door matching an upload
    on anything but its own reader's place (the other reader's, the furthest one's, the
    edition's end), on any path."""
    account, *_ = built_edition(shelf, worker_dsn, monkeypatch)
    two, five = reader(shelf, "r2a"), reader(shelf, "r5a")
    books = {"r2a": upload(shelf, two), "r5a": upload(shelf, five)}
    two.mark(books["r2a"], [1, 2])
    five.mark(books["r5a"], [1, 2, 3, 4, 5])
    for someone, at in ((two, 2), (five, 5)):
        only_up_to(shelf, tenant_dsn, database, account, someone, books[someone.label], at,
                   monkeypatch)


def test_with_the_edition_one_chapter_past_the_furthest_reader_no_reader_gets_past_their_own(
        shelf, tenant_dsn, database, worker_dsn, monkeypatch):
    """ADR 0044 section 4's second and third new tests: the edition built to the furthest
    reader's place + 1, as "one ahead" leaves it (the builder reader, also at chapter 5, has
    built it). The break it catches: chapter 6, its new person (Brannoch) or the end it puts on
    Mara's first state reaching the furthest reader at chapter 5, or anything past chapter 2 the
    reader behind them; on the raw function, the adapter, the screens or the chat."""
    account, _models, _extraction, work = started_edition(shelf, worker_dsn, monkeypatch,
                                                          through=5)
    behind, furthest = reader(shelf, "r2a"), reader(shelf, "r5a")
    books = {"r2a": upload(shelf, behind), "r5a": upload(shelf, furthest)}
    behind.mark(books["r2a"], [1, 2])
    furthest.mark(books["r5a"], [1, 2, 3, 4, 5])
    # The builder's six runs (to their place, 5, + 1), and the two readers' own runs, which their
    # marks asked for (ADR 0044 step 4) and which, with no page read, build nothing.
    assert drain(work, 60) == 8
    assert built(database, account) == [(n, "body") for n in range(1, 7)]
    with psycopg.connect(database) as conn:
        assert conn.execute("SELECT count(*) FROM entity_state WHERE owner_id=%s"
                            " AND invalid_at=6", (account,)).fetchone()[0] == 1
    for someone, at in ((behind, 2), (furthest, 5)):
        only_up_to(shelf, tenant_dsn, database, account, someone, books[someone.label], at,
                   monkeypatch)


def test_the_chats_spoiler_probes_for_the_furthest_reader_of_an_upload_with_the_next_chapter_built(
        shelf, tenant_dsn, database, worker_dsn, tmp_path, monkeypatch):
    """ADR 0044 section 4's third new test, on the chat: the probes of
    ``test_the_chats_spoiler_probes_through_the_door`` for the furthest reader of an uploaded
    file, at each place *n* with chapter *n*+1 already built (by the builder reader, at *n* as
    well, whose runs build one chapter ahead). The break it catches: chapter *n*+1 reaching the
    furthest reader's snapshot or screens, or the door's fixed checks dropping less than a memory
    built only to their place. (Since ADR 0044 step 10 the names of chapter *n*+1 are checked
    in the database, so the fixed check drops them; they still never reach the reader's
    request.)"""
    shelf.source.files[int(ALICE)] = lamps()
    account, _models, _extraction, work = started_edition(shelf, worker_dsn, monkeypatch, ALICE,
                                                          writer=Lamps, through=0)
    builder, built_book = shelf.builder
    someone = reader(shelf, "r2a")
    book = acquire(shelf, someone, "upload", ALICE)
    atoms = hosted_atoms(shelf.app, someone.owner, book)
    platform = shelf.app.state.platform_client
    local_client = extraction_client()
    Lamps(local_client, monkeypatch)
    door_drops = {}
    with local_book(tmp_path, lamps()) as (extract, memory, local_atoms, chapters):
        for at in (1, 2, 3):
            builder.mark(built_book, list(range(1, at + 1)))
            drain(work, 60)
            assert built(database, account) == [(n, "body") for n in range(1, min(at + 1, 3) + 1)]
            extract(local_client, at)
            place_at(someone, book, at, parts=3)
            assert through(tenant_dsn, someone.owner, book) == at
            assert through(database, someone.owner, book) == at
            assert get(someone, f"/api/books/{book}/graph?bookmark=99").json()[
                "as_of_chapter"] == at
            assert [row["revealed_at"] for row in get(
                someone, f"/api/books/{book}/notes?bookmark=99").json()["chapters"]] \
                == list(range(1, at + 1))
            read_text = " ".join(ch["text"] for ch in chapters if ch["ordinal"] <= at)
            with memory() as mem:
                local = fixed_check_drops(mem, min(at, mem.completion_frontier(local_atoms)),
                                          read_text)
            with HostedBookStore(tenant_dsn, OwnerId(someone.owner)).book(book) as mem:
                assert mem.completion_frontier(atoms) == at
                door_drops[at] = fixed_check_drops(mem, at, read_text)
            assert door_drops[at] >= local, at
            for probe, sentence in PROBES.items():
                unread = [name for name in UNREAD[at] if name in sentence]
                StandIns(platform, monkeypatch, reply=f"{line(1, 2)} {sentence}",
                         unsupported=UNREAD[at])
                shown = sentences(someone.ask(book))
                assert line(1, 2) in shown, (at, probe)
                assert (sentence in shown) == (not unread and probe not in door_drops[at]), (
                    at, probe)
                StandIns(platform, monkeypatch, reply=f"{line(1, 2)} {sentence}")
                shown = sentences(someone.ask(book))
                assert (sentence not in shown) == (probe in door_drops[at]), (at, probe)
    # The fixed check asks the database (ADR 0044 step 10), which holds the chapter built one
    # ahead: at 1 it knows Corvus (part 2) and drops him, not yet Vex; at 2 it knows Vex and his
    # late alias. A name of a chapter nobody has reached is in no notes: it is left to Jev and the
    # second look, as in local mode.
    assert door_drops == {1: {"in the hole", "foreshadowing"},
                          2: {"later", "late alias", "foreshadowing"}, 3: {"foreshadowing"}}


def test_a_reader_of_another_file_of_the_same_title_is_handed_nothing(
        shelf, tenant_dsn, database, worker_dsn, monkeypatch):
    """ADR 0044 section 4's fourth new test (a different split of the same file, and an unknown
    one, are ``test_the_door_refuses_a_reader_whose_split_differs_from_the_editions_or_is_unknown``
    for an upload). The break it catches: the door matching a title, an author or a catalogue
    entry rather than the file, so another edition's chapters reach this reader; or "nothing"
    told apart from an edition read at place 0."""
    *_built, work = built_edition(shelf, worker_dsn, monkeypatch)
    starting = reader(shelf, "r5a")
    fresh = upload(shelf, starting)
    other = reader(shelf, "u")
    another = upload(shelf, other, another_file_of_the_same_title())
    other.mark(another, [1, 2, 3])
    # ADR 0044 step 4: the marks asked for the reader's job; run (no page read), it builds
    # nothing, and the screens again have nothing to show.
    drain(work, 60)
    for dsn in (tenant_dsn, database):
        assert empty_row(door(dsn, other.owner, another))
    assert door_bytes(tenant_dsn, other.owner, another) \
        == door_bytes(tenant_dsn, starting.owner, fresh)
    assert not handed_anything(tenant_dsn, other.owner, another)
    assert get(other, f"/api/books/{another}/graph").json() == EMPTY_GRAPH
    models = StandIns(shelf.app.state.platform_client, monkeypatch,
                      reply=f"{tline(1, 2)} {tline(3, 3)}")
    assert tline(3, 3) in sentences(other.ask(another, "What has happened so far?"))
    assert not [word for word in later_words(3) if word in models.everything()]


def test_a_removed_upload_an_account_pending_deletion_and_the_notes_account_get_nothing(
        shelf, tenant_dsn, database, worker_dsn, monkeypatch):
    """ADR 0044 section 4's sixth new test. The break it catches: the door answering a book its
    reader removed, an account waiting to be erased handed anything past its place (in its copy
    of its data, or by the door given that owner), or the door serving the notes account's own
    book, or that account signing in."""
    account, *_ = built_edition(shelf, worker_dsn, monkeypatch)
    gone, leaving = reader(shelf, "r2a"), reader(shelf, "r5a")
    gone_book, leaving_book = upload(shelf, gone), upload(shelf, leaving)
    gone.mark(gone_book, [1, 2, 3])
    leaving.mark(leaving_book, [1, 2, 3])
    assert through(tenant_dsn, gone.owner, gone_book) == 3 \
        == through(tenant_dsn, leaving.owner, leaving_book)

    # A removed book: no row, the same as a book that does not exist.
    assert gone.call("DELETE", f"/api/books/{gone_book}").status_code == 204
    for dsn in (tenant_dsn, database):
        assert door(dsn, gone.owner, gone_book) is None
    assert get(gone, f"/api/books/{gone_book}/graph").status_code == 404

    # An account waiting to be erased. Its sign-in answers every story screen and the chat with
    # the pending refusal (``test_account_deletion_postgres::
    # test_signing_in_while_pending_answers_only_the_pending_refusal_on_every_inventoried_route``,
    # every route the app lists); the copy of its data, the one thing it may still take, holds
    # nothing past its place. This app's sign-in stand-in does not read the account's state, so
    # the state is set in the database and the copy taken through it.
    with psycopg.connect(database) as conn:
        conn.execute("UPDATE users SET status='deleting', erase_after=now()+interval '30 days'"
                     " WHERE id=%s", (leaving.owner,))
    files = copy_of(leaving.call("GET", "/api/account/export"))
    [entry] = [json.loads(body) for name, body in files.items() if name.startswith("books/")]
    assert entry["what_mothli_keeps_about_the_story"]["as_of_chapter"] == 3
    assert not [word for word in later_words(3) if word in "\n".join(files.values())]
    # The door decides nothing from the account's state: given that owner, it still hands only
    # what is up to their place (no session of theirs can reach it).
    assert through(database, leaving.owner, leaving_book) == 3

    # The notes account: its own book is not served to it, and it never signs in.
    edition = _edition_book(tenant_dsn, account)
    for dsn in (tenant_dsn, database):
        assert door(dsn, account, edition) is None
        assert door(dsn, account, leaving_book) is None
    auth = PostgresAuthRepository(database)
    now = datetime.now(UTC)
    with pytest.raises(InactiveUserError):
        asyncio.run(auth.create_session(owner_id=account, session_digest=b"n" * 32,
                                        csrf_digest=b"m" * 32, issuer="https://idp.example",
                                        old_session_digest=None, now=now,
                                        expires_at=now + timedelta(hours=1)))


def test_the_doors_answer_at_place_0_is_the_same_byte_for_byte_with_and_without_an_edition(
        shelf, tenant_dsn, database, worker_dsn, monkeypatch):
    """ADR 0044 section 4's seventh new test and the owner's rule on acceptance: nothing a reader
    is shown says anything about another. The break it catches: the door, the story memory, the
    screens or the copy of one's data answering a reader before their first chapter differently
    once an edition of their file exists (someone else has the same file); or an edition row
    naming a reader, their book or anything they did, the reader whose runs built it included."""
    first, other, later = reader(shelf, "r2a"), reader(shelf, "r5a"), reader(shelf, "r10a")
    mine = upload(shelf, first)
    elsewhere = upload(shelf, other, another_file_of_the_same_title())

    def seen(someone: Reader, book: str, *, copy: bool = True) -> tuple:
        """What the reader is given; with ``copy``, their copy of their data's story too (a
        reader may take one copy every few minutes, so each reader takes it once)."""
        raw = door_bytes(tenant_dsn, someone.owner, book)
        with HostedBookStore(tenant_dsn, OwnerId(someone.owner)).book(book) as mem:
            memory = (mem.has_rows(),
                      mem.completion_frontier(hosted_atoms(shelf.app, someone.owner, book)),
                      {bm: screens(mem, bm) for bm in range(0, 3)},
                      [sorted(map(str, mem._audit_all(table)))
                       for table in ("entities", "aliases", "themes")])
        graph = get(someone, f"/api/books/{book}/graph")
        shown = (raw, memory, (graph.status_code, graph.json()))
        if not copy:
            return shown
        files = copy_of(someone.call("GET", "/api/account/export"))
        [entry] = [json.loads(body) for name, body in files.items() if name.startswith("books/")]
        return (*shown, entry.get("what_mothli_keeps_about_the_story"))

    before = seen(first, mine)
    assert empty_row(json.loads(before[0]))
    assert seen(other, elsewhere, copy=False) == before[:3]

    account, *_ = built_edition(shelf, worker_dsn, monkeypatch)
    builder, built_book = shelf.builder
    added = acquire(shelf, later, "library")
    assert seen(first, mine, copy=False) == before[:3]
    assert seen(other, elsewhere) == before
    assert seen(later, added) == before

    # No edition row names a reader, their book or their copy (the builder's, whose runs wrote
    # every row of it, included); reading and asking change none.
    edition = everything_of(database, account)
    with psycopg.connect(database) as conn:
        names = [row[0] for row in conn.execute(
            "SELECT display_name FROM users WHERE id = ANY(%s)",
            ([first.owner, other.owner, later.owner, builder.owner],)).fetchall()]
    words = [str(first.owner), str(other.owner), str(later.owner), str(builder.owner), mine,
             elsewhere, added, built_book, *names]
    assert len(names) == 4
    assert not [word for word in words if word in json.dumps(edition)]
    first.mark(mine, [1, 2])
    StandIns(shelf.app.state.platform_client, monkeypatch, reply=f"{tline(1, 2)} Reader one.")
    assert sentences(first.ask(mine)) == [tline(1, 2), "Reader one."]
    assert everything_of(database, account) == edition


def test_an_unread_upload_whose_edition_others_built_to_the_end_gives_its_chats_nothing(
        shelf, tenant_dsn, database, worker_dsn, monkeypatch):
    """ADR 0044 section 4's eighth new test, the owner's rule for unread books: of a book not
    yet started, the home chat and the book's own chat are given nothing of the story. The break
    it catches: an edition built to the end by others reaching either chat's prompts or checks
    for a reader at place 0; checked against the same reader's situation before any edition
    existed."""
    platform = shelf.app.state.platform_client

    def asked(someone: Reader, book: str) -> str:
        models = HomeModels(platform, monkeypatch, reply="Happy reading.")
        home = someone.call("POST", "/api/reader/ask/stream",
                            {"question": "What should I read tonight?"})
        assert home.status_code == 200, home.text
        own = someone.stream(book, "What is this book about?")
        assert own.status_code == 200, own.text
        return models.everything().replace(book, "BOOK").replace(str(someone.owner), "OWNER")

    early = reader(shelf, "r2a")
    early_book = upload(shelf, early)
    before = asked(early, early_book)

    built_edition(shelf, worker_dsn, monkeypatch)
    ahead = reader(shelf, "r10a")
    ahead_book = upload(shelf, ahead)
    ahead.mark(ahead_book, list(range(1, PARTS + 1)))
    late = reader(shelf, "r2b")
    late_book = upload(shelf, late)
    after = asked(late, late_book)
    assert not [word for word in (*NAMES, "Keeper") if word in after]
    assert after == before


# ---- 13: the operator's path deleted (ADR 0044 step 6) ---------------------------------------------

def test_a_fresh_database_needs_no_step_for_a_readers_first_page_to_build_chapter_1_for_them(
        shelf, tenant_dsn, database, worker_dsn, monkeypatch):
    """With the operator's command gone, nothing but the migrations makes the notes account. On
    this test's database, migrated from nothing with no step of anyone's after: the account is
    there, suspended, with its limits row and no day or month cap of its own (so the notes
    ceiling applies), owning nothing; importing a book builds nothing; the reader's first read
    page builds chapter 1, paid by the notes account; it is handed to them only once they have
    finished it, and then chapter 2 is built (one ahead) and not handed. The break it catches: a
    fresh deployment whose readers never get notes until someone runs a step (the notes account,
    its limits row or a cap missing or wrong), or chapter 1 handed to a reader before they finish
    it."""
    with psycopg.connect(database, row_factory=dict_row) as conn:
        [notes] = conn.execute("SELECT account_id FROM notes_account").fetchall()
        account = notes["account_id"]
        assert conn.execute("SELECT status FROM users WHERE id=%s",
                            (account,)).fetchone() == {"status": "suspended"}
        assert conn.execute(
            "SELECT max_spend_usd_per_day,max_spend_usd_per_month FROM owner_limits"
            " WHERE owner_id=%s", (account,)).fetchall() == [
            {"max_spend_usd_per_day": None, "max_spend_usd_per_month": None}]
        assert conn.execute("SELECT count(*) AS n FROM books WHERE owner_id=%s",
                            (account,)).fetchone() == {"n": 0}
    extraction = extraction_client()
    models = Writer(extraction, monkeypatch)
    work = worker(shelf.app, extraction, worker_dsn)
    someone = reader(shelf, "r2a")
    book = upload(shelf, someone)
    assert drain(work, 60) == 0 and models.calls == []

    page = someone.call("PUT", f"/api/books/{book}/read-page", tpage(1, 1, 2))
    assert page.status_code == 200, page.text
    assert drain(work, 60) == 1
    assert models.parts() == [1] and built(database, account) == [(1, "body")]
    assert empty_row(door(tenant_dsn, someone.owner, book))
    assert get(someone, f"/api/books/{book}/graph").json() == EMPTY_GRAPH

    # The rest of part 1 read, then "I've read this" on it, which finishes it whatever the
    # pages covered (these pages leave the part's first characters unread).
    finished = someone.call("PUT", f"/api/books/{book}/read-page", tpage(1, 3, 40))
    assert finished.status_code == 200, finished.text
    someone.mark(book, [1])
    assert through(tenant_dsn, someone.owner, book) == 1
    assert [row["canonical_name"] for row in get(
        someone, f"/api/books/{book}/graph").json()["characters"]] == ["Mara"]
    assert drain(work, 60) == 1
    assert built(database, account) == [(1, "body"), (2, "body")]
    assert through(tenant_dsn, someone.owner, book) == 1
    assert "Corvus" not in get(someone, f"/api/books/{book}/graph?bookmark=99").text
    with psycopg.connect(database) as conn:
        assert {row[0] for row in conn.execute(
            "SELECT DISTINCT owner_id FROM cost_ledger").fetchall()} == {account}


def test_an_edition_the_removed_command_made_is_built_on_by_readers_and_its_own_job_builds_nothing(
        shelf, tenant_dsn, database, worker_dsn, monkeypatch):
    """What step 6 does with an edition the operator's command made before it (none was ever
    deployed): it is left as it is, an edition like any other. Such an edition is a real copy of
    the file on the notes account's shelf (its stored file, its title, its free-library source),
    marked as its file's edition, with a job of the notes account's own waiting; it is made here
    by the very path the command took (``add_entry`` as the notes account, the mark, the queue).
    The break it catches: that job building or spending anything (nothing treats the notes
    account's books as built whole any more: its own read map has no page, so its target is 0);
    readers of the file no longer served by it, or given a second edition beside it; or anything
    past a reader's place handed to them."""
    account = notes_account_of(database)
    command = SimpleNamespace(state=SimpleNamespace(
        settings=shelf.app.state.settings, object_storage=shelf.app.state.object_storage,
        tenant_repository=PostgresTenantRepository(shelf.tenant_dsn),
        companion_dsn=shelf.app.state.companion_dsn,
        hosted_runtime=HostedRuntimeRegistry(cache_max_entries=64, lock_max_entries=64)))
    entry = find_classic(PRIDE, min_years_since_death=(
        shelf.app.state.settings.classics_min_years_since_death))
    asyncio.run(hosted_classics.add_entry(SimpleNamespace(app=command), OwnerId(account), entry))
    with psycopg.connect(database, row_factory=dict_row) as conn:
        [copy] = conn.execute("SELECT id,title,source_kind FROM books WHERE owner_id=%s",
                              (account,)).fetchall()
        assert (copy["title"], copy["source_kind"]) == ("The Long Lamp Book", f"gutenberg:{PRIDE}")
        assert conn.execute("SELECT count(*) AS n FROM source_objects WHERE owner_id=%s",
                            (account,)).fetchone() == {"n": 1}
        conn.execute("UPDATE books SET edition_of=owner_id WHERE owner_id=%s", (account,))
    assert StoryMemoryQueue(shelf.app.state.companion_dsn, OwnerId(account)).ensure(
        str(copy["id"]))

    extraction = extraction_client()
    models = Writer(extraction, monkeypatch)
    work = worker(shelf.app, extraction, worker_dsn)
    assert drain(work, 60) == 1
    assert models.calls == [] and built(database, account) == []
    with psycopg.connect(database) as conn:
        assert conn.execute("SELECT count(*) FROM cost_ledger").fetchone()[0] == 0

    # Readers of the file, however they got it, build into that edition and are handed it.
    a, b = reader(shelf, "r2a"), reader(shelf, "u")
    book_a, book_b = add(a), upload(shelf, b)
    assert a.call("PUT", f"/api/books/{book_a}/read-page", tpage(1, 1, 2)).status_code == 200
    a.mark(book_a, [1, 2])
    drain(work, 60)
    assert built_in(database, (account, copy["id"])) == [(n, "body") for n in (1, 2, 3)]
    with psycopg.connect(database) as conn:
        assert conn.execute("SELECT count(*) FROM books WHERE owner_id=%s",
                            (account,)).fetchone()[0] == 1
    b.mark(book_b, [1, 2])
    for someone, book in ((a, book_a), (b, book_b)):
        only_up_to(shelf, tenant_dsn, database, account, someone, book, 2, monkeypatch)


def test_a_free_library_add_and_an_upload_of_its_file_are_one_book_to_the_trigger_and_the_target(
        shelf, tenant_dsn, database, worker_dsn, monkeypatch):
    """Which way a reader got a file does not matter to story memory (ADR 0044 sections 1 and
    3). Two readers do the same things to one file, one having added it from the free library and
    one having uploaded it. The break it catches: a branch on the book's source (its free-library
    tag) anywhere in the story-memory path: in what asks for a job (an add, "I've read this", a
    page read), in how far a run builds (``story_memory_target``), or in the edition it builds
    into."""
    extraction = extraction_client()
    models = Writer(extraction, monkeypatch)
    work = worker(shelf.app, extraction, worker_dsn)
    added_by, uploaded_by = reader(shelf, "r2a"), reader(shelf, "r2b")
    both = ((added_by, add(added_by)), (uploaded_by, upload(shelf, uploaded_by)))

    def target(someone: Reader, book: str) -> int:
        catalog = HostedCompanionCatalog(tenant_dsn, OwnerId(someone.owner))
        source = HostedBookSource(shelf.app.state.object_storage, catalog,
                                  shelf.app.state.companion_sources)
        return story_memory_target(book, catalog, shelf.app.state.settings, source)

    def states(someone: Reader) -> list[str]:
        return [row["state"] for row in jobs_of(database, someone.owner)]

    assert [states(someone) for someone, _book in both] == [[], []]
    assert [target(*pair) for pair in both] == [0, 0]
    for someone, book in both:
        someone.mark(book, [1, 2])
    assert [states(someone) for someone, _book in both] == [["pending"], ["pending"]]
    assert [target(*pair) for pair in both] == [0, 0]
    assert drain(work, 60) == 2 and models.calls == []
    for someone, book in both:
        page = someone.call("PUT", f"/api/books/{book}/read-page", tpage(3, 1, 2))
        assert page.status_code == 200, page.text
    assert [states(someone)[-1] for someone, _book in both] == ["pending", "pending"]
    assert [target(*pair) for pair in both] == [3, 3]
    drain(work, 60)
    assert sorted(models.parts()) == [1, 2, 3]
    assert built_for(database, added_by.owner, both[0][1]) \
        == built_for(database, uploaded_by.owner, both[1][1]) == [(n, "body") for n in (1, 2, 3)]
    assert [stamps(door(tenant_dsn, someone.owner, book)) for someone, book in both][0] \
        == stamps(door(tenant_dsn, uploaded_by.owner, both[1][1]))
    assert through(tenant_dsn, added_by.owner, both[0][1]) == 2
