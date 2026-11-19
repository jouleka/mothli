"""Story memory extracted in hosted mode is the memory local mode builds (ADR 0042 part 2).

One small book (a contents page, which is never sent to a model, then three chapters that meet
new people, give one of them another name, replace her state, and tie people together) is
extracted chapter by chapter twice with the same stand-in writer and checker: by local mode's
shared ``ChapterExtraction`` over the local store, and by ``remember_chapter`` into the edition of
a hosted reader's file through a claimed job (ADR 0044 step 3). After every chapter, at every
bookmark, the unchanged readers must answer the same from both, the hosted side through the door
at the reader's place (the chapter just built): ``score_recap``, ``same_person_names``,
``completion_frontier``, the views the checks use, and the relationships, states, themes and
event participants the cast screens read (with the parity suite's predicates, on the edition's
rows). Stand-in models only: nothing is called and nothing is paid.
"""

from __future__ import annotations

import json
import re
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from app.config import Settings
from app.content_scope import ContentStore
from app.eval.spoiler_gate import score_recap
from app.hosted.companion.jobs import ChapterNotesQueue, _Lease
from app.hosted.companion.memory import HostedBookStore
from app.hosted.companion.source import HostedBookSource
from app.hosted.companion.store import HostedCompanionCatalog
from app.hosted.companion.story_memory import remember_chapter
from app.hosted.jobs import PostgresWorkerRepository, SanitizedFailure
from app.hosted.tenant.models import OwnerId
from app.ingest.chapter_extraction import ChapterExtraction
from app.ingest.extraction.chapter_text import segment_for_ingest
from app.ingest.manifest import load_manifest, source_epub_path
from app.known_names import same_person_names
from app.llm.client import LLMClient
from app.main import create_app
from app.unicode_text import word_tokens
from tests.api._epub import epub_ncx
from tests.hosted import test_companion_read_map_postgres as read_map_gate
from tests.hosted import test_jobs_postgres as jobs_gate
from tests.hosted.test_companion_stream_postgres import Reader
from tests.hosted.test_tenant_postgres import UnusedOIDCClient
from tests.ingest._grounding import canned_grounding_verdict
from tests.parity.test_companion_memory_parity import _audit, _verdict

admin_dsn = read_map_gate.admin_dsn
database = read_map_gate.database
tenant_dsn = read_map_gate.tenant_dsn
readers = read_map_gate.readers
worker_dsn = jobs_gate.worker_dsn

# ---- the book and what the stand-in writer extracts from it ------------------------------------

CHAPTERS = {
    "nets": "Mara kept the harbour light. Corvus mended nets by the quay. "
            "Mara and Corvus were sister and brother. ",
    "ferry": "Vex arrived on the ferry at dusk. Mara, the Keeper, watched Vex from the tower. "
             "Corvus distrusted Vex. ",
    "rescued": "Rose rescued Vex from the storm. The Keeper lit the lantern for them. "
               "Rose and Vex were friends. ",
}


def harbour_book(title: str = "The Harbour Book") -> bytes:
    """Three chapters, with a map page (not story: never sent to a model) after the first. Another
    ``title`` makes another file (another edition) with the same chapters; the same title made
    twice is one file (``epub_ncx`` stamps no clock)."""
    chapter = [(f"c{n}.xhtml", f"Chapter {roman}", f"Chapter {roman}", CHAPTERS[marker] * 6)
               for n, (roman, marker) in enumerate((("I", "nets"), ("II", "ferry"),
                                                    ("III", "rescued")), start=1)]
    return epub_ncx(
        [chapter[0], ("map.xhtml", "Map: The Coast", "Map: The Coast",
                      "The harbour, the quay and the tower. " * 4), *chapter[1:]],
        title=title, author="Ada Writer")


def _person(name, *, aliases=(), state=None, known=False):
    return {"canonical_name": name, "type": "character", "aliases": list(aliases),
            "matched_roster": known, "state": state}


