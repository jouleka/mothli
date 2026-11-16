"""The streamed chat inside a book answers the same locally and hosted (ADR 0041 part 5, 9.3).

The same book, the same reading, the same questions and the same stand-in writer and checker,
through the local route (``Catalog``, a local ``MemoryDB``) and the hosted route (the hosted
adapters, the platform's client), with an empty story memory and with the same people seeded in
both. The unchanged ``stream_read_prefix`` decides both, so every reply must give the same events:
the sentences shown, how many were removed, the status, the offers and any error; and the writer
and the checker must be given the same text.
"""

from __future__ import annotations

import difflib
import json
import uuid
from datetime import UTC, datetime

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.hosted.auth.api import CSRF_HEADER
from app.hosted.storage import EPUB_MEDIA_TYPE
from app.main import create_app
from tests.hosted.test_companion_read_map_postgres import (
    hosted_settings,
    lamp_book,
    line,
    page,
    signed_in,
)
from tests.hosted.test_companion_stream_postgres import (
    ALIASES,
    CHAT,
    PEOPLE,
    QUESTION,
    StandIns,
    events_of,
    restore_caps,  # noqa: F401  (autouse: the hosted app sets this process's caps)
    seed_memory,
)
from tests.hosted.test_tenant_postgres import UnusedOIDCClient, _become
from tests.parity import test_companion_parity as read_map_parity

pytestmark = pytest.mark.postgres

postgres_database = read_map_parity.postgres_database


class LocalChat:
    """Local mode as the local stream tests run it: the stub client, extraction deferred (so
    the story memory holds only what a test puts there), streamed replies and the read map on."""

    def __init__(self, tmp_path) -> None:
        self.settings = Settings(_env_file=None, allow_stub=True, reader_page_context=True,
                                 reader_memory_ingest=False, data_dir=str(tmp_path / "local"))
        self.app = create_app(self.settings, ingest_executor=read_map_parity._NoExtraction())
        self._client = TestClient(self.app)
        self._client.__enter__()
        self.settings.reader_conversation_jev = self.settings.reader_conversation_stream = True
        self.settings.reader_read_map = True
        self.settings.reader_story_notes = self.settings.reader_recollections = False
        posted = self._client.post("/api/books",
                                   files={"file": ("b.epub", lamp_book(), EPUB_MEDIA_TYPE)})
        self.book = posted.json()["book_id"]
        self.client = self.app.state.client

    def request(self, method, suffix, body=None):
        return self._client.request(method, f"/api/books/{self.book}{suffix}", json=body)

    def seed(self, _dsn) -> None:
        with self.app.state.store.book(self.book) as mem:
            ids = {name: mem.add_entity(name, "character", ordinal) for name, ordinal in PEOPLE}
            for name, surface, ordinal in ALIASES:
                mem.add_alias(ids[name], surface, ordinal)

    def close(self) -> None:
        self._client.__exit__(None, None, None)


class HostedChat:
    """Hosted mode with the platform's (stand-in) client, on the parity database's own role."""

    def __init__(self, tmp_path, dsn: str) -> None:
        self.owner = uuid.uuid4()
        with psycopg.connect(dsn) as conn:
            conn.execute("INSERT INTO users (id,display_name) VALUES (%s,'Parity reader')",
                         (self.owner,))
        tokens, sessions = signed_in({"a": self.owner})
        self.app = create_app(
            hosted_settings(dsn, tmp_path / "objects", **CHAT),
            auth_repository=sessions,
            tenant_repository=read_map_parity._ParityTenantRepository(dsn),
            oidc_client=UnusedOIDCClient(),
            auth_clock=lambda: datetime(2026, 7, 16, 12, tzinfo=UTC))
        self._client = TestClient(self.app, base_url="https://reader.example")
        self._client.__enter__()
        self.csrf = _become(self._client, tokens, "a")
        posted = self._client.post("/api/books",
                                   files={"file": ("b.epub", lamp_book(), EPUB_MEDIA_TYPE)},
                                   headers={CSRF_HEADER: self.csrf})
        assert posted.status_code == 201, posted.text
        self.book = posted.json()["id"]
        self.client = self.app.state.platform_client

    def request(self, method, suffix, body=None):
        return self._client.request(method, f"/api/books/{self.book}{suffix}", json=body,
                                    headers={CSRF_HEADER: self.csrf})

    def seed(self, dsn) -> None:
        # Keys of no chapter of the book: the receipt the schema needs never counts, so the
        # chat's frontier is 0 here as it is in the local memory, which has no receipt.
        seed_memory(dsn, self.owner, self.book)

    def close(self) -> None:
        self._client.__exit__(None, None, None)


