"""View routes (ADR 0007 D-A10/D-A11): graph / timeline / notes / search / catch-me-up.

THE CLAMP: every bookmark-taking route computes ``effective_bookmark`` from the requested bookmark,
reader high-water, and this process's contiguous v2-marker-validated frontier ONCE at the route boundary
(typed int >= 0 — anything else is 422 fail-closed), then threads it through BOTH the view read and any
gate/synthesis call (D-A9/D-A10). Catalog progress alone can never authorize legacy partial facts.

LOCK DISCIPLINE (D-A3): the search-query embedding and the recap synthesis LLM call happen OUTSIDE
``store.book()``; only funnel reads + the deterministic gate run under the per-book lock.

CATCH-ME-UP is lazy synthesis (D5) behind the LIT-8 runtime gate: supplied-facts -> large-tier recap
-> ``assert_recap_safe`` (one regenerate on rejection, then a GENERIC 502 — the error channel must
never carry the rejected recap or a future name). Cached in-process by the full Inv-7 key
(book, catalog incarnation, bookmark, validity_snapshot, current synth model, prompt version,
atom_set_version) — the
pass-3 churn fixes are what make this cache affordable mid-ingest."""
from copy import copy
from concurrent.futures import ThreadPoolExecutor
import hashlib
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator
from app.read_frontier import ReadBoundary

from app.reader_reactions import reaction_clarification, reaction_copy

from app.ask import (
    ASK_SYSTEM,
    AskDraft,
    AskRequest,
    AskRetrievalLimit,
    AskSafetyError,
    ask_prompt,
    conversation_question,
    conversation_retrieval_question,
    recap_request as _recap_request,
    ask_repair_prompt,
    cited_sources,
    complete_ask_citations,
    draft_text,
    judge_ask_draft,
    retrieve_ask_sources,
    source_facts,
    validate_ask_draft,
)
from app.ask_context import complete_ask_context
from app.ask_evidence import (
    ASK_EVIDENCE_SYSTEM,
    AskEvidenceError,
    AskEvidenceReview,
    complete_evidence_citations,
    evidence_prompt,
    evidence_review_batches,
    merge_evidence_audits,
    validate_evidence_audit,
)
from app.ask_selection import (
    SOURCE_SELECTION_SYSTEM,
    SourceSelection,
    SourceSelectionError,
    select_sources_bounded,
)

from app.cost import CostCeilingExceeded, budgeted_completion, estimate_input_tokens, pricing_known
from app.deps import (
    book_lifecycle,
    get_catalog,
    get_client,
    get_reader_client,
    get_settings,
    get_content_store as get_store,
    get_worker,
)
from app.eval.spoiler_gate import (
    SpoilerGateError,
    assert_recap_safe,
    cache_key,
    delta_facts,
    evolve_prompt,
    flowing_system_for,
    now_prompt,
    now_system_for,
    read_text_upto,
    score_recap,
    supplied_facts,
    synth_prompt,
    validity_snapshot,
)
from app.eval.spoiler_gate.judge import (
    JUDGE_SYSTEM,
    JudgeUnavailable,
    JudgeVerdict,
    judge_prompt,
    judge_recap,
)
from app.ingest.manifest import AtomSetMismatch, assert_matches_store, load_manifest
from app.llm.retrieval import budgeted_query_embedding
from app.llm.client import ProviderUnavailable
from app.llm.jev import JevUnavailable
from app.llm.jev_budget import budgeted_jev
from app.llm import jev_tasks
from app.reading_assist import (
    CLOSEOUT_SYSTEM,
    SELECTION_SYSTEM,
    SELECTION_REVIEW_SYSTEM,
    ChapterCloseoutRequest,
    SelectionActionRequest,
    SelectionDraft,
    SelectionVerdict,
    chapter_closeout_prompt,
    chapter_passages,
    selection_prompt,
    selection_review_prompt,
)
from app.recap_grounding import (
    PRIMARY_RECAP_VERSION, primary_generation_prompt, primary_repair_context,
    primary_review_preflight, primary_snapshot, review_primary_recap,
)
# The story memory screens' bodies live in one module both modes call (ADR 0042 section 9).
from app.memory_views import (  # noqa: F401  (the three helpers are imported from here too)
    _character_card,
    _coalesce_honorific_characters,
    _visible_cast,
    character_payload,
    clamp,
    graph_payload,
    notes_payload,
)

router = APIRouter(
    prefix="/api/books/{book_id}", tags=["views"], dependencies=[Depends(book_lifecycle)]
)

RECAP_PROMPT_VERSION = "recap-v3"                 # novel synthesis prompt (LIT-29)
NON_NOVEL_RECAP_PROMPT_VERSION = "recap-v4"       # LIT-9 profile-aware neutral wording
RECAP_FAILURE_TTL_S = 60.0                        # negative cache: a double-rejected key 502s without
#                                                   re-paying until the TTL lapses (pass-2 convoy fix)
_log = logging.getLogger(__name__)


def _preparation_state(state, requested, worker, book_id):
    target = state["bookmark"] if requested is None else min(requested, state["bookmark"])
    if state["ingest_progress"] < target:
        return ("preparation_failed" if worker.status(book_id)["status"] not in {"running", "queued"}
                else "preparing")
    return "not_started" if target == 0 else None


def _recap_prompt_version(book_type, content_language="und"):
    from app.content_scope import CONTENT_POLICY_VERSION
    # Include content policy so cached recaps cannot resurrect excluded supplementary material.
    # Other profiles also key by type because their wording differs.
    base = (RECAP_PROMPT_VERSION if book_type == "novel"
            else f"{NON_NOVEL_RECAP_PROMPT_VERSION}:{book_type}")
    base += f":{CONTENT_POLICY_VERSION}"
    return base if content_language in {"und", "en"} or content_language.startswith("en-") \
        else f"{base}:source-{content_language}"

_BM = Query(default=None, ge=0, description="explicit bookmark (scrubber); clamped to the high-water")


class EntityCorrectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_entity_id: StrictInt = Field(ge=1)
    canonical_name: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=1000)
    bookmark: StrictInt = Field(ge=1)

    @field_validator("canonical_name", "reason")
    @classmethod
    def non_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("value must not be blank")
        return value


def _effective(book_id, requested, catalog, store, settings, worker):
    """The route-boundary prologue: known book -> fail-closed manifest/store check -> the CLAMP."""
    st = catalog.get_state(book_id)
    if st is None:
        raise HTTPException(404, "unknown book")
    try:
        manifest = load_manifest(settings.data_dir, book_id)
        with store.book(book_id) as mem:
            assert_matches_store(manifest, mem)
            durable = mem.completion_frontier(manifest["atoms"])
    except AtomSetMismatch as e:
        raise HTTPException(409, "atom-set mismatch (re-import the book)") from e
    eff = clamp(requested, st["bookmark"], durable)
    return eff, st, manifest


@router.post("/people")
def people(book_id: str, body: ReadBoundary, catalog=Depends(get_catalog),
           settings=Depends(get_settings),client=Depends(get_reader_client),worker=Depends(get_worker)):
    from app.read_frontier import requested_snapshot
    from app.people import visible_names
    if not settings.reader_page_context:
        raise HTTPException(422,"confirmed page context is required")
    try:
        if settings.reader_read_map:
            from app.read_map import read_snapshot
            point,chapters,*_ = read_snapshot(book_id,catalog,settings,worker)
        else:
            point,chapters,*_ = requested_snapshot(book_id,body,catalog,settings,worker)
        def run(state,questions):
            return budgeted_jev(catalog,settings,client.jev,book_id,phase="people-jev",
                state=state,questions=questions).value
        if not chapters:
            names=[]
        elif not getattr(client,"jev",None):
            raise HTTPException(503,"Jev name classification is not configured")
        else:
            names=visible_names(chapters,run=run)
        return {"read_frontier":point,"names":names}
    except LookupError as exc:
        raise HTTPException(404,"unknown book") from exc
    except (ValueError,RuntimeError) as exc:
        if isinstance(exc,CostCeilingExceeded):
            raise HTTPException(429,"book cost ceiling reached") from exc
        if isinstance(exc,JevUnavailable):
            raise HTTPException(503,"name classification is unavailable") from exc
        raise HTTPException(409,"reading context changed") from exc


