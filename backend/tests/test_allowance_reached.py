"""The streamed chats end with a distinct code when a reader's allowance is reached (ADR 0041 §6).

Only hosted mode raises an allowance (its spend adapter, ``app.hosted.companion.spend``); here the
local apps' catalog is made to raise the same exceptions, so the one shared-code change is checked
on the real streams: the error event carries the code and a message written for the reader, no
model is called after the refusal, nothing unchecked is shown and no turn is stored. A local
ceiling still ends exactly as before. Stand-in models only.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "api"))
sys.path.insert(0, str(Path(__file__).parent))
from test_first_hello import bare as bare, stand_ins  # noqa: E402,F401
from test_home_conversation import ask  # noqa: E402
from test_page_read_frontier import confirm, page_env as page_env  # noqa: E402,F401
from test_stream_conversation import events_of, stream_models  # noqa: E402

from datetime import date  # noqa: E402

from app.catalog.catalog import HOME_SPEND  # noqa: E402
from app.hosted.companion import spend  # noqa: E402
from app.hosted.companion.spend import (  # noqa: E402
    PlanNeeded, PlatformAllowanceReached, ReaderAllowanceReached)

DAY = ReaderAllowanceReached("day", "Mothli has talked a lot with you today. Back tomorrow.")
BUSY = ReaderAllowanceReached("calls_in_flight", "Mothli is still answering you.",
                              code="companion_busy")
PLATFORM = PlatformAllowanceReached("platform_day", "Mothli is resting for today.")


def refuse(catalog, monkeypatch, refusal, *, after=0, only=None):
    """Refuse every reservation after the first ``after`` (of models starting with ``only``)."""
    reserve, granted = catalog.reserve_cost, []

    def reserve_cost(book_id, **kwargs):
        if only is None or kwargs["model"].startswith(only):
            if len(granted) >= after:
                raise refusal
            granted.append(kwargs["phase"])
        return reserve(book_id, **kwargs)

    monkeypatch.setattr(catalog, "reserve_cost", reserve_cost)
    return granted


def test_a_book_reply_at_the_allowance_ends_with_its_code_and_stores_nothing(page_env, monkeypatch):
    """The break it catches: a reader at their allowance shown the local "book cost ceiling
    reached" (no code, words for the owner), a model called past the refusal, or the refused
    question stored as a turn."""
    api, bid, app, settings = page_env
    point = confirm(api, bid)
    settings.reader_conversation_jev = settings.reader_conversation_stream = True
    catalog = app.state.catalog
    incarnation = catalog.get_book(bid)["incarnation"]
    body = {"question": "What did Mara do?", "read_frontier": point}
    for refusal in (DAY, PLATFORM, BUSY):
        looked = stream_models(app, monkeypatch, "Mara put the cobalt key in a red tin.")
        refuse(catalog, monkeypatch, refusal)
        response = api.post(f"/api/books/{bid}/ask/stream", json=body)
        assert events_of(response) == [{"type": "error", "status": 429, "code": refusal.code,
                                        "message": refusal.reader_message}]
        assert "cobalt" not in response.text and looked == []
        assert catalog.get_turns(bid, incarnation=incarnation) == []
        monkeypatch.undo()


def test_an_allowance_reached_between_sentences_shows_nothing_more(page_env, monkeypatch):
    """The break it catches: an allowance reached at a sentence check (the writer already paid
    for) letting an unchecked sentence through, or the stream ending without the code."""
    api, bid, app, settings = page_env
    point = confirm(api, bid)
    settings.reader_conversation_jev = settings.reader_conversation_stream = True
    stream_models(app, monkeypatch, "Mara put the cobalt key in a red tin. Jonas waited beside the forge.")
    refuse(app.state.catalog, monkeypatch, DAY, only="jev-")
    response = api.post(f"/api/books/{bid}/ask/stream",
                        json={"question": "What did Mara do?", "read_frontier": point})
    events = events_of(response)
    assert events[-1] == {"type": "error", "status": 429, "code": "allowance_reached",
                          "message": DAY.reader_message}
    assert all(event["type"] == "error" for event in events)
    assert "Jonas" not in response.text and "cobalt" not in response.text
    incarnation = app.state.catalog.get_book(bid)["incarnation"]
    assert app.state.catalog.get_turns(bid, incarnation=incarnation) == []


def test_a_home_reply_at_the_allowance_ends_with_its_code_and_stores_nothing(bare, monkeypatch):
    """The break it catches: the home chat missing the code the book chat has, or storing the
    refused message."""
    api, app, _settings = bare
    seen = stand_ins(app, monkeypatch, "Hi, I'm Mothli, and I read along with you.")
    refuse(app.state.catalog, monkeypatch, PLATFORM)
    assert ask(api, "hello") == [{"type": "error", "status": 429, "code": "allowance_reached",
                                  "message": PLATFORM.reader_message}]
    assert seen["prompt"] == []
    assert app.state.catalog.get_turns(None, incarnation="home") == []
    assert app.state.catalog.get_costs(HOME_SPEND) == []


def test_a_home_reply_at_a_rest_line_speaks_of_the_shelf_not_one_book(bare, monkeypatch):
    """The break it catches (L6 of the plan walk, 23 November 2026): the home chat, where the
    reader is on their shelf, told "Your book and your place are right here." Only the words
    change: the same refusal, the same code, nobody rested otherwise or for longer."""
    api, app, _settings = bare
    shelf = "Your books and your place in each are right here."
    for line, said in (
        (spend._DAY, "We’ve talked a lot today, so I’m resting until tomorrow. " + shelf),
        (spend._MONTH, "We’ve talked our way through this month, so I’ll be back at the start of "
                       "the next. " + shelf),
        (spend._PLATFORM, "I’m resting for the rest of today and I’ll be back tomorrow. " + shelf),
        (spend.first_month_line(date(2026, 12, 21)),
         "We’ve talked our way through your first month together already! I’ll be back on "
         "21 December. " + shelf),
    ):
        stand_ins(app, monkeypatch, "Hi, I'm Mothli, and I read along with you.")
        refusal = spend.ReaderAllowanceReached("day", line)
        refuse(app.state.catalog, monkeypatch, refusal)
        assert ask(api, "hello") == [{"type": "error", "status": 429, "code": "allowance_reached",
                                      "message": said}]
        assert "Your book and" not in said
        # In a book the line is as it was.
        assert refusal.reader_message == line and line.endswith("Your book and your place are right here.")
        monkeypatch.undo()


def test_a_local_ceiling_ends_exactly_as_before(bare, monkeypatch):
    """The break it catches: the shared change altering local mode, where a plain ceiling has no
    code and keeps its own words."""
    api, app, _settings = bare
    stand_ins(app, monkeypatch, "Hi, I'm Mothli, and I read along with you.")
    refuse(app.state.catalog, monkeypatch, RuntimeError("book USD cost ceiling reached"))
    assert ask(api, "hello") == [{"type": "error", "status": 429,
                                  "message": "book cost ceiling reached"}]


# ADR 0045 step 5: a reader whose free weeks end (or whose plan stops) between the route's gate and
# the reply's first call is refused there, and the stream's error event is the gate's answer.
GATE = PlanNeeded("ceiling")


def test_the_gate_met_by_a_book_reply_ends_it_with_402_and_why(page_env, monkeypatch):
    """The break it catches: a 429 or an allowance line where the screens look for the gate
    (status 402, code ``mothli_plan`` and why, as the routes' own refusal), why lost, or a model
    called past the refusal."""
    api, bid, app, settings = page_env
    point = confirm(api, bid)
    settings.reader_conversation_jev = settings.reader_conversation_stream = True
    looked = stream_models(app, monkeypatch, "Mara put the cobalt key in a red tin.")
    refuse(app.state.catalog, monkeypatch, GATE)
    response = api.post(f"/api/books/{bid}/ask/stream",
                        json={"question": "What did Mara do?", "read_frontier": point})
    assert events_of(response) == [{"type": "error", "status": 402, "code": "mothli_plan",
                                    "why": "ceiling", "message": "Mothli needs the plan"}]
    assert looked == [] and "cobalt" not in response.text


def test_the_gate_met_by_a_home_reply_ends_it_with_402_and_why(bare, monkeypatch):
    """As in a book, on the home screen."""
    api, app, _settings = bare
    seen = stand_ins(app, monkeypatch, "Hi, I'm Mothli, and I read along with you.")
    refuse(app.state.catalog, monkeypatch, PlanNeeded("time"))
    assert ask(api, "hello") == [{"type": "error", "status": 402, "code": "mothli_plan",
                                  "why": "time", "message": "Mothli needs the plan"}]
    assert seen["prompt"] == []
