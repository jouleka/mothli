"""Regression boundaries for source-first cold books; no paid IO."""

from types import SimpleNamespace
import re

import pytest

from test_page_read_frontier import page_env as page_env, confirm, anchor


@pytest.mark.parametrize("cleared", [False, True])
@pytest.mark.parametrize("with_alternative,primary_invalid", [(False,False),(True,False),(True,True)])
def test_uncertain_recap_escalates_only_unsettled_claim_without_rewriting(page_env, monkeypatch, cleared, with_alternative, primary_invalid):
    import app.page_conversation as conversation

    api, bid, app, settings = page_env
    point = confirm(api, bid)
    settings.reader_conversation_jev = True
    settings.reader_conversation_routing = True
    calls = []

    def complete(system, user, tier="cheap", schema=None):
        calls.append(schema.__name__)
        assert schema.__name__ == "SceneConversationDraft"
        source_id = int(re.search(r"SOURCE \[(\d+)\]", user)[1])
        value = {
            "clarification": None,
            "insufficient_evidence": False,
            "claims": [
                {"text": "Mara put the cobalt key in a red tin.", "citation_ids": [source_id]},
            ],
        }
        if with_alternative:
            value["claims"][0]["text"] = "Mara put the cobalt key in a red tin, then kept it there."
            value["alternative_claim"] = {"text":"Mara put the key in a red tin.","citation_ids":[source_id]}
        if primary_invalid:
            value["claims"][0]["citation_ids"] = [99]
        return value, {"in": 100, "out": 40}

    def evaluate(state, questions):
        if "latest_message" in state:
            answers = {
                key: (
                    {"choice": "person"}
                    if key == "clarification_kind"
                    else {"noul": 0.99 if key in {"needs_text", "recent_recap"} else 0.001}
                )
                for key in questions
            }
        else:
            answers = {
                key: (
                    {"choice": "supported", "probabilities": {"supported": .5}}
                    if q["type"] == "choice"
                    else {"noul": .8 if key=="alternative_independent" else 0.99 if key.endswith("_scene") else 0.001}
                )
                for key, q in questions.items()
            }
        return answers, {"in": 100, "out": 40}

    monkeypatch.setattr(app.state.client, "complete", complete)
    app.state.client.jev = SimpleNamespace(
        model="jev-1.14.0", evaluate=evaluate, close=lambda: None
    )
    def independent_review(draft, sources, **kwargs):
        from app.ask_evidence import AskEvidenceError
        if with_alternative:
            assert kwargs["claim_ids"] is None and kwargs["standalone"] is True
            assert draft.claims[0].text == "Mara put the key in a red tin."
        else:
            assert kwargs["claim_ids"] == [1]
            assert draft.claims[0].text == "Mara put the cobalt key in a red tin."
        if not cleared:
            raise AskEvidenceError()

    monkeypatch.setattr(conversation, "review_escalation", independent_review)
    response = api.post(
        f"/api/books/{bid}/ask", json={"question": "Catch me up", "read_frontier": point}
    )
    assert calls == ["SceneConversationDraft"]
    if cleared:
        assert response.status_code == 200 and len(response.json()["claims"]) == 1
    else:
        assert response.status_code == 502 and "Mara" not in response.text


def test_position_jump_does_not_schedule_paid_backfill_when_deferred(page_env, monkeypatch):
    api, bid, app, settings = page_env
    settings.reader_memory_ingest = False
    monkeypatch.setattr(
        app.state.worker, "enqueue", lambda *a: pytest.fail("unexpected extraction backlog")
    )
    from app.ingest.manifest import load_manifest

    m = load_manifest(settings.data_dir, bid)
    response = api.put(
        f"/api/books/{bid}/position",
        json={"offset": m["atoms"][0]["char_len"] + 1, "cfi": "jump", "position_epoch": 0},
    )
    assert response.status_code == 200 and response.json()["bookmark"] == 1
    assert api.get(f"/api/books/{bid}/position").json()["ingest_progress"] == 0


def test_formal_arithmetic_needs_neither_provider_even_with_no_read_text(page_env, monkeypatch):
    api, bid, app, settings = page_env
    settings.reader_conversation_routing = True
    settings.reader_conversation_jev = True
    monkeypatch.setattr(
        app.state.client, "complete", lambda *a, **k: pytest.fail("arithmetic called Terra")
    )
    response = api.post(f"/api/books/{bid}/ask", json={"question": "What's 2 + 2?"})
    assert response.status_code == 200 and response.json()["message"] == "2 + 2 = 4."
    assert response.json()["cost"]["calls"] == []


