import pytest
from app.arithmetic import arithmetic_reply, arithmetic_prefix


@pytest.mark.parametrize(
    "question,answer",
    [
        ("What's 2 + 2?", "2 + 2 = 4."),
        ("Calculate (3 + 5) * 2", "(3 + 5) * 2 = 16."),
        ("What is 3 / 2?", "3 / 2 = 3/2."),
        ("Compute 1 / 0", "Division by zero is undefined."),
    ],
)
def test_bounded_exact_calculations(question, answer):
    assert arithmetic_reply(question) == answer


@pytest.mark.parametrize(
    "question",
    [
        "1984",
        "2-3",
        "2/3",
        "Who dies in chapter 2?",
        "Why does 2 + 2 equal 4?",
        "I think 2 + 2 = 5",
        "2**999999999",
        "__import__('os').system('whoami')",
        "1+" + "1" * 200,
        "Calculate 2.5 * 3",
    ],
)
def test_nonformal_or_unbounded_input_is_not_executed_or_reclassified(question):
    assert arithmetic_reply(question) is None


def test_multipart_calculation_preserves_the_entire_second_request():
    assert arithmetic_prefix("What is 2 + 2, and who has the key?") == (
        "2 + 2 = 4.", "who has the key?")
    assert arithmetic_prefix("Who has the key, and what is 2 + 2?") is None
    assert arithmetic_prefix("Chapter 2-3, and tell me the ending") is None
    assert arithmetic_prefix("__import__('os'), and who has the key?") is None
