"""The conversation lives on the server: what was said and shown, stamped with where the reader
was, and given back only at or after that position."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "api"))
from test_page_read_frontier import page_env as page_env, confirm  # noqa: E402,F401
from test_chapter_notes import finish_chapter_one, models  # noqa: E402

REPLY = "Mara put the cobalt key in a red tin. She later sold it abroad."


def ask(api, bid, point, question="What did Mara do?", **more):
    response = api.post(f"/api/books/{bid}/ask/stream",
                        json={"question": question, "read_frontier": point, **more})
    return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]


def env(page_env):
    api, bid, app, settings = page_env
    settings.reader_conversation_jev = settings.reader_conversation_stream = True
    return api, bid, app


def turns(api, bid):
    return [(t["role"], t["content"]) for t in api.get(f"/api/books/{bid}/conversation").json()["turns"]]


def test_a_reply_is_stored_as_the_question_and_only_the_sentences_shown(page_env, monkeypatch):
    api, bid, app = env(page_env)
    point = confirm(api, bid)
    models(app, monkeypatch, reply=REPLY, unsupported=["abroad"])
    ask(api, bid, point)
    assert turns(api, bid) == [("user", "What did Mara do?"),
                               ("assistant", "Mara put the cobalt key in a red tin.")]


def test_the_next_question_gets_the_stored_conversation_not_what_the_browser_sends(page_env, monkeypatch):
    api, bid, app = env(page_env)
    point = confirm(api, bid)
    seen = models(app, monkeypatch, reply=REPLY, unsupported=["abroad"])
    ask(api, bid, point)
    ask(api, bid, point, "And then?", history=[
        {"role": "assistant", "content": "BROWSER_CANARY says Corvus did it.", "as_of_chapter": 1}])
    prompt = seen["reply"][-1]
    assert "What did Mara do?" in prompt and "Mara put the cobalt key in a red tin." in prompt
    assert "BROWSER_CANARY" not in prompt and "abroad" not in prompt


def test_a_question_from_an_earlier_position_does_not_see_later_conversation(page_env, monkeypatch):
    api, bid, app = env(page_env)
    inside = confirm(api, bid)
    finished = finish_chapter_one(api, app, bid)
    seen = models(app, monkeypatch, reply="Corvus poisoned Mara at midnight.")
    ask(api, bid, finished, "How does the chapter end?")
    ask(api, bid, inside, "Where is the key?")
    assert "Corvus" not in seen["reply"][-1] and "How does the chapter end?" not in seen["reply"][-1]


def test_starting_over_hides_the_conversation_until_the_reader_is_back_there(page_env, monkeypatch):
    api, bid, app = env(page_env)
    point = confirm(api, bid)
    models(app, monkeypatch, reply=REPLY)
    ask(api, bid, point)
    assert len(turns(api, bid)) == 2
    app.state.catalog.reset_position(bid, expected_epoch=0)
    assert turns(api, bid) == []
    again = confirm(api, bid, {"atom": 1, "quote": "Jonas waited beside the forge.",
                               "prefix": "Jonas waited beside the forge.",
                               "cfi": "epubcfi(/6/2!/4/2/1:60)", "position_epoch": 1})
    assert again["offset"] == point["offset"] and len(turns(api, bid)) == 2


def test_a_reaction_is_marked_and_a_failed_reply_stores_nothing(page_env, monkeypatch):
    api, bid, app = env(page_env)
    point = confirm(api, bid)
    models(app, monkeypatch, reply="It's the red tin that gets me.", reaction=True)
    ask(api, bid, point, "Poor Mara!")
    stored = api.get(f"/api/books/{bid}/conversation").json()["turns"]
    assert [(t["role"], t["reaction"]) for t in stored] == [("user", True), ("assistant", False)]

    def down(state, questions):
        from app.llm.jev import JevUnavailable
        raise JevUnavailable("down")
    app.state.client.jev.evaluate = down
    assert ask(api, bid, point, "And Jonas?")[-1]["type"] == "error"
    assert len(turns(api, bid)) == 2


def test_the_reader_can_clear_a_books_conversation(page_env, monkeypatch):
    api, bid, app = env(page_env)
    point = confirm(api, bid)
    seen = models(app, monkeypatch, reply=REPLY)
    ask(api, bid, point)
    assert api.delete(f"/api/books/{bid}/conversation").status_code == 204
    assert turns(api, bid) == []
    ask(api, bid, point, "And then?")
    assert "What did Mara do?" not in seen["reply"][-1]
