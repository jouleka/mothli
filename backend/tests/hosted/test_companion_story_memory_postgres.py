"""The story memory screens in hosted mode (ADR 0042 part 4), against real PostgreSQL.

The cast (``/graph``), a person's card (``/character/{id}``), the story so far (``/notes``) and
progress (``/ingest``), served from a reader's own memory rows. Nothing extracts in hosted mode
yet (ADR 0042 parts 2 and 3), so the rows are seeded here the way the parity suite seeds them.
Two readers through the hosted HTTP app on the RLS-enforced tenant role, and again through a
superuser connection where only the code's own predicates keep them apart; the reader's place
decided by the read map even when a device has pushed the chapter counter ahead; and the
"waiting" and "no memory" answers the panels show.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from app.hosted.auth.api import CSRF_HEADER
from app.hosted.companion.source import HostedBookSource
from app.hosted.companion.store import HostedCompanionCatalog
from app.hosted.tenant.models import OwnerId
from app.hosted.tenant.repository import PostgresTenantRepository
from app.main import create_app
from tests._alice_shaped import alice_shaped_epub
from tests.hosted import test_tenant_postgres as tenant_gate
from tests.hosted.test_companion_read_map_postgres import hosted_settings, signed_in, upload
from tests.hosted.test_tenant_postgres import UnusedOIDCClient, _become

pytestmark = pytest.mark.postgres

admin_dsn = tenant_gate.admin_dsn
database = tenant_gate.database
tenant_dsn = tenant_gate.tenant_dsn

ROUTES = ("/graph", "/character/{person}", "/notes", "/ingest")

# The memory extraction would publish for the Lamp Tales book (``tests/_alice_shaped.py``):
# chapter 1 is its cover and 2 Project Gutenberg's own page, so only 3 to 5 are the book proper.
# Each kind of row the screens read, at a chapter before, at and after the places walked.
TITLES = {1: "Cover", 2: "Gutenberg's page", 3: "Down the Lamp-Hole", 4: "The Pool of Oil",
          5: "A Wick-Race"}
SUMMARIES = {2: "Gutenberg's page lists the chapters.", 3: "Mara falls into the lamp-hole.",
             4: "Corvus finds a pool of oil.", 5: "Vex wins the wick-race."}
# (key, name, type, revealed_at, invalid_at, retracted)
PEOPLE = [
    ("reader", "Gutenberg Reader", "character", 2, None, False),   # on a page that is no chapter
    ("mara", "Mara", "character", 3, None, False),
    ("hole", "The Lamp-Hole", "place", 3, None, False),
    ("wick", "Old Wick", "character", 3, 4, False),               # gone from chapter 4 on
    ("corvus", "Corvus", "character", 4, None, False),
    ("father", "Father Corvus", "character", 4, None, False),     # the same man, split
    ("ghost", "Ghost", "character", 4, None, True),               # withdrawn
    ("vex", "Vex", "character", 5, None, False),                  # met last
]
# (person, surface, revealed_at, retracted)
ALIASES = [("mara", "Mar", 3, False), ("father", "the Father", 4, False),
           ("mara", "Lampy", 4, True), ("corvus", "Cor", 5, False),
           ("vex", "the Lamplighter", 5, False)]
# (key, src, dst, type, label, revealed_at, invalid_at)
TIES = [
    ("reads", "reader", "mara", "other", "reads about", 2, None),
    ("lives", "mara", "hole", "other", "lives in", 3, None),
    ("dreams", "mara", "vex", "love", "dreams of", 3, None),       # of someone not met yet
    ("walks", "mara", "corvus", "friend", "walks with", 4, 5),     # ends in chapter 5
    ("blesses", "father", "mara", "family", "blesses", 4, None),
    ("argues", "mara", "corvus", "rival", "argues with", 5, None),
]
# (summary, revealed_at, order_idx, kind, invalid_at)
EVENTS = [("A cover is printed.", 2, 1, "beat", None),
          ("Mara falls down the lamp-hole.", 3, 1, "beat", None),
          ("A rumour of oil spreads.", 3, 2, "rumor", 4),
          ("Corvus finds the pool.", 4, 1, "beat", None),
          ("Vex wins the wick-race.", 5, 1, "beat", None)]
# (person, status, revealed_at, invalid_at)
STATES = [("mara", {"mood": "curious"}, 3, 5), ("corvus", {"place": "the pool"}, 4, None),
          ("mara", {"mood": "brave"}, 5, None)]


def _digest(ordinal: int) -> str:
    return hashlib.sha256(f"chapter {ordinal}".encode()).hexdigest()


def book_keys(app, owner: uuid.UUID, book: str) -> dict[int, str]:
    """The book's own chapter keys (``<book id>:<file>``), which memory chapters must carry."""
    catalog = HostedCompanionCatalog(app.state.companion_dsn, OwnerId(owner))
    source = HostedBookSource(app.state.object_storage, catalog, app.state.companion_sources)
    manifest, _chapters = source.verified_source(book, catalog.get_book(book))
    return {atom["ordinal"]: atom["key"] for atom in manifest["atoms"]}


