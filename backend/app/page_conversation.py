"""Conversation from a confirmed primary-text prefix, independent of chapter extraction latency."""
from copy import copy
from collections import deque
import hashlib
import json
import logging
import sys
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor
from threading import BoundedSemaphore
from time import perf_counter

from fastapi import HTTPException

from app.answer_bank import AnswerBusy, valid_answer
from app.ask import (ASK_JUDGE_SYSTEM, AskDraft, AskRetrievalLimit, AskSafetyError, ask_prompt,
    conversation_question, conversation_retrieval_question, retrieve_ask_sources, source_facts)
from app.ask_context import complete_ask_context
from app.ask_evidence import ASK_EVIDENCE_SYSTEM, AskEvidenceError
from app.ask_selection import SOURCE_SELECTION_SYSTEM, SourceSelection, select_sources_bounded
from app.content_scope import CONTENT_POLICY_VERSION
from app.conversation_pipeline import (ConversationDraft, DRAFT_SYSTEM, answer_conversation,
                                       compact_candidates, review_escalation, CLARIFICATIONS, BRIEF_DRAFT_SYSTEM)
from app.cost import CostCeilingExceeded, budgeted_completion, pricing_known
from app.eval.spoiler_gate import score_recap
from app.eval.spoiler_gate.synthesis import _read_visibility
from app.eval.spoiler_gate.binding import unsupported_event_bindings
from app.read_frontier import requested_snapshot
from app.retrieval_passages import passage_windows
from app.unicode_text import word_tokens, proper_words, normalize_text
from app.eval.spoiler_gate.grounding import STOPWORDS
from app.conversation_jev import (DECISION_LIMITS, FIT, SUPPORT, RELEVANCE, POLICY_VERSION,
                                  POLICY, review_with_jev, prepare_context, JevNeedsReasoning)
from app.conversation_jev import review_recap_prefix
from app.llm.jev import JevUnavailable
from app.llm.jev_budget import budgeted_jev
from app.conversation_turns import VERSION as TURN_VERSION, REPLY_SYSTEM, QUESTIONS, classify_turn, answer_dialogue

logger = logging.getLogger("mothli.conversation")
_visibility_worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="read-visibility")
_visibility_slot = BoundedSemaphore(1)


def _warm_visibility(text):
    # Only pure word sets from an already verified prefix. No model calls or stored facts.
    # One task at a time, no waiting queue; the authoritative scorer still runs every check.
    if not _visibility_slot.acquire(blocking=False):
        return None
    def prepare():
        try:
            return _read_visibility(text)
        finally:
            _visibility_slot.release()
    return _visibility_worker.submit(prepare)


