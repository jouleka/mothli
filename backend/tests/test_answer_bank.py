"""valid_answer: what a complete, cited answer must look like (kept from the removed answer bank)."""
import pytest

from app.answer_bank import valid_answer


def payload():
    return {"as_of_chapter": 2, "insufficient_evidence": False,
            "claims": [{"text": "Berenice met Aldric at the forge.", "citation_ids": [1]}],
            "citations": [{"id": 1, "ordinal": 2, "chapter_key": "two", "href": "two.xhtml",
                           "title": "Two", "excerpt": "Berenice met Aldric at the forge."}]}


def test_a_complete_cited_answer_is_valid():
    assert valid_answer(payload())


@pytest.mark.parametrize("fault", ["future", "missing", "uncited", "empty", "insufficient"])
def test_valid_answer_rejects_invalid_answers(fault):
    """Was test_reader_preparation.py::test_bank_rejects_invalid_answers; the bank half went with
    the answer cache (ADR 0043 R3), the check itself stays. A citation from past the reader's
    place ("future") is never a valid answer."""
    value = payload()
    if fault == "future":
        value["citations"][0]["ordinal"] = 3
    if fault == "missing":
        del value["citations"][0]["excerpt"]
    if fault == "uncited":
        value["claims"][0]["citation_ids"] = [99]
    if fault == "empty":
        value["claims"] = []
    if fault == "insufficient":
        value["insufficient_evidence"] = True
    assert not valid_answer(value)
