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


@pytest.mark.parametrize("supported", [True, False])
def test_a_reaction_is_answered_from_read_pages_or_falls_back_without_book_facts(
        page_env, monkeypatch, supported):
    api, bid, app, settings = page_env
    point = confirm(api, bid)
    settings.reader_conversation_jev = True
    settings.reader_conversation_routing = True
    drafted = []

    def complete(system, user, tier="cheap", schema=None):
        drafted.append(schema.__name__)
        assert "UNREAD_CANARY" not in user and "FUTURE_CHAPTER_CANARY" not in user
        if schema.__name__ == "ConversationReply":
            return {"message": "That sounds like a lot. What stayed with you?"}, {"in": 50, "out": 10}
        assert schema.__name__ == "ConversationDraft" and "reacting to what they have just read" in user
        source_id = next(int(i) for i, body in re.findall(
            r"SOURCE \[(\d+)\][^\n]*\n[^\n]*\n([^\n]*)", user) if "red tin" in body)
        return {"clarification": None, "insufficient_evidence": False, "claims": [
            {"text": "Mara put the cobalt key in a red tin.", "citation_ids": [source_id]}]}, {"in": 100, "out": 20}

    def evaluate(state, questions):
        answers = {}
        for key, q in questions.items():
            if q["type"] == "choice":
                first = next(iter(q["criteria"]))
                share = .95 if supported or not key.endswith("_relation") else .2
                answers[key] = {"choice": first, "probabilities": {
                    name: (share if name == first else (1 - share) / (len(q["criteria"]) - 1))
                    for name in q["criteria"]}}
            else:
                answers[key] = {"noul": .95 if key == "book_reaction" else .01}
        return answers, {"in": 100, "out": 10}

    def no_reasoning_rescue(*args, **kwargs):
        from app.ask_evidence import AskEvidenceError
        raise AskEvidenceError()

    import app.page_conversation as conversation
    monkeypatch.setattr(conversation, "review_escalation", no_reasoning_rescue)
    monkeypatch.setattr(app.state.client, "complete", complete)
    app.state.client.jev = SimpleNamespace(model="jev-1.14.0", evaluate=evaluate, close=lambda: None)
    response = api.post(f"/api/books/{bid}/ask",
                        json={"question": "That chapter wrecked me.", "read_frontier": point})
    assert response.status_code == 200, response.text
    data = response.json()
    if supported:
        assert drafted == ["ConversationDraft"]
        assert [c["text"] for c in data["claims"]] == ["Mara put the cobalt key in a red tin."]
        assert data["citations"] and not data["insufficient_evidence"]
    else:
        # The uncleared grounded draft is discarded; the plain reply carries no claims.
        assert drafted == ["ConversationDraft", "ConversationReply"]
        assert data["claims"] == [] and data["message"].startswith("That sounds like a lot")
        assert "Mara" not in response.text


def _page_models(app, monkeypatch, *, jev, draft=None):
    drafted, jev_calls = [], []

    def complete(system, user, tier="cheap", schema=None):
        drafted.append(schema.__name__)
        assert "UNREAD_CANARY" not in user and "FUTURE_CHAPTER_CANARY" not in user
        if schema.__name__ == "ConversationDraft":
            if draft is not None:
                return draft, {"in": 100, "out": 20}
            source_id = next(int(i) for i, body in re.findall(
                r"SOURCE \[(\d+)\][^\n]*\n[^\n]*\n([^\n]*)", user) if "red tin" in body)
            return {"clarification": None, "insufficient_evidence": False, "claims": [
                {"text": "Mara put the cobalt key in a red tin.", "citation_ids": [source_id]}]}, {"in": 100, "out": 20}
        if schema.__name__ == "ConversationVerdict":
            return {"references_future": False, "unsupported_claims": [], "missing_details": []}, {"in": 100, "out": 10}
        assert schema.__name__ == "ConversationEvidenceReview"
        import json
        packet = json.loads(user.split("\n", 1)[1])
        return {"claim_audits": [{"claim_id": c["claim_id"], "units": [
            {"unit_id": u["unit_id"], "supported": True, "diagnostic": None,
             "evidence": [{"source_id": c["sources"][0]["source_id"],
                           "passage_ids": [c["sources"][0]["passages"][0]["id"]]}]}
            for u in c["units"]]} for c in packet["claims"]]}, {"in": 100, "out": 40}

    def evaluate(state, questions):
        jev_calls.append(state)
        return jev(state, questions), {"in": 100, "out": 10}

    monkeypatch.setattr(app.state.client, "complete", complete)
    app.state.client.jev = SimpleNamespace(model="jev-1.14.0", evaluate=evaluate, close=lambda: None)
    return drafted, jev_calls


