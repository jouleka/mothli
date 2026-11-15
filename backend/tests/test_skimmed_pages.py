"""Skimmed pages: turned quickly one after another. Remembered as skimmed, never as read, so
nothing on them reaches a model; used only to ask the reader, once they settle, whether they
read those chapters or were just getting here."""
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
