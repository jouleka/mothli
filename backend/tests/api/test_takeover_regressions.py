"""Paid-call concurrency, resumable budget failures, and independent reuse/completeness gates."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import threading
from types import SimpleNamespace

import pytest

from app.answer_bank import AnswerBank, AnswerBusy
from app.ask import AskDraft, judge_ask_draft
from app.answer_reuse import review_reuse
from app.llm.jev import JevUnavailable
from app.llm.jev_tasks import reuse_answer
from test_views import env as env


def payload():
    return {"as_of_chapter": 2, "insufficient_evidence": False,
            "claims": [{"text": "Berenice met Aldric at the forge.", "citation_ids": [1]}],
            "citations": [{"id": 1, "ordinal": 2, "chapter_key": "two", "href": "two.xhtml",
                           "title": "Two", "excerpt": "Berenice met Aldric at the forge."}]}


def capture(app, bid):
    from app.answer_bank import answer_scope
    from app.api.views import _effective
    st = app.state
    eff, state, manifest = _effective(bid, None, st.catalog, st.store, st.settings, st.worker)
    eff = min(eff, state["ingest_progress"])
    return SimpleNamespace(answer_key=answer_scope(bid, eff, state, manifest, catalog=st.catalog,
                                                   store=st.store, settings=st.settings,
                                                   client=st.reader_client))


def test_local_paid_timeout_has_one_wire_attempt_and_one_conservative_charge(tmp_path):
    import httpx
    from app.config import Settings
    from app.cost import budgeted_completion
    from app.deps import build_state
    from app.llm.client import ProviderUnavailable
    settings = Settings(_env_file=None, openai_api_key="offline-test-only",
                        llm_cheap_model="gpt-6-luna", llm_large_model="gpt-6-luna",
                        data_dir=str(tmp_path))
    state = build_state(settings)
    state["catalog"].add_book("b", title="Offline retry accounting")
    client = state["client"]
    calls = []
    def timeout(request):
        calls.append(request)
        raise httpx.ReadTimeout("ambiguous paid timeout", request=request)
    original = client._oai
    try:
        with httpx.Client(transport=httpx.MockTransport(timeout)) as transport:
            client._oai = original.with_options(http_client=transport)
            with pytest.raises(ProviderUnavailable):
                budgeted_completion(state["catalog"], settings, client, "b", phase="synthesis",
                    system="Source only.", user="An offline source.", tier="large")
        assert len(calls) == 1
        assert client._embed_oai.max_retries == 0
        costs = state["catalog"].get_costs("b")
        assert len(costs) == 1 and costs[0]["phase"] == "synthesis-failed-reserved"
        assert costs[0]["usd"] > 0 and not state["catalog"].get_cost_reservations("b")
    finally:
        original.close()
        client._embed_oai.close()
        state["catalog"].close()
        state["store"].close()


@pytest.mark.parametrize("failed", [False, True])
def test_duplicate_paid_requests_share_result_or_failure_without_retaining_flights(failed):
    bank = AnswerBank()
    started, release, waiting = threading.Event(), threading.Event(), threading.Event()
    calls = []
    def operation():
        calls.append(1)
        started.set()
        assert release.wait(5)
        if failed:
            raise ValueError("safe failure")
        return payload()
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(bank.run_once, ("b", "i", "s", "q"), operation)
        assert started.wait(2)
        future = bank.inflight[("b", "i", "s", "q")]
        original = future.result
        def result():
            waiting.set()
            return original()
        future.result = result
        second = pool.submit(bank.run_once, ("b", "i", "s", "q"), operation)
        assert waiting.wait(2)
        release.set()
        if failed:
            for response in (first, second):
                with pytest.raises(ValueError, match="safe failure"):
                    response.result()
        else:
            a, b = first.result(), second.result()
            assert a == (payload(), False) and b == (payload(), True)
            b[0]["claims"].clear()
            assert a[0]["claims"]
    assert calls == [1] and not bank.inflight


def test_distinct_answer_work_is_bounded_before_provider_io():
    bank = AnswerBank()
    bank.inflight.update({i: object() for i in range(8)})
    with pytest.raises(AnswerBusy):
        bank.run_once("new", lambda: pytest.fail("provider called beyond limit"))


def test_exact_cached_answer_remains_available_when_paid_flights_are_full(env):
    api, bid, app, _ = env
    app.state.settings.reader_answer_cache = True
    bank = app.state.reader_client.answers
    scope = capture(app, bid)
    question = "Where did Berenice meet Aldric?"
    bank.set((*scope.answer_key, question), payload())
    bank.inflight.update({i: object() for i in range(8)})
    response = api.post(f"/api/books/{bid}/ask", json={"question": question})
    assert response.status_code == 200 and response.json()["cached"]
    assert response.json()["cost"]["calls"] == []


@pytest.mark.parametrize("decision", ["pass", "incompatible", "omission", "unsupported", "future", "invalid", "outage"])
@pytest.mark.parametrize("probability", [.97, 1.0])
def test_reuse_needs_fresh_own_citation_and_new_question_review(env, monkeypatch, decision, probability):
    _, bid, app, atoms = env
    candidate = {"reviewed_question": "Describe the meeting.", "payload": payload()}
    before = deepcopy(candidate)
    calls = []
    def complete(*a, **kw):
        data = json.loads(kw["user"].split("(untrusted JSON data):\n")[1])
        calls.append(data)
        assert data["reader_question"] == "Where did Berenice meet Aldric?"
        assert data["claims"][0]["claim"] == before["payload"]["claims"][0]["text"]
        assert data["claims"][0]["cited_passages"][0]["text"] == before["payload"]["citations"][0]["excerpt"]
        assert "Corvus" not in kw["user"] and "confidence" not in kw["user"]
        if decision == "outage":
            raise ValueError("provider unavailable")
        value = {"compatible_question": decision != "incompatible", "references_future": decision == "future",
                 "unsupported_claims": ["wrong actor"] if decision == "unsupported" else [],
                 "missing_details": ["missing timing"] if decision == "omission" else []}
        if decision == "invalid":
            del value["compatible_question"]
        return SimpleNamespace(value=value)
    monkeypatch.setattr("app.answer_reuse.budgeted_completion", complete)
    def run(state, questions):
        return {k: {"choice": "yes", "probabilities": {"yes": probability}, "confidence": .95}
                for k in questions}
    def review(row):
        return review_reuse("Where did Berenice meet Aldric?", row, frontier=2,
            store=app.state.store, atoms={a["key"]: a for a in atoms}, catalog=app.state.catalog,
            settings=app.state.settings, client=app.state.reader_client, book_id=bid, remember=lambda r: None)
    args = dict(run=run, settings=app.state.settings, review=review)
    if decision == "pass" and probability == 1.0:
        assert reuse_answer("Where did Berenice meet Aldric?", [candidate], **args) == before
    else:
        with pytest.raises(JevUnavailable):
            reuse_answer("Where did Berenice meet Aldric?", [candidate], **args)
    assert len(calls) == (1 if probability == 1.0 else 0) and candidate == before


def test_true_but_incomplete_answer_is_rejected_without_borrowing_context_citations():
    draft = AskDraft.model_validate({k: payload()[k] for k in ("insufficient_evidence", "claims")})
    source = {"id": 1, "ordinal": 2, "text": "Berenice met Aldric at the forge."}
    context = {"id": 2, "ordinal": 2, "text": "Berenice must also return before noon."}
    def review(_system, user, **kw):
        data = json.loads(user.split("(untrusted JSON data):\n")[1])
        assert data["claims"][0]["cited_passages"][0]["id"] == 1
        assert "before noon" not in json.dumps(data["claims"])
        assert "before noon" in json.dumps(data["question_context"])
        return {"references_future": False, "unsupported_claims": [],
                "missing_details": ["Return before noon was omitted."]}, {}
    verdict, _ = judge_ask_draft(None, draft, [source], question="What must Berenice do?",
                                frontier=2, coverage_sources=[source, context], complete=review)
    assert verdict["unsupported_claims"] == ["Return before noon was omitted."]