def seed_story_memory(dsn: str, owner: uuid.UUID, book: str, keys: dict[int, str], *,
                      receipts=(1, 2, 3, 4), people=PEOPLE, aliases=ALIASES, ties=TIES,
                      events=EVENTS, states=STATES) -> dict[str, str]:
    """Write the memory rows past row-level security, as the parity suite does; ``{key: id}``.

    Every row hangs from chapter 3's receipt (the schema wants a receipt behind each). Rows are
    recorded a millisecond apart in the order given, as extraction publishes them."""
    ids: dict[str, str] = {}
    clock = iter(datetime(2026, 1, 1, tzinfo=UTC) + timedelta(milliseconds=n)
                 for n in range(10_000))
    with psycopg.connect(dsn) as conn, conn.transaction():
        incarnation = conn.execute("SELECT incarnation FROM books WHERE owner_id=%s AND id=%s",
                                   (owner, book)).fetchone()[0]
        scope = (owner, uuid.UUID(book), incarnation)
        chapters = {}
        for ordinal, key in keys.items():
            chapters[ordinal] = uuid.uuid4()
            conn.execute("INSERT INTO chapters (owner_id,book_id,book_incarnation,id,chapter_key,"
                         "revealed_at,title,content_hash) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                         (*scope, chapters[ordinal], key, ordinal, TITLES.get(ordinal),
                          _digest(ordinal)))
            conn.execute("INSERT INTO ingested_chapters (owner_id,book_id,book_incarnation,"
                         "chapter_id,content_hash,completed_at) VALUES (%s,%s,%s,%s,%s,now())",
                         (*scope, chapters[ordinal], _digest(ordinal)))
            if ordinal not in receipts:   # written, then withdrawn: this chapter is not complete
                conn.execute("UPDATE ingested_chapters SET retracted_at=now() WHERE owner_id=%s "
                             "AND book_id=%s AND chapter_id=%s", (owner, scope[1],
                                                                  chapters[ordinal]))
        anchor = chapters[3]
        for ordinal, summary in SUMMARIES.items():
            if ordinal in chapters:
                for kind in ("chapter", "rolling-recap"):
                    conn.execute("INSERT INTO chapter_summaries (owner_id,book_id,book_incarnation,"
                                 "id,source_chapter_id,kind,summary,revealed_at) "
                                 "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                                 (*scope, uuid.uuid4(), chapters[ordinal], kind, summary, ordinal))
        for key, name, kind, revealed_at, invalid_at, retracted in people:
            ids[key] = str(uuid.uuid4())
            conn.execute("INSERT INTO entities (owner_id,book_id,book_incarnation,id,"
                         "source_chapter_id,canonical_name,entity_type,revealed_at,invalid_at,"
                         "recorded_at,retracted_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                         (*scope, ids[key], anchor, name, kind, revealed_at, invalid_at,
                          next(clock), datetime(2026, 2, 1, tzinfo=UTC) if retracted else None))
        for person, surface, revealed_at, retracted in aliases:
            conn.execute("INSERT INTO aliases (owner_id,book_id,book_incarnation,id,entity_id,"
                         "source_chapter_id,surface_form,revealed_at,recorded_at,retracted_at) "
                         "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                         (*scope, uuid.uuid4(), ids[person], anchor, surface, revealed_at,
                          next(clock), datetime(2026, 2, 1, tzinfo=UTC) if retracted else None))
        for key, src, dst, kind, label, revealed_at, invalid_at in ties:
            ids[key] = str(uuid.uuid4())
            conn.execute("INSERT INTO edges (owner_id,book_id,book_incarnation,id,"
                         "source_chapter_id,src_entity_id,dst_entity_id,relationship_type,label,"
                         "revealed_at,invalid_at,recorded_at) "
                         "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                         (*scope, ids[key], anchor, ids[src], ids[dst], kind, label, revealed_at,
                          invalid_at, next(clock)))
        for summary, revealed_at, order_idx, kind, invalid_at in events:
            conn.execute("INSERT INTO events (owner_id,book_id,book_incarnation,id,"
                         "source_chapter_id,order_idx,summary,kind,revealed_at,invalid_at,"
                         "recorded_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                         (*scope, uuid.uuid4(), anchor, order_idx, summary, kind, revealed_at,
                          invalid_at, next(clock)))
        for person, status, revealed_at, invalid_at in states:
            conn.execute("INSERT INTO entity_state (owner_id,book_id,book_incarnation,id,"
                         "entity_id,source_chapter_id,status,revealed_at,invalid_at,recorded_at) "
                         "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                         (*scope, uuid.uuid4(), ids[person], anchor, Jsonb(status), revealed_at,
                          invalid_at, next(clock)))
    return ids