EXTRACTIONS = {
    "nets": {
        "chapter_summary": "Mara kept the harbour light while Corvus mended nets.",
        "entities": [_person("Mara", state="keeping the light"), _person("Corvus")],
        "relationships": [{"src": "Mara", "dst": "Corvus", "rel_type": "family",
                           "label": "sister and brother"}],
        "events": [{"summary": "Mara kept the harbour light.", "participants": ["Mara"]},
                   {"summary": "Corvus mended nets by the quay.", "participants": ["Corvus"]}],
        "themes": [{"name": "Duty", "description": "Keeping the harbour light."}],
    },
    "ferry": {
        "chapter_summary": "Vex arrived on the ferry and Mara, the Keeper, watched him.",
        "entities": [_person("Vex", state="newly arrived"),
                     _person("Mara", aliases=["the Keeper"], state="watching the stranger",
                             known=True),
                     _person("Corvus", known=True)],
        "relationships": [{"src": "Corvus", "dst": "Vex", "rel_type": "rivalry",
                           "label": "distrusts"}],
        "events": [{"summary": "Vex arrived on the ferry at dusk.", "participants": ["Vex"]},
                   {"summary": "Mara watched Vex from the tower.",
                    "participants": ["Mara", "Vex"]}],
        "themes": [{"name": "Suspicion", "description": "Corvus distrusts the newcomer."}],
    },
    "rescued": {
        "chapter_summary": "Rose rescued Vex from the storm.",
        "entities": [_person("Rose", state="rescuer"), _person("Vex", known=True),
                     _person("Mara", aliases=["The Keeper"], state="lighting the lantern",
                             known=True)],
        "relationships": [{"src": "Rose", "dst": "Vex", "rel_type": "social",
                           "label": "friends"}],
        "events": [{"summary": "Rose rescued Vex from the storm.",
                    "participants": ["Rose", "Vex"]},
                   {"summary": "The Keeper lit the lantern.", "participants": ["Mara"]}],
        "themes": [{"name": "Rescue", "description": "Rose saves Vex."}],
    },
}


class Extractor:
    """The stand-in writer and checker for extraction: the scripted extraction of whichever
    chapter the prompt holds, and an accepting review. ``hook(marker)`` runs during an
    extraction call; every call is counted by schema."""

    def __init__(self, client, monkeypatch, *, hook=None, usage=(120, 60)):
        self.calls, self.hook, self.usage = [], hook, usage
        monkeypatch.setattr(client, "complete", self.complete)

    def complete(self, system, user, *, tier, schema):
        self.calls.append(schema.__name__)
        usage = {"in": self.usage[0], "out": self.usage[1]}
        if schema.__name__ == "ExtractionVerdict":
            return canned_grounding_verdict(user), usage
        assert schema.__name__ == "Extraction", schema.__name__
        body = user.split("CHAPTER TEXT:\n", 1)[-1]
        [marker] = [marker for marker in EXTRACTIONS if marker in body]
        if self.hook:
            self.hook(marker)
        return json.loads(json.dumps(EXTRACTIONS[marker])), usage


def extraction_client() -> LLMClient:
    """The platform's extraction client as ADR 0042 section 8 builds it: a paid writer (never
    called: ``complete`` is replaced) and the stub embedder, explicitly."""
    return LLMClient(provider="openai-compatible", api_key="sk-test-not-real",
                     embed_provider="stub", env={}, max_retries=0)


# ---- the two modes -------------------------------------------------------------------------

class Discarding:
    def submit(self, *args, **kwargs):
        return None

    def shutdown(self, wait=True, cancel_futures=False):
        return None


