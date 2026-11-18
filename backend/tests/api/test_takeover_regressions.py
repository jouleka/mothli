"""Paid-call concurrency, resumable budget failures, and independent reuse/completeness gates."""
import json

import pytest

from app.ask import AskDraft, judge_ask_draft
from test_views import env as env


def payload():
    return {"as_of_chapter": 2, "insufficient_evidence": False,
            "claims": [{"text": "Berenice met Aldric at the forge.", "citation_ids": [1]}],
            "citations": [{"id": 1, "ordinal": 2, "chapter_key": "two", "href": "two.xhtml",
                           "title": "Two", "excerpt": "Berenice met Aldric at the forge."}]}


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
