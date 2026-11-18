"""Library story memory through the door (ADR 0042 part 5), against real PostgreSQL.

A free-library title's story memory is built once, by the operator's command, as the library
account; a reader whose copy is exactly that file gets it through one database function that
takes the book and nothing else and works the reader's place out in the database. The owner's
test: readers at chapters 2, 5 and 10 of one built book; whatever answers the reader at chapter 2
holds nothing of chapters 5 or 10, on the raw function result (the RLS-enforced tenant role, and a
superuser connection with that reader's owner setting), through ``HostedBookStore``, the four
story screens, the chat and the copy of one's data. Then: no way to choose another place; moving
back shrinks it and reading on grows it; parity with local mode; the chat's spoiler probes; a
later name the writer "remembers"; nothing between readers; the operator's build; erasure,
removal and grants with library rows present; and the place column kept in step by the read map.
Stand-in writer, checker and Jev only: nothing is called and nothing paid.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import conninfo, sql
from psycopg.rows import dict_row

from app.eval.spoiler_gate import score_recap
from app.hosted.auth.repository import InactiveUserError, PostgresAuthRepository
from app.hosted.companion.jobs import StoryMemoryQueue
from app.hosted.companion.memory import HostedBookStore
from app.hosted.companion.spend import set_spend_limits, spend_limits
from app.hosted.companion.store import HostedCompanionCatalog
from app.hosted.library_memory import (
    STOPPED,
    LibraryConfigurationError,
    PostgresLibraryAccount,
    build,
    report,
)
from app.hosted.limits import update_limits
from app.hosted.purge import PostgresAccountPurge, owner_tables, purge_account
from app.hosted.runtime import HostedRuntimeRegistry
from app.hosted.tenant import api as hosted_api
from app.hosted.tenant import classics as hosted_classics
from app.hosted.tenant.grants import LIBRARY_TABLE_PRIVILEGES, purge_grants
from app.hosted.tenant.models import OwnerId
from app.hosted.tenant.repository import LIBRARY_MEMORY_FIXED, PostgresTenantRepository
from app.ingest.extraction.chapter_text import segment_for_ingest, split_hash_of
from app.main import create_app
from app.memory_views import graph_payload, notes_payload
from tests.api._epub import epub_ncx
from tests.hosted import test_companion_read_map_postgres as read_map_gate
from tests.hosted import test_jobs_postgres as jobs_gate
from tests.hosted.test_account_deletion_postgres import _role
from tests.hosted.test_account_export_postgres import copy_of
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

# The launch setting: story memory on, a reader's own books get none.
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


# ---- the app, the free library's source, the readers and the operator ---------------------------

PLACES = {"r2a": 2, "r2b": 2, "r2c": 2, "r5a": 5, "r5b": 5, "r5c": 5,
          "r10a": 10, "r10b": 10, "r10c": 10, "r10d": 10}


@pytest.fixture()
def people(database: str) -> dict:
    owners = {label: uuid.uuid4() for label in (*PLACES, "u")}
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
def shelf(tenant_dsn: str, people: dict, tmp_path, source: Source):
    client, tokens, app = app_for(tenant_dsn, people, tmp_path, **LAUNCH)
    with client:
        yield SimpleNamespace(client=client, tokens=tokens, app=app, people=people,
                              source=source, tenant_dsn=tenant_dsn)


@pytest.fixture()
def library_dsn(admin_dsn: str, database: str):
    """The library command's role, granted exactly its list, row security on."""
    tables = sorted((table, tuple(sorted(privileges)))
                    for table, privileges in LIBRARY_TABLE_PRIVILEGES.items())
    with _role(admin_dsn, database, bypass=False, tables=tables) as dsn:
        yield dsn


def command(shelf) -> SimpleNamespace:
    """What the operator's command builds from its environment (``_state_from_environment``)."""
    app = shelf.app
    return SimpleNamespace(settings=app.state.settings, object_storage=app.state.object_storage,
                           tenant_repository=PostgresTenantRepository(shelf.tenant_dsn),
                           companion_dsn=app.state.companion_dsn,
                           hosted_runtime=HostedRuntimeRegistry(cache_max_entries=64,
                                                                lock_max_entries=64))


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


