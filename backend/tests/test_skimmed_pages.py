"""Skimmed pages: turned quickly one after another. Remembered as skimmed, never as read, so
nothing on them reaches a model; used only to ask the reader, once they settle, whether they
read those chapters or were just getting here."""
import json

from test_chapter_notes import models
from test_read_map import map_env as map_env, anchor, ask, line, read_page


def skim(api, bid, chapter, first, last, *, to_chapter=None, expect=200):
    response = api.put(f"/api/books/{bid}/read-page", json={
        "start": anchor(chapter, first, after=False), "end": anchor(to_chapter or chapter, last, after=True),
        "skimmed": True})
    assert response.status_code == expect, response.text
    return response.json()


def shares(api, bid):
    return {c["atom"]: c["read"] for c in api.get(f"/api/books/{bid}/read-map").json()["chapters"]}


def test_a_skimmed_page_is_not_read_and_never_reaches_a_model(map_env, monkeypatch):
    api, bid, app, _ = map_env
    seen = models(app, monkeypatch, reply="Corvus moved lamp 31 in part 2.")
    skim(api, bid, 1, 1, 5)
    skim(api, bid, 1, 6, 10)
    assert shares(api, bid) == {1: 0, 2: 0, 3: 0}
    assert api.get(f"/api/books/{bid}/read-frontier").json()["atom"] == 0      # the place did not move
    read_page(api, bid, 2, 30, 34)
    ask(api, bid, "What did Mara do with lamp 3?")
    prompt = seen["reply"][-1]
    assert line(2, 30) in prompt and "Mara moved" not in prompt
    assert all("Mara moved" not in " ".join(s["passages"]) for s in seen["checked"])
    profile = api.get("/api/reader/profile").json()["books"][0]
    assert profile["chapters_read"] == 0


def test_settling_after_skimming_asks_about_the_chapters_gone_through_quickly(map_env):
    api, bid, _, _ = map_env
    skim(api, bid, 1, 1, 5)
    skim(api, bid, 1, 6, 10)
    skim(api, bid, 2, 1, 5)
    landed = read_page(api, bid, 3, 10, 12)
    assert landed["skipped"] == {"atoms": [1, 2], "chapters": "chapters 1 and 2", "skimmed": [1, 2]}


def test_a_jump_records_nothing_so_only_skimmed_chapters_are_named_as_skimmed(map_env):
    api, bid, _, _ = map_env
    landed = read_page(api, bid, 3, 10, 12)                       # a leap: nothing skimmed on the way
    assert landed["skipped"]["skimmed"] == []
    skim(api, bid, 1, 1, 5)                                       # went back and flicked through part 1
    assert read_page(api, bid, 3, 13, 14)["skipped"]["skimmed"] == [1]


def test_a_skim_is_reported_only_while_turning_and_asks_nothing_itself(map_env):
    api, bid, _, _ = map_env
    read_page(api, bid, 3, 10, 12)
    assert "skipped" not in skim(api, bid, 3, 13, 14)


def test_skimmed_pages_are_checked_like_read_ones(map_env):
    api, bid, _, _ = map_env
    skim(api, bid, 1, 1, 60, to_chapter=3, expect=422)           # not neighbours
    skim(api, bid, 2, 9, 3, expect=422)                          # ends before it starts
    response = api.put(f"/api/books/{bid}/read-page", json={
        "start": {**anchor(1, 1, after=False), "quote": "Nothing like this is in the book at all."},
        "end": anchor(1, 2, after=True), "skimmed": True})
    assert response.status_code == 422


def test_saying_a_chapter_was_not_read_or_reading_fresh_forgets_its_skimmed_pages(map_env):
    api, bid, _, _ = map_env
    skim(api, bid, 1, 1, 5)
    skim(api, bid, 2, 1, 5)
    api.put(f"/api/books/{bid}/read-chapters", json={"atoms": [1], "read": False})
    assert read_page(api, bid, 3, 10, 12)["skipped"]["skimmed"] == [2]
    epoch = api.get(f"/api/books/{bid}/position").json()["position_epoch"]
    api.post(f"/api/books/{bid}/position/reset", json={"position_epoch": epoch, "fresh": True})
    assert read_page(api, bid, 3, 13, 14, epoch=epoch + 1)["skipped"]["skimmed"] == []


# --- Mothli is told which chapters were gone through quickly: their numbers and nothing else -----
def everything_sent(app, monkeypatch):
    """Every word any model is given from here on: the writer's and the second look's
    instructions and prompt, and each state and question Jev is asked about. Call after ``models``."""
    sent = []
    write, judge = app.state.client.complete, app.state.client.jev.evaluate

    def complete(system, user, tier="cheap", schema=None):
        sent.append(system + "\n" + user)
        return write(system, user, tier=tier, schema=schema)

    def evaluate(state, questions):
        sent.append(json.dumps([state, questions], ensure_ascii=False))
        return judge(state, questions)

    monkeypatch.setattr(app.state.client, "complete", complete)
    app.state.client.jev.evaluate = evaluate
    return sent


