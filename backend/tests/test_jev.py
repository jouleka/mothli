"""Transport, evidence identity and failure-policy tests. Fake verdicts are not quality evidence."""
import copy
import json
from types import SimpleNamespace

import httpx
import pytest

from app.ask_evidence import complete_evidence_citations
from app.config import Settings
from app.cost.limits import CostCeilingExceeded, usd_of
from app.llm.jev import JevClient, JevUnavailable
from app.llm.jev_budget import budgeted_jev
from app.llm import jev_tasks


def answer(options=jev_tasks.SUPPORT, choice="supported", p=0.99, confidence=0.97):
    probabilities = {key: (p if key == choice else (1-p)/(len(options)-1)) for key in options}
    return {"type": "choice", "choice": choice, "confidence": confidence,
            "probabilities": probabilities}


def positive(state, questions):
    return {key: answer(q["criteria"]) for key, q in questions.items()}


def source(i, text):
    return {"id": i, "ordinal": 1, "title": "Scene", "text": text}


def draft(text="Nell gave Orin a key.", ids=(1,)):
    return {"insufficient_evidence": False,
            "claims": [{"text": text, "citation_ids": list(ids)}]}


def test_positive_audit_covers_every_unchanged_unit_and_its_exact_original_citations():
    sources = [source(1, "Nell gave Orin a key. She left at noon."),
               source(2, "A later unrelated story about Pavo.")]
    value = draft("Nell gave Orin a key; she left at noon.")
    before = copy.deepcopy(value)

    def run(state, questions):
        assert len(questions) == 3  # whole claim plus both contextual units
        assert [s["source_id"] for s in state["claims"][0]["cited_sources"]] == [1]
        assert "Pavo" not in json.dumps(state)
        assert all("state.claims[0].cited_sources" in q["instructions"] for q in questions.values())
        return positive(state, questions)

    audit = jev_tasks.evidence(value, sources, question="What happened?", run=run, settings=Settings())
    assert complete_evidence_citations(value, sources, audit) == value == before
    assert [u["unit_id"] for u in audit["claim_audits"][0]["units"]] == [0, 1]
    assert audit["claim_audits"][0]["units"][0]["evidence"][0]["passage_ids"] == [1, 2]


@pytest.mark.parametrize("mode", ["negative", "uncertain", "low_confidence", "whole_negative"])
def test_every_unit_and_whole_claim_must_pass_or_use_the_original_audit(mode):
    def run(state, questions):
        values = positive(state, questions)
        key = "c0_whole" if mode == "whole_negative" else "c0_1"
        values[key] = answer(choice="insufficient" if "negative" in mode else "supported",
                             p=0.94 if mode == "uncertain" else 0.99,
                             confidence=0.8 if mode == "low_confidence" else 0.97)
        return values
    with pytest.raises(JevUnavailable):
        jev_tasks.evidence(draft("Nell gave Orin a key; she left at noon."),
                           [source(1, "Nell gave Orin a key.")], question="What happened?",
                           run=run, settings=Settings())


def test_other_claims_cannot_lend_their_citations_to_a_claim():
    value = draft()
    value["claims"].append({"text": "Pavo died.", "citation_ids": [2]})
    def run(state, questions):
        assert state["claims"][0]["cited_sources"][0]["source_id"] == 1
        assert state["claims"][1]["cited_sources"][0]["source_id"] == 2
        assert "state.claims[1].cited_sources" in questions["c1_whole"]["instructions"]
        return positive(state, questions)
    jev_tasks.evidence(value, [source(1, "Nell gave Orin a key."), source(2, "Pavo died.")],
                       question="What happened?", run=run, settings=Settings())


@pytest.mark.parametrize("failed_check", ["whole", "0", "1"])
def test_partial_evidence_only_approves_entire_claims_with_every_unit_supported(failed_check):
    value = {"insufficient_evidence": False, "claims": [
        {"text": "Nell gave Orin a key.", "citation_ids": [1]},
        {"text": "Orin left; Nell thanked him.", "citation_ids": [2]},
        {"text": "Pavo waited.", "citation_ids": [3]}]}
    original = copy.deepcopy(value)
    sources = [source(1, "Nell gave Orin a key."), source(2, "Orin left; Nell thanked him."),
               source(3, "Pavo waited.")]
    def run(state, questions):
        assert [c["claim_id"] for c in state["claims"]] == [1, 2, 3]
        answers = positive(state, questions)
        answers[f"c1_{failed_check}"] = answer(p=.94)
        return answers
    result = jev_tasks.evidence_partial(value, sources, question="What happened?",
        run=run, settings=Settings())
    assert [c["claim_id"] for c in result["claim_audits"]] == [1, 3]
    assert value == original


@pytest.mark.parametrize("fault", ["missing", "extra"])
def test_partial_evidence_malformed_coverage_cannot_create_implicit_approvals(fault):
    def run(state, questions):
        answers = positive(state, questions)
        if fault == "missing":
            answers.pop(next(iter(answers)))
        else:
            answers["c9_whole"] = answer()
        return answers
    with pytest.raises(JevUnavailable):
        jev_tasks.evidence_partial(draft(), [source(1, "Nell gave Orin a key.")],
                                  question="What happened?", run=run, settings=Settings())


