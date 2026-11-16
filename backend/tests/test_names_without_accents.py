"""A name typed without its accent is the name in the book (6ak).

The English Karamazov spells "Adelaïda" and "Miüsov"; a reader types "Adelaida" and "Miusov". Where
words are compared, for the search and for whether the reader's question names someone they have
read about, an accented Latin letter and its plain form are the same letter. The text itself is
never changed. Measured at the Karamazov fixture's ``inside-5`` with stand-in models; each test
names the break it catches.
"""
import json
import sys
import unicodedata
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

SCRIPTS = Path(__file__).parents[1] / "scripts"
FIXTURES = Path(__file__).parent / "eval" / "fixtures"
sys.path.insert(0, str(Path(__file__).parent / "api"))
from test_ingest import InlineExecutor  # noqa: E402
from test_read_map import ask  # noqa: E402
from test_recollections import models  # noqa: E402

import app.ask as ask_module  # noqa: E402
from app.ask import retrieve_ask_sources  # noqa: E402
from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402
from app.unicode_text import fold_accents  # noqa: E402


@pytest.fixture(scope="module")
def karamazov(tmp_path_factory):
    """The Karamazov fixture read to ``inside-5`` by the read map."""
    from scripts.companion_eval import enter_by_map, karamazov_epub
    settings = Settings(_env_file=None, allow_stub=True, reader_page_context=True, reader_conversation_pipeline=True,
                        data_dir=str(tmp_path_factory.mktemp("karamazov") / "data"))
    app = create_app(settings, ingest_executor=InlineExecutor())
    settings.reader_conversation_jev = settings.reader_conversation_stream = settings.reader_read_map = True
    cases = json.loads((SCRIPTS / "companion_eval_cases.json").read_text(encoding="utf-8"))
    inside = next(p for p in cases["books"]["karamazov"]["positions"] if p["name"] == "inside-5")
    with TestClient(app) as api:
        bid = api.post("/api/books", files={"file": ("k.epub", karamazov_epub(), "application/epub+zip")}).json()["book_id"]
        enter_by_map(api, app, bid, inside, app.state.worker.source_chapters(bid))
        yield api, app, bid


def asked(app, monkeypatch, api, bid, question, **stand_ins):
    app.state.catalog.clear_turns(bid)          # each question on its own, with no earlier messages
    seen = models(app, monkeypatch, **stand_ins)
    return seen, ask(api, bid, question)


def test_folding_takes_off_accents_on_latin_letters_only():
    """Break caught: folding letters that are not an accented Latin letter (ß, æ, ø, Œ, a Cyrillic
    or Greek letter) or changing anything but the accents."""
    assert fold_accents("Adelaïda Miüsov Païssy café Ñandú garçon") == "Adelaida Miusov Paissy cafe Nandu garcon"
    assert fold_accents("Straße Æsir Øresund Œcumenical") == "Straße Æsir Øresund Œcumenical"
    assert fold_accents("Дмитрий Йошкар Δημήτρης 阿廖沙") == "Дмитрий Йошкар Δημήτρης 阿廖沙"
    assert fold_accents("é") == "e" and fold_accents("") == ""


@pytest.mark.parametrize("question,name", [("why did Adelaida leave?", "Adelaïda"), ("who is Miusov?", "Miüsov")])
def test_a_name_typed_without_its_accent_finds_passages_naming_that_person(karamazov, monkeypatch, question, name):
    """Break caught: the search comparing words exactly, so "Adelaida" shares no word with a
    passage about "Adelaïda" and the checker gets none that names her (measured: 0 of 11)."""
    api, app, bid = karamazov
    seen, _ = asked(app, monkeypatch, api, bid, question, reply="That is so.")
    assert seen["checked"]
    assert all(any(name in passage for passage in state["passages"]) for state in seen["checked"])