@router.get("/graph")
def graph(book_id: str, bookmark: int | None = _BM, catalog=Depends(get_catalog),
          store=Depends(get_store), settings=Depends(get_settings), worker=Depends(get_worker)):
    eff, _st, _m = _effective(book_id, bookmark, catalog, store, settings, worker)
    with store.book(book_id) as mem:
        return graph_payload(mem, eff)


@router.get("/memory-corrections")
def memory_corrections(book_id: str, bookmark: int | None = _BM,
                       catalog=Depends(get_catalog), store=Depends(get_store),
                       settings=Depends(get_settings), worker=Depends(get_worker)):
    """Reader-safe provenance: a correction is hidden before its story-time frontier."""
    eff, _st, _manifest = _effective(book_id, bookmark, catalog, store, settings, worker)
    if eff < 1:
        return {"as_of_chapter": eff, "items": []}
    with store.book(book_id) as mem:
        items = mem.entity_correction_history(eff)
    return {"as_of_chapter": eff, "items": items}


@router.post("/memory-corrections")
def correct_memory(book_id: str, body: EntityCorrectionRequest, request: Request,
                   catalog=Depends(get_catalog), store=Depends(get_store),
                   settings=Depends(get_settings), worker=Depends(get_worker)):
    """Publish a one-for-one identity correction at the reader's exact current frontier."""
    eff, state, _manifest = _effective(
        book_id, body.bookmark, catalog, store, settings, worker
    )
    if eff != body.bookmark or state["bookmark"] != body.bookmark:
        raise HTTPException(409, "reading progress changed; reopen the codex and try again")
    try:
        with store.book(book_id) as mem:
            result = mem.replace_entity(
                body.source_entity_id,
                effective_at=eff,
                canonical_name=body.canonical_name,
                reason=body.reason,
            )
            items = mem.entity_correction_history(eff)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"as_of_chapter": eff, **result, "items": items}


@router.get("/character/{entity_id}")
def character_card(book_id: str, entity_id: int, bookmark: int | None = _BM,
                   catalog=Depends(get_catalog), store=Depends(get_store),
                   settings=Depends(get_settings), worker=Depends(get_worker)):
    """LIT-30 name card: bookmark-clamped identity + ties for a character. A future / unknown entity
    404s (no leak — indistinguishable from nonexistent). Structured data only; no gate/judge (nothing
    is generated). ``entity_id`` is a typed path param (a non-int is 422)."""
    eff, _st, _m = _effective(book_id, bookmark, catalog, store, settings, worker)
    with store.book(book_id) as mem:
        card = character_payload(mem, eff, entity_id)
    if card is None:
        raise HTTPException(404, "unknown character")
    return card


@router.get("/timeline")
def timeline(book_id: str, bookmark: int | None = _BM, catalog=Depends(get_catalog),
             store=Depends(get_store), settings=Depends(get_settings), worker=Depends(get_worker)):
    eff, _st, _m = _effective(book_id, bookmark, catalog, store, settings, worker)
    with store.book(book_id) as mem:
        v = mem.view(eff)
        events = []
        for ev in v.timeline():
            row = dict(ev)
            row["participants"] = [p["canonical_name"] for p in v.participants_of(ev["event_id"])]
            events.append(row)
    return {"as_of_chapter": eff, "events": events}


@router.get("/notes")
def notes(book_id: str, bookmark: int | None = _BM, catalog=Depends(get_catalog),
          store=Depends(get_store), settings=Depends(get_settings), worker=Depends(get_worker)):
    """LIT-31 codex 'the story broken down': see ``app.memory_views.notes_payload``."""
    eff, _st, _m = _effective(book_id, bookmark, catalog, store, settings, worker)
    with store.book(book_id) as mem:
        return notes_payload(mem, eff)


@router.get("/search")
def search(book_id: str, q: str, k: int = Query(default=5, ge=1, le=50),
           bookmark: int | None = _BM, catalog=Depends(get_catalog), store=Depends(get_store),
           settings=Depends(get_settings), client=Depends(get_client), worker=Depends(get_worker)):
    eff, _st, _m = _effective(book_id, bookmark, catalog, store, settings, worker)
    if eff <= 0:
        return {"as_of_chapter": 0, "hits": []}
    with store.book(book_id) as mem:
        pinned = mem.pinned_identity()
    try:
        qvec = budgeted_query_embedding(
            catalog, settings, client, book_id, pinned=pinned, texts=[q]
        ).value[0]                                # OUTSIDE the lock (D-A3 — never IO under it)
    except CostCeilingExceeded as exc:
        raise HTTPException(429, "book cost ceiling reached") from exc
    with store.book(book_id) as mem:
        hits = mem.view(eff).search(qvec, k=k)
    return {"as_of_chapter": eff,
            "hits": [{"score": round(s, 6), "text": t, "revealed_at": rev, "chapter_key": key}
                     for (s, t, rev, key) in hits]}


def _ask_cost_payload(calls):
    return {
        "currency": "USD",
        "usd": f"{sum(call['usd'] for call in calls):.10f}",
        "input_tokens": sum(call["input_tokens"] for call in calls),
        "output_tokens": sum(call["output_tokens"] for call in calls),
        "pricing_known": all(call["pricing_known"] for call in calls),
        "calls": [
            {
                "provider": call["provider"],
                "model": call["model"],
                "usd": f"{call['usd']:.10f}",
            }
            for call in calls
        ],
        "payer": (
            "local offline engine"
            if calls and all(call["provider"] == "stub" for call in calls)
            else "your configured provider account"
        ),
    }


def _public_ask_source(source):
    return {
        "id": source["id"],
        "ordinal": source["ordinal"],
        "chapter_key": source["chapter_key"],
        "href": source["href"],
        "title": source["title"],
        "excerpt": source["text"],
    }