def answer_read_prefix(book_id, body, catalog, store, settings, client, worker):
    started_request = perf_counter()
    request_id = uuid4().hex[:12]
    timings = {}
    from app.api.views import _ask_cost_payload, _conversation_limit_error, _public_ask_source, _recap_input_fits
    try:
        point, chapters, state, manifest, book = requested_snapshot(
            book_id, body.read_frontier, catalog, settings, worker)
    except LookupError as exc:
        raise HTTPException(404, "unknown book") from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(409, "reading context changed; refresh before continuing") from exc
    eff = point["available_chapter"]
    timings["snapshot"] = round((perf_counter() - started_request) * 1000, 2)
    use_jev = settings.reader_conversation_jev
    draft_system = BRIEF_DRAFT_SYSTEM if settings.reader_conversation_routing else DRAFT_SYSTEM
    if use_jev and not getattr(client, "jev", None):
        raise HTTPException(503, "Jev review is not configured")
    question = conversation_question(body.question, body.history, eff)
    query = conversation_retrieval_question(body.question, body.history, eff)
    calls = []
    reviewer = copy(client)
    reviewer.conversation_review_effort = settings.reader_conversation_review_effort
    reviewer.conversation_draft_effort = settings.reader_conversation_draft_effort

    def complete(system, user, *, schema, phase, tier="cheap"):
        started = perf_counter()
        try:
            result = budgeted_completion(catalog, settings, reviewer, book_id, phase=phase,
                                         system=system, user=user, tier=tier, schema=schema)
        finally:
            timings[phase] = round(timings.get(phase, 0) + (perf_counter() - started) * 1000, 2)
        model = result.billing_model or reviewer.billing_model(tier)
        calls.append({"provider": reviewer.provider, "model": model, "phase":phase,
            "input_tokens": int(result.usage.get("in", 0)),
            "output_tokens": int(result.usage.get("out", 0)), "usd": result.usd,
            "pricing_known": pricing_known(model)})
        calls[-1]["elapsed_ms"] = round((perf_counter() - started) * 1000, 2)
        return result.value

    def jev_run(state, questions):
        started = perf_counter()
        phase = ("jev-dialogue-check" if "reply" in state else "jev-intent" if "latest_message" in state
                 else "jev-source-selection" if "passages" in state else "conversation-jev")
        try:
            result = budgeted_jev(catalog, settings, client.jev, book_id,
                phase=phase, state=state, questions=questions)
        finally:
            timings[phase] = round(timings.get(phase, 0) + (perf_counter() - started) * 1000, 2)
        calls.append({"provider": "typesafe", "model": client.jev.model, "phase":phase,
            "input_tokens": result.usage["in"], "output_tokens": result.usage["out"],
            "usd": result.usd, "pricing_known": True,
            "elapsed_ms": round((perf_counter() - started) * 1000, 2)})
        return result.value

    scope = (book_id, book["incarnation"], hashlib.sha256(json.dumps([
        point["id"], CONTENT_POLICY_VERSION, "page-conversation-v1", DRAFT_SYSTEM,
        ASK_EVIDENCE_SYSTEM, ASK_JUDGE_SYSTEM,
        [POLICY_VERSION, POLICY, FIT, SUPPORT, RELEVANCE, DECISION_LIMITS, client.jev.model]
        if use_jev else "parallel-gpt-review",
        client.extractor_version("large"), client.extractor_version("cheap"),
        getattr(client, "base_url", ""), settings.reader_conversation_review_effort,
        settings.reader_conversation_draft_effort, settings.reader_conversation_routing,
        draft_system,
        [TURN_VERSION, REPLY_SYSTEM, QUESTIONS] if settings.reader_conversation_routing else None,
    ]).encode()).hexdigest())
    key = (*scope, body.question)
    bank = getattr(client, "answers", None) if settings.reader_answer_cache and not body.history else None

    def timing_payload():
        return {"request_id":request_id, "total_ms":round((perf_counter()-started_request)*1000, 2),
                "stages_ms":dict(timings)}

    def cost_payload(items):
        value = _ask_cost_payload(items)
        for public, original in zip(value["calls"], items, strict=True):
            public.update({k: original[k] for k in ("input_tokens", "output_tokens", "elapsed_ms", "phase")})
        if client.provider == "codex-local":
            value["payer"] = ("your ChatGPT subscription and TypeSafe account"
                              if any(c["provider"] == "typesafe" for c in items)
                              else "your ChatGPT subscription")
        return value

    def payload(draft, sources, clarification=None):
        value = {"as_of_chapter": eff, "read_frontier": point,
            "insufficient_evidence": draft.insufficient_evidence,
            "claims": [c.model_dump(mode="json") for c in draft.claims],
            "citations": [_public_ask_source(s) for s in sources], "cost": cost_payload(calls)}
        if clarification:
            value.update(answer_type="conversation", message=clarification, insufficient_evidence=False)
        value["timing"] = timing_payload()
        if bank is not None and valid_answer(value):
            bank.set(key, value)
        return value

    def run():
        if bank is not None:
            cached = bank.get(key)
            if cached and cached.get("read_frontier") == point and valid_answer(cached):
                return {**cached, "cached": True, "cost": cost_payload([]), "timing":timing_payload()}
        route = classify_turn(body.question, run=jev_run, history=[m.model_dump(mode="json")
            for m in body.history if m.as_of_chapter <= eff]) if settings.reader_conversation_routing else "evidence"
        scene_bank = getattr(client, "answers", None) if settings.reader_answer_cache and route == "recent_recap" else None
        scene_key = (*scope, "scene-recap-no-dialogue", body.question)
        if scene_bank is not None:
            cached = scene_bank.get(scene_key)
            if cached and cached.get("read_frontier") == point and valid_answer(cached):
                return {**cached, "cached":True, "cost":cost_payload(calls), "timing":timing_payload()}
        if route.startswith("clarify_"):
            return payload(AskDraft(insufficient_evidence=True, claims=[]), [],
                           CLARIFICATIONS[route.removeprefix("clarify_")])
        if route == "dialogue":
            message = answer_dialogue(question, route=route, complete=complete, run=jev_run)
            return payload(AskDraft(insufficient_evidence=True, claims=[]), [], message)
        if not chapters:
            return payload(AskDraft(insufficient_evidence=True, claims=[]), [])
        retrieval_started = perf_counter()
        atoms = {a["key"]: a for a in manifest["atoms"]}
        texts = {ch["key"]: ch["text"] for ch in chapters}
        # Scene anchors are exact windows from the READ tail, not embedding calls or memory facts.
        latest = chapters[-1]
        tail = deque(passage_windows(latest["text"]), maxlen=4)
        scene_hits = [(1.0, text, latest["ordinal"], latest["key"]) for _, _, text in reversed(tail)]
        if route == "identity":
            # Introduction windows are exact source text, not stored/generated character facts.
            names = {normalize_text(w.text) for w in proper_words(body.question)} - STOPWORDS - {
                "who", "what", "remind", "tell", "please"}
            introductions = []
            if names:
                for chapter in chapters:
                    for _, _, text in passage_windows(chapter["text"]):
                        if names <= set(word_tokens(text)):
                            introductions.append((1.0, text, chapter["ordinal"], chapter["key"]))
                            break
                    if len(introductions) == 2:
                        break
            scene_hits = introductions
        if route == "recent_recap":
            atom = atoms[latest["key"]]
            candidates = [{"id":i, "ordinal":latest["ordinal"], "chapter_key":latest["key"],
                "href":atom.get("href", ""), "title":atom.get("title", ""), "source_offset":offset,
                "text":text} for i, (offset, _, text) in enumerate(tail, 1)]
        else:
            candidates = retrieve_ask_sources(query, bookmark=eff, chapters=chapters,
                read_chapter=texts.get, semantic_hits=scene_hits, atoms=atoms, candidate_pool=True,
                prefer_recent=route != "identity")
        candidates = compact_candidates(candidates)
        timings["retrieval"] = round((perf_counter() - retrieval_started) * 1000, 2)
        if not candidates:
            return payload(AskDraft(insufficient_evidence=True, claims=[]), [])
        context_notes = ""
        if route == "recent_recap":
            # Fixed recent scene, already clipped to the exact read offset. No global reranking.
            context_notes = "Give a brief catch-up on the current reading scene. Earlier dialogue is not a request to recap a previously discussed character."
        elif use_jev:
            original = candidates
            selected, context_notes = prepare_context(question, candidates, frontier=eff,
                                                       run=jev_run, settings=settings)
            candidates = complete_ask_context(query, selected, original, allow_named_fallback=True)
        if not _recap_input_fits(settings, reviewer, draft_system,
                                 ask_prompt(question, candidates, frontier=eff) + "\n" + context_notes,
                                 schema=ConversationDraft):
            if use_jev:
                raise HTTPException(413, "source context exceeds the one-draft experiment limit")
            else:
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
        visibility = _warm_visibility(permitted_text) if settings.reader_conversation_routing else None
        read_words = None

        def score(text):
            nonlocal read_words
            started_safety = perf_counter()
            if visibility is not None:
                visibility.result()
            # Keep the existing future-name oracle/prolepsis tripwire, but validate events against
            # this exact prefix rather than chapter-stamped facts which can legitimately lag it.
            with store.book(book_id) as mem:
                durable = min(state["bookmark"], state["ingest_progress"], max(0, point["atom"] - 1),
                              mem.completion_frontier(manifest["atoms"]))
                result = score_recap(mem, durable, text, read_text=permitted_text)
            if result["future_entity_leaks"]:
                if read_words is None:
                    read_words = set(word_tokens(permitted_text))
                result["future_entity_leaks"] = [name for name in result["future_entity_leaks"]
                    if not set(word_tokens(name)) <= read_words]
            result["unsupported_event_bindings"] = unsupported_event_bindings(text, facts)
            timings["safety"] = round(timings.get("safety", 0) + (perf_counter()-started_safety)*1000, 2)
            return result

        def jev_review(draft, sources, **kwargs):
            try:
                if route == "recent_recap":
                    return review_recap_prefix(draft, sources, run=jev_run, settings=settings, **kwargs)
                return review_with_jev(draft, sources, run=jev_run, settings=settings,
                    complete_instruction=route not in {"recent_recap", "identity"}, **kwargs)
            except JevNeedsReasoning as exc:
                return review_escalation(draft, sources, claim_ids=exc.claim_ids, **kwargs)

        draft_question = body.question if route == "recent_recap" else question
        draft, sources, clarification = answer_conversation(draft_question, candidates, frontier=eff,
            known_names=[], complete=complete, score=score,
            max_output_tokens=settings.cost_max_output_tokens_per_call,
            review=jev_review if use_jev else None, max_attempts=1 if use_jev else 2,
            context_notes=context_notes, draft_system=draft_system,
            allow_verified_prefix=route == "recent_recap")
        result = payload(draft, sources, clarification)
        if scene_bank is not None and valid_answer(result):
            scene_bank.set(scene_key, result)
        return result

    try:
        if bank is not None:
            value, shared = bank.run_once(key, run)
            if shared:
                value.update(cached=True, cost=cost_payload([]), timing=timing_payload())
            return value
        return run()
    except CostCeilingExceeded as exc:
        raise _conversation_limit_error(exc) from exc
    except AnswerBusy as exc:
        raise HTTPException(503, "conversation is busy; retry shortly") from exc
    except JevUnavailable as exc:
        raise HTTPException(503, "Jev review unavailable; no unchecked reply was published") from exc
    except AskRetrievalLimit as exc:
        raise HTTPException(413, "read text exceeds the bounded search budget") from exc
    except (AskSafetyError, AskEvidenceError, ValueError) as exc:
        raise HTTPException(502, "reply could not be cleared against your read pages",
                            headers={"X-Mothli-Request-Id":request_id}) from exc
    finally:
        failure = sys.exception()
        record = {"event":"conversation", "request_id":request_id,
            "elapsed_ms":round((perf_counter()-started_request)*1000, 2),
            "stages_ms":timings, "failure":type(failure).__name__ if failure else None,
            "cause":type(failure.__cause__).__name__ if failure and failure.__cause__ else None,
            "history_turns":len(body.history), "question_chars":len(body.question),
            "calls":[{k:v for k,v in call.items() if k in {"phase","input_tokens","output_tokens","elapsed_ms"}}
                     for call in calls]}
        logger.log(logging.WARNING if failure else logging.INFO, json.dumps(record))
