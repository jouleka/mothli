"""Conversation from a confirmed primary-text prefix, independent of chapter extraction latency."""
from copy import copy
from collections import deque
import json
import logging
import sys
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor
from threading import BoundedSemaphore, Lock
from time import perf_counter

from fastapi import HTTPException

from app.ask import (AskDraft, AskRetrievalLimit, AskSafetyError, ask_prompt,
    conversation_question, conversation_retrieval_question, retrieve_ask_sources, source_facts)
from app.ask_context import complete_ask_context
from app.ask_evidence import AskEvidenceError
from app.ask_selection import SOURCE_SELECTION_SYSTEM, SourceSelection, select_sources_bounded
from app.conversation_pipeline import (ConversationDraft, SceneConversationDraft, DRAFT_SYSTEM, answer_conversation,
                                       compact_candidates, review_escalation, CLARIFICATIONS, BRIEF_DRAFT_SYSTEM,
                                       review_draft, PROSE_DRAFT_SYSTEM, ProseConversationDraft)
from app.cost import CostCeilingExceeded, budgeted_completion, pricing_known
from app.eval.spoiler_gate import score_recap
from app.eval.spoiler_gate.synthesis import _read_visibility
from app.eval.spoiler_gate.binding import unsupported_event_bindings
from app.read_frontier import requested_snapshot
from app.retrieval_passages import passage_windows, recent_scene_windows
from app.unicode_text import word_tokens, proper_words, normalize_text
from app.eval.spoiler_gate.grounding import STOPWORDS
from app.conversation_jev import (review_with_jev, prepare_context, JevNeedsReasoning,
                                  answers_request)
from app.conversation_jev import review_recap_prefix
from app.llm.jev import JevUnavailable
from app.llm.jev_budget import budgeted_jev
from app.conversation_turns import (classify_turn,
                                    answer_dialogue, book_fact_risk)
from app.known_names import (same_person_names, names_note, text_same_person, first_mention)