def complete_chapter(dsn: str, owner: uuid.UUID, book: str, ordinal: int) -> None:
    """Chapter ``ordinal``'s memory becomes complete: its receipt is live again."""
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "UPDATE ingested_chapters AS receipt SET retracted_at=NULL FROM chapters AS chapter "
            "WHERE (chapter.owner_id,chapter.book_id,chapter.id)"
            "=(receipt.owner_id,receipt.book_id,receipt.chapter_id) "
            "AND chapter.owner_id=%s AND chapter.book_id=%s AND chapter.revealed_at=%s",
            (owner, book, ordinal))


class Shelf:
    """The hosted app for readers A and B, on the given database role."""

    def __init__(self, dsn: str, readers: dict, tmp_path, *, superuser: bool) -> None:
        self.tokens, sessions = signed_in(readers)
        extra = {"tenant_repository": _SuperuserRepository(dsn)} if superuser else {}
        self.app = create_app(hosted_settings(dsn, tmp_path / "objects"),
                              auth_repository=sessions, oidc_client=UnusedOIDCClient(),
                              auth_clock=lambda: datetime(2026, 7, 16, 12, tzinfo=UTC), **extra)
        self.client = TestClient(self.app, base_url="https://reader.example")

    def __enter__(self):
        self.client.__enter__()
        return self

    def __exit__(self, *exc):
        self.client.__exit__(*exc)

    def as_reader(self, label: str) -> str:
        return _become(self.client, self.tokens, label)

    def get(self, label: str, book: str, route: str, person: str = "", **query):
        csrf = self.as_reader(label)
        return self.client.get(f"/api/books/{book}" + route.format(person=person), params=query,
                               headers={CSRF_HEADER: csrf})

    def read(self, label: str, book: str, atoms, read=True) -> None:
        csrf = self.as_reader(label)
        answer = self.client.put(f"/api/books/{book}/read-chapters",
                                 json={"atoms": list(atoms), "read": read},
                                 headers={CSRF_HEADER: csrf})
        assert answer.status_code == 200, answer.text


class _SuperuserRepository(PostgresTenantRepository):
    """The real repository on a superuser connection (row-level security does not apply)."""

    async def check_runtime_role(self) -> None:
        return None


def _cast(answer) -> list[str]:
    return [person["canonical_name"] for person in answer.json()["characters"]]


def _readers_shelf(shelf: Shelf, database: str, readers: dict):
    """A's book with A's memory, read through chapter 4; B's own copy of the same file with a
    memory of B's own; and a book id of nobody's."""
    book_a = upload(shelf.client, shelf.tokens, "a", alice_shaped_epub())
    ids_a = seed_story_memory(database, readers["a"], book_a,
                              book_keys(shelf.app, readers["a"], book_a))
    shelf.read("a", book_a, [3, 4])
    book_b = upload(shelf.client, shelf.tokens, "b", alice_shaped_epub())
    ids_b = seed_story_memory(database, readers["b"], book_b,
                              book_keys(shelf.app, readers["b"], book_b),
                              people=[("zina", "Zinaida", "character", 3, None, False)],
                              aliases=[], ties=[], events=[], states=[])
    shelf.read("b", book_b, [3, 4])
    return book_a, ids_a, book_b, ids_b