@contextmanager
def local_book(tmp_path, blob: bytes):
    """Local mode: the book imported through the local app; ``extract(n)`` runs the shared
    extraction through chapter n exactly as the local worker does."""
    settings = Settings(_env_file=None, allow_stub=True, data_dir=str(tmp_path / "local"),
                        openai_api_key=None, anthropic_api_key=None, openai_base_url=None,
                        embed_api_key=None, embed_base_url=None, embed_provider="stub",
                        embed_model="lexical-stub-256")
    app = create_app(settings, ingest_executor=Discarding())
    with TestClient(app) as api:
        posted = api.post("/api/books", files={
            "file": ("harbour.epub", blob, "application/epub+zip")})
        assert posted.status_code == 201, posted.text
        book_id = posted.json()["book_id"]
        _result, chapters = segment_for_ingest(source_epub_path(settings.data_dir, book_id),
                                               book_id)
        store = ContentStore(app.state.store, settings.data_dir)

        def extract(client, ordinal):
            incarnation = app.state.catalog.get_book(book_id)["incarnation"]
            ChapterExtraction(app.state.catalog, client, settings, lambda: None,
                              lambda _delay: None).ingest_through(
                book_id, chapters, ordinal, store=store, incarnation=incarnation,
                generation=1, require_incarnation=lambda *_a: None,
                require_worker_incarnation=lambda *_a: None,
                publish_validated=lambda *_a: None, book_lifecycle=lambda _b: _nothing())

        @contextmanager
        def memory():
            # The rows themselves, as the hosted snapshot reads them (no content scope).
            with app.state.store.book(book_id) as mem:
                yield mem

        atoms = [{"key": atom["key"], "ordinal": atom["ordinal"]}
                 for atom in load_manifest(settings.data_dir, book_id)["atoms"]]
        yield extract, memory, atoms, chapters


@contextmanager
def _nothing():
    yield


@pytest.fixture()
def hosted_app(tenant_dsn: str, readers: dict, tmp_path):
    tokens, sessions = read_map_gate.signed_in(readers)
    app = create_app(read_map_gate.hosted_settings(tenant_dsn, tmp_path / "objects"),
                     auth_repository=sessions, oidc_client=UnusedOIDCClient(),
                     auth_clock=lambda: datetime(2026, 7, 16, 12, tzinfo=UTC))
    with TestClient(app, base_url="https://reader.example") as client:
        yield client, tokens, app


def hosted_book(hosted_app, label: str, blob: bytes) -> str:
    client, tokens, _app = hosted_app
    return read_map_gate.upload(client, tokens, label, blob)


def claimed(app, worker_dsn: str, owner: uuid.UUID, book: str, worker: str = "memory-worker"):
    """A claimed, started job of the book's owner, made as the companion's worker makes one
    (until part 3 adds its own kind, a companion job of the existing kind carries the lease)."""
    assert ChapterNotesQueue(app.state.companion_dsn, OwnerId(owner)).ensure(book)
    repository = PostgresWorkerRepository(worker_dsn)
    claim = repository.claim_next(worker_id=worker, lease_for=timedelta(seconds=60),
                                  kinds=frozenset({"chapter_notes"}))
    assert claim is not None and repository.start(claim)
    return repository, claim


def remember(app, worker_dsn, owner, book, client, ordinal, *, claim=None, hold=None,
             tenant=None) -> bool:
    """One run of one chapter under a claimed lease; the job succeeds or fails like a run. The
    chapter is built into the edition of the book's file, made on its first chapter, as the
    story-memory job builds it (ADR 0044 step 3). ``tenant`` replaces the RLS-enforced tenant
    role (a superuser, to test the predicates)."""
    repository, claim = claim or claimed(app, worker_dsn, owner, book)
    lease = _Lease(repository, claim, timedelta(seconds=60))
    try:
        edition = repository.edition_of(claim, create=True)
        published = remember_chapter(
            claim, repository, edition=edition, hold=hold or lease.hold,
            tenant_dsn=tenant or app.state.companion_dsn,
            settings=app.state.settings, client=client, storage=app.state.object_storage,
            sources=app.state.companion_sources, ordinal=ordinal)
    except BaseException:
        try:
            repository.fail(claim, failure=SanitizedFailure("internal_error", retryable=False),
                            retry_after=timedelta(0))
        except Exception:
            pass
        raise
    repository.succeed(claim)
    return published