def test_jev_outage_falls_back_to_the_stricter_reviews_instead_of_losing_the_answer(page_env, monkeypatch):
    from app.llm.jev import JevUnavailable

    api, bid, app, settings = page_env
    point = confirm(api, bid)
    settings.reader_conversation_jev = True
    settings.reader_conversation_routing = True

    def down(state, questions):
        raise JevUnavailable("offline")

    drafted, jev_calls = _page_models(app, monkeypatch, jev=down)
    response = api.post(f"/api/books/{bid}/ask",
                        json={"question": "Where did Mara put the key?", "read_frontier": point})
    assert response.status_code == 200, response.text
    assert [c["text"] for c in response.json()["claims"]] == ["Mara put the cobalt key in a red tin."]
    # One failed Jev attempt, then no further Jev calls; both independent GPT reviews ran.
    assert len(jev_calls) == 1
    assert sorted(drafted) == ["ConversationDraft", "ConversationEvidenceReview", "ConversationVerdict"]


@pytest.mark.parametrize("ahead", [.95, .05])
def test_an_empty_answer_to_a_question_about_later_events_says_so(page_env, monkeypatch, ahead):
    api, bid, app, settings = page_env
    point = confirm(api, bid)
    settings.reader_conversation_jev = True
    settings.reader_conversation_routing = True

    def jev(state, questions):
        answers = {}
        for key, q in questions.items():
            if q["type"] == "choice":
                first = next(iter(q["criteria"]))
                answers[key] = {"choice": first, "probabilities": {
                    name: (.9 if name == first else .1 / (len(q["criteria"]) - 1)) for name in q["criteria"]}}
            else:
                answers[key] = {"noul": ahead if key == "asks_ahead" else .99 if key == "needs_text" else .01}
        return answers

    _page_models(app, monkeypatch, jev=jev,
                 draft={"clarification": None, "insufficient_evidence": True, "claims": []})
    data = api.post(f"/api/books/{bid}/ask",
                    json={"question": "Who poisons Mara in the end?", "read_frontier": point}).json()
    assert data["insufficient_evidence"] and data["claims"] == []
    assert data.get("status") == ("ahead" if ahead > .7 else None)


@pytest.mark.parametrize("closing_risk,kept", [(.02, True), (.6, False)])
def test_prose_reply_keeps_proof_per_sentence_and_only_a_fact_free_closing(
        page_env, monkeypatch, closing_risk, kept):
    api, bid, app, settings = page_env
    point = confirm(api, bid)
    settings.reader_conversation_jev = True
    settings.reader_conversation_routing = True
    settings.reader_conversation_prose = True
    seen = {}

    def jev(state, questions):
        if "claim_2" in state:
            seen["earlier"] = state["claim_2"].get("earlier_reply_sentences")
            seen["first_has_no_context"] = "earlier_reply_sentences" not in state["claim_1"]
        answers = {}
        for key, q in questions.items():
            if q["type"] == "choice":
                first = next(iter(q["criteria"]))
                answers[key] = {"choice": first, "probabilities": {
                    name: (.95 if name == first else .05 / (len(q["criteria"]) - 1)) for name in q["criteria"]}}
            else:
                answers[key] = {"noul": closing_risk if key == "book_assertion"
                                else .99 if key == "needs_text" else .01}
        return answers

    def complete(system, user, tier="cheap", schema=None):
        assert schema.__name__ == "ProseConversationDraft" and system.startswith("You are Mothli, a reading companion")
        assert "UNREAD_CANARY" not in user and "FUTURE_CHAPTER_CANARY" not in user
        source_id = next(int(i) for i, body in re.findall(
            r"SOURCE \[(\d+)\][^\n]*\n[^\n]*\n([^\n]*)", user) if "red tin" in body)
        return {"clarification": None, "insufficient_evidence": False, "closing": "Where would you have put it?",
                "claims": [{"text": "Mara put the cobalt key in a red tin.", "citation_ids": [source_id]},
                           {"text": "She did it while Jonas waited beside the forge.", "citation_ids": [source_id]}]}, {"in": 100, "out": 30}

    monkeypatch.setattr(app.state.client, "complete", complete)
    app.state.client.jev = SimpleNamespace(model="jev-1.14.0", close=lambda: None,
                                           evaluate=lambda state, questions: (jev(state, questions), {"in": 100, "out": 10}))
    response = api.post(f"/api/books/{bid}/ask",
                        json={"question": "Where did Mara put the key?", "read_frontier": point})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["style"] == "prose" and len(data["claims"]) == 2 and data["citations"]
    assert seen == {"earlier": ["Mara put the cobalt key in a red tin."], "first_has_no_context": True}
    assert data.get("closing") == ("Where would you have put it?" if kept else None)


