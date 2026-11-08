"""Exact, epoch-bound primary-text prefixes. No model receives the unread source suffix."""
import hashlib
import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from app.content_scope import is_content
from app.ingest.extraction.chapter_text import content_hash_of
from app.ingest.manifest import load_manifest
from app.retrieval_passages import normalize_source


class ReadAnchor(BaseModel):
    model_config = ConfigDict(extra="forbid")
    atom: StrictInt = Field(ge=1, le=2**31 - 1)
    quote: str = Field(min_length=1, max_length=384)
    prefix: str = Field(max_length=192)
    cfi: str = Field(min_length=1, max_length=4096)
    position_epoch: StrictInt = Field(ge=0, le=2**63 - 1)

    @model_validator(mode="after")
    def exact_prefix(self):
        if self.quote != normalize_source(self.quote) or self.prefix != normalize_source(self.prefix):
            raise ValueError("anchor must use canonical whitespace")
        if not self.quote.startswith(self.prefix):
            raise ValueError("read prefix is outside its source quote")
        return self


class ReadBoundary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-f0-9]{64}$")
    atom: StrictInt = Field(ge=0, le=2**31 - 1)
    offset: StrictInt = Field(ge=0, le=2**31 - 1)
    position_epoch: StrictInt = Field(ge=0, le=2**63 - 1)
    available_chapter: StrictInt = Field(ge=0, le=2**31 - 1)


def anchor_offset(text, anchor):
    """Resolve one unique exact body occurrence. Ambiguity never chooses a later occurrence."""
    text = normalize_source(text)
    start = text.find(anchor.quote)
    if start < 0 or text.find(anchor.quote, start + 1) >= 0:
        raise ValueError("the page anchor is missing or ambiguous")
    return start + len(anchor.prefix)


def source_state(book_id, catalog, settings, worker):
    state, book = catalog.get_state(book_id), catalog.get_book(book_id)
    if state is None or book is None:
        raise LookupError("unknown book")
    manifest = load_manifest(settings.data_dir, book_id)
    # A cached parse must never keep authorizing text after its source has changed on disk.
    try:
        with (Path(settings.data_dir) / "books" / book_id / "source.epub").open("rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
    except OSError as exc:
        raise ValueError("source is unavailable") from exc
    if book.get("file_hash") and digest != book["file_hash"]:
        raise ValueError("source no longer matches its import")
    chapters = worker.source_chapters(book_id)
    return state, book, manifest, chapters


def snapshot(book_id, state, book, manifest, chapters, saved):
    by_atom = {ch["ordinal"]: ch for ch in chapters}
    completed = min(state["bookmark"], len(chapters))
    atom, offset = (completed, len(normalize_source(by_atom[completed]["text"]))) if completed else (0, 0)
    if saved is not None:
        if (saved["position_epoch"] != state["position_epoch"] or saved["atom"] not in by_atom
                or content_hash_of(by_atom[saved["atom"]]["text"]) != saved["source_hash"]):
            raise ValueError("saved read boundary no longer matches the source")
        text = normalize_source(by_atom[saved["atom"]]["text"])
        if not 0 <= saved["source_offset"] <= len(text):
            raise ValueError("saved read boundary is outside its source")
        atom, offset = max((atom, offset), (saved["atom"], saved["source_offset"]))
    identity = [book["incarnation"], manifest["atom_set_version"], state["position_epoch"], atom,
                offset, content_hash_of(by_atom[atom]["text"]) if atom else None, "read-prefix-v1"]
    frontier = {"id": hashlib.sha256(json.dumps(identity).encode()).hexdigest(),
        "atom": atom, "offset": offset, "position_epoch": state["position_epoch"],
        "available_chapter": atom if offset else max(0, atom - 1)}
    # Slice BEFORE retrieval, source scoring, name extraction, prompting, review, or caching.
    permitted = []
    for ch in chapters:
        if ch["ordinal"] > atom:
            break
        if not is_content(manifest["atoms"][ch["ordinal"] - 1]):
            continue
        text = normalize_source(ch["text"])
        if ch["ordinal"] == atom:
            text = text[:offset]
        if text.strip():
            permitted.append({**ch, "chapter_key": ch["key"], "text": text,
                              "revealed_at": ch["ordinal"]})
    return frontier, permitted


def current_snapshot(book_id, catalog, settings, worker):
    state, book, manifest, chapters = source_state(book_id, catalog, settings, worker)
    point, permitted = snapshot(book_id, state, book, manifest, chapters,
                               catalog.get_conversation_frontier(book_id))
    return point, permitted, state, manifest


def requested_snapshot(book_id, boundary, catalog, settings, worker):
    """Allow an older admitted point during a page turn; never widen a captured request."""
    state, book, manifest, chapters = source_state(book_id, catalog, settings, worker)
    current, _ = snapshot(book_id, state, book, manifest, chapters,
                          catalog.get_conversation_frontier(book_id))
    if boundary.position_epoch != state["position_epoch"]:
        raise RuntimeError("reading pass changed")
    if ((boundary.atom, boundary.offset) > (current["atom"], current["offset"])
            or boundary.atom > len(chapters)):
        raise ValueError("request exceeds confirmed reading progress")
    saved = ({"position_epoch": state["position_epoch"], "atom": boundary.atom,
              "source_offset": boundary.offset,
              "source_hash": content_hash_of(chapters[boundary.atom - 1]["text"])}
             if boundary.atom else None)
    point, permitted = snapshot(book_id, {**state, "bookmark": 0}, book, manifest, chapters, saved)
    if point != boundary.model_dump(mode="json"):
        raise ValueError("request boundary does not match its source snapshot")
    return point, permitted, state, manifest, book


def confirm_anchor(book_id, anchor, catalog, settings, worker):
    state, book, manifest, chapters = source_state(book_id, catalog, settings, worker)
    if state["position_epoch"] != anchor.position_epoch:
        raise RuntimeError("reading pass changed")
    saved = catalog.get_conversation_frontier(book_id)
    maximum = max(state["bookmark"], saved["atom"] if saved else 0) + 1
    if anchor.atom > min(len(chapters), maximum):
        raise ValueError("page anchor is beyond admitted reading progress")
    chapter = chapters[anchor.atom - 1]
    offset = anchor_offset(chapter["text"], anchor)
    saved = catalog.set_conversation_frontier(book_id, epoch=anchor.position_epoch,
        atom=anchor.atom, offset=offset, source_hash=content_hash_of(chapter["text"]), cfi=anchor.cfi)
    return snapshot(book_id, state, book, manifest, chapters, saved)[0]
