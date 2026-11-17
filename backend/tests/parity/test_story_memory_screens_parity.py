"""The story memory screens answer the same locally and hosted, at every place (ADR 0042 part 4).

One book with pages that are not part of the book proper (``tests/_alice_shaped.py``) is added to
the local app (SQLite catalog and ``MemoryDB``) and to the hosted app (Postgres adapters, object
storage), the same memory rows are written into each, and the reader walks through it with "I've
read this" and "I haven't read this". At every place, the cast, every person's card, the story so
far and progress must be the same in both, for every chapter asked for, once each mode's own ids
are replaced by names. Then the probes: nothing stamped after the reader's place is in any of them.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from app.ingest.manifest import load_manifest
from app.memory.dal import _now
from tests._alice_shaped import alice_shaped_epub
from tests.hosted.test_companion_story_memory_postgres import (
    ALIASES,
    EVENTS,
    PEOPLE,
    STATES,
    SUMMARIES,
    TIES,
    TITLES,
    book_keys,
    complete_chapter,
    seed_story_memory,
)
from tests.parity import test_companion_parity as read_map_parity

pytestmark = pytest.mark.postgres

postgres_database = read_map_parity.postgres_database

BOOKMARKS = (None, 0, 3, 4, 99)           # None: the reader's place; 99: past the last chapter
CARDS = (99,)    # every card, asked for past the last chapter (each mode's ids)
NOBODY = "nobody"


def _hash(ordinal: int) -> str:
    return hashlib.sha256(f"chapter {ordinal}".encode()).hexdigest()[:16]


class Local(read_map_parity.LocalReader):
    def seed(self, receipts=(1, 2, 3, 4)) -> None:
        """The same rows as ``seed_story_memory``, through the local memory's own writers."""
        state = self.app.state
        manifest = load_manifest(self.settings.data_dir, self.book)
        self.names, self.edges = {}, {}
        with state.store.book(self.book) as mem:
            for atom in manifest["atoms"]:
                ordinal, key = atom["ordinal"], atom["key"]
                with mem.transaction():
                    mem.add_chapter(key, ordinal, href=f"{ordinal}.xhtml",
                                    title=TITLES.get(ordinal), content_hash=_hash(ordinal))
                    mem.add_raw(key, ordinal, f"chapter {ordinal}", content_hash=_hash(ordinal))
                    if ordinal in SUMMARIES:
                        for kind in ("chapter", "rolling-recap"):
                            mem.add_summary(key, ordinal, SUMMARIES[ordinal], kind=kind)
                    if ordinal in receipts:
                        mem.mark_chapter_ingested(key, _hash(ordinal))
            ids = {}
            for key, name, kind, revealed_at, invalid_at, retracted in PEOPLE:
                ids[key] = mem.add_entity(name, kind, revealed_at)
                self.names[ids[key]] = key
                with mem._writer():
                    mem._conn.execute("UPDATE entities SET invalid_at=?, retracted_at=? "
                                      "WHERE entity_id=?",
                                      (invalid_at, _now() if retracted else None, ids[key]))
            for person, surface, revealed_at, retracted in ALIASES:
                alias = mem.add_alias(ids[person], surface, revealed_at)
                if retracted:
                    with mem._writer():
                        mem._conn.execute("UPDATE aliases SET retracted_at=? WHERE alias_id=?",
                                          (_now(), alias))
            for key, src, dst, kind, label, revealed_at, invalid_at in TIES:
                self.edges[mem.add_edge(ids[src], ids[dst], kind, label, revealed_at,
                                        invalid_at=invalid_at)] = key
            for summary, revealed_at, order_idx, kind, invalid_at in EVENTS:
                mem.add_event(summary, revealed_at, order_idx, kind=kind, invalid_at=invalid_at)
            for person, status, revealed_at, invalid_at in STATES:
                mem.add_state(ids[person], revealed_at, status, invalid_at=invalid_at)
        self.ids = {key: entity_id for entity_id, key in self.names.items()}
        self.ids[NOBODY] = 10_000_000
        state.catalog.set_ingest_progress(self.book, len(receipts))

    def complete(self, ordinal: int) -> None:
        state = self.app.state
        manifest = load_manifest(self.settings.data_dir, self.book)
        with state.store.book(self.book) as mem, mem.transaction():
            mem.mark_chapter_ingested(manifest["atoms"][ordinal - 1]["key"], _hash(ordinal))
        state.catalog.set_ingest_progress(self.book, ordinal)

    def get(self, suffix: str, **query):
        return self._client.get(f"/api/books/{self.book}{suffix}", params=query)