def hosted_atoms(app, owner, book) -> list[dict]:
    catalog = HostedCompanionCatalog(app.state.companion_dsn, OwnerId(owner))
    manifest, _chapters = HostedBookSource(app.state.object_storage, catalog,
                                           app.state.companion_sources).verified_source(
        book, catalog.get_book(book))
    return [{"key": atom["key"], "ordinal": atom["ordinal"]} for atom in manifest["atoms"]]


# ---- what the unchanged readers answer -----------------------------------------------------

BOOKMARKS = range(0, 6)
SENTENCES = [
    "Mara kept the light.", "Corvus mended the nets.", "Vex arrived on the ferry.",
    "The Keeper watched from the tower.", "Rose rescued Vex.", "Corvus distrusted Vex.",
    "Mara will leave the harbour soon.", "Suspicion grew in the town.",
]
FOCUS = [set(), {"mara"}, {"keeper"}, {"vex"}, {"rose"}]


def _read_text(chapters, bookmark) -> str:
    return " ".join(chapter["text"] for chapter in chapters if chapter["ordinal"] <= bookmark)


def _view(mem, bookmark) -> dict:
    view = mem.view(bookmark)
    people = {kind: [(row["canonical_name"], row["type"], row["revealed_at"])
                     for row in view.entities_of_type(kind)]
              for kind in ("character", "place", "faction", "object")}
    return {
        "characters": [(row["canonical_name"], row["revealed_at"]) for row in view.characters()],
        "by_type": people,
        "aliases": {row["canonical_name"]: [(alias["surface_form"], alias["revealed_at"])
                                            for alias in view.aliases_of(row["entity_id"])]
                    for kind in people for row in view.entities_of_type(kind)},
        "summaries": [(row["chapter_key"].split(":", 1)[1], row["revealed_at"], row["summary"])
                      for row in view.chapter_summaries()],
        "timeline": [(row["revealed_at"], row["order_idx"], row["summary"], row["kind"])
                     for row in view.timeline()],
        "chapters": [(row["chapter_key"].split(":", 1)[1], row["revealed_at"], row["title"],
                      row["part_label"] or None) for row in view.chapters()],
    }


def answers(mem, atoms, chapters) -> dict:
    read_words = set(word_tokens(" ".join([_read_text(chapters, 99), *SENTENCES])))
    result = {"audit": _audit(mem), "frontier": mem.completion_frontier(atoms)}
    for bookmark in BOOKMARKS:
        text = _read_text(chapters, bookmark)
        result[bookmark] = {
            "view": _view(mem, bookmark),
            "names": [same_person_names(mem, bookmark, read_words, focus) for focus in FOCUS],
            # As each mode decides it (ADR 0044 step 10: a hosted score names the sentence's own
            # words where a local one names the later names; ``_verdict``).
            "recap": {(sentence, index): _verdict(
                          score_recap(mem, bookmark, sentence, read_text=read), sentence, read)
                      for sentence in SENTENCES for index, read in enumerate((None, text))},
        }
    return result


def local_cast(mem, bookmark) -> dict:
    """Relationships, current states, themes and event participants, by name, as the local
    views give them to the cast and story screens."""
    view = mem.view(bookmark)
    people = {row["entity_id"]: row["canonical_name"]
              for kind in ("character", "place", "faction", "object")
              for row in view.entities_of_type(kind)}
    states = {}
    for entity_id, name in people.items():
        state = view.current_state(entity_id)
        if state is not None:
            states[name] = (json.loads(state["status_json"]), state["revealed_at"])
    return {
        "relationships": sorted((people[row["src_entity"]], people[row["dst_entity"]],
                                 row["rel_type"], row["label"], row["revealed_at"])
                                for row in view.relationships()),
        "states": states,
        "themes": sorted((row["name"], row["description"], row["revealed_at"])
                         for row in view.themes()),
        "participants": sorted((event["summary"], tuple(sorted(
            row["canonical_name"] for row in view.participants_of(event["event_id"]))))
            for event in view.timeline()),
    }


