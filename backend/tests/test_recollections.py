"""What Mothli remembers about the reader between conversations ("recollections").

A recollection is one short note about the reader, distilled from the reader's own messages a
batch at a time, checked against the message it came from, kept under a cap, and given to a reply
within a fixed budget. One tied to a book is used only while what was read when the reader said it
is still read. Stand-in writer and Jev throughout; each test names the break it catches.
"""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent / "api"))
from test_book_turns import mark, read_fresh  # noqa: E402
from test_chapter_notes import wait_for  # noqa: E402
from test_read_map import ask, line, map_env as map_env, read_page  # noqa: E402,F401

from app import recollections  # noqa: E402
from app.read_map import book_record  # noqa: E402
from app.recollections import (BLOCK_HEAD, PROMPT_BUDGET, STORE_CAP, recollection_block,  # noqa: E402
                               select_recollections)

NIGHT = "I always read at night, on the train home."
WRECKED = "Part 1 wrecked me, honestly: Mara and that lamp."


@pytest.fixture
def env(map_env):
    api, bid, app, settings = map_env
    settings.reader_recollections = True
    return api, bid, app, settings


def models(app, monkeypatch, *, reply="Hello.", changes=(), doubts=(), story=(), fail=None):
    """Stand-ins. The reply writer returns ``reply``. The distilling call returns ``changes``: a
    list of changes, a function of its prompt, or any other value as it stands. Jev finds a note
    stated by the reader unless it holds a ``doubts`` word, and free of story content unless it
    holds a ``story`` word. A reply sentence is story content, supported unless it holds a
    ``doubts`` word. ``fail`` is raised by the distilling call."""
    seen = {"distil": [], "distil_system": [], "reply": [], "system": [], "checked": [], "judged": []}

    def complete(system, user, tier="cheap", schema=None):
        if schema is not None and schema.__name__ == "RecollectionChanges":
            seen["distil"].append(user)
            seen["distil_system"].append(system)
            if fail is not None:
                raise fail
            value = changes(user) if callable(changes) else changes
            return ({"changes": list(value)} if isinstance(value, (list, tuple)) else value), {"in": 100, "out": 40}
        if schema is not None:
            return {"supported": False}, {"in": 90, "out": 5}
        seen["reply"].append(user)
        seen["system"].append(system)
        return reply, {"in": 100, "out": 40}

    def evaluate(state, questions):
        if "note" in state:
            seen["judged"].append(state)
            stated = not any(word in state["note"] for word in doubts)
            answers = {"stated": {"choice": "supported" if stated else "unestablished",
                                  "probabilities": {"supported": .95 if stated else .1}},
                       "book_assertion": {"noul": .9 if any(word in state["note"] for word in story) else .01}}
            assert set(answers) == set(questions)
            return answers, {"in": 50, "out": 5}
        if "sentence" in state:
            seen["checked"].append(state)
        bad = any(word in state.get("sentence", "") for word in doubts)
        return {name: {"noul": .9} if name == "book_assertion"
                else {"choice": "unestablished" if bad else "supported",
                      "probabilities": {"supported": .1 if bad else .95}} if q["type"] == "choice"
                else {"noul": .01} for name, q in questions.items()}, {"in": 50, "out": 5}

    monkeypatch.setattr(app.state.client, "complete", complete)
    app.state.client.jev = SimpleNamespace(model="jev-1.14.0", evaluate=evaluate, close=lambda: None)
    return seen


def change(text, message=1, kind="about", replaces=None):
    return {"kind": kind, "text": text, "message": message, "replaces": replaces}


def said(app, bid, *messages, reply="Mm."):
    """Store reader messages (each with Mothli's line after it) as the book's conversation does,
    with what is read of the book now, without running a reply."""
    catalog = app.state.catalog
    book = catalog.get_book(bid)
    drew_on = book_record(bid, book, catalog.get_read_map(bid, incarnation=book["incarnation"]))
    where = dict(incarnation=book["incarnation"], position_epoch=0, atom=1, offset=0, drew_on=drew_on)
    ids = []
    for message in messages:
        ids.append(catalog.add_turn(bid, role="user", content=message, **where))
        catalog.add_turn(bid, role="assistant", content=reply, **where)
    return ids


def chatter(count, start=1):
    """Messages with nothing in them worth keeping, each too long to count as a short answer."""
    return [f"So what about lamp number {number}, then, and where did it end up?"
            for number in range(start, start + count)]


def catch_up(app, bid):
    return app.state.recollector.catch_up(bid)


def kept(app):
    return [(r["kind"], r["text"]) for r in app.state.catalog.get_recollections()]


def idle(app):
    assert wait_for(lambda: not app.state.recollector._queued)


def listed(api):
    response = api.get("/api/reader/recollections")
    assert response.status_code == 200, response.text
    return response.json()


def block_of(prompt):
    """The recollections block of one reply prompt, or None."""
    if BLOCK_HEAD not in prompt:
        return None
    return BLOCK_HEAD + prompt.split(BLOCK_HEAD, 1)[1].split("\n\n", 1)[0]


# --- written by distilling ------------------------------------------------------------------------
def test_a_batch_of_reader_messages_becomes_notes_once_and_only_once(env, monkeypatch):
    """Breaks caught: a call for every message; the same messages read twice (two notes, two
    bills); a batch that is never read."""
    api, bid, app, _ = env
    seen = models(app, monkeypatch, changes=lambda prompt: [change("The reader reads at night, on the train home.",
                                                                   message=5)] if NIGHT in prompt else [])
    said(app, bid, *chatter(4), NIGHT)
    catch_up(app, bid)
    assert seen["distil"] == [] and kept(app) == []            # five messages: not enough yet
    said(app, bid, "And lamp 6?")
    catch_up(app, bid)
    assert len(seen["distil"]) == 1 and kept(app) == [("about", "The reader reads at night, on the train home.")]
    [note] = app.state.catalog.get_recollections()
    assert note["said"] == NIGHT and not note["tied"] and note["book_id"] is None
    # Jev was asked about that one note, against the message it came from and nothing else.
    [judged] = seen["judged"]
    assert judged["reader_message"] == NIGHT and "companion_line_before" not in judged
    catch_up(app, bid)
    catch_up(app, bid)
    assert len(seen["distil"]) == 1                              # nothing new: nothing asked
    said(app, bid, *chatter(6, start=7))
    catch_up(app, bid)
    assert len(seen["distil"]) == 2 and NIGHT not in seen["distil"][1]     # only the new messages
    assert "lamp number 12" in seen["distil"][1] and len(kept(app)) == 1