@pytest.mark.parametrize("question", ["why did Adelaida leave?", "why did Adelaïda leave?"])
@pytest.mark.parametrize("written", ["Adelaida", "Adelaïda"])
def test_a_true_sentence_naming_a_read_person_without_the_accent_is_shown(karamazov, monkeypatch, question, written):
    """A guard: it held before the change (measured). Break caught: the stricter name check
    treating a read person's plain spelling as a name not yet met and removing the sentence."""
    api, app, bid = karamazov
    sentence = f"{written} ran away with a divinity student."
    _, events = asked(app, monkeypatch, api, bid, question, reply=sentence)
    assert [e["text"] for e in events if e["type"] == "sentence"] == [sentence]


@pytest.mark.parametrize("question", ["why did Adelaida leave?", "who is Miusov?", "who is Paissy?"])
def test_a_read_name_typed_without_its_accent_is_not_a_name_not_yet_met(karamazov, monkeypatch, question):
    """Break caught: the question's "Adelaida" not matching the "Adelaïda" read, so with nothing
    shown the reply is the fixed line for "we're not there yet" about someone from chapter 1."""
    api, app, bid = karamazov
    _, events = asked(app, monkeypatch, api, bid, question, reply="Nothing true here.", doubts=["Nothing"])
    answer = events[-1]["answer"]
    assert answer["insufficient_evidence"] and "status" not in answer


def test_a_name_not_in_the_pages_read_is_still_a_name_not_yet_met(karamazov, monkeypatch):
    """A guard. Break caught: folding making every name count as read."""
    api, app, bid = karamazov
    _, events = asked(app, monkeypatch, api, bid, "who is Smerdyakov?", reply="Nothing true here.", doubts=["Nothing"])
    assert events[-1]["answer"]["status"] == "ahead"


def chapters_of(transform):
    texts = {f"c{n}": transform((FIXTURES / f"ch{n:02d}.txt").read_text(encoding="utf-8")) for n in range(1, 6)}
    chapters = [{"revealed_at": n, "chapter_key": f"c{n}"} for n in range(1, 6)]
    return chapters, texts


def search(question, chapters, texts, *, pool):
    return [(s["chapter_key"], s["text"]) for s in retrieve_ask_sources(
        question, bookmark=5, chapters=chapters, read_chapter=texts.get, semantic_hits=[], atoms={},
        candidate_pool=pool)]


def plain(text):
    """The fixture's text with every accent taken off, so no word in it has one."""
    return "".join(ch for ch in unicodedata.normalize("NFD", text) if not unicodedata.combining(ch))


@pytest.mark.parametrize("pool", [False, True])
@pytest.mark.parametrize("question", ["why did Adelaïda leave?", "who is Miüsov?", "who is Païssy?",
                                      "what did Miüsov think of Adelaïda Ivanovna?"])
def test_search_for_a_question_typed_with_the_accent_is_unchanged(monkeypatch, question, pool):
    """A guard. Break caught: folding changing the results for a question typed with the accent,
    which found its passages before; the same search with folding switched off is the reference."""
    chapters, texts = chapters_of(lambda text: text)
    folded = search(question, chapters, texts, pool=pool)
    monkeypatch.setattr(ask_module, "fold_accents", lambda text: text)
    assert folded == search(question, chapters, texts, pool=pool)
    assert folded


@pytest.mark.parametrize("pool", [False, True])
@pytest.mark.parametrize("question", ["who is Grigory?", "why did Ivan leave the family of Yefim Petrovitch?",
                                      "what's an elder?", "why did Adelaida leave?"])
def test_search_in_text_without_accents_is_unchanged(monkeypatch, question, pool):
    """A guard. Break caught: folding changing the results where neither the question nor the
    book has an accent; the same search with folding switched off is the reference."""
    chapters, texts = chapters_of(plain)
    assert all(fold_accents(text) == text for text in texts.values())
    folded = search(question, chapters, texts, pool=pool)
    monkeypatch.setattr(ask_module, "fold_accents", lambda text: text)
    assert folded == search(question, chapters, texts, pool=pool)
    assert folded