def _complete_citation_evidence(value, sources, *, catalog, settings, client, book_id,
                                remember, known_names=(), question=None, remember_jev=None):
    """Explicit evidence coverage before the fresh, claim-specific semantic review.

    This call runs outside the book lock. It may change only citation IDs, using exact spans of
    already selected completed sources; it cannot repair an unsupported assertion by changing text.
    """
    draft = AskDraft.model_validate(value)
    if draft.insufficient_evidence or client.provider == "stub":
        return complete_ask_citations(draft, sources, known_names=known_names)
    draftdict = draft.model_dump(mode="json")
    evidence_prompt(draftdict, sources, question=question)  # Validate before any paid review.
    approved = None
    remaining = list(range(1, len(draft.claims) + 1))
    jev = getattr(client, "jev", None)
    if jev is not None and settings.jev_evidence_enabled:
        def run(state, questions):
            result = budgeted_jev(catalog, settings, jev, book_id, phase="jev-citation-review",
                                  state=state, questions=questions)
            if remember_jev is not None:
                remember_jev(result)
            return result.value
        try:
            audit_fn = (jev_tasks.evidence_partial if settings.jev_partial_evidence_enabled
                        else jev_tasks.evidence)
            audit = audit_fn(draftdict, sources,
                             question=question, run=run, settings=settings)
            approved_ids = [c["claim_id"] for c in audit["claim_audits"]]
            remaining = [i for i in range(1, len(draft.claims)+1) if i not in approved_ids]
            if not remaining:
                reviewed = complete_evidence_citations(draftdict, sources, audit)
                return complete_ask_citations(AskDraft.model_validate(reviewed), sources,
                                              known_names=known_names, include_context=False)
            if approved_ids:
                approved = audit
        except JevUnavailable:
            pass  # Existing complete positive-evidence audit remains the bounded fallback.

    batches = evidence_review_batches(
        draftdict, sources, claim_ids=remaining,
        max_output_tokens=getattr(settings, "cost_max_output_tokens_per_call", 4096),
    )

    def audit_batch(claim_ids):
        return budgeted_completion(
            catalog, settings, client, book_id, phase="citation-review", system=ASK_EVIDENCE_SYSTEM,
            user=evidence_prompt(draftdict, sources, question=question, claim_ids=claim_ids),
            tier="cheap", schema=AskEvidenceReview,
        )

    audits = []
    # Two-call waves bound paid concurrency without queuing more work after a failed review.
    # Every started call is joined and accounted for before raising or releasing the route lease.
    with ThreadPoolExecutor(max_workers=min(2, len(batches)), thread_name_prefix="answer-proof") as pool:
        for start in range(0, len(batches), 2):
            requested = batches[start:start + 2]
            futures = [pool.submit(audit_batch, claim_ids) for claim_ids in requested]
            errors, completed = [], []
            for claim_ids, future in zip(requested, futures, strict=True):
                try:
                    audited = future.result()
                except (CostCeilingExceeded, ProviderUnavailable) as exc:
                    errors.append(exc)
                except Exception:
                    # SDK refusal/parse/truncation is unavailable review, never draft criticism.
                    # The budget layer retains its conservative failed-call charge.
                    errors.append(AskEvidenceError(repairable=False))
                else:
                    remember(audited)
                    completed.append((claim_ids, audited.value))
            for claim_ids, audit in completed:
                try:
                    validate_evidence_audit(draftdict, sources, audit, claim_ids=claim_ids)
                except AskEvidenceError as exc:
                    errors.append(exc)
            if errors:
                # A malformed sibling must not be disguised as repairable criticism from a
                # different valid audit. No partial result or new wave can escape this failure.
                raise next((exc for exc in errors if not getattr(exc, "repairable", False)),
                           errors[0]) from None
            for _claim_ids, audit in completed:
                audits.extend(audit["claim_audits"])
    audit_value = {"claim_audits": audits}
    if approved is not None:
        audit_value = merge_evidence_audits(draftdict, sources, approved, audit_value)
    reviewed = AskDraft.model_validate(complete_evidence_citations(
        draftdict, sources, audit_value,
    ))
    # A positive reviewer can mistake an inline chapter label for a speaker attribution.
    # Add only missing named-body context from this same selected pool, within the existing
    # three-citation cap. Name presence is a necessary clue, never truth clearance: all normal
    # deterministic gates and the fresh own-citation NLI still review the complete result.
    return complete_ask_citations(reviewed, sources, known_names=known_names, include_context=False)