def place(prompt):
    return prompt.split("WHERE THE READER IS:\n")[1].split("\n\nHOW THIS APP WORKS")[0]


QUICK_REPLY = ["We went through chapters 1 and 2 pretty quickly, so I didn’t take them in.", "Did you read them?"]


def skim_two_chapters_and_settle(api, bid):
    skim(api, bid, 1, 1, 5)
    skim(api, bid, 1, 6, 10)
    skim(api, bid, 2, 1, 5)
    read_page(api, bid, 3, 10, 12)


def test_mothli_is_told_which_chapters_were_gone_through_quickly_and_nothing_that_is_on_them(map_env, monkeypatch):
    """Catches: skimmed chapters reaching the writer as "not read with you"; and anything of a
    skimmed page other than its chapter's number reaching the writer, the second look or Jev."""
    api, bid, app, _ = map_env
    seen = models(app, monkeypatch, reply=" ".join(QUICK_REPLY))
    sent = everything_sent(app, monkeypatch)
    skim_two_chapters_and_settle(api, bid)
    events = ask(api, bid, "Can you catch me up?")
    assert place(seen["reply"][-1]) == (
        "“The Lamp Book” by Ada Writer: the reader is in chapter 3 of 3 now. Read with you: part of "
        "chapter 3. The two of you went through chapters 1 and 2 quickly here, before where the reader "
        "is: the pages were turned too fast to be read, so you did not take them in. The reader may "
        "have read them all the same; only the reader can say.")
    everything = "\n".join(sent)
    assert line(3, 10) in everything and len(seen["checked"]) == 2             # the models were reached
    # Chapters 1 and 2 are all "Mara moved lamp..." and "Corvus moved lamp...": none of it anywhere.
    assert "Mara" not in everything and "Corvus" not in everything and "in part 1" not in everything
    assert all(any("went through chapters 1 and 2 quickly here" in passage for passage in state["passages"])
               for state in seen["checked"])
    # The reply is shown, and the button under it counts exactly those two chapters.
    assert [e["text"] for e in events if e["type"] == "sentence"] == QUICK_REPLY
    assert events[-1]["answer"]["offers"] == [
        {"book_id": bid, "title": "The Lamp Book", "atoms": [1, 2], "chapters": "chapters 1 and 2"}]


def test_chapters_leapt_past_stay_not_read_with_mothli_and_only_a_skimmed_one_is_gone_through_quickly(
        map_env, monkeypatch):
    """Catches: every chapter before the place called "gone through quickly" once any page was
    skimmed, or none of them."""
    api, bid, app, _ = map_env
    seen = models(app, monkeypatch, reply="Vex moved lamp 11 in part 3.")
    read_page(api, bid, 3, 10, 12)                                 # a leap: nothing skimmed
    ask(api, bid)
    where = place(seen["reply"][-1])
    assert "Not read with you: chapters 1 and 2, which come before where the reader is." in where
    assert "quickly" not in where
    skim(api, bid, 2, 1, 5)                                        # went back and flicked through part 2
    read_page(api, bid, 3, 13, 14)
    events = ask(api, bid)
    where = place(seen["reply"][-1])
    assert "Not read with you: chapter 1, which" in where
    assert "The two of you went through chapter 2 quickly here" in where
    assert "offers" not in events[-1]["answer"]                    # a reply about the book offers nothing


def test_after_mothli_has_asked_it_is_told_so_and_after_the_reader_says_yes_nothing_is_left_to_ask(
        map_env, monkeypatch):
    """Catches: the writer told to ask again with every reply; and chapters the reader has counted
    as read still called gone through quickly."""
    api, bid, app, _ = map_env
    seen = models(app, monkeypatch, reply=" ".join(QUICK_REPLY))
    skim_two_chapters_and_settle(api, bid)
    offer = ask(api, bid, "Can you catch me up?")[-1]["answer"]["offers"][0]
    assert "already" not in place(seen["reply"][-1])
    ask(api, bid, "And where am I now?")
    where = place(seen["reply"][-1])
    assert "went through chapters 1 and 2 quickly here" in where
    assert where.endswith("You have already brought this up with the reader in this conversation: don't ask again.")
    # The reader presses "I've read chapters 1 and 2".
    assert api.put(f"/api/books/{bid}/read-chapters", json={"atoms": offer["atoms"], "read": True}).status_code == 200
    events = ask(api, bid, "And now?")
    where = place(seen["reply"][-1])
    assert "Read with you: chapters 1 and 2 in full, and part of chapter 3." in where
    assert "quickly" not in where and "already" not in where and "Not read with you" not in where
    assert "offers" not in events[-1]["answer"]                    # nothing gone past is left to count
