"""Asking Mothli the same thing twice ("Catch me up", then again hours later) gets a new reply.

The same pages gave the same recap near word for word, which no friend does. The writer is now
told when the reader asked before, what it said, and whether they have read on since.
"""
from datetime import UTC, datetime

from app.stream_conversation import asked_before

THEN = "2026-11-26T08:00:00+00:00"
NOW = datetime(2026, 11, 26, 11, 0, tzinfo=UTC)


def turns(question="Catch me up", reply="We're in St. Petersburgh with the letters.", atom=3, offset=100):
    return [{"role": "user", "content": question, "atom": atom, "source_offset": offset, "created_at": THEN},
            {"role": "assistant", "content": reply, "atom": atom, "source_offset": offset, "created_at": THEN}]


def test_first_time_has_no_note():
    assert asked_before("Catch me up", [], {"atom": 3, "offset": 100}, NOW) is None
    assert asked_before("Who is Margaret?", turns(), {"atom": 3, "offset": 100}, NOW) is None


def test_a_repeat_names_when_and_what_was_said_and_asks_for_something_new():
    note = asked_before("catch me up!", turns(), {"atom": 3, "offset": 100}, NOW)
    assert "a few hours ago" in note
    assert "We're in St. Petersburgh with the letters." in note
    assert "same place" in note
    assert "not with the same summary" in note


def test_another_way_of_asking_for_a_catch_up_counts_and_reading_on_is_noticed():
    note = asked_before("What's happened so far?", turns(), {"atom": 4, "offset": 0}, NOW)
    assert "further on in the book" in note


def test_an_unanswered_question_is_not_a_repeat():
    only_asked = turns()[:1] + [{"role": "user", "content": "hello", "atom": 3, "source_offset": 100,
                                 "created_at": THEN}]
    assert asked_before("Catch me up", only_asked, {"atom": 3, "offset": 100}, NOW) is None


def test_short_chat_said_twice_is_not_a_repeat():
    assert asked_before("thanks!", turns(question="Thanks"), {"atom": 3, "offset": 100}, NOW) is None


def test_the_same_question_word_for_word_counts_and_moving_back_claims_nothing():
    question = "Why does he want to go north?"
    note = asked_before(question, turns(question=question), {"atom": 2, "offset": 0}, NOW)
    assert "same message" in note
    assert "further on" not in note and "same place" not in note
    assert "answer it again in fresh words" in note
    assert "If the same words now mean something else" in note


def test_an_old_word_for_word_repeat_is_left_alone_but_an_old_catch_up_is_not():
    question = "Why does he want to go north?"
    old = turns(question=question) + [
        {"role": role, "content": f"filler {i}", "atom": 3, "source_offset": 100, "created_at": THEN}
        for i in range(8) for role in ("user", "assistant")]
    assert asked_before(question, old, {"atom": 3, "offset": 100}, NOW) is None
    old_recap = turns() + old[2:]
    assert asked_before("Catch me up", old_recap, {"atom": 3, "offset": 100}, NOW) is not None


def test_a_long_earlier_reply_is_quoted_up_to_a_sentence_end():
    reply = "Walton writes to Margaret. " * 80
    note = asked_before("Catch me up", turns(reply=reply), {"atom": 3, "offset": 100}, NOW)
    quoted = note.split('replied: "', 1)[1].split('"\n', 1)[0]
    assert quoted.endswith("Margaret.") and len(quoted) <= 1200
