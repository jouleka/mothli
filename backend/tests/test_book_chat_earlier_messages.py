"""The chat inside a book: earlier messages in the search words do not crowd out what is asked (6ai).

The book chat searches for the reader's message with the last of the conversation after it, so a
follow-up ("why did she do that?") finds what "she" did. In the home chat those earlier words once
pushed out the passage a plain question needed ("what's an elder?" after "maybe jane eyre", 6ah).
Measured for the book chat at the Karamazov fixture's ``inside-5`` with stand-in models: the
passage stays among those the writer and the checker are given, and the follow-up still needs,
and gets, the earlier words. Stand-in writer and Jev throughout; each test names the break it catches.
"""
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

SCRIPTS = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(Path(__file__).parent / "api"))
from test_ingest import InlineExecutor  # noqa: E402
from test_read_map import ask  # noqa: E402
from test_recollections import models  # noqa: E402

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402
from app.read_map import book_record  # noqa: E402

EARLIER = ["honestly i love how dry the narrator is. long descriptions bore me though",
           "keep your answers short pls, i mostly read on the train at night",
           "that bit about the monk sent away from athos wrecked me",
           "who is Alyosha again?",
           "i think after this one i want something lighter, maybe jane eyre"]
ELDER = "An elder was one who took your soul, your will, into his soul and his will."
LEFT = "she left the house and ran away"


@pytest.fixture(scope="module")
def karamazov(tmp_path_factory):
    """The Karamazov fixture read to ``inside-5`` by the read map; chapter 5 holds ``ELDER``."""
    from scripts.companion_eval import enter_by_map, karamazov_epub
    settings = Settings(_env_file=None, allow_stub=True, reader_page_context=True, reader_conversation_pipeline=True,
                        data_dir=str(tmp_path_factory.mktemp("karamazov") / "data"))
    app = create_app(settings, ingest_executor=InlineExecutor())
    settings.reader_conversation_jev = settings.reader_conversation_stream = settings.reader_read_map = True
    cases = json.loads((SCRIPTS / "companion_eval_cases.json").read_text(encoding="utf-8"))
    inside = next(p for p in cases["books"]["karamazov"]["positions"] if p["name"] == "inside-5")
    with TestClient(app) as api:
        bid = api.post("/api/books", files={"file": ("k.epub", karamazov_epub(), "application/epub+zip")}).json()["book_id"]
        _boundary, read_text, _later = enter_by_map(api, app, bid, inside, app.state.worker.source_chapters(bid))
        assert ELDER in " ".join(read_text.split()) and LEFT in " ".join(read_text.split())
        yield api, app, bid


def said_before(app, bid, messages):
    """The book's conversation, as the app stores it: each message with Mothli's short line after it,
    with what is read now as the record of what was read when it was said."""
    catalog = app.state.catalog
    catalog.clear_turns(bid)
    book = catalog.get_book(bid)
    drew_on = book_record(bid, book, catalog.get_read_map(bid, incarnation=book["incarnation"]))
    where = dict(incarnation=book["incarnation"], position_epoch=catalog.get_state(bid)["position_epoch"],
                 atom=5, offset=0, drew_on=drew_on)
    for message in messages:
        catalog.add_turn(bid, role="user", content=message, **where)
        catalog.add_turn(bid, role="assistant", content="Got it.", **where)


def given(app, monkeypatch, api, bid, question):
    seen = models(app, monkeypatch, reply="That is so.")
    ask(api, bid, question)
    assert seen["checked"]
    return seen


def flat(text):
    return " ".join(text.split())


def test_what_an_elder_is_reaches_the_book_reply_after_unrelated_earlier_messages(karamazov, monkeypatch):
    """Break caught: the earlier messages ("maybe jane eyre") pushing the one passage that says
    what an elder is out of the book chat's passages, so a reply says the pages do not cover it
    or its true sentence is cut. Both the checker and the writer must have it."""
    api, app, bid = karamazov
    said_before(app, bid, EARLIER)
    seen = given(app, monkeypatch, api, bid, "what's an elder?")
    assert all(any(ELDER in flat(p) for p in state["passages"]) for state in seen["checked"])
    assert ELDER in flat(seen["reply"][-1])


def test_a_follow_up_still_finds_what_the_earlier_message_was_about(karamazov, monkeypatch):
    """A guard. Break caught: dropping the earlier words from the search, so "why did she do
    that?" no longer finds Adelaïda leaving. Without the earlier message it is not found, which
    shows the earlier words are what find it."""
    api, app, bid = karamazov
    said_before(app, bid, ["wait, Adelaida just ran off and left the kid with Fyodor??"])
    seen = given(app, monkeypatch, api, bid, "why did she do that?")
    assert all(any(LEFT in flat(p) for p in state["passages"]) for state in seen["checked"])
    said_before(app, bid, [])
    seen = given(app, monkeypatch, api, bid, "why did she do that?")
    assert not any(LEFT in flat(p) for state in seen["checked"] for p in state["passages"])
