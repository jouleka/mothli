"""The chat inside a book keeps every sentence check inside the checker's size limit.

A check over the limit is refused and its sentence dropped without a word (6ad, seen with real
models in the home chat). Here a reader deep in a long book has ten passages of read text, full
chapter notes, the place line, the app help and what Mothli remembers about them: together over
the limit. Where the reader is, how the app works and what Mothli remembers are always kept; read
text and notes give way, best first, and the writer is given exactly what its checker is given.
Stand-in writer and Jev throughout; each test names the break it catches.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_check_size import worst_check  # noqa: E402
from test_read_map import map_env as map_env, read_page  # noqa: E402,F401
from test_recollections import ask, models  # noqa: E402

from app import stream_conversation  # noqa: E402
from app.ask import ask_prompt  # noqa: E402
from app.chapter_notes import note_passages, notes_block  # noqa: E402
from app.reader_place import APP_HELP  # noqa: E402
from app.recollections import BLOCK_HEAD, PASSAGE_HEAD, recollection_block  # noqa: E402
from app.stream_conversation import CHECK_INPUT_LIMIT, PROMPT_END, STREAM_SYSTEM  # noqa: E402

REPLY = "Mara moved lamp 2 in part 1."
PLACE, HELP = "Where the reader is: ", "How this app works: "


@pytest.fixture
def env(map_env):
    api, bid, app, settings = map_env
    settings.reader_recollections = True
    settings.reader_story_notes = True
    return api, bid, app, settings


def remember(app, count):
    """``count`` notes about the reader, each near the longest a note may be."""
    app.state.catalog.keep_recollections(
        [{"kind": "about", "text": f"The reader, note {n}, " + "likes long quiet evenings with a good book " * 4,
          "tied": False, "book_id": None, "drew_on": None, "said": "x",
          "said_at": "2026-11-15T00:00:00+00:00", "replaces": None} for n in range(count)], cap=10_000)


def source(n):
    # Distinct from its first words, so "in the prompt" and "among the passages" cannot be confused.
    return {"id": n, "ordinal": 1, "title": "Part 1", "source_offset": n * 2000,
            "text": f"Read passage number {n:02d} opens here. " + "Mara moved a lamp across the long room. " * 37}


NOTES = [{"title": f"Part {k}", "atom": k,
          "sentences": [f"Note {k}.{i} says that Mara carried lamp {i} " + "a" * 40 + " down the hall." for i in range(10)]}
         for k in range(1, 7)]


def deep_reader(app, monkeypatch):
    """Ten full passages of read text, retrieval's first six then four more, and 6,000 characters of notes."""
    pool = [source(n) for n in range(1, 13)]
    monkeypatch.setattr(stream_conversation, "compact_candidates", lambda *args, **more: [dict(s) for s in pool])
    monkeypatch.setattr(stream_conversation, "complete_ask_context", lambda query, selected, *a, **k: list(selected))
    monkeypatch.setattr(stream_conversation, "usable_notes", lambda *args, **more: NOTES)
    remember(app, 5)


def texts(sources):
    return [" ".join(s["text"].split()) for s in sources]


def largest(seen):
    state = max(seen["checked"], key=lambda state: len(str(state["passages"])))
    return worst_check(state["passages"], state["reader_message"])


def test_the_largest_check_of_a_deep_readers_reply_stays_inside_the_limit(env, monkeypatch):
    """Break caught: nothing holds the book chat's passages to the checker's limit, so late in a
    long reply every check is refused and true sentences are dropped (on main: over the limit)."""
    api, bid, app, _ = env
    read_page(api, bid, 1, 1, 3)
    deep_reader(app, monkeypatch)
    seen = models(app, monkeypatch, reply=REPLY)
    events = ask(api, bid)
    assert [e["text"] for e in events if e["type"] == "sentence"] == [REPLY]
    assert largest(seen) <= CHECK_INPUT_LIMIT
    # Read text gave way: the first passage retrieval ranked is kept, not all ten are.
    passages = seen["checked"][0]["passages"]
    assert texts([source(1)])[0] in passages
    assert sum(p in passages for p in texts(source(n) for n in range(1, 11))) < 10


def test_where_the_reader_is_how_the_app_works_and_what_mothli_remembers_always_reach_the_checker(env, monkeypatch):
    """Break caught: what Mothli remembers is left out whenever the passages are full, which is
    always for a reader deep in a long book (on main: missing); or the place line or the app help
    are pushed out by read text."""
    api, bid, app, _ = env
    read_page(api, bid, 1, 1, 3)
    deep_reader(app, monkeypatch)
    seen = models(app, monkeypatch, reply=REPLY)
    ask(api, bid)
    for state in seen["checked"]:
        heads = [p for p in state["passages"] if p.startswith((PLACE, HELP, PASSAGE_HEAD))]
        assert [h[:12] for h in heads] == [PLACE[:12], HELP[:12], PASSAGE_HEAD[:12]]
        assert HELP + APP_HELP in state["passages"]
    assert "note 4" in seen["checked"][0]["passages"][-1]               # all five notes
    assert block_has_all(seen["reply"][-1]) and seen["system"][-1] != STREAM_SYSTEM