def test_a_reply_starts_the_distilling_off_the_request_path(env, monkeypatch):
    """Break caught: nothing ever starts it, so a reader could talk for a year and be forgotten."""
    api, bid, app, _ = env
    seen = models(app, monkeypatch, reply="Mara moved lamp 2 in part 1.",
                  changes=lambda prompt: [change("The reader reads at night.", message=6)])
    read_page(api, bid, 1, 1, 3)
    said(app, bid, *chatter(5))
    ask(api, bid, NIGHT)                                         # the sixth message, through the app
    idle(app)
    assert kept(app) == [("about", "The reader reads at night.")]
    assert len(seen["distil"]) == 1 and seen["distil_system"][0] == recollections.DISTIL_SYSTEM


def test_a_note_jev_does_not_find_in_the_message_is_not_stored(env, monkeypatch):
    """Break caught: the distilling call invents something and it is kept as the reader's word."""
    api, bid, app, _ = env
    seen = models(app, monkeypatch, doubts=["vegetarian"], changes=[
        change("The reader is a vegetarian."), change("The reader reads at night.", message=6)])
    said(app, bid, *chatter(5), NIGHT)
    catch_up(app, bid)
    assert kept(app) == [("about", "The reader reads at night.")]
    assert [state["note"] for state in seen["judged"]] == ["The reader is a vegetarian.", "The reader reads at night."]


def test_a_newer_statement_replaces_the_older_one_instead_of_sitting_beside_it(env, monkeypatch):
    """Break caught: "loves long descriptions" and "bored by long descriptions" both kept, and
    both given to the writer."""
    api, bid, app, _ = env
    models(app, monkeypatch, changes=[change("The reader loves long descriptions.", kind="taste")])
    said(app, bid, "I love long descriptions.", *chatter(5))
    catch_up(app, bid)
    assert kept(app) == [("taste", "The reader loves long descriptions.")]
    seen = models(app, monkeypatch, changes=[change("The reader is bored by long descriptions.", kind="taste", replaces=1)])
    said(app, bid, "Honestly long descriptions bore me now.", *chatter(5))
    catch_up(app, bid)
    assert "[1] (taste) The reader loves long descriptions." in seen["distil"][0]      # it was shown what is kept
    assert kept(app) == [("taste", "The reader is bored by long descriptions.")]
    [note] = app.state.catalog.get_recollections()
    assert note["said"] == "Honestly long descriptions bore me now."
    # Saying the same thing again adds nothing.
    models(app, monkeypatch, changes=[change("the reader is bored by  long descriptions", kind="taste")])
    said(app, bid, "Long descriptions bore me.", *chatter(5))
    catch_up(app, bid)
    assert kept(app) == [("taste", "The reader is bored by long descriptions.")]


def test_nothing_is_written_from_mothlis_words_or_from_the_books_text(env, monkeypatch):
    """Breaks caught: Mothli's own sentence, or a sentence of the book, given to the distilling
    call or to Jev as if the reader had said it, and kept as a note."""
    api, bid, app, _ = env
    read_page(api, bid, 1, 1, 10)
    wrong = [change("The reader adores Zanzibar marmalade.", message=1),       # Mothli's phrase
             change("The reader saw Mara move lamp 7.", message=2)]            # the book's
    seen = models(app, monkeypatch, changes=wrong)
    said(app, bid, "I never know what to make of the opening chapters of anything.", *chatter(5),
         reply="Zanzibar marmalade is what I'd bring to this book.")
    catch_up(app, bid)
    given = json.dumps(seen["distil"] + seen["judged"], ensure_ascii=False)
    # Neither was among what the call or the check were given: there was nothing to write them from.
    assert "Zanzibar" not in given and "marmalade" not in given
    assert line(1, 7) not in given and "moved lamp" not in given
    # And a call that produces them anyway is not believed, even by a Jev that says yes to everything:
    # a name the reader did not write is not the reader's.
    assert kept(app) == [] and seen["judged"] == []
    assert app.state.catalog.get_recollection_cursor(bid, incarnation=app.state.catalog.get_book(bid)["incarnation"]) > 0


def test_a_short_answer_is_understood_from_mothlis_line_before_it_which_is_marked_and_is_no_evidence(env, monkeypatch):
    """Breaks caught: "yes" can never become a note; or Mothli's line is handed over as the
    reader's own message."""
    api, bid, app, _ = env
    seen = models(app, monkeypatch, changes=[change("The reader wants something lighter after this book.",
                                                    message=6, kind="plan")])
    asked = "Mara moved lamp 2 in part 1. Ha! Shall we find you something lighter after this book?"
    said(app, bid, *chatter(5), reply=asked)
    said(app, bid, "yes please")
    catch_up(app, bid)
    prompt = seen["distil"][0]
    assert prompt.count("MOTHLI SAID JUST BEFORE") == 1          # only before the short answer
    # The end of Mothli's reply, where its question is; not the rest of what it said.
    assert '(MOTHLI SAID JUST BEFORE, context only: "Ha! Shall we find you something lighter after this book?")' in prompt
    assert "moved lamp" not in prompt
    [judged] = seen["judged"]
    assert judged["reader_message"] == "yes please"
    assert judged["companion_line_before"] == "Ha! Shall we find you something lighter after this book?"
    assert "lighter" not in judged["reader_message"]
    assert kept(app) == [("plan", "The reader wants something lighter after this book.")]