def test_prose_flag_off_leaves_the_existing_reply_shape(page_env, monkeypatch):
    api, bid, app, settings = page_env
    point = confirm(api, bid)
    settings.reader_conversation_jev = True
    settings.reader_conversation_routing = True

    def jev(state, questions):
        answers = {}
        for key, q in questions.items():
            if q["type"] == "choice":
                first = next(iter(q["criteria"]))
                answers[key] = {"choice": first, "probabilities": {
                    name: (.95 if name == first else .05 / (len(q["criteria"]) - 1)) for name in q["criteria"]}}
            else:
                answers[key] = {"noul": .99 if key == "needs_text" else .01}
        return answers

    drafted, _ = _page_models(app, monkeypatch, jev=jev)
    data = api.post(f"/api/books/{bid}/ask",
                    json={"question": "Where did Mara put the key?", "read_frontier": point}).json()
    assert drafted == ["ConversationDraft"] and "style" not in data and "closing" not in data


@pytest.mark.parametrize("second_ok", [True, False])
def test_a_draft_stopped_before_review_gets_one_redraft_and_a_reviewed_one_none(
        page_env, monkeypatch, second_ok):
    api, bid, app, settings = page_env
    point = confirm(api, bid)
    settings.reader_conversation_jev = True
    settings.reader_conversation_routing = True
    drafts, reviews = [], []

    def jev(state, questions):
        if "claim_1" in state:
            reviews.append(1)
        return ({key: ({"choice": next(iter(q["criteria"])), "probabilities": {
                    name: (.95 if i == 0 else .05 / (len(q["criteria"]) - 1))
                    for i, name in enumerate(q["criteria"])}} if q["type"] == "choice"
                 else {"noul": .99 if key == "needs_text" else .01})
                 for key, q in questions.items()}, {"in": 100, "out": 10})

    def complete(system, user, tier="cheap", schema=None):
        drafts.append(user)
        source_id = next(int(i) for i, body in re.findall(
            r"SOURCE \[(\d+)\][^\n]*\n[^\n]*\n([^\n]*)", user) if "red tin" in body)
        good = len(drafts) == 2 and second_ok
        return {"clarification": None, "insufficient_evidence": False, "claims": [
            {"text": "Mara put the cobalt key in a red tin.", "citation_ids": [source_id if good else 99]}]}, {"in": 100, "out": 20}

    monkeypatch.setattr(app.state.client, "complete", complete)
    app.state.client.jev = SimpleNamespace(model="jev-1.14.0", evaluate=jev, close=lambda: None)
    response = api.post(f"/api/books/{bid}/ask",
                        json={"question": "Where did Mara put the key?", "read_frontier": point})
    # Exactly one redraft, carrying the private criticism; never a third attempt.
    assert len(drafts) == 2 and "PRIVATE DRAFT REVIEW" in drafts[1] and "PRIVATE DRAFT REVIEW" not in drafts[0]
    if second_ok:
        assert response.status_code == 200 and reviews == [1]
    else:
        assert response.status_code == 502 and reviews == [] and "Mara put" not in response.text
