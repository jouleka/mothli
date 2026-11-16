"""A book's stored conversation is shown the same way locally and hosted (ADR 0041 part 2, 9.3).

The same turns are stored through the local ``Catalog`` (temporary data directory) and through the
hosted adapter, each with its own read map as the record, and the same chapter marks run through
both apps at the same paths. The unchanged ``visible_turns`` decides both, so ``GET /conversation``
must answer the same at every step: the read point (but its id, which hashes the book's own
identity) and every turn shown (but when it was stored).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest

from app.hosted.companion.source import HostedBookSource
from app.hosted.companion.store import HostedCompanionCatalog
from app.hosted.tenant.models import OwnerId
from app.read_map import book_record, read_snapshot
from tests.hosted.test_companion_read_map_postgres import lamp_book, page
from tests.parity import test_companion_parity as read_map_parity
from tests.parity.test_companion_parity import HostedReader, LocalReader

pytestmark = pytest.mark.postgres

# Part 1's parity database: a fresh, fully migrated database per test.
postgres_database = read_map_parity.postgres_database

# An incarnation neither mode's book has: one value, so both modes store the same record.
OTHER_LIFETIME = str(uuid.uuid4())


def _local_side(reader: LocalReader):
    state = reader.app.state
    return state.catalog, state.worker


def _hosted_side(reader: HostedReader):
    catalog = HostedCompanionCatalog(reader.app.state.companion_dsn, OwnerId(reader.owner))
    return catalog, HostedBookSource(reader.app.state.object_storage, catalog,
                                     reader.app.state.companion_sources)


class Side:
    """One mode's reader, with the catalog and source the unchanged modules are given."""

    def __init__(self, reader, catalog, source) -> None:
        self.reader, self.catalog, self.source = reader, catalog, source

    @property
    def book(self) -> str:
        return self.reader.book

    def say(self, role, content, *, reaction=False, record="now"):
        """Store a turn as the chat does. ``record``: "now" (what is read now), None (a turn
        stored before records were kept) or "other" (a record from another shelf lifetime)."""
        point, _c, _s, _m, book, held = read_snapshot(self.book, self.catalog,
                                                      self.reader.settings, self.source)
        drew_on = {"now": book_record(self.book, book, held), None: None,
                   "other": {self.book: [OTHER_LIFETIME, held]}}[record]
        self.catalog.add_turn(self.book, incarnation=book["incarnation"], position_epoch=0,
                              atom=point["atom"], offset=point["offset"], role=role,
                              content=content, reaction=reaction, request_id=None,
                              drew_on=drew_on)

    def stored(self):
        """Every stored turn, with this mode's own book id and incarnation named alike."""
        incarnation = self.catalog.get_book(self.book)["incarnation"]
        names = {self.book: "BOOK", incarnation: "INCARNATION"}
        return [{**{key: value for key, value in turn.items()
                    if key not in ("turn_id", "created_at")},
                 "drew_on": None if turn["drew_on"] is None else {
                     names.get(book, book): [names.get(then, then), held]
                     for book, (then, held) in turn["drew_on"].items()}}
                for turn in self.catalog.get_turns(self.book, incarnation=incarnation)]


def _shown(response):
    """GET /conversation as both modes must agree on it."""
    body = response.json()
    if response.status_code != 200:
        return response.status_code, body
    for turn in body["turns"]:
        stamp = turn["created_at"]
        assert isinstance(stamp, str)
        assert datetime.fromisoformat(stamp).utcoffset() == timedelta(0), stamp
    return response.status_code, {
        "read_frontier": {k: v for k, v in body["read_frontier"].items() if k != "id"},
        "turns": [{k: v for k, v in turn.items() if k != "created_at"} for turn in body["turns"]],
    }


def test_the_same_history_shows_the_same_turns_locally_and_hosted(tmp_path, postgres_database):
    """The break it catches: a hosted adapter that stores or returns turns or their records
    differently from the local catalog, so hosted Mothli would show a turn local mode hides: after
    "I haven't read this", for a turn stored without a record, or for a record from another shelf
    lifetime. Also that a hidden turn comes back, as locally, when the chapter is read again, and
    that clearing leaves both modes with nothing."""
    blob = lamp_book()
    local = LocalReader(tmp_path, blob)
    hosted = HostedReader(tmp_path, postgres_database, blob)
    sides = (Side(local, *_local_side(local)), Side(hosted, *_hosted_side(hosted)))
    try:
        def both(method, suffix, body=None, expected=200):
            here, there = local.request(method, suffix, body), hosted.request(method, suffix, body)
            assert here.status_code == there.status_code == expected, (suffix, here.text,
                                                                       there.text)
            return here, there

        def shown_alike():
            here, there = both("GET", "/conversation")
            assert _shown(here) == _shown(there)
            return [turn["content"] for turn in _shown(there)[1]["turns"]]

        assert shown_alike() == []
        both("PUT", "/read-chapters", {"atoms": [1], "read": True})
        for side in sides:
            side.say("user", "Poor Mara!", reaction=True)
            side.say("assistant", "Mara kept lamp 2.")
            side.say("user", "Stored before records were kept.", record=None)
            side.say("user", "From another shelf lifetime.", record="other")
        assert shown_alike() == ["Poor Mara!", "Mara kept lamp 2."]

        both("PUT", "/read-chapters", {"atoms": [2], "read": True})
        for side in sides:
            side.say("user", "Corvus frightens me.", reaction=True)
            side.say("assistant", "Corvus kept lamp 9.")
        everything = ["Poor Mara!", "Mara kept lamp 2.", "Corvus frightens me.",
                      "Corvus kept lamp 9."]
        assert shown_alike() == everything
        assert sides[0].stored() == sides[1].stored()

        # "I haven't read this" hides what was written from chapter 2; reading it brings it back.
        both("PUT", "/read-chapters", {"atoms": [2], "read": False})
        assert shown_alike() == ["Poor Mara!", "Mara kept lamp 2."]
        both("PUT", "/read-chapters", {"atoms": [2], "read": True})
        assert shown_alike() == everything

        # Chapter 1 underlies every turn: unread, nothing shows; read again, all of it does.
        both("PUT", "/read-chapters", {"atoms": [1], "read": False})
        assert shown_alike() == []
        both("PUT", "/read-chapters", {"atoms": [1], "read": True})
        assert shown_alike() == everything

        # Half a page into chapter 3 hides nothing already said, and shows nothing new.
        both("PUT", "/read-page", page(3, 1, 4))
        assert shown_alike() == everything

        both("DELETE", "/conversation", expected=204)
        assert shown_alike() == []
        assert sides[0].stored() == sides[1].stored() == []
    finally:
        local.close()
        hosted.close()