def hosted_cast(dsn, owner, book, bookmark) -> dict:
    """The same, from the hosted rows with the parity suite's Postgres predicates
    (``tests/parity/_adapters.py``)."""
    with psycopg.connect(dsn, row_factory=dict_row) as conn, conn.transaction():
        conn.execute("SELECT set_config('app.owner_id', %s, true)", (str(owner),))
        scope = (owner, uuid.UUID(book))
        visible = """revealed_at<=%(b)s AND retracted_at IS NULL
                     AND (invalid_at IS NULL OR invalid_at>%(b)s)"""
        people = {row["id"]: row["canonical_name"] for row in conn.execute(
            f"SELECT id,canonical_name FROM entities WHERE owner_id=%(o)s AND book_id=%(k)s"
            f" AND {visible}", {"o": scope[0], "k": scope[1], "b": bookmark}).fetchall()}
        relationships = sorted(
            (people[row["src_entity_id"]], people[row["dst_entity_id"]],
             row["relationship_type"], row["label"], row["revealed_at"])
            for row in conn.execute(
                f"SELECT * FROM edges WHERE owner_id=%(o)s AND book_id=%(k)s AND {visible}",
                {"o": scope[0], "k": scope[1], "b": bookmark}).fetchall()
            if row["src_entity_id"] in people and row["dst_entity_id"] in people)
        states = {}
        for entity_id, name in people.items():
            row = conn.execute(
                f"""SELECT status,revealed_at FROM entity_state
                    WHERE owner_id=%(o)s AND book_id=%(k)s AND entity_id=%(e)s AND {visible}
                    ORDER BY revealed_at DESC,recorded_at DESC LIMIT 1""",
                {"o": scope[0], "k": scope[1], "e": entity_id, "b": bookmark}).fetchone()
            if row is not None:
                states[name] = (row["status"], row["revealed_at"])
        themes = sorted((row["name"], row["description"], row["revealed_at"]) for row in
                        conn.execute(f"SELECT * FROM themes WHERE owner_id=%(o)s AND book_id=%(k)s"
                                     f" AND {visible}",
                                     {"o": scope[0], "k": scope[1], "b": bookmark}).fetchall())
        participants = sorted(
            (event["summary"], tuple(sorted(
                people[row["entity_id"]] for row in conn.execute(
                    """SELECT entity_id FROM event_participants WHERE owner_id=%(o)s
                       AND book_id=%(k)s AND event_id=%(e)s AND revealed_at<=%(b)s
                       AND retracted_at IS NULL""",
                    {"o": scope[0], "k": scope[1], "e": event["id"], "b": bookmark}).fetchall()
                if row["entity_id"] in people)))
            for event in conn.execute(
                f"SELECT id,summary FROM events WHERE owner_id=%(o)s AND book_id=%(k)s"
                f" AND {visible}", {"o": scope[0], "k": scope[1], "b": bookmark}).fetchall())
    return {"relationships": relationships, "states": states, "themes": themes,
            "participants": participants}


def hosted_answers(dsn, owner, book, atoms, chapters) -> dict:
    with HostedBookStore(dsn, OwnerId(owner)).book(book) as mem:
        return answers(mem, atoms, chapters)


# ---- the test ------------------------------------------------------------------------------