# --- bounded, always ------------------------------------------------------------------------------
def note(number, kind="about", text=None, **more):
    return {"recollection_id": number, "kind": kind, "tied": False, "book_id": None, "drew_on": None,
            "text": text or f"The reader said thing number {number} about how they like to read, at some length.",
            **more}


def test_a_thousand_notes_put_no_more_into_a_prompt_than_ten(env, monkeypatch):
    """Break caught: the prompt grows with the store, which is the fault this feature exists to avoid."""
    api, bid, app, _ = env
    kinds = ["about", "taste", "plan"]
    few = [note(n, kinds[n % 3], "The reader " + f"likes point {n:04d} " * 12) for n in range(1, 11)]
    many = [note(n, kinds[n % 3], "The reader " + f"likes point {n:04d} " * 12) for n in range(1, 1001)]
    blocks = [recollection_block(select_recollections(held, read={}, book_id=bid, query="point 7"))
              for held in (few, many)]
    assert all(len(block) <= PROMPT_BUDGET for block in blocks) and len(blocks[1]) <= len(blocks[0])
    assert blocks[1].count("\n- ") >= 3

    # The same through the app, with a thousand rows in the store (put there past the cap).
    catalog = app.state.catalog
    read_page(api, bid, 1, 1, 3)
    seen = models(app, monkeypatch, reply="Mara moved lamp 2 in part 1.")
    sizes = []
    for count in (10, 1000):
        catalog.keep_recollections([{"kind": kinds[n % 3], "text": "The reader " + f"likes point {n:04d} " * 12,
                                     "tied": False, "book_id": None, "drew_on": None,
                                     "said": "x", "said_at": "2026-11-15T00:00:00+00:00", "replaces": None}
                                    for n in range(count - len(catalog.get_recollections()))], cap=10_000)
        assert len(catalog.get_recollections()) == count
        catalog.clear_turns(bid)                                 # the same conversation each time
        ask(api, bid, "What just happened?")
        sizes.append((len(seen["reply"][-1]), len(block_of(seen["reply"][-1])),
                      max(len(json.dumps(state["passages"])) for state in seen["checked"])))
        seen["checked"].clear()
    assert sizes[1][1] <= PROMPT_BUDGET and sizes[1][0] <= sizes[0][0] and sizes[1][2] <= sizes[0][2]


def test_half_the_room_goes_to_who_the_reader_is_then_plans_then_moments_that_match(env):
    """Breaks caught: a pile of feelings pushes out "wants short answers"; a feeling about another
    book, or one whose text is no longer read, is given to the writer."""
    _api, bid, _app, _ = env
    read = {bid: ["inc", {1: [(0, 500)]}], "other": ["inc2", {1: [(0, 500)]}]}
    here = {bid: ["inc", {"1": [[0, 400]]}]}
    held = ([note(n, "about") for n in range(1, 21)] + [note(n, "plan") for n in range(21, 41)]
            + [note(41, "feeling", "The reader was wrecked by the lamp scene.", tied=True, book_id=bid, drew_on=here),
               note(42, "feeling", "The reader laughed at the cart.", tied=True, book_id=bid,
                    drew_on={bid: ["inc", {"2": [[0, 10]]}]}),                       # chapter 2 is not read
               note(43, "feeling", "The reader cried at the harbour.", tied=True, book_id="other",
                    drew_on={"other": ["inc2", {"1": [[0, 100]]}]}),
               note(44, "feeling", "The reader loved the storm.", tied=True, book_id="other",
                    drew_on={"other": ["inc2", {"1": [[0, 100]]}]})])
    titles = {bid: "The Lamp Book", "other": "The Other Book"}
    chosen = select_recollections(held, read=read, book_id=bid, query="That storm again!", titles=titles)
    block = recollection_block(chosen)
    lines = block.split("\n")[1:]
    assert len(block) <= PROMPT_BUDGET
    assert lines[0] == "- " + held[19]["text"]                                       # newest first
    assert any("thing number 40" in text for text in lines)                          # a plan got in
    assert "- About this book: The reader was wrecked by the lamp scene." in lines
    assert "- About “The Other Book”: The reader loved the storm." in lines           # matches the message
    assert not any("cart" in text or "harbour" in text for text in lines)
    assert select_recollections(held, read=read, book_id=bid, query="That storm again!", titles=titles) == chosen


def test_over_the_cap_the_least_recently_used_go_first_and_about_and_taste_last(env, monkeypatch):
    """Break caught: the store grows for ever, or the cap throws away who the reader is first."""
    api, bid, app, _ = env
    catalog = app.state.catalog
    assert STORE_CAP == 300

    def add(kind, text):
        catalog.keep_recollections([{"kind": kind, "text": text, "tied": False, "book_id": None, "drew_on": None,
                                     "said": "x", "said_at": "2026-11-15T00:00:00+00:00",
                                     "replaces": None}], cap=4)

    add("about", "The reader reads at night.")
    add("plan", "The reader wants a sea story next.")
    add("plan", "The reader wants a short book next.")
    add("taste", "The reader loves dry humour.")
    ids = {r["text"]: r["recollection_id"] for r in catalog.get_recollections()}
    catalog.touch_recollections([ids["The reader wants a sea story next."]])           # used in a reply just now
    add("plan", "The reader is curious about Jane Eyre.")
    # Five over a cap of four: the plan not used for longest went, not the older about note.
    assert sorted(text for _, text in kept(app)) == sorted([
        "The reader reads at night.", "The reader wants a sea story next.", "The reader loves dry humour.",
        "The reader is curious about Jane Eyre."])
    add("about", "The reader wants short answers.")
    add("about", "The reader reads on a phone.")
    assert {kind for kind, _ in kept(app)} == {"about", "taste"} and len(kept(app)) == 4
    add("taste", "The reader dislikes cliffhangers.")
    assert len(kept(app)) == 4 and ("about", "The reader reads at night.") not in kept(app)

    # And through distilling: the app's own cap is applied to what a batch adds.
    monkeypatch.setattr(recollections, "STORE_CAP", 5)
    models(app, monkeypatch, changes=[change(f"The reader wants plan {n}.", kind="plan") for n in range(4)])
    said(app, bid, "I want plan 0, plan 1, plan 2 and plan 3.", *chatter(5))
    catch_up(app, bid)
    assert len(kept(app)) == 5


