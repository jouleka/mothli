"""One-call budget, scoped proof, complete batch coverage and fail-closed typed review."""
from types import SimpleNamespace

import pytest

from app.ask import AskDraft
from app.ask_evidence import AskEvidenceError
from app.config import Settings
from app.conversation_jev import review_packet, review_with_jev
from app.conversation_pipeline import answer_conversation

SOURCES = [dict(id=1, ordinal=1, chapter_key="one", title="One", text="Mara lent Jonas a key."),
           dict(id=2, ordinal=1, chapter_key="one", title="One", text="Nell kept three coins.")]
VALUE = {"insufficient_evidence": False,
         "claims": [{"text": "Mara lent Jonas a key.", "citation_ids": [1]}]}
DRAFT = AskDraft.model_validate(VALUE)
SETTINGS = SimpleNamespace(jev_min_probability=.95, jev_min_confidence=.85)


def positive(questions):
    return {k: ({"type":"noul", "noul":.999 if k == "addresses_message" else .001}
                if q["type"] == "noul" else {"choice":"supported",
                "probabilities":{"supported":.999}, "confidence":.99}) for k,q in questions.items()}


def test_own_citations_are_frozen_and_uncited_body_cannot_be_borrowed():
    state, questions = review_packet(DRAFT, SOURCES[:1], question="Who lent the key?", frontier=1)
    assert state["claim_1"]["cited_passages"] == [SOURCES[0]["text"]]
    assert SOURCES[1]["text"] not in str(state)
    assert "claim_1.cited_passages" in questions["c0_whole"]["instructions"]
    assert "omits_requirement" in questions and "addresses_message" in questions


@pytest.mark.parametrize("failure", ["missing", "rejected", "uncertain", "low-probability", "invalid-probability"])
def test_typed_batch_never_approves_partial_negative_or_uncertain_results(failure):
    def run(state, questions):
        answers = positive(questions)
        if failure == "missing":
            answers.pop("c0_whole")
        elif failure in ("rejected", "uncertain"):
            answers["omits_requirement"]["noul"] = .99 if failure == "rejected" else .5
        elif failure == "low-probability":
            answers["c0_whole"]["probabilities"]["supported"] = .94
        else:
            answers["addresses_message"]["noul"] = True
        return answers
    with pytest.raises(AskEvidenceError):
        review_with_jev(DRAFT, SOURCES[:1], question="Who lent the key?", frontier=1,
                        run=run, settings=SETTINGS)


@pytest.mark.parametrize("rejected", [False, True])
def test_candidate_makes_exactly_one_generation_and_one_batch_without_repair(rejected):
    calls = []
    def complete(*args, **kwargs):
        calls.append(kwargs["phase"])
        return {**VALUE, "clarification": None}
    def run(state, questions):
        calls.append("jev")
        result = positive(questions)
        if rejected:
            result["omits_requirement"]["noul"] = .99
        return result
    def review(draft, sources, **kwargs):
        review_with_jev(draft, sources, run=run, settings=SETTINGS, **kwargs)
    def invoke():
        return answer_conversation("Who lent Jonas a key?", SOURCES, frontier=1,
            known_names=[], complete=complete, score=lambda _: {
                "future_entity_leaks": [], "prolepsis_hits": [], "unsupported_event_bindings": []},
            max_output_tokens=4096, review=review, max_attempts=1)
    if rejected:
        with pytest.raises(AskEvidenceError):
            invoke()
    else:
        assert invoke()[0] == DRAFT
    assert calls == ["synthesis", "jev"]


def test_experiment_requires_explicit_local_page_and_jev_configuration():
    with pytest.raises(ValueError, match="requires local page context and Jev"):
        Settings(_env_file=None, reader_conversation_jev=True)