@pytest.mark.postgres
def test_hosted_chapters_build_the_story_memory_local_mode_builds(
        hosted_app, readers, tenant_dsn, database, worker_dsn, tmp_path, monkeypatch):
    """The break it catches: a hosted publish that drops or adds a row, stamps one at another
    chapter (a fact shown a chapter early), loses an alias or a closed state, attaches a later
    chapter's name to a different person than local mode does (a roster rebuilt in another order
    or with other ids), keys chapters so the completion frontier stays 0, or reorders the people
    of one chapter: any of these answers a check or a screen differently from local mode."""
    client = extraction_client()
    extractor = Extractor(client, monkeypatch)
    http, tokens, app = hosted_app
    owner = readers["a"]
    book = hosted_book(hosted_app, "a", harbour_book())
    atoms = hosted_atoms(app, owner, book)
    reader = Reader(http, tokens, "a", owner)
    with local_book(tmp_path, harbour_book()) as (extract, memory, local_atoms, chapters):
        assert [atom["ordinal"] for atom in atoms] == [1, 2, 3, 4]
        assert chapters[1]["title"] == "Map: The Coast"
        for ordinal in (1, 2, 3, 4):
            calls = len(extractor.calls)
            extract(client, ordinal)
            local_calls = extractor.calls[calls:]
            assert remember(app, worker_dsn, owner, book, client, ordinal) is True
            assert extractor.calls[calls + len(local_calls):] == local_calls
            # ADR 0044 step 3: the chapter is the edition's, handed to the reader through the
            # door up to their place, which is the chapter just built.
            reader.mark(book, [ordinal])
            edition = edition_scope(database, owner, book)
            with memory() as mem:
                expected = answers(mem, local_atoms, chapters)
                expected_cast = {bm: local_cast(mem, bm) for bm in BOOKMARKS}
            for dsn in (tenant_dsn, database):   # row security, and the predicates alone
                assert hosted_answers(dsn, owner, book, atoms, chapters) == expected, ordinal
            # The edition's own rows, with the parity suite's predicates, read past row security:
            # a reader's session cannot read them at all
            # (``test_story_memory_editions_postgres.py``).
            assert {bm: hosted_cast(database, *edition, bm) for bm in BOOKMARKS} \
                == expected_cast, ordinal
            assert expected["frontier"] == ordinal

    # A floor under the comparison: the book exercises what it claims to.
    assert extractor.calls.count("Extraction") == 2 * 3          # the map page: no call
    final = expected
    assert final[2]["names"][2] == []                            # "the Keeper" not yet given
    assert final[3]["names"][2] == [["Mara", "the Keeper"]]
    assert expected_cast[3]["states"]["Mara"] == ({"note": "watching the stranger"}, 3)
    assert expected_cast[2]["states"]["Mara"] == ({"note": "keeping the light"}, 1)
    assert final[3]["recap"][("Rose rescued Vex.", 1)]["dropped_by_the_chat"]       # Rose
    assert not final[4]["recap"][("Rose rescued Vex.", 1)]["names_a_later_name"]
    assert ("Corvus", "Vex", "rivalry", "distrusts", 3) in expected_cast[3]["relationships"]
    assert re.fullmatch(r"[0-9a-f-]{36}:c1\.xhtml", atoms[0]["key"])
    with psycopg.connect(database) as conn:          # the replaced state is closed, not deleted
        assert conn.execute(
            "SELECT revealed_at,invalid_at FROM entity_state WHERE owner_id=%s AND book_id=%s"
            " AND status->>'note'='keeping the light'", edition).fetchone() == (1, 3)
        # The reader owns no story memory of their own.
        assert conn.execute("SELECT count(*) FROM chapters WHERE owner_id=%s",
                            (owner,)).fetchone()[0] == 0


def edition_scope(dsn, owner, book) -> tuple:
    """(notes account, edition id as text) of the live edition of the reader's book's file."""
    with psycopg.connect(dsn) as conn:
        [(account, edition)] = conn.execute(
            """SELECT edition.owner_id, edition.id::text FROM books AS mine
               JOIN books AS edition ON edition.file_hash = mine.file_hash
                AND edition.split_hash = mine.split_hash AND edition.deleted_at IS NULL
               JOIN notes_account AS notes ON notes.account_id = edition.owner_id
               WHERE mine.owner_id = %s AND mine.id = %s""", (owner, book)).fetchall()
    return account, edition