def test_a_note_too_long_too_many_changes_or_a_long_message_cannot_swell_the_store(env, monkeypatch):
    """Breaks caught: one call writes a 5,000-character note, or two hundred of them, each paid
    for with a check and then carried in every prompt's selection."""
    api, bid, app, _ = env
    long_message = "I like sea stories. " * 40
    seen = models(app, monkeypatch, changes=[
        change("The reader " + "really " * 60 + "likes sea stories."),
        *[change(f"The reader likes sea stories, point {n}.") for n in range(1, 30)]])
    said(app, bid, long_message, *chatter(5))
    catch_up(app, bid)
    # Only the first four changes are looked at; the over-long one among them is dropped unchecked.
    assert len(kept(app)) == len(seen["judged"]) == recollections.CHANGES_MOST - 1
    assert kept(app)[0] == ("about", "The reader likes sea stories, point 1.")
    assert all(len(r["text"]) <= recollections.NOTE_LIMIT and len(r["said"]) <= recollections.SAID_CHARS
               for r in app.state.catalog.get_recollections())


def test_the_distilling_call_itself_stays_the_same_size_whatever_is_kept_or_waiting(env, monkeypatch):
    """Break caught: the call that keeps memory small is itself given every note and every
    waiting message, and grows until it is refused."""
    api, bid, app, _ = env
    catalog = app.state.catalog
    catalog.keep_recollections([{"kind": "taste", "text": f"The reader likes point {n} " * 6, "tied": False,
                                 "book_id": None, "drew_on": None, "said": "x",
                                 "said_at": "2026-11-15T00:00:00+00:00", "replaces": None}
                                for n in range(300)], cap=300)
    seen = models(app, monkeypatch, fail=RuntimeError("provider down"))
    said(app, bid, *chatter(40))
    with pytest.raises(RuntimeError):
        catch_up(app, bid)                                       # the call failed: forty are still waiting
    seen = models(app, monkeypatch)
    said(app, bid, *chatter(40, start=41))
    catch_up(app, bid)
    assert 1 <= len(seen["distil"]) <= recollections.RUN_MOST    # one run makes a few calls, not eighty messages' worth
    ceiling = (recollections.KNOWN_BUDGET + recollections.BATCH_MOST
               * (recollections.MESSAGE_CHARS + recollections.CONTEXT_CHARS + 120) + 400)
    assert all(len(prompt) <= ceiling for prompt in seen["distil"])
    assert all(prompt.count("\n[") <= recollections.BATCH_MOST + 40 for prompt in seen["distil"])


# --- spoiler safety -------------------------------------------------------------------------------
def test_a_feeling_goes_from_the_prompt_and_the_list_when_its_text_is_no_longer_read_and_comes_back(env, monkeypatch):
    """Break caught: "read it fresh" or "I haven't read this", and Mothli still says what the
    reader felt about pages it is meant to have forgotten."""
    api, bid, app, _ = env
    feeling = "Part 1 wrecked the reader: Mara and that lamp."
    read_page(api, bid, 1, 1, 3)
    mark(api, bid, [2])
    models(app, monkeypatch, story=["Mara"], changes=[change(feeling, kind="feeling")])
    said(app, bid, WRECKED, *chatter(5))
    catch_up(app, bid)

    def given():
        seen = models(app, monkeypatch, reply="Mara moved lamp 2 in part 1.")
        point = api.get(f"/api/books/{bid}/read-frontier").json()
        ask(api, bid, "What about Mara?", epoch=point["position_epoch"])
        idle(app)
        return json.dumps(seen["reply"] + seen["checked"], ensure_ascii=False)

    def on_list():
        return [(r["text"], r["message"]) for r in listed(api)["recollections"]]

    assert "wrecked the reader" in given() and on_list() == [(feeling, WRECKED)]
    mark(api, bid, [2], read=False)              # another chapter: the feeling was said with it read
    assert "wrecked" not in given() and on_list() == [] and listed(api)["hidden"] == 1
    mark(api, bid, [2])
    assert "wrecked the reader" in given() and on_list() == [(feeling, WRECKED)]
    read_fresh(api, bid)
    assert "wrecked" not in given() and on_list() == [] and listed(api)["hidden"] == 1
    read_page(api, bid, 1, 1, 3, epoch=1)
    assert "wrecked" not in given()              # chapter 2 was read then, and is not yet read again
    mark(api, bid, [2])
    assert "wrecked the reader" in given() and on_list() == [(feeling, WRECKED)]


def test_a_note_that_says_anything_of_the_book_is_tied_to_it_whatever_its_kind(env, monkeypatch):
    """Break caught (found while trying to break it): "The reader finds Darcy unbearable" is a
    taste, so it outlived "read it fresh" and told a reader starting over that there is a Darcy
    to find unbearable. Only a note Jev finds free of story content is kept apart from the book."""
    api, bid, app, _ = env
    read_page(api, bid, 1, 1, 3)
    models(app, monkeypatch, story=["Mara"], changes=[
        change("The reader finds Mara unbearable.", kind="taste"),
        change("The reader loves dry humour.", message=2, kind="taste")])
    said(app, bid, "Mara is unbearable!", "I love dry humour.", *chatter(4))
    catch_up(app, bid)
    by_text = {r["text"]: r for r in app.state.catalog.get_recollections()}
    assert by_text["The reader finds Mara unbearable."]["tied"] and by_text["The reader finds Mara unbearable."]["book_id"] == bid
    assert not by_text["The reader loves dry humour."]["tied"]
    read_fresh(api, bid)
    assert [r["text"] for r in listed(api)["recollections"]] == ["The reader loves dry humour."]
    read_page(api, bid, 3, 1, 3, epoch=1)                    # reading again, somewhere else in the book
    seen = models(app, monkeypatch, reply="Hello.")
    ask(api, bid, "Hello again", epoch=1)
    assert "Mara" not in json.dumps(seen["reply"] + seen["checked"]) and "dry humour" in seen["reply"][-1]