def built_edition(shelf, library_dsn, worker_dsn, monkeypatch, entry=PRIDE, *, writer=Writer,
                  **options):
    """The operator's command for one entry, then the worker until it has nothing left."""
    extraction = extraction_client()
    models = writer(extraction, monkeypatch, **options)
    library = PostgresLibraryAccount(library_dsn)
    build(command(shelf), library, [entry])
    work = worker(shelf.app, extraction, worker_dsn)
    drain(work, 60)
    return library.account(), models, extraction, work


def door(dsn: str, owner, book) -> dict | None:
    """The function's raw answer for ``book``, with ``owner`` as the transaction's owner."""
    with psycopg.connect(dsn, row_factory=dict_row) as conn, conn.transaction():
        if owner is not None:
            conn.execute("SELECT set_config('app.owner_id', %s, true)", (str(owner),))
        return conn.execute("SELECT * FROM library_story_memory(%s)", (book,)).fetchone()


def stamps(row: dict) -> list[int]:
    """Every chapter number in the door's answer: the chapter stamps and every end."""
    found = [item["ordinal"] for name in ("chapters", "completed") for item in row[name]]
    for name in ("entities", "aliases", "themes", "events", "summaries", "edges", "states"):
        for item in row[name]:
            found.append(item["revealed_at"])
            found += [item[key] for key in ("invalid_at", "chapter_ordinal")
                      if item.get(key) is not None]
    return found


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

def test_readers_at_2_5_and_10_are_each_handed_only_what_is_up_to_their_place(
        shelf, library_dsn, tenant_dsn, database, worker_dsn, monkeypatch):
    """The owner's test. The break it catches: a row of a later chapter, or an end ("this state
    ends at chapter 6") past the reader's place, leaving the database for that reader's request,
    on the row-security role or past it, or reaching their story memory, the four screens, the
    chat's prompts and checks, or their copy of their data."""
    account, _models, _extraction, _work = built_edition(shelf, library_dsn, worker_dsn,
                                                         monkeypatch)
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
        books[label] = add(someone)
        someone.mark(books[label], list(range(1, at + 1)))

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
            assert mem.shared
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