def test_recap_checks_all_sentences_and_the_complete_composition():
    def run(state, questions):
        assert "Nell gave Orin a key." == "".join(state["sentences"])
        assert set(questions) == {"whole", "s0"}
        return positive(state, questions)
    assert not jev_tasks.recap("Nell gave Orin a key.", {"events": ["Nell gave Orin a key."]},
                               run=run, settings=Settings())["references_future"]


@pytest.mark.parametrize("fault", ["missing", "foreign", "model", "nan", "confidence", "usage",
                                    "choice", "distribution", "redirect", "http", "oversized"])
def test_transport_rejects_invalid_responses_without_leaking_provider_content(fault):
    def handler(request):
        assert request.url == "https://api.typesafe.ai/v1/systemone"
        assert request.headers["authorization"] == "Bearer private-test-key"
        payload = {"model": "jev-1.13.0", "answers": {"q": answer()},
                   "usage": {"input_tokens": 42, "output_tokens": 20}}
        if fault == "missing":
            payload["answers"] = {}
        if fault == "foreign":
            payload["answers"]["foreign"] = answer()
        if fault == "model":
            payload["model"] = "jev-latest"
        if fault == "nan":
            payload["answers"]["q"]["probabilities"]["supported"] = float("nan")
        if fault == "confidence":
            payload["answers"]["q"]["confidence"] = True
        if fault == "usage":
            payload["usage"]["input_tokens"] = 0
        if fault == "choice":
            payload["answers"]["q"]["choice"] = "foreign"
        if fault == "distribution":
            payload["answers"]["q"]["probabilities"]["supported"] = 0.2
        if fault == "redirect":
            return httpx.Response(307, headers={"location": "https://example.com/private-test-key"})
        if fault == "http":
            return httpx.Response(401, text="private-test-key")
        if fault == "oversized":
            return httpx.Response(200, text="x" * 262145)
        return httpx.Response(200, content=json.dumps(payload).encode())
    client = JevClient(api_key="private-test-key", transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(JevUnavailable) as caught:
            client.evaluate({}, {"q": {"type": "choice", "criteria": jev_tasks.SUPPORT}})
        assert "private-test-key" not in str(caught.value) + repr(caught.value)
    finally:
        client.close()


@pytest.mark.parametrize("probability", [True, "0.9", -0.1, 1.1, None])
def test_noul_transport_requires_a_real_bounded_probability(probability):
    def handler(request):
        return httpx.Response(200, json={"model":"jev-1.14.0",
            "answers":{"q":{"type":"noul","noul":probability}},
            "usage":{"input_tokens":10,"output_tokens":3}})
    client = JevClient(api_key="synthetic", model="jev-1.14.0", transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(JevUnavailable):
            client.evaluate({}, {"q":{"type":"noul","instructions":"Is the text supported?"}})
    finally:
        client.close()


def test_jev_spend_is_reserved_and_settled_including_failed_or_over_limit_calls():
    class Catalog:
        def reserve_cost(self, *args, **kwargs):
            self.reservation = kwargs
            return "r"
        def settle_cost(self, *args, **kwargs): self.settled = kwargs
        def note_reservation_actual(self, *args, **kwargs): self.actual = kwargs
    catalog = Catalog()
    jev = SimpleNamespace(model="jev-1.13.0", evaluate=lambda *_: ({}, {"in": 400, "out": 20}))
    result = budgeted_jev(catalog, Settings(), jev, "book", phase="test", state={"text": "x"*500},
                          questions={"q": {"type": "choice", "criteria": jev_tasks.SUPPORT}})
    assert result.usd == usd_of(jev.model, {"in": 400, "out": 20}) > 0
    assert catalog.reservation["input_tokens"] > catalog.actual["input_tokens"]
    assert catalog.settled == {}
    jev.evaluate = lambda *_: ({}, {"in": 999999, "out": 20})
    with pytest.raises(CostCeilingExceeded):
        budgeted_jev(catalog, Settings(), jev, "book", phase="test", state={}, questions={"q": {}})
    assert catalog.settled["phase"] == "test-provider-limit-violation"
    def failing(*_): raise JevUnavailable("unavailable")
    jev.evaluate = failing
    with pytest.raises(JevUnavailable):
        budgeted_jev(catalog, Settings(), jev, "book", phase="test", state={}, questions={"q": {}})
    assert catalog.settled["phase"] == "test-failed-reserved"


def test_oversized_input_falls_back_before_any_paid_call():
    with pytest.raises(JevUnavailable):
        budgeted_jev(None, Settings(), SimpleNamespace(model="jev-1.13.0"), "book", phase="test",
                     state={"text": "x" * 30000}, questions={"q": {}})


NO_CREDITS = "Your organization has no available TypeSafe API credits"
QUESTION = {"q": {"type": "choice", "criteria": jev_tasks.SUPPORT}}


def provider(status=402, message=NO_CREDITS, *, error=None, body=None):
    """A real JevClient whose provider answers every call with ``status``, or fails with ``error``."""
    def handler(request):
        if error is not None:
            raise error
        if body is not None:
            return httpx.Response(status, text=body)
        return httpx.Response(status, json={"error": {"message": message}})
    return JevClient(api_key="private-test-key", model="jev-1.14.0",
                     transport=httpx.MockTransport(handler))


def refusal(jev, state=None):
    try:
        with pytest.raises(JevUnavailable) as caught:
            jev.evaluate(state or {}, QUESTION)
    finally:
        jev.close()
    assert "private-test-key" not in str(caught.value) + repr(caught.value) + str(caught.value.detail)
    return caught.value


@pytest.mark.parametrize("status,reason,refused", [
    (402, "no_credits", True), (401, "unauthorized", True), (403, "unauthorized", True),
    (429, "rate_limited", True), (400, "refused", True), (404, "refused", True),
    (422, "refused", True), (500, "unavailable", False), (503, "unavailable", False),
    (307, "unavailable", False)])
def test_a_provider_refusal_keeps_what_kind_it_was(status, reason, refused):
    """The status was thrown away, so "no credits" read the same as a timeout."""
    failure = refusal(provider(status))
    assert (failure.reason, failure.refused) == (reason, refused)


@pytest.mark.parametrize("error", [httpx.ReadTimeout("slow"), httpx.ConnectError("no route")])
def test_a_call_that_may_have_run_is_not_called_a_refusal(error):
    """A timeout or lost connection says nothing about spend: it must never count as refused."""
    failure = refusal(provider(error=error))
    assert (failure.reason, failure.refused, failure.detail) == ("unavailable", False, None)
    # Raised by our own code with no provider answer at all: the same.
    assert (JevUnavailable("x").reason, JevUnavailable("x").refused) == ("unavailable", False)
    malformed = refusal(provider(200, body="{}"))
    assert (malformed.reason, malformed.refused) == ("unavailable", False)


def test_the_providers_own_message_is_kept_only_where_it_can_hold_no_secret():
    """The message is for the owner's log. It must not carry the key or anything we sent."""
    assert refusal(provider(402)).detail == NO_CREDITS
    assert refusal(provider(402, body=json.dumps({"message": NO_CREDITS}))).detail == NO_CREDITS
    assert refusal(provider(402, body=json.dumps({"error": NO_CREDITS}))).detail == NO_CREDITS
    # A provider that repeats the key back, whole or in part.
    assert refusal(provider(401, "Incorrect API key provided: private-test-key")).detail is None
    assert refusal(provider(401, "Incorrect API key provided: privat***-key")).detail is None
    # A complaint about the request can quote the reader's question or the book: never kept.
    assert refusal(provider(422, "state.sentence is invalid: Mara put the cobalt key"),
                   {"sentence": "Mara put the cobalt key"}).detail is None
    # Anything that is not one short line of text.
    assert refusal(provider(402, body="<html>" + "x" * 5000 + "</html>")).detail is None
    assert refusal(provider(402, body=json.dumps({"error": {"message": {"nested": 1}}}))).detail is None
    long = refusal(provider(429, "Slow down. " * 100)).detail
    assert long.startswith("Slow down.") and len(long) <= 200 and "\n" not in long


def test_a_refused_call_is_not_charged_and_one_whose_spend_is_unknown_still_is(tmp_path):
    """Each refused call stayed in the ledger at its full reserved size: about 120 of them used
    up the book's ceiling, and the book stayed locked after the provider recovered."""
    from app.catalog.catalog import Catalog
    catalog = Catalog(tmp_path / "catalog.db")
    catalog.add_book("book", title="A Book")
    settings = Settings(_env_file=None)
    # Room for two calls at their reserved size, so a third charged failure hits the ceiling.
    settings.cost_max_output_tokens_per_book = 2 * settings.cost_max_output_tokens_per_call

    def call(jev):
        try:
            return budgeted_jev(catalog, settings, jev, "book", phase="test", state={}, questions=QUESTION)
        finally:
            jev.close()

    for status in (402, 401, 429, 400, 402):
        with pytest.raises(JevUnavailable):
            call(provider(status))
    assert catalog.get_costs("book") == [] and catalog.get_cost_reservations("book") == []

    # Spend is unknown when the server errors or the call times out: the estimate stays counted.
    for jev in (provider(500), provider(error=httpx.ReadTimeout("slow"))):
        with pytest.raises(JevUnavailable):
            call(jev)
    charged = catalog.get_costs("book")
    assert [c["phase"] for c in charged] == ["test-failed-reserved"] * 2
    assert all(c["output_tokens"] == settings.cost_max_output_tokens_per_call for c in charged)
    assert catalog.get_cost_reservations("book") == []
    with pytest.raises(CostCeilingExceeded):
        call(provider(402))