def test_the_message_a_note_came_from_is_not_shown_once_what_it_was_said_about_is_no_longer_read(env, monkeypatch):
    """Break caught (found while trying to break it): the note is harmless ("reads at night") but
    the message quoted beside it on the reader's screen is not."""
    api, bid, app, _ = env
    read_page(api, bid, 1, 1, 3)
    message = "Mara moving lamp 2 kept me up; I always read at night."
    models(app, monkeypatch, changes=[change("The reader reads at night.")])
    said(app, bid, message, *chatter(5))
    catch_up(app, bid)
    assert [(r["text"], r["message"]) for r in listed(api)["recollections"]] == [("The reader reads at night.", message)]
    read_fresh(api, bid)
    assert [(r["text"], r["message"]) for r in listed(api)["recollections"]] == [("The reader reads at night.", None)]
    assert "Mara" not in json.dumps(listed(api))


def test_a_name_the_reader_did_not_write_is_never_kept(env, monkeypatch):
    """Break caught (found while trying to break it): the distilling model knows the book. Asked
    to note "that servant scares me" it writes the servant's name, which the reader has not met,
    and the note would then be evidence for a reply that says it."""
    api, bid, app, _ = env
    read_page(api, bid, 1, 1, 3)
    seen = models(app, monkeypatch, story=["Vex", "servant"], changes=[
        change("The reader is scared of Vex.", kind="feeling"),
        change("Vex scares the reader.", kind="feeling"),                # a name may open the note
        change("The servant scares the reader: Vex.", kind="feeling"),   # or follow a colon
        change("The reader is scared of the servant.", kind="feeling")])
    said(app, bid, "That servant scares me.", *chatter(5))
    catch_up(app, bid)
    assert kept(app) == [("feeling", "The reader is scared of the servant.")]
    assert [state["note"] for state in seen["judged"]] == ["The reader is scared of the servant."]


def test_the_readers_guess_at_what_will_happen_in_a_book_is_not_kept(env, monkeypatch):
    """Break caught (found while trying to break it): a kept note is among the passages a reply
    is checked against, so the reader's own "Corvus will poison Mara", once a note, could stand
    as support for Mothli saying so. The fixed foreshadowing pattern replies are held to drops
    it; a plan that says nothing of a book may still use the future tense."""
    api, bid, app, _ = env
    read_page(api, bid, 1, 1, 3)
    models(app, monkeypatch, story=["Corvus"], changes=[
        change("The reader thinks Corvus will poison Mara.", kind="feeling"),
        change("The reader is sure Corvus is going to betray Mara.", kind="taste"),
        change("The reader will read a sea story next.", message=2, kind="plan")])
    said(app, bid, "I think Corvus will poison Mara, or betray her.", "I will read a sea story next.", *chatter(4))
    catch_up(app, bid)
    assert kept(app) == [("plan", "The reader will read a sea story next.")]


def test_replacing_a_note_cannot_carry_it_past_what_was_read_when_the_newer_message_was_said(env, monkeypatch):
    """Break caught (found while trying to break it): a note tied to chapter 2 is hidden when
    chapter 2 is marked not read; a later "replacement" would have been stored with the smaller
    read map of the new message and so shown, carrying chapter 2 with it."""
    api, bid, app, _ = env
    read_page(api, bid, 1, 1, 3)
    mark(api, bid, [2])
    models(app, monkeypatch, story=["Corvus"], changes=[change("The reader pities Corvus.", kind="feeling")])
    said(app, bid, "Poor Corvus!", *chatter(5))
    catch_up(app, bid)
    mark(api, bid, [2], read=False)
    seen = models(app, monkeypatch, story=["Corvus"], changes=[
        change("The reader pities Corvus and his lamp.", kind="feeling", replaces=1)])
    said(app, bid, "I still think about him.", *chatter(5))
    catch_up(app, bid)
    assert "Corvus" not in seen["distil"][0]                     # the hidden note was not shown to the call
    assert kept(app) == [("feeling", "The reader pities Corvus.")] and listed(api)["recollections"] == []

    # The same through a batch that spans the change: its first message was said with chapter 2
    # not read, its last with chapter 2 read again. The note is shown to the call (it is usable
    # now), but may not be replaced from the earlier message.
    said(app, bid, "I still think about him, you know, all the time.")
    mark(api, bid, [2])
    seen = models(app, monkeypatch, story=["Corvus"], changes=[
        change("The reader pities Corvus and his lamp.", kind="feeling", replaces=1)])
    said(app, bid, *chatter(5))
    catch_up(app, bid)
    assert "[1] (feeling) The reader pities Corvus." in seen["distil"][0]
    assert kept(app) == [("feeling", "The reader pities Corvus.")]
    mark(api, bid, [2], read=False)
    assert listed(api)["recollections"] == [] and listed(api)["hidden"] == 1


