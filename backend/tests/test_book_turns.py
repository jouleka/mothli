"""Earlier turns inside a book follow what is read of it now. A turn is stored with the book's read
map as it was when its prompt was built, and is said or shown again only while all of that is
still read: on the book's conversation, in the reader profile's reactions, and in what the writer
and the checker are given with the next question."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "api"))
from test_chapter_notes import models  # noqa: E402
from test_read_map import anchor, ask, map_env as map_env, read_page  # noqa: E402,F401

SURFACES = ("conversation", "reactions", "writer")
# Neither line is a sentence of the book, so finding one in a prompt means the turn was passed on.
MARA = ("Poor Mara, that was cruel!", "Mara was the one who carried lamp 2.")
CORVUS = ("Corvus frightens me, honestly!", "Corvus was the one who carried lamp 9.")
VEX = ("What a brute Vex is!", "Vex was the one who carried lamp 50.")


def mark(api, bid, atoms, read=True):
    assert api.put(f"/api/books/{bid}/read-chapters", json={"atoms": atoms, "read": read}).status_code == 200


def read_fresh(api, bid, epoch=0):
    response = api.post(f"/api/books/{bid}/position/reset", json={"position_epoch": epoch, "fresh": True})
    assert response.status_code == 200, response.text


def shown(api, bid):
    return [turn["content"] for turn in api.get(f"/api/books/{bid}/conversation").json()["turns"]]


def reactions(api):
    return api.get("/api/reader/profile").json()["books"][0]["reactions"]


def say(app, monkeypatch, api, bid, exchange):
    """One exchange, the reader's message taken as a reaction to the book, shown on both screens."""
    models(app, monkeypatch, reply=exchange[1], reaction=True)
    ask(api, bid, exchange[0])
    assert shown(api, bid)[-2:] == list(exchange) and reactions(api)[-1] == exchange[0]


def carried(surface, exchange, api, bid, app, monkeypatch):
    """Which lines of an earlier exchange one surface still carries.

    ``writer`` asks a new question and looks at everything the writer and the checker were given.
    """
    if surface == "conversation":
        return [line for line in exchange if line in shown(api, bid)]
    if surface == "reactions":
        return [line for line in exchange if line in reactions(api)]
    seen = models(app, monkeypatch, reply="Hello.")
    ask(api, bid, "And then?")
    given = seen["reply"][-1] + json.dumps(seen["checked"], ensure_ascii=False)
    return [line for line in exchange if line in given]


# --- the two shapes found in audit 6v ------------------------------------------------------------
@pytest.mark.parametrize("surface", SURFACES)
def test_a_turn_written_from_chapters_only_marked_read_goes_when_the_book_is_read_fresh(
        map_env, monkeypatch, surface):
    """Catches: a turn written before any page was reported sits at place (0, 0), which always
    counted as read. After "read it fresh" it stayed on the book's conversation and among the
    profile's reactions, and went back to the writer as soon as anything else was read."""
    api, bid, app, _ = map_env
    mark(api, bid, [1])                                   # the reader's word only: no page, no place
    say(app, monkeypatch, api, bid, MARA)
    read_fresh(api, bid)
    if surface != "writer":
        assert carried(surface, MARA, api, bid, app, monkeypatch) == []
    mark(api, bid, [3])                                   # something else is read; chapter 1 is not
    assert carried(surface, MARA, api, bid, app, monkeypatch) == []


@pytest.mark.parametrize("surface", SURFACES)
def test_a_turn_about_a_chapter_goes_when_that_chapter_is_marked_not_read(map_env, monkeypatch, surface):
    """Catches: looking at where the reader was, not at what the turn was written from. The place
    (chapter 1) is still read; the chapter the turn is about (chapter 2) no longer is."""
    api, bid, app, _ = map_env
    read_page(api, bid, 1, 1, 3)
    mark(api, bid, [2])
    say(app, monkeypatch, api, bid, CORVUS)
    mark(api, bid, [2], read=False)
    assert carried(surface, CORVUS, api, bid, app, monkeypatch) == []


# --- and what must not be hidden -----------------------------------------------------------------
@pytest.mark.parametrize("surface", SURFACES)
def test_a_turn_stays_while_the_book_is_only_read_further_or_read_again(map_env, monkeypatch, surface):
    """Catches: hiding too much. More pages, another chapter and "read again" (which keeps what
    was read) leave every stretch the turn was written from still read."""
    api, bid, app, _ = map_env
    read_page(api, bid, 1, 1, 3)
    mark(api, bid, [2])
    say(app, monkeypatch, api, bid, CORVUS)
    read_page(api, bid, 1, 4, 9)
    mark(api, bid, [3])
    again = api.post(f"/api/books/{bid}/position/reset", json={"position_epoch": 0})
    assert again.status_code == 200, again.text
    expected = [CORVUS[0]] if surface == "reactions" else list(CORVUS)
    assert carried(surface, CORVUS, api, bid, app, monkeypatch) == expected


