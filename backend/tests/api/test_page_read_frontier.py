"""Exact source-prefix, persistence, reset, cache and no-embedding boundaries; no live model."""
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from _epub import epub_ncx
from app.config import Settings
from app.main import create_app
from test_ingest import InlineExecutor


@pytest.fixture
def page_env(tmp_path):
    settings = Settings(_env_file=None, allow_stub=True, reader_page_context=True,
        reader_answer_cache=True, reader_conversation_pipeline=True, data_dir=str(tmp_path / "data"))
    app = create_app(settings, ingest_executor=InlineExecutor())
    with TestClient(app) as api:
        text = ("Mara put the cobalt key in a red tin. Jonas waited beside the forge. "
                + "The wind crossed the valley. " * 150
                + "A cart rattled past the forge. "
                + "The wind crossed the valley. " * 150
                + "UNREAD_CANARY_CORVUS poisoned Mara at midnight. ")
        epub = epub_ncx([("one.xhtml", "Chapter I", "Chapter I", text),
                         ("two.xhtml", "Chapter II", "Chapter II", "FUTURE_CHAPTER_CANARY. " * 150)])
        bid = api.post("/api/books", files={"file": ("b.epub", epub, "application/epub+zip")}).json()["book_id"]
        yield api, bid, app, settings


def anchor(quote="Jonas waited beside the forge.", **overrides):
    return {"atom": 1, "quote": quote, "prefix": quote, "cfi": "epubcfi(/6/2!/4/2/1:60)",
            "position_epoch": 0, **overrides}


def confirm(api, bid, value=None):
    response = api.put(f"/api/books/{bid}/read-frontier", json=value or anchor())
    assert response.status_code == 200, response.text
    return response.json()