# --- the reader is in control ---------------------------------------------------------------------
def test_deleted_notes_stay_deleted(env, monkeypatch):
    """Break caught: the reader deletes a note and the next batch writes it again from the same
    old messages; or "delete all" leaves messages waiting that turn into notes afterwards."""
    api, bid, app, _ = env
    seen = models(app, monkeypatch, changes=lambda prompt: [change("The reader reads at night.")] if NIGHT in prompt else [
        change("The reader likes lamps.")] if "I like lamps" in prompt else [])
    said(app, bid, NIGHT, *chatter(5))
    catch_up(app, bid)
    [entry] = listed(api)["recollections"]
    assert entry["kind"] == "about" and entry["message"] == NIGHT and entry["said_at"] and entry["book"] is None
    assert api.delete(f"/api/reader/recollections/{entry['id']}").status_code == 204
    assert api.delete(f"/api/reader/recollections/{entry['id']}").status_code == 404
    said(app, bid, *chatter(6, start=7))
    catch_up(app, bid)
    assert len(seen["distil"]) == 2 and NIGHT not in seen["distil"][1] and kept(app) == []

    said(app, bid, "I like lamps, I really do.", *chatter(2))         # three waiting, not yet looked at
    assert api.delete("/api/reader/recollections").status_code == 204
    said(app, bid, *chatter(6, start=20))
    catch_up(app, bid)
    assert "I like lamps" not in seen["distil"][-1] and kept(app) == [] and listed(api) == {"recollections": [], "hidden": 0}


def test_clearing_a_conversation_distils_first_and_keeps_the_notes(env, monkeypatch):
    """Breaks caught: "Clear" throws away what the reader said before anyone looked at it; or
    takes the notes with the conversation; or the next conversation is never read because its
    messages reuse the numbers of the cleared ones."""
    api, bid, app, _ = env
    seen = models(app, monkeypatch, changes=lambda prompt: [change("The reader reads at night.")] if NIGHT in prompt else [
        change("The reader likes lamps.", message=6)] if "I like lamps" in prompt else [])
    said(app, bid, *chatter(6))
    catch_up(app, bid)                                               # these six have been looked at
    assert cursor(app, bid) > 0 and kept(app) == []
    said(app, bid, NIGHT, "And lamp 2?")                              # two more: below the batch size
    assert api.delete(f"/api/books/{bid}/conversation").status_code == 204
    assert api.get(f"/api/books/{bid}/conversation").json()["turns"] == []
    idle(app)
    assert kept(app) == [("about", "The reader reads at night.")] and len(seen["distil"]) == 2
    assert NIGHT in seen["distil"][1] and "lamp number 3" not in seen["distil"][1]     # only what was waiting
    [entry] = listed(api)["recollections"]
    assert entry["message"] == NIGHT                                 # the note keeps the message it came from
    # A new conversation in the same book: SQLite gives its messages the numbers of the cleared
    # ones, all at or below where the old conversation had been read to.
    ids = said(app, bid, *chatter(5), "I like lamps, I really do.")
    assert min(ids) == 1 and cursor(app, bid) == 0
    catch_up(app, bid)
    assert ("about", "The reader likes lamps.") in kept(app) and NIGHT not in seen["distil"][-1]


def test_a_run_overtaken_by_clear_keeps_nothing_twice(env, monkeypatch):
    """Break caught: the conversation is cleared while its batch is being read; the batch handed
    over at the clear and the one already under way both store the same note."""
    api, bid, app, _ = env
    cleared = []

    def changes(prompt):
        if not cleared:
            cleared.append(api.delete(f"/api/books/{bid}/conversation").status_code)
        return [change("The reader reads at night.")]

    seen = models(app, monkeypatch, changes=changes)
    said(app, bid, NIGHT, *chatter(5))
    catch_up(app, bid)                                               # overtaken: keeps nothing itself
    idle(app)
    assert cleared == [204] and len(seen["distil"]) == 2
    assert kept(app) == [("about", "The reader reads at night.")]


# --- used in the chat inside a book ---------------------------------------------------------------
def test_notes_reach_the_writer_and_the_checker_in_the_book_chat_and_nothing_with_the_setting_off(env, monkeypatch):
    """Breaks caught: the notes are kept and never used; the checker is not given them, so "you
    told me part 1 wrecked you" is cut; or they are used with the setting off."""
    from app.stream_conversation import STREAM_SYSTEM
    api, bid, app, settings = env
    read_page(api, bid, 1, 1, 3)
    models(app, monkeypatch, story=["Mara"], changes=[
        change("Part 1 wrecked the reader: Mara and that lamp.", kind="feeling"),
        change("The reader wants short answers.", message=2)])
    said(app, bid, WRECKED, "Short answers please.", *chatter(4))
    catch_up(app, bid)
    app.state.catalog.clear_turns(bid)

    reply = "You told me part 1 wrecked you. You told me you love Tolstoy."
    seen = models(app, monkeypatch, reply=reply)
    ask(api, bid, "Remind me how I took part 1?")
    prompt, system = seen["reply"][-1], seen["system"][-1]
    assert block_of(prompt) == (
        BLOCK_HEAD + "\n- The reader wants short answers.\n- About this book: Part 1 wrecked the reader: Mara and that lamp.")
    assert recollections.RECOLLECTION_RULE in system and system != STREAM_SYSTEM
    assert system.replace(recollections.RECOLLECTION_RULE, "") == STREAM_SYSTEM
    for state in seen["checked"]:
        [passage] = [p for p in state["passages"] if p.startswith(recollections.PASSAGE_HEAD)]
        assert "Part 1 wrecked the reader: Mara and that lamp." in passage and "Tolstoy" not in passage
    assert not any("Tolstoy" in passage for state in seen["checked"] for passage in state["passages"])

    settings.reader_recollections = False
    seen = models(app, monkeypatch, reply=reply)
    ask(api, bid, "Remind me how I took part 1?")
    assert seen["system"][-1] == STREAM_SYSTEM and BLOCK_HEAD not in seen["reply"][-1]
    assert "wrecked the reader" not in json.dumps(seen["reply"] + seen["checked"])
    assert api.get("/api/reader/recollections").status_code == 404
    assert api.delete("/api/reader/recollections").status_code == 404
    assert api.delete("/api/reader/recollections/1").status_code == 404
    said(app, bid, *chatter(8))
    ask(api, bid, "One more?")
    idle(app)
    assert seen.get("distil") == []                                  # and nothing is distilled


