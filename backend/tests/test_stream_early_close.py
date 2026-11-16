"""A reader who cancels a reply while it is being written: the browser stops the request, and the
server, seeing the connection go before the reply is done, stores nothing. A reply that was
finished keeps its turn, as before.

The request is driven through the app itself, as the browser drives it, so the test covers how the
endpoint wraps the reply in a streamed response and not only the reply's own generator.
"""
import gc
import json
import logging
import sys
from pathlib import Path

import anyio

sys.path.insert(0, str(Path(__file__).parent / "api"))
from test_book_turns import shown  # noqa: E402
from test_chapter_notes import models  # noqa: E402
from test_home_conversation import ask as ask_home, shelf as shelf  # noqa: E402,F401
from test_read_map import ask, map_env as map_env, read_page  # noqa: E402,F401

REPLY = "Mara moved lamp 2 in part 1. Mara moved lamp 3 in part 1. Mara moved lamp 4 in part 1. " \
        "Mara moved lamp 5 in part 1. Mara moved lamp 6 in part 1."


def leave_after_first_sentence(api, path, body):
    """POST ``body`` to ``path``; once the first sentence has reached the browser, the browser goes
    away. Returns the events it had received."""
    received = []

    async def run():
        gone = anyio.Event()
        sent_request = False

        async def receive():
            nonlocal sent_request
            if not sent_request:
                sent_request = True
                return {"type": "http.request", "body": json.dumps(body).encode(), "more_body": False}
            await gone.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if gone.is_set():
                raise OSError("the browser has gone")
            if message["type"] == "http.response.body":
                for row in message.get("body", b"").decode().split("\n\n"):
                    if row.startswith("data: "):
                        received.append(json.loads(row[6:]))
                if any(event["type"] == "sentence" for event in received):
                    gone.set()

        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
                 "scheme": "http", "path": path, "raw_path": path.encode(), "root_path": "",
                 "query_string": b"", "server": ("testserver", 80), "client": ("testclient", 50000),
                 "headers": [(b"host", b"testserver"), (b"content-type", b"application/json")]}
        try:
            await api.app(scope, receive, send)
        except OSError:
            pass
    api.portal.call(run)
    gc.collect()        # whatever the server left of the abandoned reply is finished with now
    return received


def finished_streams(caplog, event):
    """The streams whose reply generator has run its last lines (where a turn would be stored).
    Call ``caplog.set_level`` first: the line is logged at INFO."""
    records = [json.loads(r.getMessage()) for r in caplog.records if r.name == "mothli.conversation"]
    return [record for record in records if record.get("event") == event]


def test_a_book_reply_the_reader_leaves_before_it_is_done_is_not_stored(map_env, monkeypatch, caplog):
    """Catches: the server finishing and storing a reply whose request was stopped, so that a reply
    the reader cancelled comes back the next time the conversation is loaded (audit 6z)."""
    api, bid, app, _ = map_env
    caplog.set_level(logging.INFO, logger="mothli.conversation")
    read_page(api, bid, 1, 1, 9)
    models(app, monkeypatch, reply=REPLY)
    point = api.get(f"/api/books/{bid}/read-frontier").json()
    events = leave_after_first_sentence(api, f"/api/books/{bid}/ask/stream",
                                        {"question": "Where did the lamps go?", "read_frontier": point})
    assert events and events[0]["type"] == "sentence"
    assert not any(event["type"] == "done" for event in events)
    # The reply's generator was closed, not left waiting (which would also store nothing, for now):
    # its last lines ran, after fewer sentences than the reply had.
    [record] = finished_streams(caplog, "conversation-stream")
    assert 1 <= record["sentences"] < 5 and record["failure"] is None
    assert shown(api, bid) == []
    incarnation = app.state.catalog.get_book(bid)["incarnation"]
    assert app.state.catalog.get_turns(bid, incarnation=incarnation) == []
    # A reply that was finished is stored, as it always was.
    ask(api, bid, "Where did the lamps go?")
    assert shown(api, bid) == ["Where did the lamps go?", REPLY]


def test_a_home_reply_the_reader_leaves_before_it_is_done_is_not_stored(shelf, monkeypatch, caplog):
    """Catches: the same on the home screen, where leaving the page drops a reply in flight."""
    api, app, _ = shelf
    caplog.set_level(logging.INFO, logger="mothli.conversation")
    models(app, monkeypatch, reply="Corvus moved lamp 21 in part 2. Corvus moved lamp 22 in part 2. "
                                   "Corvus moved lamp 23 in part 2. Corvus moved lamp 24 in part 2.")
    events = leave_after_first_sentence(api, "/api/reader/ask/stream", {"question": "What about lamp 21?"})
    assert events and events[0]["type"] == "sentence"
    assert not any(event["type"] == "done" for event in events)
    [record] = finished_streams(caplog, "conversation-home")
    assert 1 <= record["sentences"] < 4 and record["failure"] is None
    assert api.get("/api/reader/conversation").json()["turns"] == []
    ask_home(api, "What about lamp 21?")
    assert [turn["role"] for turn in api.get("/api/reader/conversation").json()["turns"]] == ["user", "assistant"]
