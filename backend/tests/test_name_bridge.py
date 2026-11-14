"""Other name forms found in the read pages: exact text in, typed judgment out, fail closed."""
import pytest

from app.known_names import (BRIDGE_MIN, bridge_candidates, first_mention, name_runs,
                             text_same_person)
from app.retrieval_passages import passage_windows

TAIL = "The third son was Alexey. It is of him that I must now speak, for he wore the cassock."
OPEN = "He was only twenty. First of all, this young man, Alyosha, was not a fanatic. Ivan knew it."


def test_runs_are_adjacent_capitalised_words_without_sentence_openers():
    assert name_runs("But Fyodor Pavlovitch met Ivan. He left for Petersburg.") == [
        "Fyodor Pavlovitch", "Ivan", "Petersburg"]


def test_sentence_openers_are_not_offered_as_names():
    passages = ["Immediately Fyodor Pavlovitch rose. First he bowed. Such was Alyosha. Yes, Ivan knew Alexey."]
    assert bridge_candidates({"alyosha"}, passages) == ["Immediately Fyodor Pavlovitch", "Alexey", "Ivan"]


def test_candidates_exclude_the_reader_form_and_rank_by_frequency():
    assert bridge_candidates({"alyosha"}, [TAIL, OPEN + " Then came Ivan again."])[:2] == ["Ivan", "Alexey"]
    assert "Alyosha" not in bridge_candidates({"alyosha"}, [TAIL, OPEN])


def test_first_mention_returns_the_previous_chapter_tail_and_never_reads_ahead():
    chapters = [{"ordinal": 1, "key": "a", "text": TAIL}, {"ordinal": 2, "key": "b", "text": OPEN},
                {"ordinal": 3, "key": "c", "text": "UNREAD Alyosha is Rakitin."}]
    hits = first_mention({"alyosha"}, chapters[:2], passage_windows)
    assert [(text, chapter["ordinal"]) for text, chapter in hits] == [(TAIL, 1), (OPEN, 2)]
    assert first_mention({"zossima"}, chapters[:2], passage_windows) == []


@pytest.mark.parametrize("score,expected", [
    (BRIDGE_MIN, [["Alyosha", "Alexey"]]), (BRIDGE_MIN - .01, [])])
def test_only_a_confident_judgment_links_two_forms(score, expected):
    asked = {}

    def run(state, questions):
        asked.update(state=state, questions=questions)
        return {key: {"noul": score if state["candidates"][key] == "Alexey" else .05} for key in questions}

    assert text_same_person("Alyosha", {"alyosha"}, [TAIL, OPEN], run=run) == expected
    # Jev sees only the supplied read passages and candidate strings taken from them.
    assert asked["state"]["name_passages"] == [TAIL, OPEN]
    assert set(asked["state"]["candidates"].values()) <= set(name_runs(TAIL + " " + OPEN))


@pytest.mark.parametrize("bad", [None, True, 1.2, "0.99"])
def test_malformed_or_missing_judgments_link_nothing(bad):
    assert text_same_person("Alyosha", {"alyosha"}, [TAIL, OPEN],
                            run=lambda state, q: {key: {"noul": bad} for key in q}) == []
    assert text_same_person("Alyosha", {"alyosha"}, [TAIL, OPEN], run=lambda state, q: {}) == []
    assert text_same_person("Alyosha", {"alyosha"}, ["no names here."], run=lambda *a: 1 / 0) == []
