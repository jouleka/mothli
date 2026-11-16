"""The future-tense tripwire and the word "will".

"Will" the noun ("you surrender your will to him") is not the future tense. The tripwire cut
true sentences about elders and obedience for that word alone (audit 6ac). It is narrowed only
where the word before it makes the future tense impossible.
"""
import pytest

from app.eval.spoiler_gate.synthesis import PROLEPSIS_RE


def hits(sentence):
    return [match.group(0).lower() for match in PROLEPSIS_RE.finditer(sentence)]


@pytest.mark.parametrize("sentence", [
    "You surrender your will to him in the hope of becoming free of yourself.",
    "An elder takes your soul and your will into his soul and his will.",
    "Surrendering one’s will can lead to freedom from oneself.",
    "Surrendering one's will is the whole idea.",
    "It was their will against his.",
    "The will to obey is what the elder asks for.",
    "She had a will of iron.",
    "My will is not my own, the monk says.",
    "The dog obeyed its will, not hers.",
    "It bent our will to his.",
])
def test_the_noun_after_a_word_that_cannot_precede_the_future_tense_is_not_a_hit(sentence):
    # Breaks if the narrowing is removed: a true sentence about what has been read is cut.
    assert hits(sentence) == []


@pytest.mark.parametrize("sentence", [
    "Dmitri will kill his father.",
    "Lydia will elope before the summer is out.",
    "He will regret that.",
    "Everyone will love one another, Alyosha dreams.",
    # Words that can stand before the noun AND before the future tense stay hits.
    "Whoever marries her will regret it.",
    "The brothers left on their own will tear each other apart.",
    "The ill will recover and the good will prosper.",
    "Those set free will return.",
    "You surrender your will to him, and you will regret it.",
    # A comma or a second space is not the plain noun phrase: stay on the safe side.
    "The elder, in his  will be found the answer.",
    "Darcy shall propose.",
    "She is going to refuse him.",
])
def test_the_future_tense_is_still_a_hit(sentence):
    # Breaks if the narrowing lets a statement about what is coming through this check.
    assert hits(sentence)


@pytest.mark.parametrize("sentence", [
    "An elder is a spiritual guide to whom someone voluntarily gives up their own will in obedience.",
    "Giving up your own will is the hardest part.",
    "He bent her own will to his.",
    "It is not my own will, the monk says.",
    "Surrendering one’s own will is the whole idea.",
])
def test_own_will_after_a_possessive_is_the_noun(sentence):
    # Breaks if "their own will" is taken for the future tense: true sentences about elders
    # and obedience were cut for it (audit 6ae, afterwards).
    assert hits(sentence) == []


@pytest.mark.parametrize("sentence", [
    # "On their own" and "of his own" can stand before the future tense: these stay hits.
    "The brothers left on their own will tear each other apart.",
    "Left on her own will she manage?",
    "A house of his own will cost more than he has.",
    "People with money of their own will leave first.",
    "Those who own will pay.",
    "Whatever they own will be sold.",
])
def test_own_before_the_future_tense_is_still_a_hit(sentence):
    # Breaks if the narrowing for "own will" lets a statement about what is coming through.
    assert hits(sentence)