def test_a_reader_with_no_notes_gets_the_prompt_they_had_before(env, monkeypatch):
    """Break caught: switching the feature on changes every reply's instructions, measured voice
    included, before a single note exists."""
    from app.stream_conversation import STREAM_SYSTEM
    api, bid, app, _ = env
    read_page(api, bid, 1, 1, 3)
    seen = models(app, monkeypatch, reply="Mara moved lamp 2 in part 1.")
    ask(api, bid)
    assert seen["system"][-1] == STREAM_SYSTEM and BLOCK_HEAD not in seen["reply"][-1]
    assert not any(p.startswith(recollections.PASSAGE_HEAD) for state in seen["checked"] for p in state["passages"])


def test_the_notes_never_make_a_check_larger_than_the_checker_accepts(env, monkeypatch):
    """Break caught: a sentence check over the checker's size limit is refused and its sentence
    dropped (6ad, seen with real models when passages were added to the home chat). The notes
    are one more passage, always kept: when the largest check of a reply has no room for
    everything, read text gives way, for the writer and the checker alike."""
    from app import stream_conversation
    api, bid, app, _ = env
    read_page(api, bid, 1, 1, 3)
    models(app, monkeypatch, changes=[change("The reader wants short answers.")])
    said(app, bid, "Short answers please, as short as you can make them.", *chatter(5))
    catch_up(app, bid)
    app.state.catalog.clear_turns(bid)
    seen = models(app, monkeypatch, reply="Mara moved lamp 2 in part 1.")
    ask(api, bid)
    assert "short answers" in block_of(seen["reply"][-1])            # room enough: given
    room = stream_conversation.check_room("What just happened?", [])

    def size(passages):
        return sum(len(json.dumps(p, ensure_ascii=False).encode()) + 2 for p in passages)
    largest = max(size(state["passages"]) for state in seen["checked"])
    assert largest <= room
    before = seen["checked"][0]["passages"]
    # The same reply with room for the passages it had before and not one byte more.
    monkeypatch.setattr(stream_conversation, "CHECK_INPUT_LIMIT",
                        stream_conversation.CHECK_INPUT_LIMIT - room + largest - 1)
    app.state.catalog.clear_turns(bid)
    seen = models(app, monkeypatch, reply="Mara moved lamp 2 in part 1.")
    events = ask(api, bid)
    assert [e["text"] for e in events if e["type"] == "sentence"] == ["Mara moved lamp 2 in part 1."]
    assert "short answers" in block_of(seen["reply"][-1]) and seen["system"][-1] != stream_conversation.STREAM_SYSTEM
    for state in seen["checked"]:
        assert size(state["passages"]) < largest
        assert [p for p in state["passages"] if p.startswith(recollections.PASSAGE_HEAD)] == [before[-1]]
        dropped = [p for p in before if p not in state["passages"]]
        assert dropped and all(p not in seen["reply"][-1] for p in dropped)    # read text gave way


def test_a_fault_in_reading_the_notes_costs_the_reader_the_notes_not_the_reply(env, monkeypatch):
    """Break caught: a damaged row in the store, and no question in any book is answered."""
    api, bid, app, _ = env
    read_page(api, bid, 1, 1, 3)
    seen = models(app, monkeypatch, reply="Mara moved lamp 2 in part 1.")
    monkeypatch.setattr("app.stream_conversation.recall", lambda *args, **more: 1 / 0)
    events = ask(api, bid)
    assert [e["text"] for e in events if e["type"] == "sentence"] == ["Mara moved lamp 2 in part 1."]
    assert BLOCK_HEAD not in seen["reply"][-1]


def test_a_long_title_or_a_long_note_cannot_push_the_block_past_its_budget(env):
    """Break caught: the budget counts the notes and forgets what is wrapped round them."""
    _api, bid, _app, _ = env
    here = {bid: ["inc", {"1": [[0, 10]]}]}
    held = [note(n, "feeling", "The reader felt " + "very " * 40 + "sad.", tied=True, book_id=bid, drew_on=here)
            for n in range(1, 40)]
    for current, title in ((bid, "x"), ("another", "A Very Long Title " * 30)):
        chosen = select_recollections(held, read={bid: ["inc", {1: [(0, 10)]}]}, book_id=current,
                                      query="very sad", titles={bid: title})
        assert chosen and len(recollection_block(chosen)) <= PROMPT_BUDGET
        assert len(recollections.recollection_passage(chosen)) <= PROMPT_BUDGET + len(recollections.PASSAGE_HEAD)


# --- when the distilling call fails or returns nonsense -------------------------------------------
def cursor(app, bid):
    catalog = app.state.catalog
    return catalog.get_recollection_cursor(bid, incarnation=catalog.get_book(bid)["incarnation"])


def test_a_failed_distilling_call_keeps_nothing_and_is_tried_again(env, monkeypatch):
    """Break caught: a provider error loses those messages for good."""
    api, bid, app, _ = env
    models(app, monkeypatch, fail=RuntimeError("provider down"))
    said(app, bid, NIGHT, *chatter(5))
    with pytest.raises(RuntimeError):
        catch_up(app, bid)
    assert kept(app) == [] and cursor(app, bid) == 0
    models(app, monkeypatch, changes=[change("The reader reads at night.")])
    catch_up(app, bid)
    assert kept(app) == [("about", "The reader reads at night.")] and cursor(app, bid) > 0


@pytest.mark.parametrize("nonsense", [
    "I could not find anything.", {"changes": "none"}, {"notes": []}, None, 7,
    {"changes": [change("The reader reads at night.", kind="secret"),
                 change("The reader reads at night.", message=99), change("The reader reads at night.", message=0),
                 change("", message=1), change("Night.", message=1), {"kind": "about"}, "about", None,
                 change("The reader reads at night.", message="one"), change(["The reader reads at night."]),
                 change("The reader reads at night.", replaces=3),
                 {**change("The reader reads at night."), "book_id": "bk1"}]}])