def test_mixed_math_does_not_mark_an_unread_book_question_as_answered(page_env, monkeypatch):
    api, bid, app, settings = page_env
    settings.reader_conversation_routing = True
    settings.reader_conversation_jev = True
    monkeypatch.setattr(app.state.client,"complete",lambda *a,**kw:pytest.fail("no read text"))
    def evaluate(state, questions):
        assert state["latest_message"] == "who dies at the end?"
        return {k:({"choice":"person"} if k=="clarification_kind" else
                   {"noul":.99 if k=="needs_text" else .001}) for k in questions}, {"in":100,"out":30}
    app.state.client.jev = SimpleNamespace(model="jev-1.14.0",evaluate=evaluate,close=lambda:None)
    response = api.post(f"/api/books/{bid}/ask",json={"question":"What is 2 + 2, and who dies at the end?"})
    assert response.status_code == 200
    value = response.json()
    assert value["calculation"] == "2 + 2 = 4."
    assert value["insufficient_evidence"] and value["claims"] == []


@pytest.mark.parametrize("bookmark,expected", [(None, 1), (0, 0), (999, 1)])
def test_missing_boundary_cannot_enter_full_recap_or_widen_requested_bookmark(
    page_env, monkeypatch, bookmark, expected
):
    api, bid, app, _ = page_env
    confirm(api, bid)
    import app.api.views as views
    import app.page_conversation as conversation

    monkeypatch.setattr(views, "catch_me_up", lambda *a, **k: pytest.fail("legacy full recap"))

    def source_first(book, body, *args):
        assert body.read_frontier.available_chapter == expected
        return {
            "as_of_chapter": expected,
            "claims": [],
            "citations": [],
            "insufficient_evidence": True,
        }

    monkeypatch.setattr(conversation, "answer_read_prefix", source_first)
    payload = {"question": "Catch me up"}
    if bookmark is not None:
        payload["bookmark"] = bookmark
    assert api.post(f"/api/books/{bid}/ask", json=payload).status_code == 200


def test_cold_names_are_exact_visible_candidates_and_use_no_terra(page_env, monkeypatch):
    api, bid, app, _ = page_env
    point = confirm(api, bid, anchor("Mara put the cobalt key in a red tin."))
    calls = []

    def evaluate(state, questions):
        calls.append(1)
        assert "Jonas" not in str(state) and "UNREAD_CANARY" not in str(state)
        return {
            key: {"noul": 0.999 if state["candidates"][int(key)]["name"] == "Mara" else 0.001}
            for key in questions
        }, {"in": 100, "out": 20}

    app.state.client.jev = SimpleNamespace(
        evaluate=evaluate, model="jev-1.14.0", close=lambda: None
    )
    monkeypatch.setattr(
        app.state.client, "complete", lambda *a, **k: pytest.fail("name lookup generated prose")
    )
    response = api.post(f"/api/books/{bid}/people", json=point)
    assert response.status_code == 200 and response.json()["names"] == ["Mara"]
    assert response.json()["read_frontier"] == point
    assert len(calls) == 1
    forged = {**point, "offset": point["offset"] + 100}
    assert api.post(f"/api/books/{bid}/people", json=forged).status_code == 409
    assert len(calls) == 1


@pytest.mark.parametrize("uncertain", [False, True])
def test_translation_jev_clearance_or_reasoning_fallback_remains_mandatory(
    page_env, monkeypatch, uncertain
):
    api, bid, app, settings = page_env
    confirm(api, bid)
    settings.reader_conversation_jev = True
    app.state.client.provider = "openai-compatible"
    calls = []
    text = "Mara put the cobalt key in a red tin."

    def complete(system, user, tier="cheap", schema=None):
        calls.append(schema.__name__)
        if schema.__name__ == "SelectionDraft":
            return {
                "insufficient_evidence": False,
                "text": "Mara placed a cobalt key in a red tin.",
                "citation_ids": [1],
            }, {
                "in": 20,
                "out": 10,
            }
        return {"references_future": False, "unsupported_claims": [], "missing_details": []}, {
            "in": 20,
            "out": 10,
        }

    def evaluate(state, questions):
        return {
            key: {"noul": 0.5 if uncertain else 0.999 if key == "faithful" else 0.001}
            for key in questions
        }, {"in": 100, "out": 20}

    app.state.client.jev = SimpleNamespace(
        evaluate=evaluate, model="jev-1.14.0", close=lambda: None
    )
    monkeypatch.setattr(app.state.client, "complete", complete)
    body = {"action": "translate", "text": text, "atom": 1, "cfi": "cfi"}
    response = api.post(f"/api/books/{bid}/selection-action", json=body)
    assert response.status_code == 200 and response.json()["citation"]["excerpt"] == text
    assert calls == ["SelectionDraft"] + (["SelectionVerdict"] if uncertain else [])
    before = len(calls)
    assert (
        api.post(
            f"/api/books/{bid}/selection-action", json={**body, "text": "Invented source"}
        ).status_code
        == 422
    )
    assert len(calls) == before
