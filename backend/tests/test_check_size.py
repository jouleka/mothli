"""A sentence check has a size limit, and a check over it is refused: the sentence is dropped,
and with every sentence refused the reader gets no reply at all. Seen with real models when six
book suggestions were added to the home chat's passages: four of six replies were refused."""
from app.llm.jev_budget import estimate_jev_input
from app.stream_conversation import (CHECK_INPUT_LIMIT, MAX_SENTENCE_CHARS, MAX_SENTENCES, check_room,
                                     fit_passages, sentence_packet)


def worst_check(passages, question):
    """The estimate for the last sentence of the longest reply: the largest check there can be."""
    sentence = "s" * MAX_SENTENCE_CHARS
    earlier = tuple("e" * MAX_SENTENCE_CHARS for _ in range(MAX_SENTENCES - 1))
    state, questions, *_ = sentence_packet(sentence, earlier, passages, question=question, identities=())
    return estimate_jev_input("jev-1.14.0", state, questions)


def test_fixed_passages_stay_and_ranked_ones_fill_what_room_is_left_in_order():
    # Breaks if the place, the app help or a suggestion can be pushed out by read text.
    fixed = ["place " + "p" * 300, "help " + "h" * 900]
    ranked = [f"passage {n} " + "x" * 1500 for n in range(10)]
    kept = fit_passages(fixed, ranked, budget=6000)
    assert kept[:2] == fixed
    assert kept[2:] == ranked[:3]                      # in rank order, as many as fit


def test_a_shorter_passage_further_down_still_gets_in():
    # Breaks if one long passage ends the list while room remains for a short one.
    kept = fit_passages([], ["a" * 900, "b" * 5000, "c" * 900], budget=2000)
    assert kept == ["a" * 900, "c" * 900]


def test_the_largest_check_of_a_full_home_reply_stays_inside_the_limit():
    # Ten passages of read text, six suggestions, the place and the help: over the limit as they
    # stand. Breaks if a change adds passages without making room for them.
    question = "any new book you'd recommend for me? " * 20
    fixed = (["Where the reader is: " + "w" * 600, "How this app works: " + "a" * 1200]
             + ["A book Mothli may suggest. " + "b" * 420 for _ in range(6)])
    ranked = ["From “Pride and Prejudice”, chapter 3: " + "r" * 1500 for _ in range(10)] + ["notes " + "n" * 3000] * 2
    assert worst_check(fixed + ranked, question) > CHECK_INPUT_LIMIT
    fitted = fit_passages(fixed, ranked, budget=check_room(question, identities=()))
    assert fitted[:len(fixed)] == fixed and len(fitted) > len(fixed)
    assert worst_check(fitted, question) <= CHECK_INPUT_LIMIT


def test_text_outside_the_basic_letters_is_counted_as_the_limit_counts_it():
    # The limit counts bytes. Breaks if room is counted in characters: Russian text is two bytes a letter.
    question = "что почитать дальше?"
    ranked = ["Из книги: " + "ж" * 1500 for _ in range(10)]
    fitted = fit_passages([], ranked, budget=check_room(question, identities=()))
    assert 0 < len(fitted) < 10
    assert worst_check(fitted, question) <= CHECK_INPUT_LIMIT