@pytest.mark.parametrize("surface", SURFACES)
def test_reading_the_chapter_again_brings_its_turn_back(map_env, monkeypatch, surface):
    """Catches: a turn dropped for good, or a rule that only knows a reset. Marking the chapter
    read again restores exactly what the turn was written from, after either way of losing it."""
    api, bid, app, _ = map_env
    read_page(api, bid, 1, 1, 3)
    mark(api, bid, [2])
    say(app, monkeypatch, api, bid, CORVUS)
    expected = [CORVUS[0]] if surface == "reactions" else list(CORVUS)
    mark(api, bid, [2], read=False)
    mark(api, bid, [2])
    assert carried(surface, CORVUS, api, bid, app, monkeypatch) == expected
    read_fresh(api, bid)
    mark(api, bid, [2])                                   # chapter 2 back, the page of chapter 1 not
    assert carried("conversation", CORVUS, api, bid, app, monkeypatch) == []
    read_page(api, bid, 1, 1, 3, epoch=1)
    assert carried(surface, CORVUS, api, bid, app, monkeypatch) == expected


def test_a_turn_asked_from_a_chapter_marked_not_read_follows_the_text_it_was_written_from(map_env, monkeypatch):
    """Catches: the reader's place deciding anything. The reader sits in chapter 2, says they have
    not read it, and asks about chapter 1. The old place rule hid that turn at once; it was
    written from chapter 1 alone, so it stays while chapter 1 is read and goes when it is not."""
    api, bid, app, _ = map_env
    read_page(api, bid, 2, 1, 3)
    mark(api, bid, [1])
    mark(api, bid, [2], read=False)                       # the reader's place is still in chapter 2
    models(app, monkeypatch, reply=MARA[1], reaction=True)
    ask(api, bid, MARA[0])
    for surface in SURFACES:
        expected = [MARA[0]] if surface == "reactions" else list(MARA)
        assert carried(surface, MARA, api, bid, app, monkeypatch) == expected, surface
    mark(api, bid, [1], read=False)
    mark(api, bid, [3])
    for surface in SURFACES:
        assert carried(surface, MARA, api, bid, app, monkeypatch) == [], surface


@pytest.mark.parametrize("surface", SURFACES)
def test_a_turn_stored_without_its_read_map_is_never_said_or_shown(map_env, monkeypatch, surface):
    """Catches: trusting a turn stored before turns kept the map. Its place is read, but nothing
    says what else it was written from, so it cannot be shown to be safe. The rows are left alone."""
    api, bid, app, _ = map_env
    read_page(api, bid, 1, 1, 3)
    catalog = app.state.catalog
    where = dict(incarnation=catalog.get_book(bid)["incarnation"], position_epoch=0, atom=1, offset=10)
    catalog.add_turn(bid, role="user", content=VEX[0], reaction=True, **where)
    catalog.add_turn(bid, role="assistant", content=VEX[1], **where)
    assert carried(surface, VEX, api, bid, app, monkeypatch) == []
    stored = [turn["content"] for turn in catalog.get_turns(bid, incarnation=where["incarnation"])]
    assert stored[:2] == list(VEX)
    assert api.delete(f"/api/books/{bid}/conversation").status_code == 204       # clearing removes those too
    assert catalog.get_turns(bid, incarnation=where["incarnation"]) == []


# --- further ways a stored turn could outlive the text it was written from -----------------------
def test_a_reply_being_written_while_its_chapter_is_marked_not_read_is_stored_but_not_shown_again(
        map_env, monkeypatch):
    """Catches: taking the map when the turn is stored rather than when its prompt was built. The
    reply was written with chapter 2 in the prompt; by the time it is stored the chapter is not
    read, and a map taken then would say the turn holds nothing of it."""
    api, bid, app, _ = map_env
    read_page(api, bid, 1, 1, 3)
    mark(api, bid, [2])
    models(app, monkeypatch, reply=CORVUS[1], reaction=True)
    write = app.state.client.complete

    def forget_then_write(system, user, tier="cheap", schema=None):
        if schema is None:
            app.state.catalog.clear_read_map(bid, atom=2)
        return write(system, user, tier=tier, schema=schema)
    monkeypatch.setattr(app.state.client, "complete", forget_then_write)
    ask(api, bid, CORVUS[0])
    for surface in SURFACES:
        assert carried(surface, CORVUS, api, bid, app, monkeypatch) == [], surface
    incarnation = app.state.catalog.get_book(bid)["incarnation"]
    stored = [turn["content"] for turn in app.state.catalog.get_turns(bid, incarnation=incarnation)]
    assert stored[:2] == list(CORVUS)