@pytest.mark.parametrize("role", ["tenant", "superuser"])
def test_second_reader_gets_first_readers_story_memory_as_missing_on_every_route(
    role, tenant_dsn: str, database: str, tmp_path
) -> None:
    """The break it catches: any of the four routes that tells reader B that A's book exists (a
    status or body other than a random id's), shows B any of A's people, ties, notes or progress,
    or mixes A's rows into B's own copy of the same file. Run on the RLS-enforced tenant role and
    on a superuser connection, where only the routes' own owner predicates stand between them."""
    readers = {"a": uuid.uuid4(), "b": uuid.uuid4()}
    with psycopg.connect(database) as conn:
        for label, owner in readers.items():
            conn.execute("INSERT INTO users (id,display_name) VALUES (%s,%s)",
                          (owner, f"Reader {label.upper()}"))
    dsn = tenant_dsn if role == "tenant" else database
    with Shelf(dsn, readers, tmp_path, superuser=role == "superuser") as shelf:
        book_a, ids_a, book_b, ids_b = _readers_shelf(shelf, database, readers)
        seen_by_a = [shelf.get("a", book_a, route, ids_a["mara"]).json() for route in ROUTES]
        assert _cast(shelf.get("a", book_a, "/graph")) == ["Mara", "Corvus"]
        missing = str(uuid.uuid4())
        for route in ROUTES:
            for person in (ids_a["mara"], ids_a["corvus"], missing):
                hidden = shelf.get("b", book_a, route, person)
                absent = shelf.get("b", missing, route, person)
                assert (hidden.status_code, hidden.json()) == (absent.status_code, absent.json())
                assert (hidden.status_code, hidden.json()) == (404, {"detail": "unknown book"})
                assert hidden.headers["cache-control"] == "private, no-store"
        # B's own copy of the same file holds B's rows only, and A's people are no one there.
        assert _cast(shelf.get("b", book_b, "/graph")) == ["Zinaida"]
        notes = shelf.get("b", book_b, "/notes").json()
        assert [person["name"] for person in notes["cast"]] == ["Zinaida"]
        assert shelf.get("b", book_b, "/ingest").json()["ingest_progress"] == 4
        assert shelf.get("b", book_b, "/character/{person}", ids_b["zina"]).status_code == 200
        for person in (ids_a["mara"], ids_a["vex"]):
            card = shelf.get("b", book_b, "/character/{person}", person)
            assert (card.status_code, card.json()) == (404, {"detail": "unknown character"})
        # And nothing of B's reached A.
        assert [shelf.get("a", book_a, route, ids_a["mara"]).json() for route in ROUTES] \
            == seen_by_a


def test_the_place_is_the_read_map_even_when_a_device_pushes_the_counter_ahead(
    tenant_dsn: str, database: str, tmp_path
) -> None:
    """The break it catches: views clamped to the chapter counter alone, which a stale device's
    position report raises past what the read map says was read (open items, ADR 0041 part 1):
    the reader would see people of chapters they have not finished."""
    readers = {"a": uuid.uuid4(), "b": uuid.uuid4()}
    with psycopg.connect(database) as conn:
        for owner in readers.values():
            conn.execute("INSERT INTO users (id,display_name) VALUES (%s,'Reader')", (owner,))
    with Shelf(tenant_dsn, readers, tmp_path, superuser=False) as shelf:
        book = upload(shelf.client, shelf.tokens, "a", alice_shaped_epub())
        ids = seed_story_memory(database, readers["a"], book, book_keys(shelf.app, readers["a"],
                                                                         book),
                                receipts=(1, 2, 3, 4, 5))
        shelf.read("a", book, [3])
        with psycopg.connect(database) as conn:
            conn.execute("UPDATE reading_state SET bookmark=5 WHERE owner_id=%s AND book_id=%s",
                         (readers["a"], book))
        graph = shelf.get("a", book, "/graph", bookmark=5)
        assert graph.json()["as_of_chapter"] == 3
        assert _cast(graph) == ["Mara", "Old Wick"]
        assert shelf.get("a", book, "/notes").json()["as_of_chapter"] == 3
        assert shelf.get("a", book, "/character/{person}", ids["corvus"]).status_code == 404
        # The read map moves the place, both ways.
        shelf.read("a", book, [4, 5])
        assert "Vex" in _cast(shelf.get("a", book, "/graph"))
        shelf.read("a", book, [4], read=False)
        assert _cast(shelf.get("a", book, "/graph")) == ["Mara", "Old Wick"]


def _allow_story_memory_jobs(database: str) -> None:
    """Stands in for ADR 0042 part 3's migration, which adds the ``story_memory`` job kind."""
    with psycopg.connect(database) as conn:
        conn.execute("ALTER TABLE jobs DROP CONSTRAINT jobs_kind_ck")
        conn.execute("ALTER TABLE jobs ADD CONSTRAINT jobs_kind_ck CHECK (kind IN "
                     "('ingest_book','chapter_notes','recollect','story_memory'))")