def test_no_argument_device_tab_or_direct_write_moves_the_place_and_the_door_refuses_the_rest(
        shelf, library_dsn, tenant_dsn, database, worker_dsn, admin_dsn, monkeypatch):
    """The break it catches: a place taken from a caller (an argument, a requested chapter), from
    the counter a device's position report raises, from a stale tab, or from a column written
    past every chapter the reader has a read page in; or the door answering a caller with no
    owner setting, another reader's book, a removed book, an upload of the very same file, a
    library book whose file differs, or the library account itself."""
    account, *_ = built_edition(shelf, library_dsn, worker_dsn, monkeypatch)
    a, b = reader(shelf, "r2a"), reader(shelf, "r5a")
    book, other = add(a), add(b)
    a.mark(book, [1, 2])
    b.mark(other, [1, 2, 3, 4, 5])
    assert through(tenant_dsn, a.owner, book) == 2

    with psycopg.connect(database) as conn:
        assert conn.execute(
            "SELECT pg_get_function_identity_arguments('library_story_memory'::regproc)"
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
        library_book = conn.execute("SELECT id FROM books WHERE owner_id=%s",
                                    (account,)).fetchone()[0]
    assert door(tenant_dsn, account, library_book) is None
    uploader = reader(shelf, "u")
    upload = read_map_gate.upload(shelf.client, shelf.tokens, "u", LONG)
    assert door(tenant_dsn, uploader.owner, upload) is None
    uploader.mark(upload, [1, 2, 3])
    assert not HostedBookStore(tenant_dsn, OwnerId(uploader.owner)).shared(upload)
    assert get(uploader, f"/api/books/{upload}/graph").status_code == 404
    shelf.source.files[int(PRIDE)] = long_lamps("The Long Lamp Book, Revised")
    differs = reader(shelf, "r10a")
    changed = add(differs)
    differs.mark(changed, [1, 2, 3])
    assert door(tenant_dsn, differs.owner, changed) is None
    assert get(differs, f"/api/books/{changed}/graph").status_code == 404
    assert a.call("DELETE", f"/api/books/{book}").status_code == 204
    assert door(tenant_dsn, a.owner, book) is None and door(database, a.owner, book) is None

    # Executable by the tenant role alone: not by everyone, the worker or the purge.
    with psycopg.connect(database) as conn:
        roles = {name: conn.execute(
            "SELECT has_function_privilege(%s,'library_story_memory(uuid)','EXECUTE')",
            (name,)).fetchone()[0] for name in (
            conninfo.conninfo_to_dict(tenant_dsn)["user"],
            conninfo.conninfo_to_dict(worker_dsn)["user"],
            conninfo.conninfo_to_dict(library_dsn)["user"], "public")}
    assert list(roles.values()) == [True, False, False, False]


# ---- 3: moving back shrinks, moving on grows ------------------------------------------------------

def test_i_havent_read_this_shrinks_the_next_answer_and_reading_on_grows_it(
        shelf, library_dsn, tenant_dsn, database, worker_dsn, monkeypatch):
    """The break it catches: a per-reader copy kept in step (or not) beside the edition, so
    "I haven't read this" leaves later people in the reader's memory, or reading on does not
    bring them."""
    account, *_ = built_edition(shelf, library_dsn, worker_dsn, monkeypatch)
    a = reader(shelf, "r5a")
    book = add(a)
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


def test_a_reader_of_the_library_edition_gets_what_local_mode_builds_to_their_place(
        shelf, library_dsn, tenant_dsn, worker_dsn, tmp_path, monkeypatch):
    """The break it catches: the door answering a check or a screen differently from a local
    memory built only to the reader's place: a row of a later chapter, an end past the place,
    a chapter key that is the library's and not the reader's (the frontier then stays 0), a lost
    alias or state, or the people of one chapter in another order."""
    shelf.source.files[int(ALICE)] = harbour_book()
    built_edition(shelf, library_dsn, worker_dsn, monkeypatch, ALICE, writer=Extractor)
    someone = reader(shelf, "r2a")
    book = add(someone, ALICE)
    atoms = hosted_atoms(shelf.app, someone.owner, book)
    local_client = extraction_client()
    Extractor(local_client, monkeypatch)
    with local_book(tmp_path, harbour_book()) as (extract, memory, local_atoms, chapters):
        assert [atom["ordinal"] for atom in atoms] == [1, 2, 3, 4]
        for at in (0, 1, 2, 3, 4):
            if at:
                extract(local_client, at)
            place_at(someone, book, at, parts=4)
            with memory() as mem:
                expected = answers(mem, local_atoms, chapters)
                expected_screens = {bm: screens(mem, bm) for bm in range(0, 6)}
            with HostedBookStore(tenant_dsn, OwnerId(someone.owner)).book(book) as mem:
                assert mem.shared
                assert answers(mem, atoms, chapters) == expected, at
                assert {bm: screens(mem, bm) for bm in range(0, 6)} == expected_screens, at
            assert expected["frontier"] == at
    # A floor under the comparison: the book exercises what it claims to.
    assert expected[3]["names"][2] == [["Mara", "the Keeper"]]
    assert ("Corvus", "Vex", "rivalry", "distrusts", 3) in expected_screens[3]["ties"]


# ---- 5: the chat's spoiler probes through the door --------------------------------------------------

# What the stand-in checker doubts at each place in the stream tests' "empty memory" probes: a name
# its passages (the read text) do not hold.
UNREAD = {1: ("Corvus", "Vex", "Lamplighter"), 2: ("Vex", "Lamplighter"), 3: ("Lamplighter",)}


@pytest.mark.parametrize("hole", [False, True])
def test_the_chats_spoiler_probes_through_the_door(
        shelf, library_dsn, tenant_dsn, database, worker_dsn, tmp_path, monkeypatch, hole):
    """The chat's spoiler probes (a person first met later, one first met in an unremembered
    chapter, a late alias, foreshadowing) for a reader of the library edition at places before,
    at and after each person's first chapter, with and without an unremembered chapter. The break
    it catches: the door's fixed checks dropping less than a memory built only to the reader's
    place (local mode's, the usual per-reader case, ADR 0042 section 3), or the hosted chat
    showing a probe its checks drop. It records plainly what version 1 means: with a checker that
    doubts every name its passages do not hold (the stream tests' probes with no memory) every
    probe goes; with a checker that clears everything, a person or name first met after the place
    is shown, because nothing stamped after the place is handed out for the fixed check to know
    (see ``test_a_later_name_the_writer_remembers_is_left_to_jev_and_the_second_look``)."""
    shelf.source.files[int(ALICE)] = lamps()
    account, *_ = built_edition(shelf, library_dsn, worker_dsn, monkeypatch, ALICE,
                                writer=Lamps, reject={2} if hole else ())
    assert built(database, account) == [(1, "body"), (2, "unremembered" if hole else "body"),
                                        (3, "body")]
    someone = reader(shelf, "r2a")
    book = add(someone, ALICE)
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
    assert door_drops == {1: {"foreshadowing"}, 2: {"foreshadowing"}, 3: {"foreshadowing"}}


# ---- 6: a later name the writer "remembers" ------------------------------------------------------

REMEMBERED = "Zephyrine lit the harbour lamps."   # first met in part 4


def test_a_later_name_the_writer_remembers_is_left_to_jev_and_the_second_look(
        shelf, library_dsn, tenant_dsn, worker_dsn, monkeypatch):
    """ADR 0042 section 3's probe, with stand-in models (the result decides whether version 2 goes
    back to the owner; real models are for ``scripts/library_later_name_probe.py``). A reader at
    chapter 2 of an edition built to the end; the writer names Zephyrine, first met in part 4, as
    a model remembering the novel would. What it records, plainly:

    - Through the door the fixed name check cannot know the name: nothing stamped after the
      place is handed out, so ``score_recap`` finds no leak. The edition's own rows, which the
      rejected unclamped design would have handed out, would have caught it.
    - So the sentence is dropped only when Jev or the second look doubts it: with a checker that
      clears everything it is shown; with one that doubts it, it is not.
    The break it catches: a later name, or the edition's later rows, reaching the reader's
    request to block it ("version 2"), which ADR 0042 section 3 did not choose."""
    account, *_ = built_edition(shelf, library_dsn, worker_dsn, monkeypatch)
    someone = reader(shelf, "r2a")
    book = add(someone)
    someone.mark(book, [1, 2])
    with HostedBookStore(tenant_dsn, OwnerId(someone.owner)).book(book) as mem:
        assert score_recap(mem, 2, REMEMBERED)["future_entity_leaks"] == []
    with HostedBookStore(tenant_dsn, OwnerId(account)).book(
            str(_library_book(tenant_dsn, account))) as everything:
        assert score_recap(everything, 2, REMEMBERED)["future_entity_leaks"] == ["Zephyrine"]

    platform = shelf.app.state.platform_client
    cleared = StandIns(platform, monkeypatch, reply=f"{tline(1, 2)} {REMEMBERED}")
    assert sentences(someone.ask(book)) == [tline(1, 2), REMEMBERED]
    assert "Zephyrine" not in "\n".join(cleared.prompts)     # the writer was never told the name
    StandIns(platform, monkeypatch, reply=f"{tline(1, 2)} {REMEMBERED}",
             unsupported=("Zephyrine",))
    assert sentences(someone.ask(book)) == [tline(1, 2)]


def _library_book(dsn: str, account) -> uuid.UUID:
    with psycopg.connect(dsn) as conn, conn.transaction():
        conn.execute("SELECT set_config('app.owner_id', %s, true)", (str(account),))
        return conn.execute("SELECT id FROM books WHERE owner_id=%s AND deleted_at IS NULL",
                            (account,)).fetchone()[0]


# ---- 7: nothing between readers ------------------------------------------------------------------

def test_the_edition_tells_no_reader_anything_of_another_and_its_account_is_nobodys(
        shelf, library_dsn, tenant_dsn, database, worker_dsn, monkeypatch):
    """The break it catches: library memory that appears for a title only once someone has read
    it, or differs between readers before anyone reads; a reader's request that changes the
    edition's rows or makes a job of theirs; one reader's place, chat or memory reaching another
    reader of the same edition; or the library account signed into or listed to a reader."""
    account, models, *_ = built_edition(shelf, library_dsn, worker_dsn, monkeypatch)
    a, b = reader(shelf, "r5a"), reader(shelf, "r2a")
    book_a, book_b = add(a), add(b)
    before = [get(someone, f"/api/books/{book}/graph").json()
              for someone, book in ((a, book_a), (b, book_b))]
    assert before == [{"as_of_chapter": 0, "characters": [], "relationships": []}] * 2
    unbuilt = add(a, ALICE)              # a title nobody built: the same for everyone, too
    assert get(a, f"/api/books/{unbuilt}/graph").status_code == 404

    edition = everything_of(database, account)
    calls = list(models.calls)
    a.mark(book_a, [1, 2, 3, 4, 5])
    b.mark(book_b, [1, 2])
    platform = shelf.app.state.platform_client
    StandIns(platform, monkeypatch, reply=f"{tline(5, 1)} Reader A's own line.")
    assert sentences(a.ask(book_a)) == [tline(5, 1), "Reader A's own line."]
    for path in ("graph", "notes", "ingest", "conversation", "read-map"):
        assert get(a, f"/api/books/{book_a}/{path}").status_code == 200, path
    refused = a.call("POST", f"/api/books/{book_a}/memory-corrections", {
        "source_entity_id": str(uuid.uuid4()), "canonical_name": "Someone Else",
        "reason": "a typo", "bookmark": 5})
    assert refused.status_code == 409 and refused.json()["detail"] == LIBRARY_MEMORY_FIXED
    for own in ("memory", "memory-corrections"):
        shown = get(a, f"/api/books/{book_a}/{own}").json()
        assert not [name for name in NAMES if name in json.dumps(shown)], own
    copy_of(a.call("GET", "/api/account/export"))
    assert everything_of(database, account) == edition and models.calls == calls
    assert jobs_of(database, a.owner) == [] == jobs_of(database, b.owner)

    # Reader B sees nothing of A: not A's place, chat or what A was handed.
    assert through(tenant_dsn, b.owner, book_b) == 2
    assert get(b, f"/api/books/{book_b}/graph").json()["as_of_chapter"] == 2
    assert get(b, f"/api/books/{book_b}/conversation").json()["turns"] == []
    b_copy = "\n".join(copy_of(b.call("GET", "/api/account/export")).values())
    assert "Reader A's own line." not in b_copy and "Zephyrine" not in b_copy
    assert book_a not in b_copy

    # The library account: never signed into, listed to no reader.
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
    library_book = str(_library_book(tenant_dsn, account))
    for someone in (a, b):
        listed = get(someone, "/api/books").text + get(someone, f"/api/classics/{PRIDE}").text
        assert library_book not in listed and str(account) not in listed


# ---- 8: the operator's build ---------------------------------------------------------------------

def test_the_command_builds_a_whole_title_once_carries_on_a_half_built_one_and_pays_its_own_way(
        shelf, library_dsn, tenant_dsn, database, worker_dsn, monkeypatch):
    """The break it catches: a build that stops at a read map the library account does not have,
    builds a title twice or starts a half-built one again (paying again), spends a reader's
    allowance, or ignores the library account's own caps or the platform's day."""
    library = PostgresLibraryAccount(library_dsn)
    library.check_runtime_role()
    extraction = extraction_client()
    models = Writer(extraction, monkeypatch)
    state = command(shelf)
    readers_before = {label: everything_of(database, owner)
                      for label, owner in shelf.people.items()}

    # Its own caps: none of its day left, so the build waits whole for tomorrow.
    [queued] = build(state, library, [PRIDE])
    account = library.account()
    assert (queued.title, queued.chapters, queued.built_through, queued.state) == (
        "The Long Lamp Book", PARTS, 0, "building")
    update_limits(database, account, {"max_spend_usd_per_day": Decimal("0")})
    work = worker(shelf.app, extraction, worker_dsn)
    assert drain(work, 60) == 0 and models.calls == []
    [waiting] = report(state, library)
    assert (waiting.built_through, waiting.cost_usd, waiting.state) == (
        0, Decimal(0), "waiting for tomorrow")

    # The platform's day bounds it too.
    update_limits(database, account, {"max_spend_usd_per_day": Decimal("10")})
    with psycopg.connect(database) as conn:
        conn.execute("UPDATE jobs SET run_after=now() WHERE owner_id=%s", (account,))
        # Other readers have spent the platform's whole day.
        conn.execute("INSERT INTO platform_spend_days (day,settled_usd)"
                     " VALUES ((now() AT TIME ZONE 'UTC')::date,2)")
    before = spend_limits()
    set_spend_limits(reader_usd_per_day=before.reader_usd_per_day,
                     reader_usd_per_month=before.reader_usd_per_month,
                     platform_usd_per_day=Decimal("1"))
    assert work.run_once()
    assert models.calls == [] and built(database, account) == []
    set_spend_limits(reader_usd_per_day=before.reader_usd_per_day,
                     reader_usd_per_month=before.reader_usd_per_month,
                     platform_usd_per_day=before.platform_usd_per_day)
    with psycopg.connect(database) as conn:
        conn.execute("UPDATE jobs SET run_after=now() WHERE owner_id=%s AND state='pending'",
                     (account,))

    # Half built, then run again: it carries on from where it was.
    assert drain(work, 4) == 4
    assert built(database, account) == [(n, "body") for n in range(1, 5)]
    [half] = build(state, library, [PRIDE])
    assert (half.built_through, half.state) == (4, "building")
    assert drain(work, 60) == PARTS - 4
    [whole] = build(state, library, [PRIDE])
    assert (whole.built_through, whole.chapters, whole.unremembered, whole.state) == (
        PARTS, PARTS, 0, "built")
    assert whole.cost_usd > 0
    assert drain(work, 60) == 0
    assert models.parts() == list(range(1, PARTS + 1))        # each chapter extracted once
    with psycopg.connect(database) as conn:
        assert conn.execute("SELECT count(*) FROM books WHERE owner_id=%s",
                            (account,)).fetchone()[0] == 1
        assert conn.execute("SELECT sum(usd) FROM cost_ledger WHERE owner_id=%s",
                            (account,)).fetchone()[0] == whole.cost_usd
        spenders = {row[0] for row in conn.execute("SELECT DISTINCT owner_id FROM cost_ledger")}
        assert spenders == {account}
    assert {label: everything_of(database, owner) for label, owner in shelf.people.items()} \
        == readers_before
    assert whole.line() == (f"{PRIDE} The Long Lamp Book: built through chapter {PARTS} of "
                            f"{PARTS}, 0 unremembered, ${whole.cost_usd:.4f}, built")
    # Asked for an entry the library does not have: said so, nothing else done.
    [missing] = build(state, library, ["99999999"])
    assert missing.line() == "99999999: not in the free library"
    assert library.account() == account


def test_three_unremembered_chapters_in_a_row_stop_a_book_for_the_library_and_for_readers(
        shelf, library_dsn, tenant_dsn, database, worker_dsn, people, tmp_path, monkeypatch):
    """The break it catches: a model failing every chapter, each chapter paid for twice and kept
    unremembered, chapter after chapter, quietly: for the library's build and for a reader's own
    book alike; or a stopped book started again by the next run or the next build."""
    account, models, extraction, work = built_edition(shelf, library_dsn, worker_dsn,
                                                      monkeypatch, reject={4, 5, 6})
    assert built(database, account) == [*[(n, "body") for n in (1, 2, 3)],
                                        *[(n, "unremembered") for n in (4, 5, 6)]]
    assert models.parts() == [1, 2, 3, *[4] * 4, *[5] * 4, *[6] * 4]
    stopped = [row for row in jobs_of(database, account) if row["state"] == "failed"
               and row["sanitized_error"]["code"] == "too_many_unremembered"]
    assert [row["sanitized_error"] for row in stopped] == [
        {"code": "too_many_unremembered", "retryable": False}]
    assert not [row for row in jobs_of(database, account) if row["state"] == "pending"]
    with psycopg.connect(database) as conn:
        assert conn.execute("SELECT count(*) FROM audit_events WHERE owner_id=%s AND"
                            " action='job.fail' AND metadata->>'reason_code'="
                            "'too_many_unremembered'", (account,)).fetchone()[0] == 1
    library = PostgresLibraryAccount(library_dsn)
    [edition] = report(command(shelf), library)
    assert (edition.built_through, edition.unremembered, edition.state) == (6, 3, STOPPED)
    calls = list(models.calls)
    assert build(command(shelf), library, [PRIDE])[0].state == STOPPED
    assert drain(work, 60) == 0
    # Asked again by hand: the run stops before anything is spent.
    with psycopg.connect(database) as conn:
        library_book = conn.execute("SELECT id FROM books WHERE owner_id=%s",
                                    (account,)).fetchone()[0]
    assert StoryMemoryQueue(shelf.app.state.companion_dsn, OwnerId(account)).ensure(
        str(library_book))
    assert drain(work, 60) == 1 and models.calls == calls
    assert built(database, account)[-1] == (6, "unremembered")

    # A reader's own book, with their own memory on: the same rule.
    client, tokens, app = memory_app_for(shelf.tenant_dsn, people, tmp_path / "own", **MEMORY)
    with client:
        own = extraction_client()
        writer = Writer(own, monkeypatch, reject={2, 3, 4})
        someone = Reader(client, tokens, "u", people["u"])
        book = read_map_gate.upload(client, tokens, "u", LONG)
        someone.mark(book, list(range(1, 9)))
        drain(worker(app, own, worker_dsn), 60)
        assert built(database, someone.owner) == [(1, "body"), *[(n, "unremembered")
                                                                  for n in (2, 3, 4)]]
        assert writer.parts() == [1, *[2] * 4, *[3] * 4, *[4] * 4]
        assert {row["sanitized_error"]["code"] for row in jobs_of(database, someone.owner)
                if row["state"] == "failed"} >= {"too_many_unremembered"}
        calls = list(writer.calls)
        someone.mark(book, [9])
        drain(worker(app, own, worker_dsn), 60)
        assert writer.calls == calls and len(built(database, someone.owner)) == 4


def test_the_library_role_is_exactly_what_the_command_needs(library_dsn, database, admin_dsn):
    """The break it catches: a library role that can read or change a reader's rows, bypasses
    row security, or is missing what it needs; or a second library account."""
    library = PostgresLibraryAccount(library_dsn)
    library.check_runtime_role()
    with psycopg.connect(database, autocommit=True) as conn:
        role = conninfo.conninfo_to_dict(library_dsn)["user"]
        conn.execute(sql.SQL("GRANT SELECT ON chapters TO {}").format(sql.Identifier(role)))
        with pytest.raises(LibraryConfigurationError):
            library.check_runtime_role()
        conn.execute(sql.SQL("REVOKE SELECT ON chapters FROM {}").format(sql.Identifier(role)))
    account = library.ensure_account()
    assert library.ensure_account() == account == library.account()
    with psycopg.connect(database, row_factory=dict_row) as conn:
        assert conn.execute("SELECT status,email,display_name FROM users WHERE id=%s",
                            (account,)).fetchone() == {
            "status": "suspended", "email": None, "display_name": "Mothli library"}
        assert conn.execute("SELECT count(*) AS n FROM owner_limits WHERE owner_id=%s",
                            (account,)).fetchone()["n"] == 1
        other = uuid.uuid4()
        conn.execute("INSERT INTO users (id,display_name) VALUES (%s,'Someone')", (other,))
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute("INSERT INTO library_account (account_id) VALUES (%s)", (other,))


# ---- 9: erasure, removal and the purge with library rows present ---------------------------------

def test_removing_a_book_and_erasing_a_reader_leave_the_edition_and_take_all_of_theirs(
        shelf, library_dsn, tenant_dsn, database, worker_dsn, admin_dsn, monkeypatch):
    """The break it catches: a reader's removal or erasure reaching the library's rows (every
    other reader of the title would lose its memory), leaving anything of the reader's behind,
    or the purge erasing the library account."""
    account, *_ = built_edition(shelf, library_dsn, worker_dsn, monkeypatch)
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


def test_the_place_follows_the_read_map_in_every_path_and_nothing_else_writes_it(
        shelf, library_dsn, tenant_dsn, database, worker_dsn, monkeypatch):
    """The break it catches: ``memory_through`` left behind by a read-map path (a page that
    finishes a chapter, chapters marked read or not read, a book added again), so the door hands
    out less, or more, than the map says was read; or a second writer of the column."""
    built_edition(shelf, library_dsn, worker_dsn, monkeypatch)
    a = reader(shelf, "r5a")
    book = add(a)
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
    again = add(a)
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


def test_the_door_refuses_a_reader_whose_split_differs_from_the_editions_or_is_unknown(
        shelf, library_dsn, tenant_dsn, database, worker_dsn, monkeypatch):
    """The break it catches: the door matching on the file alone, so a copy of the very same file
    split another way (numbered one on, as another version of the splitter could number it) is
    handed the edition's chapter 4 as its chapter 4; or a reader's book, or the edition, whose
    split is not known being matched rather than refused. Each refusal is shown to be the split's
    alone: putting the fingerprint back hands the reader exactly what it did before."""
    account, *_ = built_edition(shelf, library_dsn, worker_dsn, monkeypatch)
    edition = _library_book(tenant_dsn, account)
    a = reader(shelf, "r5a")
    book = add(a)
    a.mark(book, [1, 2, 3])
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
            set_split(owner, which, value)
            assert door(tenant_dsn, a.owner, book) is None, (owner, value)
            assert door(database, a.owner, book) is None, (owner, value)
            assert not HostedBookStore(tenant_dsn, OwnerId(a.owner)).shared(book)
            assert get(a, f"/api/books/{book}/graph").status_code == 404
        set_split(owner, which, split_hash)
        assert door(tenant_dsn, a.owner, book) == before

    # An import of the same bytes whose splitter numbers every chapter one on: same file,
    # another split, refused; the reader keeps nothing of the edition's.
    real = hosted_api.split_hash_of
    monkeypatch.setattr(hosted_api, "split_hash_of", lambda chapters, book_id: real(
        [{**chapter, "ordinal": chapter["ordinal"] + 1} for chapter in chapters], book_id))
    later = reader(shelf, "r10a")
    shifted = add(later)
    assert fingerprints(database, later.owner, shifted)[0] == file_hash
    assert fingerprints(database, later.owner, shifted)[1] not in (split_hash, None)
    later.mark(shifted, [1, 2, 3, 4])
    assert door(tenant_dsn, later.owner, shifted) is None
    assert door(database, later.owner, shifted) is None
    assert get(later, f"/api/books/{shifted}/graph").status_code == 404
    assert door(tenant_dsn, a.owner, book) == before