def outcome(events, book: str) -> list:
    """What the reader is shown of one reply, with nothing that names the mode's own book."""
    shown = []
    for event in events:
        if event["type"] == "sentence":
            shown.append(("sentence", event["text"]))
        elif event["type"] == "done":
            answer = event["answer"]
            offers = [{**offer, "book_id": offer["book_id"] == book}
                      for offer in answer.get("offers", [])]
            shown.append(("done", [claim["text"] for claim in answer["claims"]],
                          answer["dropped_sentences"], answer["insufficient_evidence"],
                          answer.get("status"), offers))
        else:
            shown.append(("error", event))
    return shown


def given(models: StandIns, book: str) -> tuple:
    """What the writer and the checker were given: the checks run on threads, so in any order."""
    return ([prompt.replace(book, "BOOK") for prompt in models.prompts],
            sorted(json.dumps(state, ensure_ascii=False, sort_keys=True).replace(book, "BOOK")
                   for state in models.checked))


# (reply, stand-in options, question); each is asked in turn, after the ones before it.
PROBES = [
    (f"{line(1, 2)} She later sold it abroad. What did you make of that?",
     {"unsupported": ["abroad"]}, QUESTION),
    (f"{line(1, 2)} Vex moved lamp 50 in part 3.", {"unsupported": ["Vex"]}, QUESTION),
    (f"{line(1, 2)} Vex moved lamp 50 in part 3.", {}, QUESTION),
    (f"{line(1, 2)} The Lamplighter moved lamp 50.", {}, QUESTION),
    (f"{line(1, 2)} Mara will lose lamp 2 later.", {}, QUESTION),
    (f"{line(1, 2)} She seems careful with it. She later sold it abroad. She must return it at "
     "dawn.", {"unsupported": ["careful", "abroad"], "second": {"careful": True, "abroad": False},
               "sure": ["dawn"]}, QUESTION),
    ("Mara sold it abroad.", {"unsupported": ["abroad"]}, QUESTION),
    ("Nothing true here.", {"unsupported": ["Nothing"]}, "who is Vex?"),
    ("Nothing true here.", {"unsupported": ["Nothing"]}, "who is Mara?"),
    (line(1, 2), {"down": True}, QUESTION),
]
# After a leap into part 3, past the rest of part 2: the reply that says Mothli was not there.
LEAP = ("I wasn’t with you for the rest of chapter 2. Vex moved lamp 11 in part 3.", {},
        "catch me up")


@pytest.mark.parametrize("memory", ["empty", "seeded"])
def test_the_same_reading_and_reply_give_the_same_events_locally_and_hosted(
        tmp_path, postgres_database, monkeypatch, memory):
    """The break it catches: a hosted chat that shows, removes or labels a sentence differently
    from local mode on the same inputs (looser is the danger: a hosted sentence shown that local
    mode removes), or gives the writer or the checker other text."""
    local, hosted = LocalChat(tmp_path), HostedChat(tmp_path, postgres_database)
    try:
        sides = (local, hosted)
        for side in sides:
            assert side.request("PUT", "/read-chapters",
                                {"atoms": [1], "read": True}).status_code == 200
            assert side.request("PUT", "/read-page", page(2, 1, 5)).status_code == 200
            if memory == "seeded":
                side.seed(postgres_database)
        compared = []
        for step, (reply, options, question) in enumerate(PROBES + [LEAP]):
            if step == len(PROBES):
                for side in sides:
                    assert side.request("PUT", "/read-page", page(3, 10, 12)).status_code == 200
            results = []
            for side in sides:
                models = StandIns(side.client, monkeypatch, reply=reply, **options)
                point = side.request("GET", "/read-frontier").json()
                response = side.request("POST", "/ask/stream",
                                        {"question": question, "read_frontier": point})
                assert response.status_code == 200, response.text
                results.append((outcome(events_of(response), side.book),
                                given(models, side.book)))
            assert results[0][0] == results[1][0], (step, reply)
            for mine, theirs in zip(results[0][1], results[1][1], strict=True):
                assert mine == theirs, (step, "\n".join(difflib.unified_diff(
                    "\n".join(mine).splitlines(), "\n".join(theirs).splitlines(), lineterm="",
                    n=1)))
            compared.append(results[0][0])
        # The comparison covered what it is for: shown, removed, labelled, offered and failed.
        flat = [item for shown in compared for item in shown]
        assert any(kind == "sentence" for kind, *_rest in flat)
        dones = [item for item in flat if item[0] == "done"]
        assert any(done[2] for done in dones) and any(done[4] == "ahead" for done in dones)
        assert any(done[5] for done in dones)
        assert any(item[0] == "error" for item in flat)
        removed_names = [shown for shown in compared[2:4]]
        if memory == "seeded":
            assert all(("sentence", line(1, 2)) in shown and len(shown) == 2
                       for shown in removed_names)
        assert ("sentence", "Mara will lose lamp 2 later.") not in compared[4]
        # Turns are stored the same way: the same conversation, as each mode shows it.
        assert ([turn["content"] for turn in local.request("GET", "/conversation").json()["turns"]]
                == [turn["content"]
                    for turn in hosted.request("GET", "/conversation").json()["turns"]])
    finally:
        local.close()
        hosted.close()
