"""Contract/concurrency regressions; real model quality is measured separately."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.ask import AskDraft, AskSafetyError
from app.ask_evidence import AskEvidenceError
from app.conversation_pipeline import answer_conversation, compact_candidates, review_draft, review_escalation
from app.evidence_text import field_segments


SOURCES = [dict(id=i, ordinal=1, chapter_key="one", title="One", text=text)
           for i, text in enumerate(("Mara lent Jonas a key.", "Nell kept three coins."), 1)]
DRAFT = AskDraft.model_validate({"insufficient_evidence": False, "claims": [
    {"text": "Mara lent Jonas a key.", "citation_ids": [1]}]})


def audit():
    return {"claim_audits": [{"claim_id": 1, "units": [
        {"unit_id": u["id"], "supported": True,
         "evidence": [{"source_id": 1, "passage_ids": [1]}], "diagnostic": None}
        for u in field_segments(DRAFT.claims[0].text, "answer")]}]}


def review(complete):
    return review_draft(DRAFT, SOURCES, question="Who lent Jonas a key?", frontier=1,
                        scene_sources=SOURCES, coverage_sources=SOURCES, complete=complete,
                        max_output_tokens=4096)


def test_reviews_overlap_and_each_receives_only_its_own_positive_evidence():
    barrier = threading.Barrier(2)
    def complete(system, user, *, schema, phase):
        barrier.wait(timeout=2)
        if phase == "citation-review":
            packet = json.loads(user.split("\n", 1)[1])
            assert "sources" not in packet
            assert [s["source_id"] for s in packet["claims"][0]["sources"]] == [1]
            return audit()
        packet = json.loads(user[user.index('{'):])
        assert [s["id"] for s in packet["claims"][0]["cited_passages"]] == [1]
        return {"references_future": False, "unsupported_claims": [], "missing_details": []}
    assert review(complete) is None


@pytest.mark.parametrize("corruption", ["missing-unit", "foreign-citation", "forged-span", "omission"])
def test_no_partial_missing_or_cross_claim_proof_is_published(corruption):
    def complete(system, user, *, schema, phase):
        if phase == "judge":
            return {"references_future": False, "unsupported_claims": [],
                    "missing_details": ["quantity"] if corruption == "omission" else []}
        value = audit()
        unit = value["claim_audits"][0]["units"][0]
        if corruption == "missing-unit":
            value["claim_audits"][0]["units"] = []
        if corruption == "foreign-citation":
            unit["evidence"][0]["source_id"] = 2
        if corruption == "forged-span":
            unit["evidence"][0]["passage_ids"] = [999]
        return value
    with pytest.raises((AskEvidenceError, AskSafetyError)):
        review(complete)


def test_failed_sibling_is_joined_before_request_scope_is_released():
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    def complete(system, user, *, schema, phase):
        if phase == "citation-review":
            entered.set()
            assert release.wait(timeout=2)
            finished.set()
            return audit()
        assert entered.wait(timeout=2)
        raise RuntimeError("review unavailable")
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(review, complete)
        assert entered.wait(timeout=2)
        assert not future.done()
        release.set()
        with pytest.raises(RuntimeError, match="unavailable"):
            future.result(timeout=2)
    assert finished.is_set()


def test_unavailable_source_and_future_binding_never_reach_review():
    phases = []
    def complete(system, user, *, schema, phase, tier="cheap"):
        phases.append(phase)
        return {"insufficient_evidence": False, "claims": [
            {"text": "Mara lent Jonas a key.", "citation_ids": [999]}]}
    with pytest.raises(AskSafetyError):
        answer_conversation("Who lent Jonas a key?", SOURCES, frontier=1,
            known_names=[], complete=complete, score=lambda _: {}, max_output_tokens=4096)
    assert phases == ["synthesis", "synthesis"]


def test_compaction_retains_every_byte_at_its_original_location_and_repairs_hints():
    pool = [
        dict(SOURCES[0], id=1, source_offset=0, text="Mara lent"),
        dict(SOURCES[0], id=2, source_offset=0, context_ids=[1]),
        dict(SOURCES[0], id=3, source_offset=40, text="Mara lent", context_ids=[1]),
    ]
    result = compact_candidates(pool)
    assert [s["id"] for s in result] == [2, 3]
    assert result[0]["context_ids"] == []
    assert result[1]["context_ids"] == [2]
    assert pool[0]["text"] == "Mara lent"  # Never mutate a caller's retrieval pool.


def test_ambiguous_reference_returns_only_a_claim_free_clarification():
    phases = []
    def complete(system, user, *, schema, phase, tier="cheap"):
        phases.append(phase)
        return {"insufficient_evidence": True, "claims": [], "clarification": "person"}
    draft, sources, clarification = answer_conversation("Was that fair to him?", SOURCES,
        frontier=1, known_names=[], complete=complete, score=lambda _: {}, max_output_tokens=4096)
    assert not draft.claims and not sources and clarification == "Who do you mean here?"
    assert phases == ["synthesis"]


@pytest.mark.parametrize("failure", [None, "semantic", "missing", "foreign", "span"])
def test_escalation_requires_both_complete_own_source_proof_and_semantic_verdict(failure):
    calls = []
    def complete(system, user, *, schema, phase, tier):
        calls.append(phase)
        assert schema.__name__ == "ConversationEscalation" and tier == "large"
        packet = json.loads(user.split("\n", 1)[1])
        assert [s["source_id"] for s in packet["claims"][0]["sources"]] == [1]
        value = {**audit(), "references_future":False, "unsupported_claims":[], "missing_details":[]}
        if failure == "semantic":
            value["missing_details"] = ["condition missing"]
        if failure == "missing":
            value["claim_audits"] = []
        if failure == "foreign":
            value["claim_audits"][0]["units"][0]["evidence"][0]["source_id"] = 2
        if failure == "span":
            value["claim_audits"][0]["units"][0]["evidence"][0]["passage_ids"] = [999]
        return value
    def invoke():
        return review_escalation(DRAFT, SOURCES, question="Who lent the key?", complete=complete,
                                 max_output_tokens=4096)
    if failure:
        with pytest.raises(AskEvidenceError):
            invoke()
    else:
        assert invoke() is None
    assert calls == ["conversation-escalation"]