class Hosted(read_map_parity.HostedReader):
    def seed(self, dsn: str) -> None:
        ids = seed_story_memory(dsn, self.owner, self.book,
                                book_keys(self.app, self.owner, self.book))
        self.dsn = dsn
        people = {key for key, *_rest in PEOPLE}
        self.names = {value: key for key, value in ids.items() if key in people}
        self.edges = {value: key for key, value in ids.items() if key not in people}
        self.ids = {key: value for key, value in ids.items() if key in people}
        self.ids[NOBODY] = "00000000-0000-4000-8000-000000000000"

    def complete(self, ordinal: int) -> None:
        complete_chapter(self.dsn, self.owner, self.book, ordinal)

    def get(self, suffix: str, **query):
        return self._client.get(f"/api/books/{self.book}{suffix}", params=query,
                                headers={"x-csrf-token": self.csrf})


def _named(value, side):
    """A payload with each mode's own ids replaced by what they name."""
    if isinstance(value, list):
        return [_named(item, side) for item in value]
    if not isinstance(value, dict):
        return value
    out = {}
    for key, item in value.items():
        if key in ("entity_id", "src_entity", "dst_entity"):
            out[key] = side.names[item]
        elif key == "edge_id":
            out[key] = side.edges[item]
        elif key == "chapter_key":                # keys are "<book id>:<file>" in both modes
            out[key] = item.removeprefix(f"{side.book}:")
        else:
            out[key] = _named(item, side)
    return out


def _screens(side) -> dict:
    """Everything the four screens answer at the reader's current place."""
    def answer(response):
        return response.status_code, _named(response.json(), side)

    seen = {"ingest": answer(side.get("/ingest"))}
    for bookmark in BOOKMARKS:
        query = {} if bookmark is None else {"bookmark": bookmark}
        seen[("graph", bookmark)] = answer(side.get("/graph", **query))
        seen[("notes", bookmark)] = answer(side.get("/notes", **query))
        if bookmark in CARDS:
            for person, entity_id in side.ids.items():
                seen[("card", person, bookmark)] = answer(side.get(f"/character/{entity_id}",
                                                                   **query))
    return seen


def _cast(seen, bookmark=None):
    return [person["canonical_name"] for person in seen[("graph", bookmark)][1]["characters"]]


def _everything(seen, bookmark=None) -> str:
    """Every word the screens show as of one chapter asked for, as one text."""
    shown = [seen[("graph", bookmark)], seen[("notes", bookmark)]]
    shown += [value for key, value in seen.items() if key[0] == "card" and key[2] == bookmark]
    return json.dumps(shown, ensure_ascii=False)