def test_nonsense_from_the_distilling_call_keeps_nothing_and_is_not_asked_for_again(env, monkeypatch, nonsense):
    """Breaks caught: a malformed answer crashes the run on every reply for ever; or a change
    naming a message that does not exist is stored against some other message."""
    api, bid, app, _ = env
    seen = models(app, monkeypatch, changes=nonsense)
    said(app, bid, NIGHT, *chatter(5))
    catch_up(app, bid)
    catch_up(app, bid)
    assert kept(app) == [] and seen["judged"] == [] and len(seen["distil"]) == 1 and cursor(app, bid) > 0


def test_when_jev_cannot_judge_nothing_unchecked_is_kept_and_the_batch_waits(env, monkeypatch):
    """Break caught: an outage of the checker lets unchecked notes in, or throws the batch away."""
    from app.llm.jev import JevUnavailable
    api, bid, app, _ = env
    models(app, monkeypatch, changes=[change("The reader reads at night."), change("The reader likes lamps.")])

    def down(state, questions):
        raise JevUnavailable("down")
    app.state.client.jev.evaluate = down
    said(app, bid, NIGHT, *chatter(5))
    with pytest.raises(JevUnavailable):
        catch_up(app, bid)
    assert kept(app) == [] and cursor(app, bid) == 0
    models(app, monkeypatch, changes=[change("The reader reads at night.")])
    catch_up(app, bid)
    assert kept(app) == [("about", "The reader reads at night.")]


def test_a_malformed_judgment_keeps_that_note_out_and_the_rest_in(env, monkeypatch):
    """Break caught: a judgment with no probability is read as a yes."""
    api, bid, app, _ = env
    models(app, monkeypatch, changes=[change("The reader reads at night."), change("The reader likes lamps.")])
    honest = app.state.client.jev.evaluate

    def evaluate(state, questions):
        answers, usage = honest(state, questions)
        if "night" in state.get("note", ""):
            answers["stated"] = {"choice": "supported"}
        return answers, usage
    app.state.client.jev.evaluate = evaluate
    said(app, bid, NIGHT + " I like lamps.", *chatter(5))
    catch_up(app, bid)
    assert kept(app) == [("about", "The reader likes lamps.")]


def test_distilling_waits_after_the_provider_refuses_the_account(env, monkeypatch):
    """Break caught: every reply pays the writer for a batch the checker's provider will refuse."""
    from app.llm.jev import JevUnavailable
    from app.recollections import Recollector
    now, runs = [1000.0], []

    class Refused(Recollector):
        def catch_up(self, book_id):
            runs.append(book_id)
            raise JevUnavailable("Jev review unavailable", reason="no_credits")

    builder = Refused(None, None, lambda: None, clock=lambda: now[0])
    try:
        assert builder.ensure("b") and wait_for(lambda: not builder._queued)
        assert builder.ensure("b") is False and builder.ensure("c") is False
        now[0] += 301
        assert builder.ensure("b") and wait_for(lambda: len(runs) == 2)
    finally:
        builder.close()


# --- housekeeping ---------------------------------------------------------------------------------
def test_the_setting_follows_the_read_map_unless_set_and_cannot_be_on_without_it():
    from app.config import Settings
    stream = dict(_env_file=None, reader_page_context=True, jev_enabled=True, reader_conversation_jev=True)
    assert Settings(_env_file=None).reader_recollections is False
    assert Settings(**stream).reader_recollections is True
    assert Settings(**stream, reader_recollections=False).reader_recollections is False
    assert Settings(**stream, reader_read_map=False).reader_recollections is False
    with pytest.raises(ValueError):
        Settings(**stream, reader_read_map=False, reader_recollections=True)


def test_removing_a_book_takes_the_notes_tied_to_it_and_leaves_the_rest(env, monkeypatch):
    """Break caught: a note about a book that is gone stays in the store for ever, hidden, using
    up the cap; or removing a book forgets that the reader reads at night."""
    api, bid, app, _ = env
    read_page(api, bid, 1, 1, 3)
    models(app, monkeypatch, story=["Mara"], changes=[
        change("The reader finds Mara unbearable.", kind="taste"),
        change("The reader reads at night.", message=2)])
    said(app, bid, "Mara is unbearable!", NIGHT, *chatter(4))
    catch_up(app, bid)
    assert api.delete(f"/api/books/{bid}").status_code in (200, 204)
    assert kept(app) == [("about", "The reader reads at night.")]
    assert listed(api) == {"hidden": 0, "recollections": [{
        "id": 2, "kind": "about", "text": "The reader reads at night.", "book": None, "message": None,
        "said_at": listed(api)["recollections"][0]["said_at"]}]}


def test_the_same_functions_read_the_conversation_across_books(env, monkeypatch):
    """Part two calls these from the home chat. Break caught: they only work with a book id."""
    api, bid, app, _ = env
    catalog = app.state.catalog
    book = catalog.get_book(bid)
    seen = models(app, monkeypatch, changes=[change("The reader reads at night.")])
    where = dict(incarnation="home", position_epoch=0, atom=0, offset=0,
                 drew_on={bid: [book["incarnation"], {}]})
    for message in (NIGHT, *chatter(5)):
        catalog.add_turn(None, role="user", content=message, **where)
        catalog.add_turn(None, role="assistant", content="Mm.", **where)
    catch_up(app, None)
    assert kept(app) == [("about", "The reader reads at night.")] and len(seen["distil"]) == 1
    assert catalog.get_recollection_cursor(None, incarnation="home") > 0
    assert [c["phase"] for c in catalog.get_costs(bid) if c["phase"].startswith("recollect")]   # spend has a book
