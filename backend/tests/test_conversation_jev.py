"""Typed evidence preparation, own-citation checks and a single Terra draft."""
from types import SimpleNamespace

import pytest

from app.ask import AskDraft
from app.ask_evidence import AskEvidenceError
from app.config import Settings
from app.conversation_jev import prepare_context, review_packet, review_with_jev, review_recap_prefix, JevNeedsReasoning
from app.conversation_pipeline import answer_conversation

SOURCES = [dict(id=1, ordinal=1, chapter_key="one", title="One", text="Mara lent Jonas a key."),
           dict(id=2, ordinal=1, chapter_key="one", title="One", text="Nell kept three coins.")]
VALUE = {"insufficient_evidence": False,
         "claims": [{"text": "Mara lent Jonas a key.", "citation_ids": [1]}]}
DRAFT = AskDraft.model_validate(VALUE)
SETTINGS = SimpleNamespace(jev_min_probability=.95, jev_min_confidence=.85)


def positive(questions):
    return {k: ({"type":"noul", "noul":.001}
                if q["type"] == "noul" else {"choice":"supported",
                "probabilities":{"supported":.999}, "confidence":.99}) for k,q in questions.items()}


def test_own_citations_are_frozen_and_uncited_body_cannot_be_borrowed():
    state, questions = review_packet(DRAFT, SOURCES[:1], question="Who lent the key?", frontier=1)
    assert state["claim_1"]["cited_passages"] == [SOURCES[0]["text"]]
    assert SOURCES[1]["text"] not in str(state)
    assert "claim_1.cited_passages" in questions["c0_relation"]["instructions"]
    assert "c0_omitted_qualification" in questions
    assert len(questions) == 4 and "candidate_response" not in state


@pytest.mark.parametrize("failure", ["missing", "rejected", "uncertain", "low-probability", "invalid-probability"])
def test_typed_batch_never_approves_partial_negative_or_uncertain_results(failure):
    def run(state, questions):
        answers = positive(questions)
        if failure == "missing":
            answers.pop("c0_relation")
        elif failure in ("rejected", "uncertain"):
            answers["c0_omitted_qualification"]["noul"] = .99 if failure == "rejected" else .5
        elif failure == "low-probability":
            answers["c0_relation"]["probabilities"]["supported"] = .79
        else:
            answers["c0_omitted_qualification"]["noul"] = True
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
            result["c0_omitted_qualification"]["noul"] = .99
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


def test_preparation_keeps_conflicting_evidence_and_never_changes_source_bodies():
    sources = [dict(id=i, ordinal=1, chapter_key="one", title="One", text=f"Passage {i}.")
               for i in range(1, 9)]
    seen = []
    def run(state, questions):
        seen.extend(state["passages"].values())
        return {k: ({"noul": .99 if k == "p8_conflict" else .01}
                    if q["type"] == "noul" else {"probabilities": {
                        "direct": .8, "context": .1, "unrelated": .1}})
                for k, q in questions.items()}
    selected, notes = prepare_context("No, he stole it.", sources, frontier=1,
        run=run, settings=SimpleNamespace(jev_model="jev-1.14.0"))
    assert seen == [s["text"] for s in sources]
    assert selected[0] == sources[-1] and len(selected) == 6
    assert "[8]" in notes and "not book facts" in notes
    with pytest.raises(AskEvidenceError):
        prepare_context("Where?", sources, frontier=0, run=run, settings=SETTINGS)


def test_preparation_plans_all_packets_before_any_paid_call():
    def run(*args):
        pytest.fail("oversized source must fail before spending")
    with pytest.raises(Exception, match="bounded Jev context"):
        prepare_context("Where?", [{**SOURCES[0], "text": "x" * 30001}], frontier=1,
                        run=run, settings=SimpleNamespace(jev_model="jev-1.14.0"))


@pytest.mark.parametrize("first_bad", [False, True])
def test_recap_keeps_only_a_useful_verified_prefix_with_original_citations(first_bad):
    draft = AskDraft(insufficient_evidence=False,claims=[
        {"text":"Mara lent Jonas a key.","citation_ids":[1]},
        {"text":"Nell kept three coins.","citation_ids":[2]}])
    def run(state, questions):
        values = positive(questions)
        for k in values:
            if k.endswith("_scene"):
                values[k] = {"noul":.99}
        values["c0_unestablished_location" if first_bad else "c1_unestablished_location"]["noul"] = .5
        return values
    def invoke():
        return review_recap_prefix(draft,SOURCES,question="Catch me up",frontier=1,run=run,settings=SETTINGS)
    if first_bad:
        with pytest.raises(JevNeedsReasoning):
            invoke()
    else:
        value = invoke()
        assert value.claims == draft.claims[:1]
        assert value.claims[0].citation_ids == [1]


def test_only_unsettled_claim_ids_are_sent_for_reasoning():
    draft = AskDraft(insufficient_evidence=False,claims=[
        {"text":"Mara lent Jonas a key.","citation_ids":[1]},
        {"text":"Nell kept three coins.","citation_ids":[2]}])
    def run(state,questions):
        answers=positive(questions)
        answers["c1_unestablished_location"]["noul"]=.5
        return answers
    with pytest.raises(JevNeedsReasoning) as exc:
        review_with_jev(draft,SOURCES,question="What happened?",frontier=1,run=run,settings=SETTINGS)
    assert exc.value.claim_ids == [2]
