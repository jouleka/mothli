"""A new reader's first hello: Mothli on the home screen with nothing on the shelf.

The first thing a new reader said ("hi! i'm new here, what can you do?") got the fixed line for
"nothing passed the checks", because an empty shelf was answered with no sentences and no model
call (6al). Now it gets a written reply, every sentence of it checked as any other, with the app's
help and the free library's reviewed candidates to draw on, and its spend recorded in a ledger of
its own. Stand-in models throughout; each test says the break it catches.
"""
import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent / "api"))
from _epub import three_chapter_book  # noqa: E402
from test_chapter_notes import wait_for  # noqa: E402
from test_home_conversation import ask, shelf as shelf  # noqa: E402,F401
from test_ingest import InlineExecutor  # noqa: E402

from app import home_conversation  # noqa: E402
from app.api import classics as classics_api  # noqa: E402
from app.catalog.catalog import HOME_SPEND  # noqa: E402
from app.classics import find_classic  # noqa: E402
from app.config import Settings  # noqa: E402
from app.home_conversation import HOME_SYSTEM  # noqa: E402
from app.home_suggestions import LIMIT  # noqa: E402
from app.llm.jev import JevUnavailable  # noqa: E402
from app.main import create_app  # noqa: E402
from app.reader_place import APP_HELP  # noqa: E402

HELLO = "hi! i'm new here, what can you do?"
WELCOME = ("Hi, I’m Mothli, and I read along with you. "
           "I only know the pages you’ve read with me, so I never spoil a thing. "
           "You might like Frankenstein by Mary Wollstonecraft Shelley.")


def stand_ins(app, monkeypatch, reply, *, doubt=(), down=False):
    """A writer that returns ``reply``; Jev finds a sentence supported unless it holds a ``doubt``
    word (and the second look agrees), or is down. Records each instruction, prompt and sentence."""
    seen = {"system": [], "prompt": [], "checked": [], "states": []}

    def complete(system, user, tier="cheap", schema=None):
        if schema is not None:
            return {"supported": False}, {"in": 90, "out": 5}
        seen["system"].append(system)
        seen["prompt"].append(user)
        return reply, {"in": 100, "out": 40}

    def evaluate(state, questions):
        if down:
            raise JevUnavailable("down")
        if "sentence" in state:
            seen["checked"].append(state["sentence"])
            seen["states"].append(state)
        bad = any(word in state.get("sentence", "") for word in doubt)
        return {name: {"noul": .9} if name == "book_assertion"
                else {"choice": "unestablished" if bad else "supported",
                      "probabilities": {"supported": .1 if bad else .95}} if q["type"] == "choice"
                else {"noul": .01} for name, q in questions.items()}, {"in": 50, "out": 5}

    monkeypatch.setattr(app.state.client, "complete", complete)
    app.state.client.jev = SimpleNamespace(model="jev-1.14.0", evaluate=evaluate, close=lambda: None)
    return seen


@pytest.fixture
def bare(tmp_path):
    """A new reader: nothing on the shelf, nothing in the reading history."""
    settings = Settings(_env_file=None, allow_stub=True, reader_page_context=True,
                        reader_conversation_pipeline=True, data_dir=str(tmp_path / "data"))
    app = create_app(settings, ingest_executor=InlineExecutor())
    settings.reader_conversation_jev = settings.reader_conversation_stream = settings.reader_read_map = True
    with TestClient(app) as api:
        assert app.state.catalog.list_books() == []
        yield api, app, settings


def shown(events):
    return [event["text"] for event in events if event["type"] == "sentence"]


def test_an_empty_shelf_gets_a_written_reply_and_every_sentence_shown_was_checked(bare, monkeypatch):
    """Break caught: an empty shelf answered with no sentences and no model call, so the reader's
    first hello got "We haven't got there yet" (6al); and a reply shown unchecked because there is
    no book to check it against."""
    api, app, _ = bare
    seen = stand_ins(app, monkeypatch, WELCOME)
    events = ask(api, HELLO)
    assert len(seen["prompt"]) == 1
    said = shown(events)
    assert said == ["Hi, I’m Mothli, and I read along with you.",
                    "I only know the pages you’ve read with me, so I never spoil a thing.",
                    "You might like Frankenstein by Mary Wollstonecraft Shelley."]
    for sentence in said:                       # Jev judged each sentence the reader was shown
        assert sentence in seen["checked"]
    answer = events[-1]["answer"]
    assert events[-1]["type"] == "done" and not answer["insufficient_evidence"]
    assert [claim["text"] for claim in answer["claims"]] == said
    # It is kept as the home conversation, like any other reply.
    turns = [turn["content"] for turn in api.get("/api/reader/conversation").json()["turns"]]
    assert turns == [HELLO, " ".join(said)]


