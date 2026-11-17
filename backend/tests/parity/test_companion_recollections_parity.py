"""What Mothli remembers comes out the same locally and hosted (ADR 0041 part 7, section 9.3).

The same book, the same reading, the same messages and the same stand-in writer and checker:
locally the unchanged ``Recollector.catch_up`` distils on the local ``Catalog``; hosted, the queue
adds a durable ``recollect`` job and the worker's dispatcher runs the same ``pending`` and
``distil`` over the hosted adapters. After every step the unchanged ``listed`` (the reader's list)
and ``recall`` (what a reply is given, in a book and on the home screen) must give the same notes
in both modes, and the distilling call and the checks must have been given the same text. Only the
ids (a row number locally, a UUID hosted) and the dates differ.
"""

from __future__ import annotations

import json

import pytest

from app.home_conversation import HOME, read_maps, shelf_books
from app.hosted.auth.api import CSRF_HEADER
from app.hosted.companion.jobs import CompanionJobs, RecollectQueue
from app.hosted.companion.source import HostedBookSource
from app.hosted.companion.store import HostedCompanionCatalog
from app.hosted.jobs import PostgresWorkerRepository
from app.hosted.tenant.models import OwnerId
from app.hosted.worker import DurableWorker
from app.read_map import book_record, read_snapshot
from app.recollections import listed, recall
from tests.hosted.test_companion_read_map_postgres import page
from tests.hosted.test_companion_recollections_postgres import HomeModels, change, chatter
from tests.hosted.test_companion_stream_postgres import restore_caps  # noqa: F401  (autouse)
from tests.parity import test_companion_stream_parity as stream_parity
from tests.parity.test_companion_stream_parity import HostedChat, LocalChat

pytestmark = pytest.mark.postgres

postgres_database = stream_parity.postgres_database

NIGHT = "I always read at night, on the train home."
WRECKED = "Part 1 wrecked me, honestly: Mara and that lamp."
LIGHTER = "After this I want something lighter, maybe a comedy."


def distilled(prompt: str) -> list[dict]:
    """The stand-in distilling call: a note for each message worth one, by its number."""
    lines = [line for line in prompt.split("READER MESSAGES", 1)[1].splitlines()
             if line.startswith("[")]
    found = []
    for number, line in enumerate(lines, 1):
        if NIGHT in line:
            found.append(change("The reader reads at night, on the train home.", message=number))
        if WRECKED in line:
            found.append(change("Part 1 wrecked the reader: Mara and that lamp.", message=number,
                                kind="feeling"))
        if LIGHTER in line:
            found.append(change("The reader wants something lighter next, maybe a comedy.",
                                message=number, kind="plan"))
    return found


class Side:
    """One mode: its catalog and source as the unchanged modules are given them, how it stores a
    message as the chats do, how it distils, and how it calls the reader's routes."""

    def __init__(self, chat, catalog, source, *, distil, call, names) -> None:
        self.chat, self.catalog, self.source = chat, catalog, source
        self._distil, self._call, self.names = distil, call, names

    @property
    def book(self) -> str:
        return self.chat.book

    @property
    def settings(self):
        return self.chat.app.state.settings

    def said(self, book, *messages: str) -> None:
        if book is None:
            drew_on = read_maps(shelf_books(self.catalog, self.settings, self.source))
            incarnation = HOME
        else:
            held = read_snapshot(book, self.catalog, self.settings, self.source)[5]
            record = self.catalog.get_book(book)
            drew_on, incarnation = book_record(book, record, held), record["incarnation"]
        for message in messages:
            for role, content in (("user", message), ("assistant", "Mm.")):
                self.catalog.add_turn(book, role=role, content=content, incarnation=incarnation,
                                      position_epoch=0, atom=0, offset=0, drew_on=drew_on)

    def distil(self, book) -> None:
        self._distil(book)

    def call(self, method: str, path: str, body=None):
        return self._call(method, path, body)

    def plain(self, value):
        """``value`` with this mode's book id and incarnation named alike."""
        text = json.dumps(value, sort_keys=True, ensure_ascii=False)
        for real, name in self.names().items():
            text = text.replace(real, name)
        return json.loads(text)

    def view(self) -> dict:
        """What the reader is shown and what a reply is given, ids and dates aside."""
        shown = listed(self.catalog)
        notes = [{key: value for key, value in note.items() if key not in ("id", "said_at")}
                 for note in shown["recollections"]]
        given = {where: [(chosen["kind"], chosen["text"], line) for chosen, line in recall(
                     self.catalog, read={}, book_id=book, query="night train comedy Mara")]
                 for where, book in (("book", self.book), ("home", None))}
        stored = [{key: note[key] for key in ("kind", "text", "tied", "book_id", "drew_on", "said")}
                  for note in self.catalog.get_recollections()]
        return self.plain({"hidden": shown["hidden"], "listed": notes, "given": given,
                           "stored": stored})