@router.post("/ask/stream")
def ask_the_book_stream(
    book_id: str,
    body: AskRequest,
    request: Request,
    catalog=Depends(get_catalog),
    store=Depends(get_store),
    settings=Depends(get_settings),
    client=Depends(get_reader_client),
    worker=Depends(get_worker),
):
    """Server-sent events: one ``sentence`` per checked sentence, then ``done`` or ``error``."""
    if not (settings.reader_conversation_stream and settings.reader_page_context):
        raise HTTPException(404, "streamed replies are not enabled")
    if body.read_frontier is None:
        raise HTTPException(422, "a streamed reply needs the confirmed read position")
    import json
    from fastapi.responses import StreamingResponse
    from app.stream_conversation import stream_read_prefix
    events = stream_read_prefix(book_id, body, catalog, store, settings, client, worker,
                                notes_builder=request.app.state.notes_builder,
                                recollector=request.app.state.recollector)
    return StreamingResponse(
        ("data: " + json.dumps(event, ensure_ascii=False) + "\n\n" for event in events),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@router.get("/conversation")
def stored_conversation(
    book_id: str,
    catalog=Depends(get_catalog),
    settings=Depends(get_settings),
    worker=Depends(get_worker),
):
    """What was said and shown in this book and may be shown again, oldest first: see ``visible_turns``."""
    from app.read_frontier import current_snapshot
    from app.read_map import prefix_map, read_snapshot, visible_turns
    try:
        if settings.reader_read_map:
            point, _chapters, _state, _manifest, book, read_map = read_snapshot(
                book_id, catalog, settings, worker)
        else:
            point = current_snapshot(book_id, catalog, settings, worker)[0]
            book, read_map = catalog.get_book(book_id), prefix_map(point["atom"], point["offset"])
        turns = visible_turns(book_id, book, read_map, catalog, limit=80)
    except LookupError as exc:
        raise HTTPException(404, "unknown book") from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(409, "reading context changed; refresh before continuing") from exc
    return {"read_frontier": point, "turns": [
        {"role": turn["role"], "content": turn["content"], "atom": turn["atom"],
         "reaction": bool(turn["reaction"]), "created_at": turn["created_at"]} for turn in turns]}


@router.delete("/conversation", status_code=204)
def clear_conversation(book_id: str, request: Request, catalog=Depends(get_catalog),
                       settings=Depends(get_settings)):
    if catalog.get_book(book_id) is None:
        raise HTTPException(404, "unknown book")
    if settings.reader_recollections:
        # What the reader said of themselves and nobody has looked at yet is taken first, to be
        # distilled off this request; the conversation is gone when this returns either way.
        request.app.state.recollector.clearing(book_id)
    else:
        catalog.clear_turns(book_id)


@router.post("/ask")
def ask_the_book(
    book_id: str,
    body: AskRequest,
    catalog=Depends(get_catalog),
    store=Depends(get_store),
    settings=Depends(get_settings),
    client=Depends(get_reader_client),
    worker=Depends(get_worker),
    *, request: Request = None,
):
    if body.read_frontier is not None or settings.reader_page_context:
        if not settings.reader_page_context:
            raise HTTPException(422, "page context is not enabled")
        from app.page_conversation import answer_read_prefix
        if body.read_frontier is None:
            from app.read_frontier import ReadBoundary, current_snapshot
            try:
                point = current_snapshot(book_id,catalog,settings,worker)[0]
            except LookupError as exc:
                raise HTTPException(404,"unknown book") from exc
            except (ValueError,RuntimeError) as exc:
                raise HTTPException(409,"read boundary could not be verified against the source") from exc
            if body.bookmark is not None and body.bookmark < point["available_chapter"]:
                from app.read_frontier import snapshot
                state = catalog.get_state(book_id)
                point = snapshot(book_id,{**state,"bookmark":min(state["bookmark"],body.bookmark)},
                    catalog.get_book(book_id),load_manifest(settings.data_dir,book_id),
                    worker.source_chapters(book_id),None)[0]
            body = body.model_copy(update={"read_frontier":ReadBoundary.model_validate(point)})
        return answer_read_prefix(book_id, body, catalog, store, settings, client, worker)
    eff, state, manifest = _effective(book_id, body.bookmark, catalog, store, settings, worker)
    clarification = reaction_clarification(body.question, body.history, eff)
    if clarification:
        if settings.reader_conversation_pipeline:
            clarification = reaction_copy(body.question)[1]
        return {"as_of_chapter": eff, "answer_type": "conversation", "message": clarification,
                "insufficient_evidence": False, "claims": [], "citations": [],
                "cost": _ask_cost_payload([])}
    waiting = _preparation_state(state, body.bookmark, worker, book_id)
    if waiting:
        return {"as_of_chapter": eff, "insufficient_evidence": True, "status": waiting,
                "claims": [], "citations": [], "cost": _ask_cost_payload([])}
    if _recap_request(body.question):
        recap = catch_me_up(book_id, request, body.bookmark, catalog, store, settings, client, worker)
        return {"as_of_chapter": recap["as_of_chapter"], "answer_type": "recap",
                "recap": recap["recap"], "insufficient_evidence": recap["recap"] is None,
                "status": "ready" if recap["recap"] else "paratext_only",
                "claims": [], "citations": [], "cost": None}
    return _ask_the_book(book_id, body, catalog, store, settings, client, worker)


def _conversation_limit_error(exc):
    if "per-call" in str(exc):
        return HTTPException(413, {"code": "request_too_large",
                                   "message": "This request is too large. Try a selected passage."})
    return HTTPException(429, "book cost ceiling reached")


def _ask_the_book(book_id, body, catalog, store, settings, client, worker):
    """Answer only from completed, retrieved passages and return chapter-navigable citations."""
    eff, state, manifest = _effective(book_id, body.bookmark, catalog, store, settings, worker)
    eff = min(eff, state["ingest_progress"])
    question = conversation_question(body.question, body.history, eff)
    retrieval_question = conversation_retrieval_question(body.question, body.history, eff)
    calls = []

    def remember(result, *, provider, model):
        model = getattr(result, "billing_model", None) or model
        calls.append({
            "provider": provider,
            "model": model,
            "input_tokens": int(result.usage.get("in", 0) or 0),
            "output_tokens": int(result.usage.get("out", 0) or 0),
            "usd": float(result.usd),
            "pricing_known": pricing_known(model),
        })

    if eff <= 0:
        return {
            "as_of_chapter": 0,
            "insufficient_evidence": True,
            "claims": [],
            "citations": [],
            "cost": _ask_cost_payload(calls),
        }
    def finish(payload):
        reaction = reaction_copy(body.question)
        if reaction and not payload["insufficient_evidence"] and not settings.reader_conversation_pipeline:
            payload["acknowledgment"] = reaction[0]
        return payload

    with store.book(book_id) as mem:
        pinned = mem.pinned_identity()
    try:
        embedded = budgeted_query_embedding(
            catalog, settings, client, book_id, pinned=pinned, texts=[retrieval_question]
        )
    except CostCeilingExceeded as exc:
        raise _conversation_limit_error(exc) from exc
    embed_identity = client.embed_identity()
    remember(
        embedded,
        provider="stub" if embed_identity.startswith("stub:") else client.embed_provider,
        model=embed_identity,
    )
    atoms = {atom["key"]: atom for atom in manifest["atoms"]}
    try:
        with store.book(book_id) as mem:
            view = mem.view(eff)
            known_names = []
            for character in view.characters():
                known_names.append(character["canonical_name"])
                known_names.extend(alias["surface_form"]
                                   for alias in view.aliases_of(character["entity_id"]))
            sources = retrieve_ask_sources(
                retrieval_question,
                bookmark=eff,
                chapters=view.chapters(),
                read_chapter=view.raw_text,
                semantic_hits=view.search(embedded.value[0], k=6),
                atoms=atoms,
                candidate_pool=client.provider != "stub",
                known_names=known_names,
            )
    except AskRetrievalLimit as exc:
        raise HTTPException(
            413, "completed text is too large to search at once; choose an earlier chapter"
        ) from exc
    if sources and client.provider != "stub" and settings.reader_conversation_pipeline:
        from app.conversation_pipeline import (
            DRAFT_SYSTEM, ConversationDraft, answer_conversation, compact_candidates,
        )
        # Fuse selection and drafting only when EVERY candidate fits. Never silently truncate
        # the pool, raise a ceiling, or replace the original all-candidate selection fallback.
        compacted = compact_candidates(sources)
        if _recap_input_fits(settings, client, DRAFT_SYSTEM,
                             ask_prompt(question, compacted, frontier=eff), schema=ConversationDraft):
            reviewer = copy(client)
            reviewer.conversation_review_effort = settings.reader_conversation_review_effort

            def conversation_complete(system, user, *, schema, phase, tier="cheap"):
                result = budgeted_completion(catalog, settings, reviewer, book_id,
                    phase=phase, system=system, user=user, tier=tier, schema=schema)
                remember(result, provider=reviewer.provider, model=reviewer.billing_model(tier))
                return result.value

            def conversation_score(text):
                with store.book(book_id) as mem:
                    return score_recap(mem, eff, text, read_text=read_text_upto(mem, eff))

            try:
                draft, used, clarification = answer_conversation(question, compacted, frontier=eff,
                    known_names=known_names, complete=conversation_complete,
                    score=conversation_score, max_output_tokens=settings.cost_max_output_tokens_per_call)
            except CostCeilingExceeded as exc:
                raise _conversation_limit_error(exc) from exc
            except (AskSafetyError, AskEvidenceError, ValueError) as exc:
                raise HTTPException(502, "answer could not be cleared against the pages you have read") from exc
            if clarification:
                return {"as_of_chapter": eff, "answer_type": "conversation", "message": clarification,
                        "insufficient_evidence": False, "claims": [], "citations": [],
                        "cost": _ask_cost_payload(calls)}
            return finish({"as_of_chapter": eff,
                "insufficient_evidence": draft.insufficient_evidence,
                "claims": [claim.model_dump(mode="json") for claim in draft.claims],
                "citations": [_public_ask_source(source) for source in used],
                "cost": _ask_cost_payload(calls)})
    if sources and client.provider != "stub":
        candidates = sources
        try:
            selected = None
            jev = getattr(client, "jev", None)
            if jev is not None and settings.jev_selection_enabled:
                def select_run(state, questions):
                    result = budgeted_jev(catalog, settings, jev, book_id,
                                          phase="jev-source-selection", state=state,
                                          questions=questions)
                    remember(result, provider="typesafe", model=jev.model)
                    return result.value
                try:
                    selected = jev_tasks.select(question, sources, run=select_run,
                                                settings=settings, frontier=eff)
                except JevUnavailable:
                    pass
            if selected is None:
                from app.cost.limits import completion_input_overhead
                overhead = completion_input_overhead(client)
                if client.provider == "openai-compatible" and not getattr(client, "_is_native_openai", False):
                    overhead += settings.cost_max_output_tokens_per_call + 512
                def fits_selection(prompt):
                    return (estimate_input_tokens(SOURCE_SELECTION_SYSTEM, prompt, SourceSelection)
                            + overhead <= settings.cost_max_input_tokens_per_call)
                def complete_selection(prompt):
                    selection = budgeted_completion(
                        catalog, settings, client, book_id, phase="source-selection",
                        system=SOURCE_SELECTION_SYSTEM, user=prompt, tier="cheap", schema=SourceSelection,
                    )
                    remember(selection, provider=client.provider, model=client.billing_model("cheap"))
                    return selection.value
                selected = select_sources_bounded(question, sources, fits=fits_selection,
                                                  complete=complete_selection, frontier=eff)
            sources = selected
            sources = complete_ask_context(retrieval_question, sources, candidates, known_names=known_names,
                                           allow_named_fallback=True)
        except CostCeilingExceeded as exc:
            raise _conversation_limit_error(exc) from exc
        except (SourceSelectionError, ValueError) as exc:
            raise HTTPException(502, "evidence selection could not be cleared") from exc
    if not sources:
        return {
            "as_of_chapter": eff,
            "insufficient_evidence": True,
            "claims": [],
            "citations": [],
            "cost": _ask_cost_payload(calls),
        }

    prompt = ask_prompt(question, sources, frontier=eff)
    repair_feedback = []
    for _attempt in range(2):
        try:
            generated = budgeted_completion(
                catalog,
                settings,
                client,
                book_id,
                phase="synthesis",
                system=ASK_SYSTEM,
                user=ask_repair_prompt(prompt, repair_feedback),
                tier="large",
                schema=AskDraft,
            )
        except CostCeilingExceeded as exc:
            raise _conversation_limit_error(exc) from exc
        remember(generated, provider=client.provider, model=client.billing_model("large"))
        try:
            completed = _complete_citation_evidence(
                generated.value, sources, catalog=catalog, settings=settings, client=client,
                book_id=book_id, known_names=known_names,
                question=question,
                remember=lambda result: remember(result, provider=client.provider,
                                                  model=client.billing_model("cheap")),
                remember_jev=lambda result: remember(result, provider="typesafe",
                                                      model=client.jev.model),
            )
            # The real-provider helper above completed every positive evidence audit. Lexical
            # overlap is not entailment; supported paraphrases still need the fresh judge below.
            draft = validate_ask_draft(completed, sources, known_names=known_names,
                                       defer_lexical_to_judge=client.provider != "stub")
        except CostCeilingExceeded as exc:
            raise _conversation_limit_error(exc) from exc
        except AskEvidenceError as exc:
            if not exc.repairable:
                raise HTTPException(502, "answer evidence review could not be cleared") from exc
            repair_feedback = list(exc.review_feedback)
            continue
        except (AskSafetyError, ValueError) as exc:
            repair_feedback = [
                "The draft was not traceable to its own cited passages or had an invalid citation "
                "shape. Use only directly supported factual sentences, omit standalone Yes/No "
                "acknowledgments, and preserve source wording for identities and qualifications."
            ]
            if isinstance(exc, AskSafetyError):
                repair_feedback.extend(exc.review_feedback)
            continue
        if draft.insufficient_evidence:
            return {
                "as_of_chapter": eff,
                "insufficient_evidence": True,
                "claims": [],
                "citations": [],
                "cost": _ask_cost_payload(calls),
            }

        answer = draft_text(draft)
        with store.book(book_id) as mem:
            score = score_recap(mem, eff, answer, read_text=read_text_upto(mem, eff))
        if (
            score["future_entity_leaks"]
            or score["prolepsis_hits"]
            or score["unsupported_event_bindings"]
        ):
            repair_feedback = [
                "The draft failed the completed-source safety checks. Remove unsupported "
                "developments or bindings and state only what its own cited passages establish."
            ]
            if score["prolepsis_hits"]:
                repair_feedback.append(
                    "Future-modal wording is blocked even inside indirect speech. Report only "
                    "the completed statement in past tense with explicit named speakers and "
                    "subjects: 'Mara instructed Lio to marry', not 'Lio will marry'. Do not "
                    "change the source's certainty or convert its prediction into an event.")
            continue

        used = cited_sources(draft, sources)

        def judge_complete(system, user, tier="cheap", schema=None):
            result = budgeted_completion(
                catalog,
                settings,
                client,
                book_id,
                phase="judge",
                system=system,
                user=user,
                tier=tier,
                schema=schema,
            )
            remember(result, provider=client.provider, model=client.billing_model(tier))
            return result.value, result.usage

        try:
            verdict, _usage = judge_ask_draft(
                client,
                draft,
                used,
                tier="cheap",
                complete=judge_complete,
                question=question,
                frontier=eff,
                scene_sources=[s for s in candidates if s["ordinal"] == eff]
                    if client.provider != "stub" else (),
                coverage_sources=sources if client.provider != "stub" else (),
            )
        except CostCeilingExceeded as exc:
            raise _conversation_limit_error(exc) from exc
        except JudgeUnavailable:
            continue
        if verdict["references_future"] or verdict["unsupported_claims"]:
            repair_feedback = verdict["unsupported_claims"] or [
                "The draft asserted a development not established by its cited passages."
            ]
            continue
        return finish({
            "as_of_chapter": eff,
            "insufficient_evidence": False,
            "claims": [claim.model_dump(mode="json") for claim in draft.claims],
            "citations": [_public_ask_source(source) for source in used],
            "cost": _ask_cost_payload(calls),
        })
    raise HTTPException(502, "answer could not be cleared against the pages you have read")


@router.post("/selection-action")
def selection_action(
    book_id: str,
    body: SelectionActionRequest,
    catalog=Depends(get_catalog),
    store=Depends(get_store),
    settings=Depends(get_settings),
    client=Depends(get_reader_client),
    worker=Depends(get_worker),
):
    """Explain, define, or translate only the reader-supplied visible selection."""
    from app.reading_assist import SELECTION_CLARIFICATIONS
    if settings.reader_page_context:
        from app.read_frontier import current_snapshot
        from app.retrieval_passages import normalize_source
        try:
            if settings.reader_read_map:
                from app.read_map import read_snapshot
                point,_,_state,manifest,*_ = read_snapshot(book_id,catalog,settings,worker)
            else:
                point,_,_state,manifest = current_snapshot(book_id,catalog,settings,worker)
        except LookupError as exc:
            raise HTTPException(404,"unknown book") from exc
        except (ValueError,RuntimeError) as exc:
            raise HTTPException(409,"read boundary could not be verified against the source") from exc
        eff=point["available_chapter"]
        if settings.reader_read_map:
            # The reader selected this on their screen, wherever in the book they are. It is
            # still matched against its chapter below, and only the selection itself is used.
            if not 1 <= body.atom <= len(manifest["atoms"]):
                raise HTTPException(422,"selection is outside the book")
            eff=max(eff,body.atom-1)
        elif body.atom>min(len(manifest["atoms"]),max(point["atom"],_state["bookmark"])+1):
            raise HTTPException(422,"selection is outside the current reading position")
        original=worker.source_chapters(book_id)[body.atom-1]["text"]
        if normalize_source(body.text) not in normalize_source(original):
            raise HTTPException(422,"selection could not be matched to its source")
    else:
        eff, _state, manifest = _effective(book_id, None, catalog, store, settings, worker)
    if body.atom > min(len(manifest["atoms"]), eff + 1):
        raise HTTPException(422, "selection is outside the current reading position")
    atom = manifest["atoms"][body.atom - 1]
    source = {
        "id": 1,
        "ordinal": body.atom,
        "chapter_key": atom["key"],
        "href": atom.get("href", ""),
        "title": atom.get("title", "") or f"Chapter {body.atom}",
        "text": body.text,
        "cfi": body.cfi,
    }
    calls = []

    def remember(result, *, model, provider=None):
        calls.append({
            "provider": provider or client.provider,
            "model": model,
            "input_tokens": int(result.usage.get("in", 0) or 0),
            "output_tokens": int(result.usage.get("out", 0) or 0),
            "usd": float(result.usd),
            "pricing_known": pricing_known(model),
        })

    def run_selection(state,questions):
        result=budgeted_jev(catalog,settings,client.jev,book_id,phase="selection-jev",
            state=state,questions=questions)
        remember(result,model=client.jev.model,provider="typesafe")
        return result.value

    prompt = selection_prompt(body)
    if settings.reader_conversation_pipeline:
        client = copy(client)
        client.conversation_review_effort = settings.reader_conversation_review_effort
        client.conversation_draft_effort = settings.reader_conversation_draft_effort
    for _attempt in range(2):
        try:
            generated = budgeted_completion(
                catalog,
                settings,
                client,
                book_id,
                phase="synthesis",
                system=SELECTION_SYSTEM,
                user=prompt,
                tier="large",
                schema=SelectionDraft,
            )
        except CostCeilingExceeded as exc:
            raise HTTPException(429, "book cost ceiling reached") from exc
        except ValueError:
            # The SDK validates structured output before returning. Malformed output must
            # follow the bounded failure path rather than escape as an unhandled HTTP 500.
            continue
        remember(generated, model=client._model_for("large"))
        try:
            draft = SelectionDraft.model_validate(generated.value)
        except ValueError:
            continue
        if draft.insufficient_evidence:
            return {
                "action": body.action,
                "as_of_chapter": eff,
                "insufficient_evidence": True,
                "text": None,
                "citation": None,
                "message": (SELECTION_CLARIFICATIONS.get(draft.clarification)
                            if body.action == "explain" else None),
                "cost": _ask_cost_payload(calls),
            }

        def judge_complete(system, user, tier="cheap", schema=None):
            result = budgeted_completion(
                catalog,
                settings,
                client,
                book_id,
                phase="judge",
                system=system,
                user=user,
                tier=tier,
                schema=schema,
            )
            remember(result, model=client._model_for(tier))
            return result.value, result.usage

        try:
            fast_clear=False
            if (body.action=="translate" and draft.text != body.text and settings.reader_conversation_jev
                    and getattr(client,"jev",None)):
                from app.selection_jev import review_selection
                try:
                    fast_clear=review_selection(body,draft,run=run_selection)
                except JevUnavailable:
                    pass  # Typed provider uncertainty/failure never clears the draft.
            if fast_clear:
                verdict={"references_future":False,"unsupported_claims":[],"missing_details":[]}
            elif settings.reader_conversation_pipeline and client.provider != "stub":
                verdict, _usage = judge_complete(SELECTION_REVIEW_SYSTEM,
                    selection_review_prompt(body, draft), schema=SelectionVerdict)
                verdict = SelectionVerdict.model_validate(verdict, strict=True).model_dump(mode="json")
            else:
                verdict, _usage = judge_recap(
                    client, draft.text or "", source_facts([source]), tier="cheap", complete=judge_complete)
        except CostCeilingExceeded as exc:
            raise HTTPException(429, "book cost ceiling reached") from exc
        except (JudgeUnavailable, ValueError):
            continue
        if (verdict["references_future"] or verdict["unsupported_claims"]
                or verdict.get("missing_details")):
            continue
        citation = _public_ask_source(source)
        citation["cfi"] = body.cfi
        return {
            "action": body.action,
            "as_of_chapter": eff,
            "insufficient_evidence": False,
            "text": draft.text,
            "acknowledgment": (reaction_copy(body.question)[0]
                               if not settings.reader_conversation_pipeline
                               and body.question and reaction_copy(body.question) else None),
            "citation": citation,
            "cost": _ask_cost_payload(calls),
        }
    raise HTTPException(502, "selection help could not be cleared against the selected passage")


@router.post("/chapter-closeout")
def chapter_closeout(
    book_id: str,
    body: ChapterCloseoutRequest,
    catalog=Depends(get_catalog),
    store=Depends(get_store),
    settings=Depends(get_settings),
    client=Depends(get_client),
    worker=Depends(get_worker),
):
    """Generate cited takeaways from exactly one completed, ingested chapter."""
    eff, state, manifest = _effective(book_id, body.chapter, catalog, store, settings, worker)
    if eff != body.chapter or state["ingest_progress"] < body.chapter:
        raise HTTPException(409, "chapter is not completed and remembered yet")
    atom = manifest["atoms"][body.chapter - 1]
    with store.book(book_id) as mem:
        raw = mem.view(body.chapter).raw_text(atom["key"])
    sources = chapter_passages(
        raw or "",
        ordinal=body.chapter,
        chapter_key=atom["key"],
        href=atom.get("href", ""),
        title=atom.get("title", "") or f"Chapter {body.chapter}",
    )
    calls = []

    def remember(result, *, model, provider=None):
        calls.append({
            "provider": provider or client.provider,
            "model": model,
            "input_tokens": int(result.usage.get("in", 0) or 0),
            "output_tokens": int(result.usage.get("out", 0) or 0),
            "usd": float(result.usd),
            "pricing_known": pricing_known(model),
        })

    if not sources:
        return {
            "chapter": body.chapter,
            "as_of_chapter": eff,
            "insufficient_evidence": True,
            "claims": [],
            "citations": [],
            "cost": _ask_cost_payload(calls),
        }
    prompt = chapter_closeout_prompt(body.chapter, sources)
    repair_feedback = []
    for _attempt in range(2):
        try:
            generated = budgeted_completion(
                catalog,
                settings,
                client,
                book_id,
                phase="synthesis",
                system=CLOSEOUT_SYSTEM,
                user=ask_repair_prompt(prompt, repair_feedback),
                tier="large",
                schema=AskDraft,
            )
        except CostCeilingExceeded as exc:
            raise HTTPException(429, "book cost ceiling reached") from exc
        remember(generated, model=client._model_for("large"))
        try:
            completed = _complete_citation_evidence(
                generated.value, sources, catalog=catalog, settings=settings, client=client,
                book_id=book_id,
                remember=lambda result: remember(result, model=client._model_for("cheap")),
                remember_jev=lambda result: remember(result, provider="typesafe",
                                                      model=client.jev.model),
            )
            draft = validate_ask_draft(completed, sources,
                                       defer_lexical_to_judge=client.provider != "stub")
        except CostCeilingExceeded as exc:
            raise HTTPException(429, "book cost ceiling reached") from exc
        except AskEvidenceError as exc:
            if not exc.repairable:
                raise HTTPException(502, "answer evidence review could not be cleared") from exc
            repair_feedback = list(exc.review_feedback)
            continue
        except (AskSafetyError, ValueError) as exc:
            repair_feedback = ["Use only complete factual claims supported by their own cited passages."]
            if isinstance(exc, AskSafetyError):
                repair_feedback.extend(exc.review_feedback)
            continue
        if draft.insufficient_evidence:
            return {
                "chapter": body.chapter,
                "as_of_chapter": eff,
                "insufficient_evidence": True,
                "claims": [],
                "citations": [],
                "cost": _ask_cost_payload(calls),
            }
        answer = draft_text(draft)
        with store.book(book_id) as mem:
            score = score_recap(mem, eff, answer, read_text=read_text_upto(mem, eff))
        if (
            score["future_entity_leaks"]
            or score["prolepsis_hits"]
            or score["unsupported_event_bindings"]
        ):
            continue
        used = cited_sources(draft, sources)

        def judge_complete(system, user, tier="cheap", schema=None):
            result = budgeted_completion(
                catalog,
                settings,
                client,
                book_id,
                phase="judge",
                system=system,
                user=user,
                tier=tier,
                schema=schema,
            )
            remember(result, model=client._model_for(tier))
            return result.value, result.usage

        try:
            verdict, _usage = judge_ask_draft(
                client,
                draft,
                used,
                tier="cheap",
                complete=judge_complete,
            )
        except CostCeilingExceeded as exc:
            raise HTTPException(429, "book cost ceiling reached") from exc
        except JudgeUnavailable:
            continue
        if verdict["references_future"] or verdict["unsupported_claims"]:
            repair_feedback = verdict["unsupported_claims"] or [
                "The draft asserted a development not established by its cited passages."
            ]
            continue
        return {
            "chapter": body.chapter,
            "as_of_chapter": eff,
            "insufficient_evidence": False,
            "claims": [claim.model_dump(mode="json") for claim in draft.claims],
            "citations": [_public_ask_source(source) for source in used],
            "cost": _ask_cost_payload(calls),
        }
    raise HTTPException(502, "chapter closeout could not be cleared against the completed chapter")


def _budgeted_runner(catalog, settings, client, book_id, phase):
    def complete(system, user, tier="cheap", schema=None):
        result = budgeted_completion(
            catalog, settings, client, book_id, phase=phase, system=system, user=user,
            tier=tier, schema=schema,
        )
        return result.value, result.usage
    return complete


def _review_recap(client, text, facts, *, catalog, settings, book_id, phase="judge", tier="large"):
    jev = getattr(client, "jev", None)
    if jev is not None and settings.jev_recap_enabled:
        def run(state, questions):
            return budgeted_jev(catalog, settings, jev, book_id, phase=f"jev-{phase}",
                                 state=state, questions=questions).value
        try:
            return jev_tasks.recap(text, facts, run=run, settings=settings), {}
        except JevUnavailable:
            pass
    runner = _budgeted_runner(catalog, settings, client, book_id, phase)
    def complete(system, user, **kwargs):
        if jev is not None:
            system += jev_tasks.SUMMARY_GROUNDING_RULES
        return runner(system, user, **kwargs)
    return judge_recap(client, text, facts, tier=tier, complete=complete)


def _make_now(client, store, catalog, settings, book_id, eff, delta, facts, *, book_type="novel",
              content_language="und"):
    """LIT-29 'right now' one-liner (option b): its OWN small (cheap-tier) model call, spoiler-gated by
    the deterministic gate AND the judge, cost-recorded, fail-SAFE to None. It NEVER blocks the recap —
    a failed / gate-rejected one-liner degrades to absent (spoiler-safety is fail-CLOSED, the one-liner
    is fail-safe-to-absent). LLM calls run OUTSIDE the per-book lock (D-A3); only the deterministic gate
    runs under it. Returns the one-liner text or None."""
    if not delta["chapter_summaries"]:
        return None                                        # nothing newly completed to describe
    if not _recap_input_fits(settings, client, JUDGE_SYSTEM,
                             judge_prompt("", facts), schema=JudgeVerdict):
        return None                                        # cannot review even the shortest output
    try:
        now_text, _usage = _budgeted_runner(
            catalog, settings, client, book_id, "now"
        )(now_system_for(book_type, content_language) + (
            jev_tasks.SUMMARY_GROUNDING_RULES if getattr(client, "jev", None) is not None else ""),
          now_prompt(eff, delta, book_type=book_type), tier="cheap")
    except Exception:
        return None                                        # provider error -> no one-liner (fail-safe)
    with store.book(book_id) as mem:                       # deterministic gate under the lock (no IO)
        try:
            assert_recap_safe(mem, eff, now_text, read_text=read_text_upto(mem, eff))
        except SpoilerGateError:
            return None                                    # spoiler-bearing details stay server-side
    try:                                                   # the judge backstop, OUTSIDE the lock
        verdict, _judge_usage = _review_recap(
            client, now_text, facts, tier="cheap", catalog=catalog, settings=settings,
            book_id=book_id, phase="now-judge",
        )
    except (JudgeUnavailable, CostCeilingExceeded):
        return None
    if verdict["references_future"] or (getattr(client, "jev", None) is not None
                                        and verdict["unsupported_claims"]):
        return None
    return now_text.strip() or None


def _recap_input_fits(settings, client, system, user, *, schema=None):
    """Preflight only input shape; the budgeted runner still atomically admits book-level spend."""
    from app.cost.limits import completion_input_overhead
    peak_input = estimate_input_tokens(system, user, schema) + completion_input_overhead(client)
    if (schema is not None and client.provider == "openai-compatible"
            and not getattr(client, "_is_native_openai", False)):
        peak_input += settings.cost_max_output_tokens_per_call + 512
    return peak_input <= settings.cost_max_input_tokens_per_call


def _cumulative_recap_facts(facts):
    """Exactly the evidence synth_prompt supplies, with no additional event history in review."""
    return {"characters": facts["characters"], "chapter_summaries": facts["chapter_summaries"],
            "events": facts["events"][:20]}


@router.get("/catch-me-up")
def catch_me_up(book_id: str, request: Request, bookmark: int | None = _BM,
                catalog=Depends(get_catalog), store=Depends(get_store),
                settings=Depends(get_settings), client=Depends(get_reader_client), worker=Depends(get_worker)):
    eff, st, manifest = _effective(book_id, bookmark, catalog, store, settings, worker)
    # the recap can only describe INGESTED chapters: min with the ingest high-water (LIT-12's honest
    # lag — the pending/uningested tail is not recapped), reported as the true as_of.
    eff = min(eff, st["ingest_progress"])
    if eff == 0:
        return {"recap": None, "now": None, "as_of_chapter": 0, "cast_size": 0,
                "open_threads": 0, "cast": [], "cached": False}

    recaps = request.app.state.recaps
    with store.book(book_id) as mem:
        if not mem.view(eff).chapters():
            return {"recap": None, "now": None, "as_of_chapter": eff, "cast_size": 0,
                    "open_threads": 0, "cast": [], "cached": False, "status": "paratext_only"}
        cmu = mem.view(eff).catch_me_up()
        facts = supplied_facts(mem, eff)
        snap = validity_snapshot(mem, eff)
        book_type = mem.book_profile()["book_type"]
        content_language = mem.content_language()
    synth_model = client.extractor_version("large")
    use_primary = client.provider != "stub"
    if use_primary:
        review_endpoint = hashlib.sha256(str(getattr(client, "base_url", "")).encode()).hexdigest()
        synth_model += f":{PRIMARY_RECAP_VERSION}:{client.extractor_version('cheap')}:{review_endpoint}"
    fast_draft = (use_primary and getattr(client, "_is_native_openai", False)
                  and client._model_for("large") == "gpt-6-luna")
    synthesis_client = client
    if fast_draft:
        # Drafting four sentences does not need a long reasoning pass. The positive source
        # audit and separate citation judge below retain the original client and reasoning.
        synthesis_client = copy(client)
        synthesis_client.recap_generation_profile = True
        synth_model += ":recap-draft-none-v1"
    if getattr(client, "jev", None) is not None:
        synth_model += (f":jev-hybrid-recap-v2:{settings.jev_recap_enabled}:{client.jev.model}:"
                        f"{settings.jev_min_probability}:{settings.jev_min_confidence}")
    incarnation = catalog.get_book(book_id)["incarnation"]
    key = (book_id, incarnation, cache_key(
        book_id,
        eff,
        snap,
        synth_model=synth_model,
        recap_prompt_version=_recap_prompt_version(book_type, content_language),
        atom_set_version=manifest["atom_set_version"],
    ))
    cached = recaps.get(key)
    if cached is not None and cached.get("as_of_chapter") == eff:
        return {**cached, "cached": True}

    # SINGLE-FLIGHT per cache key (review pass-1): N concurrent cold requests must pay for ONE
    # synthesis. The per-key lock serializes the miss path; losers re-check the cache after acquiring.
    # A double-rejected key is NEGATIVELY cached for RECAP_FAILURE_TTL_S (review pass-2 MEDIUM: the
    # rejection path otherwise re-paid 2 calls per waiter in a serial convoy).
    def _recently_failed():
        return recaps.failure_recent(key, ttl=RECAP_FAILURE_TTL_S)

    if _recently_failed():
        raise HTTPException(502, "recap generation recently failed the spoiler gate; retry shortly")
    with recaps.flight(key):
        cached = recaps.get(key)
        if cached is not None and cached.get("as_of_chapter") == eff:
            return {**cached, "cached": True}
        if _recently_failed():                    # the flight ahead of us just failed — don't re-pay
            raise HTTPException(502, "recap generation recently failed the spoiler gate; retry shortly")

        # LIT-29 evolve inputs, read under the lock: the DELTA (facts first revealed at eff), the
        # bookmark-bounded CAST (clickable names), and recap(eff-1) from the cache — "the story as last
        # told". recap(N-1) is keyed on snapshot(eff-1): if any <=eff-1 fact changed, its key misses and
        # we fall back to a cumulative synthesis (correct — the prior recap would be stale). The chain
        # warms as the reader progresses (each bookmark finds its predecessor cached).
        with store.book(book_id) as mem:
            delta = delta_facts(mem, eff)
            cast = _visible_cast(mem, eff)
            snap_prev = validity_snapshot(mem, eff - 1) if eff >= 2 else None
            try:
                primary = primary_snapshot(mem, eff) if use_primary else None
            except AskRetrievalLimit as exc:
                raise HTTPException(413, "completed recap text exceeds the search budget") from exc
        prior_recap = None
        if snap_prev is not None:
            prior_key = (book_id, incarnation, cache_key(
                book_id,
                eff - 1,
                snap_prev,
                synth_model=synth_model,
                recap_prompt_version=_recap_prompt_version(book_type, content_language),
                atom_set_version=manifest["atom_set_version"],
            ))
            prior_payload = recaps.get(prior_key) or {}
            prior_recap = (prior_payload.get("recap")
                           if prior_payload.get("as_of_chapter") == eff - 1 else None)

        system = flowing_system_for(book_type, content_language)
        if getattr(client, "jev", None) is not None:
            system += jev_tasks.SUMMARY_GROUNDING_RULES
        cumulative_facts = _cumulative_recap_facts(facts)
        full_review_fits = not use_primary and _recap_input_fits(
            settings, client, JUDGE_SYSTEM, judge_prompt("", facts), schema=JudgeVerdict,
        )
        recap_final = None
        primary_correction, primary_now = "", None
        atoms = {atom["key"]: atom for atom in manifest["atoms"]}
        source_prompt = None
        if use_primary:
            try:
                source_prompt = primary_generation_prompt(primary, eff, atoms,
                    byte_limit=min(18000, settings.cost_max_input_tokens_per_call//2))
            except JudgeUnavailable as exc:
                raise HTTPException(502, "completed primary evidence could not be prepared") from exc
            if not _recap_input_fits(settings, client, ASK_EVIDENCE_SYSTEM,
                                     primary_review_preflight(source_prompt), schema=AskEvidenceReview):
                raise HTTPException(429, "recap proof exceeds the per-call input ceiling")
        for attempt in range(2):
            # attempt 0: EVOLVE from recap(N-1) + the delta (non-repetitive). If it fails the gate,
            # attempt 1 falls back to the cumulative synthesis — proven grounded, so the reader still
            # gets a recap rather than a 502. The evolve is the optimization; the cumulative is the
            # safety net. BOTH run under FLOWING_SYSTEM (the anti-foreshadow contract stays intact).
            if prior_recap and attempt == 0 and full_review_fits:
                evolved = evolve_prompt(eff, prior_recap, delta, book_type=book_type)
            else:
                evolved = None
            if evolved is not None and _recap_input_fits(settings, client, system, evolved):
                prompt, review_facts = evolved, facts
            else:
                prompt = synth_prompt(eff, cumulative_facts, book_type=book_type)
                review_facts = cumulative_facts
            if use_primary:
                prompt = source_prompt
            prompt += primary_correction
            if use_primary:
                prompt += ("\nReturn at most six complete sentences, each at most 800 characters. "
                           "Use explicit named subjects so every sentence can be reviewed separately. "
                           "Every fact must also be verifiable against the completed primary text; "
                           "derived chapter memory may contain old errors.")
            if (not _recap_input_fits(settings, client, system, prompt)
                    or not use_primary and not _recap_input_fits(settings, client, JUDGE_SYSTEM,
                                            judge_prompt("", review_facts), schema=JudgeVerdict)):
                raise HTTPException(429, "recap evidence exceeds the per-call input ceiling")
            try:
                recap, _usage = _budgeted_runner(
                    catalog, settings, synthesis_client, book_id, "synthesis"
                )(system, prompt, tier="large")
            except CostCeilingExceeded as exc:
                raise HTTPException(429, "book cost ceiling reached") from exc
            with store.book(book_id) as mem:
                try:
                    if use_primary:
                        score = score_recap(mem, eff, recap, read_text=read_text_upto(mem, eff))
                        if score["future_entity_leaks"] or score["prolepsis_hits"]:
                            _log.warning(
                                "Recap temporal gate withheld chapter %s, attempt %s (names=%s, language=%s)",
                                eff, attempt + 1, len(score["future_entity_leaks"]), len(score["prolepsis_hits"]),
                            )
                            continue
                        # Legacy-memory word overlap and event-role signatures cannot validate
                        # or veto primary facts: extraction may omit a source-supported action.
                        # Complete positive support is mandatory in the raw-source audits below,
                        # followed by the independent own-citation judge (the same Ask policy).
                    else:
                        assert_recap_safe(mem, eff, recap, read_text=read_text_upto(mem, eff))
                except SpoilerGateError:
                    # spoiler-bearing details stay server-side (D-A9 pass-2) — never the response
                    continue
            if use_primary:
                try:
                    primary_now, _sources = review_primary_recap(
                        recap, primary, eff, atoms, catalog=catalog, settings=settings,
                        client=client, book_id=book_id, generation_prompt=source_prompt,
                    )
                except CostCeilingExceeded as exc:
                    raise HTTPException(429, "book cost ceiling reached") from exc
                except (JudgeUnavailable, AskEvidenceError, ValueError) as exc:
                    _log.warning("Recap review withheld chapter %s, attempt %s (%s)",
                                 eff, attempt + 1, type(exc).__name__)
                    primary_correction = primary_repair_context(recap, primary, eff, atoms,
                        feedback=getattr(exc, "review_feedback", ()), generation_prompt=source_prompt)
                    continue
                recap_final = recap
                break
            # THE LLM-JUDGE BACKSTOP (LIT-14, ADR 0004 Vector 3), OUTSIDE the lock (D-A3): the semantic
            # net for a paraphrased future EVENT the deterministic gate can't see (past tense, no future
            # name, no modal). references_future is a HARD reject; a judge outage fails CLOSED (no
            # verdict = unsafe) — both regenerate, then a generic 502. Soft unsupported_claims stay
            # server-side (reader-data-only). The judge sees only the bookmark-bounded facts + the recap.
            if not _recap_input_fits(settings, client, JUDGE_SYSTEM,
                                     judge_prompt(recap, review_facts), schema=JudgeVerdict):
                raise HTTPException(429, "recap review exceeds the per-call input ceiling")
            try:
                verdict, _judge_usage = _review_recap(
                    client, recap, review_facts, catalog=catalog, settings=settings,
                    book_id=book_id,
                )
            except CostCeilingExceeded as exc:
                raise HTTPException(429, "book cost ceiling reached") from exc
            except JudgeUnavailable:
                continue
            if verdict["references_future"] or (getattr(client, "jev", None) is not None
                                                and verdict["unsupported_claims"]):
                continue
            recap_final = recap
            break

        if recap_final is not None:
            recaps.clear_failed(key)
            # the 'right now' one-liner — best-effort, fail-safe to absent (never blocks a cleared recap)
            now_text = primary_now if use_primary else _make_now(
                client, store, catalog, settings, book_id, eff, delta, facts,
                book_type=book_type, content_language=content_language,
            )
            # READER DATA ONLY (review pass-1 HIGH): future_theme_hits / grounded_rate are gate
            # diagnostics computed from the unfiltered audit hatch — a scrubbed-back client must never
            # receive a FUTURE theme label; they stay out of the payload AND the cache.
            payload = {"recap": recap_final, "now": now_text, "as_of_chapter": eff,
                       "cast_size": cmu["cast_size"], "open_threads": cmu["open_threads"],
                       "cast": cast, "cached": False}
            recaps.set(key, payload)
            return dict(payload)                  # never hand out the cached dict itself
        recaps.mark_failed(key)
    # fail CLOSED with a GENERIC error: no recap text, no names, no details (D-A9 + pass-2 finding)
    raise HTTPException(502, "recap generation failed the spoiler gate; try again later")