def test_the_prompt_says_the_shelf_is_empty_and_carries_the_app_help_and_the_candidates(bare, monkeypatch):
    """Break caught: the writer left to guess at an empty shelf (or told nothing of the app or the
    free library), or a different list given to the writer and the checker."""
    api, app, _ = bare
    seen = stand_ins(app, monkeypatch, WELCOME)
    ask(api, HELLO)
    prompt, system = seen["prompt"][-1], seen["system"][-1]
    assert "\n\nWHERE THE READER IS:\n- The shelf is empty: no book is on it yet, and nothing has been read together.\n" in prompt
    assert "\n\nHOW THIS APP WORKS:\n" + APP_HELP + "\n\n" in prompt
    listed = prompt.split("BOOKS YOU CAN SUGGEST")[1].split("\n\nWHAT YOU HAVE READ TOGETHER")[0].splitlines()[1:]
    assert len(listed) == LIMIT and any(line.startswith("- “Frankenstein” by Mary Wollstonecraft Shelley") for line in listed)
    assert "(nothing read yet)" in prompt
    # The checker is given the same lines: the shelf, the help and each candidate.
    passages = seen["states"][-1]["passages"]
    joined = " ".join(passages)
    assert "The shelf is empty" in joined and APP_HELP in joined
    for line in listed:
        assert sum(passage.endswith(line[2:]) for passage in passages) == 1
    # The instructions add only what an empty shelf needs, after the rest unchanged.
    assert system.startswith(HOME_SYSTEM.removesuffix("Output only the words of your reply."))
    assert "EMPTY SHELF:" in system and "add a book of their own" in system


def test_a_reply_naming_a_candidate_carries_the_add_button_and_the_button_works(bare, monkeypatch):
    """Break caught: no "Add … to my shelf" under a suggestion made to an empty shelf, or one whose
    id the add-classic call does not accept."""
    api, app, _ = bare
    stand_ins(app, monkeypatch, WELCOME)
    answer = ask(api, "What should I read first?")[-1]["answer"]
    assert answer["suggested"] == [{"classic_id": "84", "title": "Frankenstein", "author": "Mary Wollstonecraft Shelley"}]
    monkeypatch.setattr(classics_api, "fetch_classic", lambda entry, *, max_bytes, client=None: three_chapter_book())
    added = api.post("/api/classics/84")
    assert added.status_code == 201
    assert [book["source"] for book in api.get("/api/books").json()] == ["gutenberg:84"]


def test_spend_with_no_book_is_recorded_in_the_home_ledger_and_bounded_by_its_ceiling(bare, monkeypatch):
    """Break caught: spend for a reply with no book going unrecorded (there was no book to record
    it against) or unbounded (no ceiling to refuse a call past it)."""
    api, app, settings = bare
    catalog = app.state.catalog
    stand_ins(app, monkeypatch, WELCOME)
    ask(api, HELLO)
    costs = catalog.get_costs(HOME_SPEND)
    phases = [cost["phase"] for cost in costs]
    assert phases.count("home-reply") == 1 and phases.count("home-sentence-check") == 3
    assert catalog.get_cost_reservations(HOME_SPEND) == []
    assert all(cost["input_tokens"] > 0 for cost in costs)
    # The per-book ceiling holds for the home ledger: one more call would pass it, so it is refused
    # before any model is called, and nothing is shown.
    settings.cost_max_input_tokens_per_book = sum(cost["input_tokens"] for cost in costs) + 1
    seen = stand_ins(app, monkeypatch, WELCOME)
    events = ask(api, "And what else?")
    assert events == [{"type": "error", "status": 429, "message": "book cost ceiling reached"}]
    assert seen["prompt"] == [] and len(catalog.get_costs(HOME_SPEND)) == len(costs)
    with pytest.raises(RuntimeError, match="cost ceiling"):
        catalog.reserve_cost(HOME_SPEND, phase="home-reply", model="jev-1.14.0", input_tokens=2,
                             output_tokens=0, usd=0.0, max_input_tokens=1, max_output_tokens=1, max_usd=1.0)


def test_the_home_ledger_is_no_book_and_a_made_up_book_is_still_refused(bare):
    """Break caught: the reserved id loosening the ledger's own rule, so spend could be recorded
    against a book that does not exist; and the home ledger leaking into a book's."""
    _api, app, _ = bare
    catalog = app.state.catalog
    with pytest.raises(ValueError, match="unknown book"):
        catalog.reserve_cost("bkmadeup", phase="x", model="m", input_tokens=1, output_tokens=0, usd=0.0,
                             max_input_tokens=10, max_output_tokens=10, max_usd=1.0)
    reservation = catalog.reserve_cost(HOME_SPEND, phase="x", model="m", input_tokens=1, output_tokens=0,
                                       usd=0.0, max_input_tokens=10, max_output_tokens=10, max_usd=1.0)
    catalog.settle_cost(HOME_SPEND, reservation)
    assert [cost["phase"] for cost in catalog.get_costs(HOME_SPEND)] == ["x"]
    assert catalog.total_cost(HOME_SPEND) == 0.0 and catalog.list_books() == []