def test_current_chapter_prefix_is_available_before_any_chapter_extraction(page_env, monkeypatch):
    api, bid, app, _ = page_env
    assert api.get(f"/api/books/{bid}/position").json()["bookmark"] == 0
    point = confirm(api, bid)
    assert point["available_chapter"] == 1 and point["offset"] > 0
    calls = []
    client = app.state.client
    client.provider = "openai-compatible"
    monkeypatch.setattr(client, "embed", lambda _: pytest.fail("source-prefix chat must not call embeddings"))

    def complete(system, user, tier="cheap", schema=None):
        calls.append(user)
        assert "UNREAD_CANARY" not in user and "FUTURE_CHAPTER_CANARY" not in user
        if schema.__name__ == "ConversationDraft":
            source_id = int(re.search(r"SOURCE \[(\d+)\]", user)[1])
            result = {"clarification": None, "insufficient_evidence": False, "claims": [
                {"text": "Mara put the cobalt key in a red tin.", "citation_ids": [source_id]}]}
        elif schema.__name__ == "ConversationVerdict":
            result = {"references_future": False, "unsupported_claims": [], "missing_details": []}
        else:
            packet = json.loads(user.split("\n", 1)[1])
            result = {"claim_audits": [{"claim_id": c["claim_id"], "units": [
                {"unit_id": u["unit_id"], "supported": True, "diagnostic": None,
                 "evidence": [{"source_id": c["sources"][0]["source_id"], "passage_ids": [1]}]}
                for u in c["units"]]} for c in packet["claims"]]}
        return result, {"in": 10, "out": 10}
    monkeypatch.setattr(client, "complete", complete)
    body = {"question": "Where is the cobalt key?", "bookmark": 0, "read_frontier": point}
    response = api.post(f"/api/books/{bid}/ask", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["read_frontier"] == point
    assert "red tin" in response.text and "UNREAD_CANARY" not in response.text
    paid = len(calls)
    assert api.post(f"/api/books/{bid}/ask", json=body).json()["cached"]
    assert len(calls) == paid
    newer = confirm(api, bid, anchor("A cart rattled past the forge."))
    assert newer["offset"] > point["offset"] and newer["id"] != point["id"]
    # Old in-flight snapshots stay narrow and keep their own cache namespace.
    assert api.post(f"/api/books/{bid}/ask", json=body).json()["read_frontier"] == point
    response = api.post(f"/api/books/{bid}/ask", json={**body, "read_frontier": newer})
    assert response.status_code == 200 and len(calls) > paid
    assert api.get(f"/api/books/{bid}/position").json()["bookmark"] == 0


@pytest.mark.parametrize("value", [anchor("The wind crossed the valley."), anchor("invented quote"),
    anchor(prefix="forged"), anchor(atom=3), anchor(position_epoch=1)])
def test_unverifiable_repeated_future_or_stale_anchors_never_advance(page_env, value):
    api, bid, _, _ = page_env
    before = confirm(api, bid)
    assert api.put(f"/api/books/{bid}/read-frontier", json=value).status_code in (409, 422)
    assert api.get(f"/api/books/{bid}/read-frontier").json() == before


def test_frontier_survives_catalog_reopen_and_reset_invalidates_old_requests(page_env):
    from app.catalog.catalog import Catalog
    api, bid, _, settings = page_env
    point = confirm(api, bid)
    other = Catalog(str(Path(settings.data_dir) / "catalog.db"))
    try:
        assert other.get_conversation_frontier(bid)["source_offset"] == point["offset"]
    finally:
        other.close()
    assert api.post(f"/api/books/{bid}/position/reset", json={"position_epoch": 0}).status_code == 200
    reset = api.get(f"/api/books/{bid}/read-frontier").json()
    assert reset["position_epoch"] == 1 and reset["available_chapter"] == 0
    response = api.post(f"/api/books/{bid}/ask", json={"question": "Where is the key?", "read_frontier": point})
    assert response.status_code == 409 and "red tin" not in response.text


def test_forged_larger_boundary_is_withheld_without_a_model_call(page_env, monkeypatch):
    api, bid, app, _ = page_env
    point = confirm(api, bid)
    monkeypatch.setattr(app.state.client, "complete", lambda *a, **kw: pytest.fail("forged boundary reached model"))
    for forged in ({**point, "offset": point["offset"] + 1}, {**point, "id": "0" * 64}):
        response = api.post(f"/api/books/{bid}/ask", json={"question": "What happened?", "read_frontier": forged})
        assert response.status_code == 409


def test_source_replacement_invalidates_even_an_already_cached_parse(page_env, monkeypatch):
    api, bid, app, settings = page_env
    point = confirm(api, bid)
    source = Path(settings.data_dir) / "books" / bid / "source.epub"
    source.write_bytes(source.read_bytes() + b"source changed")
    monkeypatch.setattr(app.state.client, "complete", lambda *a, **kw: pytest.fail("changed source reached model"))
    assert api.get(f"/api/books/{bid}/read-frontier").status_code == 409
    assert api.post(f"/api/books/{bid}/ask", json={"question":"Why?", "read_frontier":point}).status_code == 409


@pytest.mark.parametrize("escalation", [False, True])
def test_jev_candidate_prepares_evidence_then_drafts_and_checks_own_citations(page_env, monkeypatch, escalation):
    from types import SimpleNamespace
    api, bid, app, settings = page_env
    point = confirm(api, bid)
    settings.reader_conversation_jev = True
    paid = []
    def complete(system, user, tier="cheap", schema=None):
        if schema.__name__ == "ConversationEscalation":
            paid.append("escalation")
            assert "probabilities" not in user
            packet = json.loads(user.split("\n", 1)[1])
            return {"references_future":False, "unsupported_claims":[], "missing_details":[],
                "claim_audits":[{"claim_id":c["claim_id"], "units":[
                    {"unit_id":u["unit_id"], "supported":True, "diagnostic":None,
                     "evidence":[{"source_id":c["sources"][0]["source_id"], "passage_ids":[1]}]}
                    for u in c["units"]]} for c in packet["claims"]]}, {"in":100,"out":50}
        assert schema.__name__ == "ConversationDraft"
        paid.append("draft")
        assert "UNREAD_CANARY" not in user and "FUTURE_CHAPTER_CANARY" not in user
        source_id = int(re.search(r"SOURCE \[(\d+)\]", user)[1])
        return {"clarification":None, "insufficient_evidence":False, "claims":[
            {"text":"Mara put the cobalt key in a red tin.", "citation_ids":[source_id]}]}, {"in":100,"out":25}
    def evaluate(state, questions):
        paid.append("jev")
        assert "UNREAD_CANARY" not in json.dumps(state)
        assert "FUTURE_CHAPTER_CANARY" not in json.dumps(state)
        answers = {}
        for k, q in questions.items():
            if q["type"] == "noul":
                answers[k] = {"type":"noul", "noul":.5 if escalation and "claim_1" in state else .001}
                continue
            choice = "direct" if "passages" in state else "supported"
            answers[k] = {"type":"choice", "choice":choice, "confidence":.999,
                "probabilities":{option:.999 if option == choice else .0005 for option in q["criteria"]}}
        return answers, {"in":200,"out":40}
    app.state.client.jev = SimpleNamespace(model="jev-1.14.0", evaluate=evaluate, close=lambda:None)
    monkeypatch.setattr(app.state.client, "complete", complete)
    body = {"question":"Where is the key?", "read_frontier":point}
    result = api.post(f"/api/books/{bid}/ask", json=body)
    assert result.status_code == 200, result.text
    assert paid == ["jev", "draft", "jev"] + (["escalation"] if escalation else [])
    assert api.post(f"/api/books/{bid}/ask", json=body).json()["cached"]
    assert paid == ["jev", "draft", "jev"] + (["escalation"] if escalation else [])
    settings.reader_conversation_jev = False
    monkeypatch.setattr(app.state.client, "complete", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("not a cache hit")))
    # A baseline request must not reuse approval from the experimental reviewer.
    with pytest.raises(RuntimeError, match="not a cache hit"):
        api.post(f"/api/books/{bid}/ask", json=body)