def local_side(local: LocalChat) -> Side:
    state = local.app.state
    local.settings.reader_recollections = True

    def names():
        return {local.book: "BOOK", state.catalog.get_book(local.book)["incarnation"]: "INCARNATION"}

    return Side(local, state.catalog, state.worker,
                distil=lambda book: state.recollector.catch_up(book),
                call=lambda method, path, body: local._client.request(method, path, json=body),
                names=names)


def hosted_side(hosted: HostedChat, dsn: str) -> Side:
    state = hosted.app.state
    owner = OwnerId(hosted.owner)
    catalog = HostedCompanionCatalog(state.companion_dsn, owner)
    work = DurableWorker(
        PostgresWorkerRepository(dsn),
        CompanionJobs(tenant_dsn=state.companion_dsn, settings=state.settings,
                      client=state.platform_client, storage=state.object_storage,
                      sources=state.companion_sources),
        "parity-worker")

    def distil(book):
        RecollectQueue(state.companion_dsn, owner).ensure(book)
        while work.run_once():
            pass

    def names():
        return {hosted.book: "BOOK", catalog.get_book(hosted.book)["incarnation"]: "INCARNATION"}

    return Side(hosted, catalog,
                HostedBookSource(state.object_storage, catalog, state.companion_sources),
                distil=distil,
                call=lambda method, path, body: hosted._client.request(
                    method, path, json=body, headers={CSRF_HEADER: hosted.csrf}),
                names=names)


def test_the_same_messages_give_the_same_notes_locally_and_hosted(tmp_path, postgres_database,
                                                                  monkeypatch):
    """The break it catches: a hosted adapter that keeps, orders, ties, hides or hands to a reply
    the reader's notes differently from the local catalog (looser is the danger: a note about
    pages no longer read listed or given to a reply hosted when local mode holds it back), or a
    hosted distilling call given other messages than the local one."""
    local, hosted = LocalChat(tmp_path), HostedChat(tmp_path, postgres_database)
    try:
        sides = (local_side(local), hosted_side(hosted, postgres_database))
        models = [HomeModels(side.chat.client, monkeypatch, changes=distilled, story=["Mara"])
                  for side in sides]
        views = []

        def both(step) -> None:
            for side in sides:
                step(side)

        def alike() -> dict:
            mine, theirs = (side.view() for side in sides)
            assert mine == theirs
            assert [side.plain(m.distils) for side, m in zip(sides, models, strict=True)] == [
                sides[0].plain(models[0].distils)] * 2
            assert [side.plain(m.notes_judged) for side, m in zip(sides, models, strict=True)] == [
                sides[0].plain(models[0].notes_judged)] * 2
            views.append(mine)
            return mine

        for side in sides:
            assert side.chat.request("PUT", "/read-chapters",
                                     {"atoms": [1], "read": True}).status_code == 200
            assert side.chat.request("PUT", "/read-page", page(2, 1, 5)).status_code == 200
        assert alike()["listed"] == []

        # A book's conversation: six messages, two worth a note, one of them a moment of part 1.
        both(lambda side: side.said(side.book, *chatter(4), NIGHT, WRECKED))
        both(lambda side: side.distil(side.book))
        view = alike()
        assert [(note["kind"], note["book"] is not None) for note in view["listed"]] == [
            ("feeling", True), ("about", False)]

        # "I haven't read this" for part 1 holds the moment back, in the list and from replies;
        # read again, it is back.
        for side in sides:
            assert side.chat.request("PUT", "/read-chapters",
                                     {"atoms": [1], "read": False}).status_code == 200
        view = alike()
        assert view["hidden"] == 1 and [note["kind"] for note in view["listed"]] == ["about"]
        assert all("Mara" not in line for _kind, _text, line in view["given"]["book"])
        for side in sides:
            assert side.chat.request("PUT", "/read-chapters",
                                     {"atoms": [1], "read": True}).status_code == 200
        assert alike()["hidden"] == 0

        # The home conversation: its own cursor, its own batch.
        both(lambda side: side.said(None, *chatter(5, start=20), LIGHTER))
        both(lambda side: side.distil(None))
        view = alike()
        assert [note["kind"] for note in view["listed"]] == ["plan", "feeling", "about"]
        assert any("comedy" in text for _kind, text, _line in view["given"]["home"])
        # Nothing new waits: asking again distils nothing in either mode.
        both(lambda side: side.distil(None))
        both(lambda side: side.distil(side.book))
        alike()

        # Forget one (by the id each mode lists), then everything.
        for side in sides:
            [first, *_rest] = listed(side.catalog)["recollections"]
            response = side.call("DELETE", f"/api/reader/recollections/{first['id']}")
            assert response.status_code == 204, response.text
        assert [note["kind"] for note in alike()["listed"]] == ["feeling", "about"]
        for side in sides:
            assert side.call("DELETE", "/api/reader/recollections").status_code == 204
        assert alike() == {"hidden": 0, "listed": [], "given": {"book": [], "home": []},
                           "stored": []}
        assert len(models[0].distils) == 2
    finally:
        local.close()
        hosted.close()
