"""Conversation from a confirmed primary-text prefix, independent of chapter extraction latency."""
from copy import copy
from collections import deque
import hashlib
import json

from fastapi import HTTPException

from app.answer_bank import AnswerBusy, valid_answer
from app.ask import (ASK_JUDGE_SYSTEM, AskDraft, AskRetrievalLimit, AskSafetyError, ask_prompt,
    conversation_question, conversation_retrieval_question, retrieve_ask_sources, source_facts)
from app.ask_context import complete_ask_context
from app.ask_evidence import ASK_EVIDENCE_SYSTEM, AskEvidenceError
from app.ask_selection import SOURCE_SELECTION_SYSTEM, SourceSelection, select_sources_bounded
from app.content_scope import CONTENT_POLICY_VERSION
from app.conversation_pipeline import (ConversationDraft, DRAFT_SYSTEM, answer_conversation,
                                       compact_candidates)
from app.cost import CostCeilingExceeded, budgeted_completion, pricing_known
from app.eval.spoiler_gate import score_recap
from app.eval.spoiler_gate.binding import unsupported_event_bindings
from app.read_frontier import requested_snapshot
from app.retrieval_passages import passage_windows
from app.unicode_text import word_tokens


def answer_read_prefix(book_id, body, catalog, store, settings, client, worker):
    from app.api.views import _ask_cost_payload, _conversation_limit_error, _public_ask_source, _recap_input_fits
    try:
        point, chapters, state, manifest, book = requested_snapshot(
            book_id, body.read_frontier, catalog, settings, worker)
    except LookupError as exc:
        raise HTTPException(404, "unknown book") from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(409, "reading context changed; refresh before continuing") from exc
    eff = point["available_chapter"]
    question = conversation_question(body.question, body.history, eff)
    query = conversation_retrieval_question(body.question, body.history, eff)
    calls = []
    reviewer = copy(client)
    reviewer.conversation_review_effort = settings.reader_conversation_review_effort

    def complete(system, user, *, schema, phase, tier="cheap"):
        result = budgeted_completion(catalog, settings, reviewer, book_id, phase=phase,
                                     system=system, user=user, tier=tier, schema=schema)
        model = result.billing_model or reviewer.billing_model(tier)
        calls.append({"provider": reviewer.provider, "model": model,
            "input_tokens": int(result.usage.get("in", 0)),
            "output_tokens": int(result.usage.get("out", 0)), "usd": result.usd,
            "pricing_known": pricing_known(model)})
        return result.value

    scope = (book_id, book["incarnation"], hashlib.sha256(json.dumps([
        point["id"], CONTENT_POLICY_VERSION, "page-conversation-v1", DRAFT_SYSTEM,
        ASK_EVIDENCE_SYSTEM, ASK_JUDGE_SYSTEM,
        client.extractor_version("large"), client.extractor_version("cheap"),
        getattr(client, "base_url", ""), settings.reader_conversation_review_effort,
    ]).encode()).hexdigest())
    key = (*scope, body.question)
    bank = getattr(client, "answers", None) if settings.reader_answer_cache and not body.history else None

    def cost_payload(items):
        value = _ask_cost_payload(items)
        if client.provider == "codex-local":
            value["payer"] = "your ChatGPT subscription"
        return value

    def payload(draft, sources, clarification=None):
        value = {"as_of_chapter": eff, "read_frontier": point,
            "insufficient_evidence": draft.insufficient_evidence,
            "claims": [c.model_dump(mode="json") for c in draft.claims],
            "citations": [_public_ask_source(s) for s in sources], "cost": cost_payload(calls)}
        if clarification:
            value.update(answer_type="conversation", message=clarification, insufficient_evidence=False)
        if bank is not None and valid_answer(value):
            bank.set(key, value)
        return value

    def run():
        if bank is not None:
            cached = bank.get(key)
            if cached and cached.get("read_frontier") == point and valid_answer(cached):
                return {**cached, "cached": True, "cost": cost_payload([])}
        if not chapters:
            return payload(AskDraft(insufficient_evidence=True, claims=[]), [])
        atoms = {a["key"]: a for a in manifest["atoms"]}
        texts = {ch["key"]: ch["text"] for ch in chapters}
        # Scene anchors are exact windows from the READ tail, not embedding calls or memory facts.
        latest = chapters[-1]
        tail = deque(passage_windows(latest["text"]), maxlen=4)
        scene_hits = [(1.0, text, latest["ordinal"], latest["key"]) for _, _, text in reversed(tail)]
        candidates = retrieve_ask_sources(query, bookmark=eff, chapters=chapters,
            read_chapter=texts.get, semantic_hits=scene_hits, atoms=atoms, candidate_pool=True)
        candidates = compact_candidates(candidates)
        if not candidates:
            return payload(AskDraft(insufficient_evidence=True, claims=[]), [])
        if not _recap_input_fits(settings, reviewer, DRAFT_SYSTEM,
                                 ask_prompt(question, candidates, frontier=eff), schema=ConversationDraft):
            original = candidates
            selected = select_sources_bounded(question, candidates,
                fits=lambda prompt: _recap_input_fits(settings, reviewer, SOURCE_SELECTION_SYSTEM,
                                                       prompt, schema=SourceSelection),
                complete=lambda prompt: complete(SOURCE_SELECTION_SYSTEM, prompt,
                                                 schema=SourceSelection, phase="source-selection"),
                frontier=eff)
            candidates = complete_ask_context(query, selected, original, allow_named_fallback=True)
        if not candidates:
            return payload(AskDraft(insufficient_evidence=True, claims=[]), [])
        facts = source_facts(candidates)
        permitted_text = "\n".join(ch["text"] for ch in chapters)
        read_words = set(word_tokens(permitted_text))

        def score(text):
            # Keep the existing future-name oracle/prolepsis tripwire, but validate events against
            # this exact prefix rather than chapter-stamped facts which can legitimately lag it.
            with store.book(book_id) as mem:
                durable = min(state["bookmark"], state["ingest_progress"], max(0, point["atom"] - 1),
                              mem.completion_frontier(manifest["atoms"]))
                result = score_recap(mem, durable, text, read_text=permitted_text)
            result["future_entity_leaks"] = [name for name in result["future_entity_leaks"]
                if not set(word_tokens(name)) <= read_words]
            result["unsupported_event_bindings"] = unsupported_event_bindings(text, facts)
            return result

        draft, sources, clarification = answer_conversation(question, candidates, frontier=eff,
            known_names=[], complete=complete, score=score,
            max_output_tokens=settings.cost_max_output_tokens_per_call)
        return payload(draft, sources, clarification)

    try:
        if bank is not None:
            value, shared = bank.run_once(key, run)
            if shared:
                value.update(cached=True, cost=cost_payload([]))
            return value
        return run()
    except CostCeilingExceeded as exc:
        raise _conversation_limit_error(exc) from exc
    except AnswerBusy as exc:
        raise HTTPException(503, "conversation is busy; retry shortly") from exc
    except AskRetrievalLimit as exc:
        raise HTTPException(413, "read text exceeds the bounded search budget") from exc
    except (AskSafetyError, AskEvidenceError, ValueError) as exc:
        raise HTTPException(502, "reply could not be cleared against your read pages") from exc