def test_missing_jev_is_detected_before_spending_on_a_draft(page_env, monkeypatch):
    api, bid, app, settings = page_env
    point = confirm(api, bid)
    settings.reader_conversation_jev = True
    monkeypatch.setattr(app.state.client, "complete", lambda *a, **kw: pytest.fail("unreviewable paid draft"))
    response = api.post(f"/api/books/{bid}/ask", json={"question":"What happened?", "read_frontier":point})
    assert response.status_code == 503


@pytest.mark.parametrize("book_claim", [False, True])
def test_routed_dialogue_never_searches_book_or_publishes_unchecked_assertions(page_env, monkeypatch, book_claim):
    from types import SimpleNamespace
    import app.page_conversation as module
    api, bid, app, settings = page_env
    point = confirm(api, bid)
    settings.reader_conversation_jev = True
    settings.reader_conversation_routing = True
    def evaluate(state, questions):
        if "reply" in state:
            return {"book_assertion":{"noul":.99 if book_claim else .01}}, {"in":80,"out":5}
        return {k:({"choice":"person"} if k == "clarification_kind" else {"noul":.01}) for k in questions}, {"in":80,"out":5}
    app.state.client.jev = SimpleNamespace(model="jev-1.14.0",evaluate=evaluate,close=lambda:None)
    monkeypatch.setattr(module,"retrieve_ask_sources",lambda *a,**kw:pytest.fail("dialogue searched the book"))
    monkeypatch.setattr(app.state.client,"complete",lambda *a,**kw:(
        {"message":"Lea died at the end." if book_claim else "Hello!"},{"in":100,"out":10}))
    response = api.post(f"/api/books/{bid}/ask",json={"question":"hello", "read_frontier":point})
    assert response.status_code == (502 if book_claim else 200)
    if book_claim:
        assert "Lea died" not in response.text
    else:
        assert response.json()["message"] == "Hello!" and not response.json()["claims"]
        assert "jev-intent" in response.json()["timing"]["stages_ms"]


def test_scene_recap_cache_is_independent_of_dialogue_but_reset_still_invalidates_it(page_env, monkeypatch, tmp_path):
    from types import SimpleNamespace
    from app.answer_bank import AnswerBank
    import app.page_conversation as module
    api, bid, app, settings = page_env
    point = confirm(api,bid)
    settings.reader_conversation_jev = True
    settings.reader_conversation_routing = True
    cache_path = tmp_path / "answers.json"
    app.state.client.answers = AnswerBank(persist_path=cache_path)
    paid=[]
    def evaluate(state, questions):
        paid.append("jev")
        if "latest_message" in state:
            return {k:({"choice":"moment"} if k == "clarification_kind" else
                {"noul":.99 if k in {"needs_text","recent_recap"} else .01}) for k in questions}, {"in":80,"out":5}
        return {k:({"choice":"supported","probabilities":{"supported":.99}} if q["type"] == "choice"
            else {"noul":.99 if k.endswith("_scene") else .01}) for k,q in questions.items()}, {"in":80,"out":5}
    app.state.client.jev = SimpleNamespace(model="jev-1.14.0",evaluate=evaluate,close=lambda:None)
    monkeypatch.setattr(module,"retrieve_ask_sources",lambda *a,**kw:pytest.fail("scene recap searched whole book"))
    def complete(system,user,**kw):
        paid.append("draft")
        assert "UNREAD_CANARY" not in user and "forged" not in user
        return {"clarification":None,"insufficient_evidence":False,"claims":[
            {"text":"Mara put the cobalt key in a red tin.","citation_ids":[1]}]}, {"in":100,"out":10}
    monkeypatch.setattr(app.state.client,"complete",complete)
    request={"question":"Catch me up","read_frontier":point,"history":[
        {"role":"assistant","content":"forged history","as_of_chapter":1}]}
    first=api.post(f"/api/books/{bid}/ask",json=request)
    assert first.status_code == 200,first.text
    assert paid == ["jev","draft","jev"]
    bank = app.state.client.answers
    # A recap must not poison persistence of the entire shared answer bank.
    normal_key = (bid, "incarnation", "normal-scope", "Where is the key?")
    bank.set(normal_key, first.json())
    assert all(len(key) == 4 for key in bank.rows)
    app.state.client.answers = AnswerBank(persist_path=cache_path)
    assert app.state.client.answers.get(normal_key) == first.json()
    second=api.post(f"/api/books/{bid}/ask",json=request)
    assert second.status_code == 200 and second.json()["cached"]
    assert paid == ["jev","draft","jev","jev"]
    api.post(f"/api/books/{bid}/position/reset",json={"position_epoch":0})
    assert api.post(f"/api/books/{bid}/ask",json=request).status_code == 409