def block_has_all(prompt):
    return BLOCK_HEAD in prompt and all(f"note {n}," in prompt for n in range(5))


def test_the_writer_is_given_exactly_the_passages_its_checker_is_given(env, monkeypatch):
    """Break caught: passages are fitted for the checker but the prompt is still built from every
    source and note, so the writer writes from text its checker lacks and those sentences are cut.
    (On main nothing gives way, so the second assertion fails there.)"""
    api, bid, app, _ = env
    read_page(api, bid, 1, 1, 3)
    deep_reader(app, monkeypatch)
    seen = models(app, monkeypatch, reply=REPLY)
    ask(api, bid)
    prompt, passages = seen["reply"][-1], seen["checked"][0]["passages"]
    candidates = texts(source(n) for n in range(1, 11)) + note_passages(NOTES)
    assert [text in prompt for text in candidates] == [text in passages for text in candidates]
    assert any(text not in passages for text in candidates)            # something did give way
    assert all(state["passages"] == passages for state in seen["checked"])


def spy(monkeypatch):
    """What retrieval and recall returned, to build the prompt and passages main built from them."""
    got = {}
    for name in ("compact_candidates", "complete_ask_context", "recall"):
        real = getattr(stream_conversation, name)

        def wrapped(*args, real=real, name=name, **more):
            got[name] = real(*args, **more)
            return got[name]
        monkeypatch.setattr(stream_conversation, name, wrapped)
    return got


def as_on_main(got, question, place, notes):
    """The prompt and the checker's passages exactly as main builds them, with nothing dropped."""
    core = got["complete_ask_context"]
    chosen = {s["id"] for s in core}
    sources = core + [s for s in got["compact_candidates"] if s["id"] not in chosen][:10 - len(core)]
    remembered = got.get("recall") or []
    # Chapter 1 is only part read: no chapter is completed yet (2026-11-20, the writer was told
    # the chapter the reader is in was completed).
    prompt = ask_prompt(question, sources, frontier=0).removesuffix(PROMPT_END)
    if notes:
        prompt += "\n\n" + notes_block(notes)
    prompt += "\n\nWHERE THE READER IS:\n" + place + "\n\nHOW THIS APP WORKS:\n" + APP_HELP
    passages = texts(sources) + note_passages(notes) + [PLACE + place, HELP + APP_HELP]
    if remembered:
        prompt += "\n\n" + recollection_block(remembered)
        passages.append(PASSAGE_HEAD + " ".join(text.removeprefix("- ") for _, text in remembered))
    return prompt + "\n\nReply to the reader now.", passages


def test_an_ordinary_reply_where_everything_fits_is_built_exactly_as_before(env, monkeypatch):
    """Break caught: the fitting reorders or drops passages of an ordinary reply that was inside the
    limit all along. A guard: this holds on main too."""
    api, bid, app, _ = env
    read_page(api, bid, 1, 1, 3)
    notes = NOTES[:1]
    monkeypatch.setattr(stream_conversation, "usable_notes", lambda *args, **more: notes)
    remember(app, 2)
    got = spy(monkeypatch)
    seen = models(app, monkeypatch, reply=REPLY)
    ask(api, bid)
    passages = seen["checked"][0]["passages"]
    place = next(p for p in passages if p.startswith(PLACE)).removeprefix(PLACE)
    assert got["compact_candidates"] and got["recall"]
    assert (seen["reply"][-1], passages) == as_on_main(got, seen["checked"][0]["reader_message"], place, notes)


def test_with_no_recollections_and_the_setting_off_the_prompts_are_as_before(env, monkeypatch):
    """Break caught: the change alters the system prompt or the prompt of a reply that fits. A
    guard: this holds on main too."""
    api, bid, app, settings = env
    settings.reader_recollections = False
    read_page(api, bid, 1, 1, 3)
    monkeypatch.setattr(stream_conversation, "usable_notes", lambda *args, **more: [])
    got = spy(monkeypatch)
    seen = models(app, monkeypatch, reply=REPLY)
    ask(api, bid)
    passages = seen["checked"][0]["passages"]
    place = next(p for p in passages if p.startswith(PLACE)).removeprefix(PLACE)
    assert "recall" not in got and seen["system"][-1] == STREAM_SYSTEM
    assert (seen["reply"][-1], passages) == as_on_main(got, seen["checked"][0]["reader_message"], place, [])