def test_the_story_memory_screens_are_the_same_locally_and_hosted_at_every_place(
        tmp_path, postgres_database):
    """The break it catches: a hosted cast, card, story or progress that differs from local mode
    for the same rows and the same reading: a person, alias, tie, state, event or summary shown
    hosted that local mode keeps back (or the reverse), a page that is not part of the book proper
    counted, a different clamp, a person met later answering with a card, or progress that runs
    ahead. Walks from before anyone is met to past the last chapter, and back."""
    blob = alice_shaped_epub()
    local = Local(tmp_path, blob)
    hosted = Hosted(tmp_path, postgres_database, blob)
    try:
        local.seed()
        hosted.seed(postgres_database)
        places = {}

        def compare(label):
            here, there = _screens(local), _screens(hosted)
            for key, value in here.items():
                assert there[key] == value, (label, key)
            assert there.keys() == here.keys()
            places[label] = there

        def both(action):
            for side in (local, hosted):
                action(side)

        compare("nothing read")
        for atom in (3, 4, 5):
            both(lambda side, atom=atom: side.request("PUT", "/read-chapters",
                                                      {"atoms": [atom], "read": True}))
            compare(f"read {atom}")
        both(lambda side: side.complete(5))
        compare("chapter 5 complete")
        both(lambda side: side.request("PUT", "/read-chapters", {"atoms": [4], "read": False}))
        compare("chapter 4 not read")
    finally:
        local.close()
        hosted.close()

    # ---- the probes: what the walk shows, in both modes (they are equal) ----
    gone = (404, {"detail": "unknown character"})
    nothing = places["nothing read"]
    assert nothing["ingest"] == (200, {"ingest_progress": 4, "status": "idle", "flags": [],
                                       "error": None})
    assert _cast(nothing) == [] and nothing[("notes", None)][1]["chapters"] == []
    assert nothing[("graph", None)][1]["as_of_chapter"] == 0
    for person in ("reader", "mara", "corvus", "vex", NOBODY):
        assert nothing[("card", person, 99)] == gone

    third = places["read 3"]
    for bookmark in (None, 3, 4, 99):             # asking for more never shows more
        assert third[("graph", bookmark)][1]["as_of_chapter"] == 3
        assert _cast(third, bookmark) == ["Mara", "Old Wick"]
        shown = _everything(third, bookmark)
        for later in ("Corvus", "Vex", "Cor\"", "Lamplighter", "walks with", "dreams of",
                      "brave", "the pool", "pool of oil", "Pool of Oil", "wick-race", "Ghost",
                      "Lampy", "Gutenberg", "cover"):
            assert later not in shown, (bookmark, later)
    assert _cast(third, 0) == []
    mara = third[("card", "mara", 99)][1]
    assert mara["status"] == {"mood": "curious"}           # its end, at chapter 5, is not said
    assert [(tie["name"], tie["label"]) for tie in mara["ties"]] == [("The Lamp-Hole", "lives in")]
    for person in ("corvus", "father", "vex", "ghost", "reader", NOBODY):
        assert third[("card", person, 99)] == gone, person
    notes = third[("notes", None)][1]
    assert [(chapter["revealed_at"], chapter["title"], chapter["summary"], chapter["events"])
            for chapter in notes["chapters"]] == [
        (3, "Down the Lamp-Hole", "Mara falls into the lamp-hole.",
         ["Mara falls down the lamp-hole.", "A rumour of oil spreads."])]
    assert [person["name"] for person in notes["cast"]] == ["Mara", "Mar", "Old Wick"]

    fourth = places["read 4"]
    assert _cast(fourth) == ["Mara", "Corvus"]                # Old Wick ended; Father Corvus folded
    graph = fourth[("graph", None)][1]
    assert graph["characters"][1]["aliases"] == ["Father Corvus", "the Father"]
    assert sorted((tie["src_entity"], tie["dst_entity"], tie["label"], tie["invalid_at"])
                  for tie in graph["relationships"]) == [
        ("corvus", "mara", "blesses", None), ("mara", "corvus", "walks with", None)]
    assert fourth[("card", "mara", 99)][1]["status"] == {"mood": "curious"}
    assert "A rumour of oil spreads." not in _everything(fourth)
    for person in ("vex", "ghost"):
        assert fourth[("card", person, 99)] == gone

    # Past the last chapter, but chapter 5's memory is not complete: still as of chapter 4.
    fifth = places["read 5"]
    assert fifth[("graph", None)][1]["as_of_chapter"] == 4 and "Vex" not in _cast(fifth)
    assert fifth["ingest"][1]["ingest_progress"] == 4
    built = places["chapter 5 complete"]
    assert _cast(built) == ["Mara", "Corvus", "Vex"] and _cast(built, 99) == _cast(built)
    assert built[("card", "mara", 99)][1]["status"] == {"mood": "brave"}
    assert built[("card", "corvus", 99)][1]["aliases"] == ["Cor"]
    assert built["ingest"][1]["ingest_progress"] == 5
    # "I haven't read chapter 4": back to chapter 3, everything after it gone again.
    assert places["chapter 4 not read"][("graph", None)] == third[("graph", None)]
    assert places["chapter 4 not read"][("notes", None)] == third[("notes", None)]