def _memory_job(database: str, owner: uuid.UUID, book: str, *, state: str, run_after: str):
    with psycopg.connect(database) as conn:
        incarnation = conn.execute("SELECT incarnation FROM books WHERE id=%s",
                                   (book,)).fetchone()[0]
        conn.execute("DELETE FROM jobs WHERE owner_id=%s AND kind='story_memory'", (owner,))
        conn.execute(
            "INSERT INTO jobs (owner_id,book_id,book_incarnation,kind,state,idempotency_key,"
            f"run_after) VALUES (%s,%s,%s,'story_memory',%s,%s,{run_after})",
            (owner, book, incarnation, state, f"story-memory:{uuid.uuid4()}"))


def test_progress_says_running_waiting_or_nothing_and_a_book_with_no_memory_is_absent(
    tenant_dsn: str, database: str, tmp_path
) -> None:
    """The break it catches: a book with no story memory answered as an empty one (the panels
    would say "no one yet" where they should say Mothli keeps no list), a memory waiting for the
    reader's next day reported as running (the app would poll all night) or as idle (the panels
    could not say Mothli will catch up tomorrow), and another reader's job seen in B's progress."""
    readers = {"a": uuid.uuid4(), "b": uuid.uuid4()}
    with psycopg.connect(database) as conn:
        for owner in readers.values():
            conn.execute("INSERT INTO users (id,display_name) VALUES (%s,'Reader')", (owner,))
    _allow_story_memory_jobs(database)
    with Shelf(tenant_dsn, readers, tmp_path, superuser=False) as shelf:
        book = upload(shelf.client, shelf.tokens, "a", alice_shaped_epub())
        shelf.read("a", book, [3])
        for route in ROUTES:
            answer = shelf.get("a", book, route, str(uuid.uuid4()))
            assert (answer.status_code, answer.json()) == (
                404, {"detail": "no story memory for this book"}), route
        # Being built, nothing published yet: an empty cast, and progress the app polls.
        _memory_job(database, readers["a"], book, state="running", run_after="now()")
        assert shelf.get("a", book, "/graph").json() == {
            "as_of_chapter": 0, "characters": [], "relationships": []}
        assert shelf.get("a", book, "/ingest").json() == {
            "ingest_progress": 0, "status": "running", "flags": [], "error": None}
        _memory_job(database, readers["a"], book, state="pending",
                    run_after="now() + interval '15 seconds'")
        assert shelf.get("a", book, "/ingest").json()["status"] == "running"
        # Capped: the claim moved it to the next UTC midnight.
        _memory_job(database, readers["a"], book, state="pending",
                    run_after="(date_trunc('day', now() AT TIME ZONE 'UTC') + interval '1 day') "
                              "AT TIME ZONE 'UTC'")
        seed_story_memory(database, readers["a"], book, book_keys(shelf.app, readers["a"], book),
                          receipts=(1, 2, 3))
        assert shelf.get("a", book, "/ingest").json() == {
            "ingest_progress": 3, "status": "waiting", "flags": [], "error": None}
        # B's own book of the same file: no memory and none of A's job.
        book_b = upload(shelf.client, shelf.tokens, "b", alice_shaped_epub())
        answer = shelf.get("b", book_b, "/ingest")
        assert (answer.status_code, answer.json()) == (
            404, {"detail": "no story memory for this book"})
        # A finished job is no job: the memory is simply there.
        with psycopg.connect(database) as conn:
            conn.execute("UPDATE jobs SET state='succeeded',completed_at=now() WHERE owner_id=%s",
                         (readers["a"],))
        assert shelf.get("a", book, "/ingest").json()["status"] == "idle"


def test_the_story_memory_screens_refuse_a_memory_built_from_another_split_of_the_book(
    tenant_dsn: str, database: str, tmp_path
) -> None:
    """The break it catches: memory rows whose chapters are not the book's own chapters at the
    same places (another split of the file) served anyway, as if their stamps meant this book's
    chapters (the local ``assert_matches_store`` check, which hosted sources lacked)."""
    readers = {"a": uuid.uuid4(), "b": uuid.uuid4()}
    with psycopg.connect(database) as conn:
        for owner in readers.values():
            conn.execute("INSERT INTO users (id,display_name) VALUES (%s,'Reader')", (owner,))
    with Shelf(tenant_dsn, readers, tmp_path, superuser=False) as shelf:
        book = upload(shelf.client, shelf.tokens, "a", alice_shaped_epub())
        keys = book_keys(shelf.app, readers["a"], book)
        seed_story_memory(database, readers["a"], book, {**keys, 4: keys[5], 5: keys[4]})
        shelf.read("a", book, [3, 4])
        for route in ROUTES:
            answer = shelf.get("a", book, route, str(uuid.uuid4()))
            assert (answer.status_code, answer.json()) == (
                409, {"detail": "atom-set mismatch (re-import the book)"}), route