def test_an_outage_of_the_checker_shows_nothing_on_an_empty_shelf(bare, monkeypatch):
    """Break caught: an empty shelf's reply shown unchecked when Jev cannot judge it."""
    api, app, _ = bare
    stand_ins(app, monkeypatch, WELCOME, down=True)
    response = api.post("/api/reader/ask/stream", json={"question": HELLO})
    events = [json.loads(row[6:]) for row in response.text.splitlines() if row.startswith("data: ")]
    assert [event["type"] for event in events] == ["error"] and events[0]["status"] == 503
    assert "Mothli" not in response.text and "Frankenstein" not in response.text
    assert api.get("/api/reader/conversation").json()["turns"] == []


def test_with_no_book_the_fixed_gate_still_cuts_a_sentence_about_what_will_happen(bare, monkeypatch):
    """Break caught: the fixed gate silently passing every sentence when there is no book to compare
    against. With no book there is no name the reader has not met, but the pattern for what "will"
    happen is about the sentence alone, and it still holds."""
    api, app, _ = bare
    stand_ins(app, monkeypatch, "Hi, I’m Mothli. In Frankenstein the creature will turn on his maker.")
    events = ask(api, HELLO)
    assert shown(events) == ["Hi, I’m Mothli."] and events[-1]["answer"]["dropped_sentences"] == 1


def test_a_sentence_jev_doubts_is_cut_on_an_empty_shelf_too(bare, monkeypatch):
    """Break caught: Jev's judgment skipped, or its doubt overruled, for a reply with no book."""
    api, app, _ = bare
    stand_ins(app, monkeypatch, WELCOME + " Its ending is a famous shipwreck.", doubt=["shipwreck"])
    events = ask(api, HELLO)
    assert "Its ending is a famous shipwreck." not in shown(events) and len(shown(events)) == 3


def test_an_empty_shelf_does_not_stop_the_home_conversation_being_distilled(bare, monkeypatch):
    """Break caught: what a new reader tells Mothli about themselves never kept while the shelf is
    empty, because there was no book to record the distilling's spend against."""
    from test_recollections import change, models
    api, app, settings = bare
    settings.reader_recollections = True
    seen = models(app, monkeypatch, reply="Hi, I’m Mothli.",
                  changes=lambda prompt: [change("The reader likes ghost stories.", message=1)]
                  if "ghost stories" in prompt else [])
    for question in ("hi! i love ghost stories", "what can you do?", "how do I add a book?",
                     "is it free?", "can I read in French?", "ok, thanks!"):
        ask(api, question)
    assert wait_for(lambda: not app.state.recollector._queued and seen["distil"])
    assert wait_for(lambda: [r["text"] for r in app.state.catalog.get_recollections()] == [
        "The reader likes ghost stories."])
    assert [c["phase"] for c in app.state.catalog.get_costs(HOME_SPEND) if c["phase"].startswith("recollect")]


# --- with books on the shelf nothing changes -------------------------------------------------------
# The prompt and instructions of a home reply with books on the shelf, as main wrote them before
# this change (9e1a59f), for the three-book shelf below and a fixed list of candidates.
MAIN_SYSTEM = "6b8fd6f272da114d577668aafd6ebc35fc15002e04bd2d123afce98dceab0448"
MAIN_PROMPT = "933ae137ebb0b480ff8c981e01dcbc6d7482e78367068b67e58edff8e4cf3f30"


def test_with_books_on_the_shelf_the_prompt_and_instructions_are_as_before(shelf, monkeypatch):
    """Guard, not a fix: catches the empty-shelf change reaching a reply with books on the shelf."""
    api, app, _ = shelf
    frankenstein = {"classic_id": "84", "title": "Frankenstein", "author": "Mary Wollstonecraft Shelley",
                    "genres": ["Classics"], "blurb": find_classic("84")["blurb"]}
    monkeypatch.setattr(home_conversation, "suggestions", lambda *args, **kwargs: [frankenstein])
    seen = stand_ins(app, monkeypatch, "Corvus moved lamp 21 in part 2.")
    ask(api, "What did Corvus do with lamp 21?")
    digest = lambda text: hashlib.sha256(text.encode("utf-8")).hexdigest()  # noqa: E731
    assert digest(seen["system"][-1]) == MAIN_SYSTEM and digest(HOME_SYSTEM) == MAIN_SYSTEM
    assert digest(seen["prompt"][-1]) == MAIN_PROMPT
