"""The opt-in HTTP path retains frontier, privacy and fail-closed publication boundaries."""
import json
import re

from test_views import env as env


def test_conversation_route_clamps_sources_caches_only_standalone_and_withholds_bad_proof(env, monkeypatch):
    api, bid, app, _ = env
    app.state.settings.reader_conversation_pipeline = True
    app.state.settings.reader_answer_cache = True
    app.state.settings.reader_conversation_review_effort = "low"
    client = app.state.client
    client.provider = "openai-compatible"
    prompts, broken = [], [False]

    def complete(system, user, tier="cheap", schema=None):
        prompts.append(user)
        assert "Corvus" not in user  # Chapter three is still unread, even for bookmark=99.
        if schema.__name__ == "ConversationDraft":
            matches = re.findall(r"SOURCE \[(\d+)\].*?\n.*?\n(.*?)(?=\n\nSOURCE|\n\nReturn|$)",
                                 user, re.S)
            source_id = next(int(i) for i, text in matches if "Berenice" in text)
            value = {"clarification": None, "insufficient_evidence": False, "claims": [
                {"text": "Berenice met Aldric at the forge.", "citation_ids": [source_id]}]}
        elif schema.__name__ == "ConversationVerdict":
            value = {"references_future": False, "unsupported_claims": [], "missing_details": []}
        else:
            packet = json.loads(user.split("\n", 1)[1])
            value = {"claim_audits": [{"claim_id": c["claim_id"], "units": [
                {"unit_id": u["unit_id"], "supported": True, "diagnostic": None,
                 "evidence": [{"source_id": c["sources"][0]["source_id"],
                               "passage_ids": [999 if broken[0] else 1]}]}
                for u in c["units"]]} for c in packet["claims"]]}
        return value, {"in": 10, "out": 10}

    monkeypatch.setattr(client, "complete", complete)
    question = {"question": "Who did Berenice meet?", "bookmark": 99}
    first = api.post(f"/api/books/{bid}/ask", json=question)
    assert first.status_code == 200, first.text
    assert first.json()["as_of_chapter"] == 2
    calls = len(prompts)
    cached = api.post(f"/api/books/{bid}/ask", json=question).json()
    assert cached["cached"] and len(prompts) == calls
    assert cached["cost"]["calls"] == []
    contextual = {**question, "history": [
        {"role": "user", "content": "My private thought about Berenice", "as_of_chapter": 2}]}
    assert api.post(f"/api/books/{bid}/ask", json=contextual).status_code == 200
    assert len(client.answers.rows) == 1  # No private/contextual answer enters the reusable bank.
    broken[0] = True
    withheld = api.post(f"/api/books/{bid}/ask", json=contextual)
    assert withheld.status_code == 502
    assert "Berenice met Aldric" not in withheld.text
    assert len(client.answers.rows) == 1