@pytest.mark.parametrize("surface", SURFACES)
def test_a_turn_written_under_the_read_map_is_not_given_back_by_the_older_position_rule(
        map_env, monkeypatch, surface):
    """Catches: a reader of stored turns that goes round the filter. With the read map switched
    off, three places read turns "at or before the reader's position". A turn written from a
    chapter read out of order sits at an early place and came back while that chapter, by the
    position rule, was far ahead of the reader."""
    api, bid, app, settings = map_env
    mark(api, bid, [3])                                   # the last chapter, read out of order
    say(app, monkeypatch, api, bid, VEX)
    settings.reader_read_map = False
    if surface != "writer":
        assert carried(surface, VEX, api, bid, app, monkeypatch) == []
    confirmed = api.put(f"/api/books/{bid}/read-frontier", json=anchor(1, 3, after=True))
    assert confirmed.status_code == 200 and confirmed.json()["atom"] == 1, confirmed.text
    assert carried(surface, VEX, api, bid, app, monkeypatch) == []


def test_the_older_position_rule_still_gives_back_its_own_turns_from_that_place_on(map_env, monkeypatch):
    """Catches: the read map switched off losing its conversation. A turn written at a position
    is stored with everything up to it, and comes back at or after that position, not before."""
    api, bid, app, settings = map_env
    settings.reader_read_map = False
    api.put(f"/api/books/{bid}/read-frontier", json=anchor(1, 6, after=True))
    models(app, monkeypatch, reply=MARA[1], reaction=True)
    ask(api, bid, MARA[0])
    for surface in SURFACES:
        expected = [MARA[0]] if surface == "reactions" else list(MARA)
        assert carried(surface, MARA, api, bid, app, monkeypatch) == expected, surface
    app.state.catalog.reset_position(bid, expected_epoch=0)                      # starting over
    assert shown(api, bid) == [] and reactions(api) == []
    api.put(f"/api/books/{bid}/read-frontier", json=anchor(1, 3, after=True, epoch=1))
    assert carried("writer", MARA, api, bid, app, monkeypatch) == []            # not yet back at sentence 6
    api.put(f"/api/books/{bid}/read-frontier", json=anchor(1, 6, after=True, epoch=1))
    assert shown(api, bid)[:2] == list(MARA)


def test_the_evaluation_seeds_earlier_turns_the_server_will_pass_on(map_env, monkeypatch):
    """Catches: the companion evaluation storing a case's earlier conversation without the map,
    so that every follow-up case would be asked with no conversation behind it."""
    from scripts import companion_eval as ev
    api, bid, app, settings = map_env
    read_page(api, bid, 1, 1, 3)
    boundary = api.get(f"/api/books/{bid}/read-frontier").json()
    ev.seed_turns(app, settings, bid, boundary, [{"role": "user", "content": MARA[0]},
                                                 {"role": "assistant", "content": MARA[1]}])
    assert carried("writer", MARA, api, bid, app, monkeypatch) == list(MARA)
    # And it is held to the same rule afterwards.
    mark(api, bid, [1], read=False)
    assert carried("conversation", MARA, api, bid, app, monkeypatch) == []


def test_the_shared_rule_reads_one_record_for_a_book_turn_and_a_home_turn():
    """Catches: a second scheme. One function decides for both conversations, from one record."""
    from app.read_map import written_from_read
    turn = lambda record: {"drew_on": record}  # noqa: E731
    then = {"b": ["one", {"2": [[0, 40]], "3": [[10, 20]]}]}
    assert written_from_read(turn(then), {"b": ["one", {2: [(0, 90)], 3: [(0, 20)]}]})
    assert not written_from_read(turn(then), {"b": ["one", {2: [(0, 90)]}]})              # chapter 3 gone
    assert not written_from_read(turn(then), {"b": ["one", {2: [(0, 39)], 3: [(0, 20)]}]})  # one character short
    assert not written_from_read(turn(then), {"b": ["two", {2: [(0, 90)], 3: [(0, 20)]}]})  # another shelf lifetime
    assert not written_from_read(turn(then), {})                                          # the book is gone
    assert not written_from_read(turn(None), {"b": ["one", {2: [(0, 90)]}]})               # no record
    assert written_from_read(turn({}), {})                                                # drew on no book