REACTION_NOTES = (
    "The reader is reacting to what they have just read, not asking a question. Reply as a companion "
    "who has read the same pages: in one or two short sentences, name the specific moment or detail "
    "in these passages that most plausibly prompted the reaction. State only what the passages show. "
    "Do not summarize the chapter, explain it, or ask for clarification, and keep clarification null. "
    "If no passage plausibly relates to the reaction, return insufficient_evidence=true.")

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
    calculation, reader_message = None, body.question
    if settings.reader_conversation_routing:
        from app.arithmetic import arithmetic_prefix
        combined = arithmetic_prefix(body.question)
        if combined:
            calculation, reader_message = combined
    draft_system = BRIEF_DRAFT_SYSTEM if settings.reader_conversation_routing else DRAFT_SYSTEM
    prose = bool(settings.reader_conversation_prose and use_jev)
    if prose:
        draft_system = PROSE_DRAFT_SYSTEM
    question = conversation_question(reader_message, body.history, eff)
    query = conversation_retrieval_question(reader_message, body.history, eff)
    calls = []
    reviewer = copy(client)
    reviewer.conversation_review_effort = settings.reader_conversation_review_effort
    reviewer.conversation_draft_effort = settings.reader_conversation_draft_effort
    if getattr(reviewer, "_oai", None) is not None:
        reviewer._oai = reviewer._oai.with_options(timeout=settings.reader_conversation_timeout_seconds)
    signals = {}

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

    timing_lock = Lock()

    def jev_run(state, questions):
        started = perf_counter()
        phase = ("jev-dialogue-check" if "reply" in state else "jev-intent" if "latest_message" in state
                 else "jev-source-selection" if "passages" in state else "conversation-jev")
        try:
            result = budgeted_jev(catalog, settings, client.jev, book_id,
                phase=phase, state=state, questions=questions)
        finally:
            with timing_lock:
                timings[phase] = round(timings.get(phase, 0) + (perf_counter() - started) * 1000, 2)
        calls.append({"provider": "typesafe", "model": client.jev.model, "phase":phase,
            "input_tokens": result.usage["in"], "output_tokens": result.usage["out"],
            "usd": result.usd, "pricing_known": True,
            "elapsed_ms": round((perf_counter() - started) * 1000, 2)})
        return result.value

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
        elif draft.insufficient_evidence and signals.get("ahead"):
            value["status"] = "ahead"
        if signals.get("prose_reply") and draft.claims:
            value["style"] = "prose"
            if signals.get("closing"):
                value["closing"] = signals["closing"]
        if calculation:
            value["calculation"] = calculation
        value["timing"] = timing_payload()
        return value

    def run():
        if settings.reader_conversation_routing:
            from app.arithmetic import arithmetic_reply
            calculated=arithmetic_reply(body.question)
            if calculated is not None:
                return payload(AskDraft(insufficient_evidence=True,claims=[]),[],calculated)
        if use_jev and not getattr(client,"jev",None):
            raise HTTPException(503,"Jev review is not configured")
        route = "evidence"
        if settings.reader_conversation_routing:
            try:
                route = classify_turn(reader_message, run=jev_run, signals=signals,
                    history=[m.model_dump(mode="json") for m in body.history if m.as_of_chapter <= eff])
            except JevUnavailable:
                # Routing only chooses a cheaper path. Without it, take the full evidence path.
                signals["jev_down"] = True
        if route.startswith("clarify_"):
            return payload(AskDraft(insufficient_evidence=True, claims=[]), [],
                           CLARIFICATIONS[route.removeprefix("clarify_")])
        def plain_reply():
            message = answer_dialogue(question, route="dialogue", complete=complete, run=jev_run)
            return payload(AskDraft(insufficient_evidence=True, claims=[]), [], message)

        if route == "dialogue" or (route == "reaction" and not chapters):
            return plain_reply()
        if not chapters:
            return payload(AskDraft(insufficient_evidence=True, claims=[]), [])
        permitted_text = "\n".join(ch["text"] for ch in chapters)
        identities = []
        if route != "recent_recap":
            # Reviewed memory, bounded by completed chapters AND by the exact read prefix.
            with store.book(book_id) as mem:
                durable = min(state["bookmark"], state["ingest_progress"], max(0, point["atom"] - 1),
                              mem.completion_frontier(manifest["atoms"]))
                if durable > 0 and mem.view(durable).characters():
                    identities = same_person_names(mem, durable, set(word_tokens(permitted_text)),
                                                   set(word_tokens(query, min_length=2)))
        known_names = [form for group in identities for form in group]
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
            covered = any(names & set(word_tokens(form, min_length=2))
                          for group in identities for form in group)
            if names and use_jev and not covered and not signals.get("jev_down"):
                # No reviewed memory for this name (memory off, or not yet ingested): look for
                # its other forms in the pages where the reader first met it.
                mentions = first_mention(names, chapters, passage_windows)
                asked = " ".join(w.text for w in proper_words(body.question)
                                 if normalize_text(w.text) in names)
                try:
                    found = text_same_person(asked, names, [text for text, _ in mentions], run=jev_run)
                except JevUnavailable:
                    signals["jev_down"] = True
                    found = []
                if found:
                    identities = identities + found
                    known_names = [form for group in identities for form in group]
                    # The passages that establish the link stay in the pool as citable evidence.
                    introductions = [(1.0, text, chapter["ordinal"], chapter["key"])
                                     for text, chapter in mentions]
            # The reader's own form first, then each other known form of the same person, so the
            # passage that introduces "Alexey" is offered when the reader asks about "Alyosha".
            wanted = [names] if names else []
            for group in identities:
                forms = [set(word_tokens(form, min_length=2)) for form in group]
                if names and any(names & form for form in forms):
                    wanted += [form for form in forms if form and form not in wanted]
            for position, form in enumerate(wanted[:4]):
                found = 0
                for chapter in chapters:
                    for _, _, text in passage_windows(chapter["text"]):
                        if form <= set(word_tokens(text)):
                            hit = (1.0, text, chapter["ordinal"], chapter["key"])
                            if hit not in introductions:
                                introductions.append(hit)
                                found += 1
                            break
                    if found == (2 if position == 0 else 1):
                        break
            scene_hits = introductions
        if route == "recent_recap":
            atom = atoms[latest["key"]]
            candidates = [{"id":i, "ordinal":latest["ordinal"], "chapter_key":latest["key"],
                "href":atom.get("href", ""), "title":atom.get("title", ""), "source_offset":offset,
                "text":text} for i, (offset, _, text) in enumerate(recent_scene_windows(latest["text"]), 1)]
        else:
            candidates = retrieve_ask_sources(query, bookmark=eff, chapters=chapters,
                read_chapter=texts.get, semantic_hits=scene_hits, atoms=atoms, candidate_pool=True,
                prefer_recent=route != "identity", known_names=known_names)
        candidates = compact_candidates(candidates)
        timings["retrieval"] = round((perf_counter() - retrieval_started) * 1000, 2)
        if not candidates:
            return plain_reply() if route == "reaction" else payload(
                AskDraft(insufficient_evidence=True, claims=[]), [])
        context_notes = ""
        if route == "recent_recap":
            # Fixed recent scene, already clipped to the exact read offset. No global reranking.
            context_notes = ("Give a brief catch-up on the material currently being presented. "
                f"Lead with the most recent substantive detail in SOURCE [{candidates[-1]['id']}], "
                "at the end of the permitted text; use earlier passages for necessary context. "
                "Do not repeat an opening introduction when newer material is available. "
                "Select one salient point; a catch-up need not catalogue every action or speech. "
                "Prefer a single simple sentence, adding a second only if essential. "
                "Do not add a location from a chapter title. If a pronoun's referent needs an "
                "earlier passage, cite that passage too or omit the unnecessary qualifier. "
                "Preserve the narrator's uncertainty and attributed opinions. Earlier dialogue "
                "is not a request to recap a previously discussed character.")
        elif use_jev:
            original = candidates
            try:
                if signals.get("jev_down"):
                    raise JevUnavailable("Jev already failed in this request")
                selected, context_notes = prepare_context(question, candidates, frontier=eff,
                                                           run=jev_run, settings=settings)
            except JevUnavailable:
                # Retrieval already ordered the pool by priority; use its first six bodies.
                signals["jev_down"] = True
                selected, context_notes = candidates[:6], ""
            candidates = complete_ask_context(query, selected, original, known_names=known_names,
                                              allow_named_fallback=True)
        context_notes = "\n".join(note for note in (
            context_notes, names_note(identities), REACTION_NOTES if route == "reaction" else "") if note)
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
            return plain_reply() if route == "reaction" else payload(
                AskDraft(insufficient_evidence=True, claims=[]), [])
        facts = source_facts(candidates)
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

        def gpt_review(draft, sources, **kwargs):
            # The stricter pair of independent reviews. It clears a whole draft or nothing, so
            # it cannot stand in for the standalone-meaning check on a substituted alternative.
            if kwargs.get("require_standalone"):
                raise JevUnavailable("Jev review unavailable")
            return review_draft(draft, sources, **{k: kwargs[k] for k in (
                "question", "frontier", "scene_sources", "coverage_sources", "complete",
                "max_output_tokens")})

        def jev_review(draft, sources, **kwargs):
            if signals.get("jev_down"):
                return gpt_review(draft, sources, **kwargs)
            kwargs["identities"] = identities
            kwargs["connected"] = prose and route != "recent_recap"
            try:
                if route == "recent_recap":
                    return review_recap_prefix(draft, sources, run=jev_run, settings=settings,
                                               trace_id=request_id, **kwargs)
                return review_with_jev(draft, sources, run=jev_run, settings=settings,
                    complete_instruction=route not in {"recent_recap", "identity", "reaction"}, **kwargs)
            except JevUnavailable:
                signals["jev_down"] = True
                kwargs.pop("identities", None)
                kwargs.pop("connected", None)
                return gpt_review(draft, sources, **kwargs)
            except JevNeedsReasoning as exc:
                if exc.review_alternative is not None:
                    candidate, candidate_sources = exc.review_alternative
                    review_escalation(candidate, candidate_sources, claim_ids=None,
                                      standalone=True, **kwargs)
                    return candidate
                standalone = bool(kwargs.get("require_standalone",False))
                shorter = review_escalation(draft, sources,
                    claim_ids=None if standalone else exc.claim_ids, standalone=standalone, **kwargs)
                if shorter is not None:
                    # The second review rejected a later sentence. The proven leading sentences
                    # are published only if they still answer what was asked.
                    try:
                        if not answers_request(shorter, question=kwargs["question"], run=jev_run):
                            raise AskEvidenceError()
                    except JevUnavailable:
                        raise AskEvidenceError() from None
                    signals["shortened"] = True
                return shorter

        draft_question = reader_message if route == "recent_recap" else question
        prose_reply = prose and route != "recent_recap"
        proposed = {}
        try:
            draft, sources, clarification = answer_conversation(draft_question, candidates, frontier=eff,
                known_names=known_names, complete=complete, score=score,
                max_output_tokens=settings.cost_max_output_tokens_per_call,
                review=jev_review if use_jev else None,
                max_attempts=1 if use_jev else 2,
                context_notes=context_notes, draft_system=draft_system,
                draft_schema=(SceneConversationDraft if route=="recent_recap"
                              else ProseConversationDraft if prose_reply else ConversationDraft),
                allow_verified_prefix=route == "recent_recap",
                on_generated=lambda value: proposed.update(closing=getattr(value, "closing", None)),
                retry_unreviewed=use_jev)
        except (AskSafetyError, AskEvidenceError, ValueError):
            if route != "reaction":
                raise
            # A reaction never ends in an error: an uncleared grounded reply is discarded and
            # the reader gets the plain acknowledgment that carries no book facts.
            return plain_reply()
        if route == "reaction" and (draft.insufficient_evidence or clarification):
            return plain_reply()
        if prose_reply and draft.claims:
            signals["prose_reply"] = True
            closing = " ".join((proposed.get("closing") or "").split())
            if closing and not signals.get("jev_down"):
                # The one uncited line. It is kept only when it carries no book content and
                # passes the same spoiler scorer; otherwise the checked reply goes out without it.
                try:
                    safety = score(closing)
                    if (book_fact_risk(closing, question, run=jev_run) < .1 and not any(safety[key] for key in (
                            "future_entity_leaks", "prolepsis_hits", "unsupported_event_bindings"))):
                        signals["closing"] = closing
                except (JevUnavailable, AskEvidenceError):
                    pass
        return payload(draft, sources, clarification)

    try:
        return run()
    except CostCeilingExceeded as exc:
        raise _conversation_limit_error(exc) from exc
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
