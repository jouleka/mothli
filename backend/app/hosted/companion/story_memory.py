"""Story memory for one chapter of one hosted reader's book (ADR 0042 section 1, part 2).

The local extraction runs unchanged; only where its memory lives differs. For one chapter:

1. a throw-away **working copy** of the book's memory is built in memory (an SQLite ``MemoryDB``
   at ``:memory:``, nothing on disk) from this owner's Postgres rows, read through the tenant
   role: the live chapters that have a receipt, with their text from ``HostedBookSource`` (each
   checked against the published SHA-256, so a stale row fails closed), the people, their other
   names and their states, and the pinned extraction model. Postgres ids map to local integer
   ids, in the order the rows were recorded, which is the order local mode wrote them;
2. ``ChapterExtraction.ingest_through`` (``app/ingest/chapter_extraction.py``) runs on it,
   unchanged: extraction and its one repair, the review, identity preparation, and
   ``ingest_chapter``, which stamps every row with the chapter it becomes visible at;
3. inside the working copy's commit, before it is committed, exactly what that chapter added is
   read back (its new rows, and the earlier states it closed) and **published** by
   ``PostgresWorkerRepository.commit_chapter_memory`` in one transaction fenced by the job's
   lease, with the receipt last. The publish refuses when the memory in Postgres no longer reads
   as it did when the copy was taken (a reader's correction arrived meanwhile): the copy is then
   rebuilt and the refusal reaches the shared loop as ``StalePreparedRoster``, so the chapter is
   prepared again against the corrected roster, without paying for the extraction again, exactly
   as local mode does when a correction lands between preparation and commit;
4. the shared loop then calls ``finalize_ingest``, which settles the chapter's deferred
   extraction calls; a crash before that leaves them open, and the next run, finding the
   receipt, settles them;
5. the working copy is dropped.

Nothing that must stay out reaches Postgres: no chapter text, no passages, no vectors. Nothing
here decides what is extracted, what passes review, or at which chapter a fact becomes visible.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from contextlib import contextmanager, nullcontext

from app.content_scope import ContentMemory
from app.hosted.companion import OwnerBoundAdapter, as_uuid
from app.hosted.companion.source import HostedBookSource
from app.hosted.companion.store import ChapterCatalog
from app.hosted.jobs import ClaimedJob, PostgresWorkerRepository, StoryMemoryMoved
from app.hosted.tenant.models import OwnerId
from app.ingest.chapter_extraction import ChapterExtraction, _empty_extraction
from app.ingest.extraction.chapter_text import content_hash_of
from app.ingest.extraction.pipeline import StalePreparedRoster
from app.ingest.manifest import assert_matches_store
from app.llm import versioning
from app.memory import dal

# Every purpose a chapter's model calls are reserved under (``ChapterExtraction``'s phases).
CHAPTER_PURPOSES = ("extraction", "extraction-review", "embedding", "identity-review",
                    "jev-identity", "jev-extraction-screen")

# What a working copy is built from, read in one statement (one database moment): the live
# chapters with a live receipt for their content, and every person, other name and state of the
# book's incarnation, in the order they were recorded. The tenant role reads it to build the copy;
# the worker reads it again inside the publish to refuse a copy that moved meanwhile. Both name
# the owner; the tenant's transaction sets it for row-level security as well.
COPY_SQL = """
SELECT
  COALESCE((SELECT json_agg(json_build_object(
              'id', chapter.id, 'key', chapter.chapter_key, 'ordinal', chapter.revealed_at,
              'href', chapter.href, 'fragment', chapter.fragment, 'title', chapter.title,
              'part_label', chapter.part_label, 'kind', chapter.kind,
              'content_hash', chapter.content_hash,
              'extractor_version', chapter.extractor_version,
              'recorded_at', chapter.recorded_at, 'extractor_model', receipt.extractor_model,
              'completed_at', receipt.completed_at)
            ORDER BY chapter.revealed_at, chapter.id)
            FROM public.chapters AS chapter
            JOIN public.ingested_chapters AS receipt
              ON (receipt.owner_id, receipt.book_id, receipt.book_incarnation, receipt.chapter_id)
               = (chapter.owner_id, chapter.book_id, chapter.book_incarnation, chapter.id)
            WHERE chapter.owner_id=%(owner)s AND chapter.book_id=%(book)s
              AND chapter.book_incarnation=%(incarnation)s
              AND chapter.retracted_at IS NULL AND receipt.retracted_at IS NULL
              AND receipt.content_hash=chapter.content_hash), '[]'::json) AS chapters,
  COALESCE((SELECT json_agg(json_build_object(
              'id', entity.id, 'name', entity.canonical_name, 'type', entity.entity_type,
              'revealed_at', entity.revealed_at, 'invalid_at', entity.invalid_at,
              'extractor_version', entity.extractor_version,
              'recorded_at', entity.recorded_at, 'retracted_at', entity.retracted_at)
            ORDER BY entity.recorded_at, entity.id)
            FROM public.entities AS entity
            WHERE entity.owner_id=%(owner)s AND entity.book_id=%(book)s
              AND entity.book_incarnation=%(incarnation)s), '[]'::json) AS entities,
  COALESCE((SELECT json_agg(json_build_object(
              'id', alias.id, 'entity_id', alias.entity_id, 'surface_form', alias.surface_form,
              'revealed_at', alias.revealed_at, 'recorded_at', alias.recorded_at,
              'retracted_at', alias.retracted_at)
            ORDER BY alias.recorded_at, alias.id)
            FROM public.aliases AS alias
            WHERE alias.owner_id=%(owner)s AND alias.book_id=%(book)s
              AND alias.book_incarnation=%(incarnation)s), '[]'::json) AS aliases,
  COALESCE((SELECT json_agg(json_build_object(
              'id', state.id, 'entity_id', state.entity_id, 'status', state.status,
              'revealed_at', state.revealed_at, 'invalid_at', state.invalid_at,
              'extractor_version', state.extractor_version,
              'recorded_at', state.recorded_at, 'retracted_at', state.retracted_at)
            ORDER BY state.recorded_at, state.id)
            FROM public.entity_state AS state
            WHERE state.owner_id=%(owner)s AND state.book_id=%(book)s
              AND state.book_incarnation=%(incarnation)s), '[]'::json) AS states
"""


def copy_digest(payload) -> str:
    """A fingerprint of what ``COPY_SQL`` read: equal exactly when the memory reads the same."""
    tables = {name: payload[name] for name in ("chapters", "entities", "aliases", "states")}
    encoded = json.dumps(tables, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class ChapterOutOfOrder(ValueError):
    """The chapter asked for is not the next one: an earlier chapter has no published memory.
    Chapters are extracted in order (each against the roster of the ones before it)."""


class SelfRelation(RuntimeError):
    """The chapter ties a person to themselves (two of their names in one relationship), which
    the local store keeps and the hosted schema refuses (``edges`` checks ``src <> dst``).
    Nothing is published; the job counts it as a chapter that could not be done."""


class WorkingCopyMismatch(ValueError):
    """The published memory does not match the book's text (a stale or foreign row), or the
    shared commit changed something it should not have. Nothing is published; the message names
    no book, person or text."""


class _CopyReader(OwnerBoundAdapter):
    """Reads one book's memory for the working copy, through the tenant role, for one owner."""

    __slots__ = ()

    def read(self, book_id, incarnation):
        with self._transaction() as conn:
            return conn.execute(COPY_SQL, {"owner": self._owner.value, "book": as_uuid(book_id),
                                           "incarnation": as_uuid(incarnation)}).fetchone()


# What the working copy's commit may touch, and the key each table's rows are known by.
_KEYS = {"chapters": "chapter_key", "ingested_chapters": "chapter_key",
         "raw_chapters": "chapter_key", "chapter_summaries": "summary_id",
         "entities": "entity_id", "aliases": "alias_id", "entity_state": "state_id",
         "edges": "edge_id", "events": "event_id", "event_participants": None,
         "themes": "theme_id", "entity_corrections": "correction_id"}


def _key(table, row):
    key = _KEYS[table]
    return (row["event_id"], row["entity_id"]) if key is None else row[key]


def _sha256(text) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


class _WorkingCopy:
    """The ``store`` the shared extraction is given: one book's memory, held in memory only,
    rebuilt from Postgres, which publishes the one chapter it commits."""

    def __init__(self, reader, repository, claim, book, manifest, chapters, client, ordinal,
                 kind=None):
        self._reader, self._repository, self._claim = reader, repository, claim
        self._book_id, self._incarnation = book["book_id"], book["incarnation"]
        self._book = book
        self.manifest = manifest
        self._text = {chapter["ordinal"]: chapter for chapter in chapters}
        self._client = client
        self.ordinal = ordinal
        self._kind = kind               # the published chapter's kind, when not the shared code's
        self.published = False
        self.copied_ordinals = frozenset()
        self._lock = threading.Lock()
        self._memory = None
        self._memory = self._build()

    # ---- building the copy ---------------------------------------------------------------
    def _build(self) -> dal.MemoryDB:
        payload = self._reader.read(self._book_id, self._incarnation)
        if payload is None:
            raise LookupError("unknown book")
        memory = dal.MemoryDB(":memory:", self._book_id, vector_backend="bruteforce")
        try:
            self._fill(memory, payload)
        except BaseException:
            memory.close()
            raise
        old, self._memory = self._memory, memory
        if old is not None:
            memory._active_owner = old._active_owner
            old.close()
        return memory

    def _fill(self, memory, payload) -> None:
        memory.set_content_language(self._book["content_language"])
        memory.set_book_profile(book_type=self._book["book_type"], confidence=0.0,
                                detector_version="hosted-copy", signals=())
        self._uuids = {"entities": {}, "entity_state": {}}
        local = {"entities": {}, "entity_state": {}}
        models = set()
        with memory.transaction():
            for row in payload["chapters"]:
                source = self._text.get(row["ordinal"])
                text = (source or {}).get("text") or ""
                if source is None or source["key"] != row["key"] or _sha256(text) != row[
                        "content_hash"]:
                    raise WorkingCopyMismatch("published memory does not match the book's text")
                short = content_hash_of(text)
                memory._ins("chapters", chapter_key=row["key"], book_id=self._book_id,
                            revealed_at=row["ordinal"],
                            href=row["href"] or source.get("href") or "",
                            fragment=row["fragment"] or "", title=row["title"],
                            part_label=row["part_label"], kind=row["kind"], content_hash=short,
                            schema_version=dal.SCHEMA_VERSION,
                            extractor_version=row["extractor_version"],
                            recorded_at=row["recorded_at"], retracted_at=None)
                memory._ins("raw_chapters", chapter_key=row["key"], book_id=self._book_id,
                            revealed_at=row["ordinal"], text=text, char_count=len(text),
                            content_hash=short, recorded_at=row["recorded_at"],
                            retracted_at=None)
                memory._ins("ingested_chapters", chapter_key=row["key"], book_id=self._book_id,
                            content_hash=short, cost_pending=0,
                            extractor_model=row["extractor_model"],
                            completed_at=row["completed_at"])
                models.add(row["extractor_model"])
            for number, row in enumerate(payload["entities"], start=1):
                local["entities"][row["id"]] = number
                self._uuids["entities"][number] = uuid.UUID(row["id"])
                memory._ins("entities", entity_id=number, book_id=self._book_id,
                            canonical_name=row["name"], type=row["type"],
                            revealed_at=row["revealed_at"], invalid_at=row["invalid_at"],
                            schema_version=dal.SCHEMA_VERSION,
                            extractor_version=row["extractor_version"],
                            recorded_at=row["recorded_at"], retracted_at=row["retracted_at"])
            for number, row in enumerate(payload["aliases"], start=1):
                memory._ins("aliases", alias_id=number,
                            entity_id=local["entities"][row["entity_id"]], book_id=self._book_id,
                            surface_form=row["surface_form"], revealed_at=row["revealed_at"],
                            recorded_at=row["recorded_at"], retracted_at=row["retracted_at"])
            for number, row in enumerate(payload["states"], start=1):
                self._uuids["entity_state"][number] = uuid.UUID(row["id"])
                memory._ins("entity_state", state_id=number,
                            entity_id=local["entities"][row["entity_id"]], book_id=self._book_id,
                            revealed_at=row["revealed_at"], invalid_at=row["invalid_at"],
                            status_json=json.dumps(row["status"]),
                            schema_version=dal.SCHEMA_VERSION,
                            extractor_version=row["extractor_version"],
                            recorded_at=row["recorded_at"], retracted_at=row["retracted_at"])
            if models:
                # The book's extraction model is pinned by its first chapter and carried on every
                # receipt; hosted extraction embeds with the stub only, whose identity is fixed.
                if len(models) != 1 or None in models:
                    raise versioning.ModelMigrationRequired(
                        [("extractor_model", versioning.FORCE_RE_EXTRACT)])
                canary = self._client.embed([versioning.EMBED_CANARY_TEXT])[0][0]
                memory.pin_models(extractor_model=next(iter(models)),
                                  embed_model=self._client.embed_identity(),
                                  embed_dim=len(canary), embed_canary=canary)
        self.copied_ordinals = frozenset(row["ordinal"] for row in payload["chapters"])
        self._digest = copy_digest(payload)
        self._rows = {table: {_key(table, row): dict(row) for row in memory._audit_all(table)}
                      for table in _KEYS}

    def close(self) -> None:
        if self._memory is not None:
            self._memory.close()

    # ---- what the shared extraction calls ------------------------------------------------
    @contextmanager
    def book(self, book_id):
        if book_id != self._book_id:
            raise LookupError("unknown book")
        with self._lock:
            self._memory._active_owner = threading.get_ident()
            try:
                assert_matches_store(self.manifest, self._memory)
                yield _PublishingMemory(self)
            finally:
                self._memory._active_owner = None

    # ---- the publish -----------------------------------------------------------------------
    def rebuild(self) -> dal.MemoryDB:
        return self._build()

    def publish(self, memory) -> None:
        """Publish what the open commit added, before it is committed here."""
        if memory._transaction_failed:
            raise WorkingCopyMismatch("the chapter's commit failed part-way")
        added = self._added(memory)
        if added is None:
            return
        chapter, receipt, rows = added
        self._repository.commit_chapter_memory(
            self._claim, book_id=as_uuid(self._book_id),
            book_incarnation=as_uuid(self._incarnation), copied=self._digest,
            chapter=chapter, receipt=receipt, rows=rows)
        self.published = True

    def _added(self, memory):
        """(chapter, receipt, rows) for the one chapter this commit added, or None."""
        new, closed = {}, []
        for table in _KEYS:
            before = self._rows[table]
            now = sorted(((_key(table, row), dict(row)) for row in memory._audit_all(table)),
                         key=lambda item: item[0])
            if set(before) - {key for key, _row in now}:
                raise WorkingCopyMismatch("the chapter's commit removed a copied row")
            for key, row in now:
                if key not in before:
                    continue
                if row == before[key]:
                    continue
                changed = {name for name in row if row[name] != before[key][name]}
                if (table == "entity_state" and changed == {"invalid_at"}
                        and before[key]["invalid_at"] is None):
                    closed.append((key, row["invalid_at"]))
                    continue
                raise WorkingCopyMismatch("the chapter's commit changed a copied row")
            new[table] = [row for key, row in now if key not in before]
        if not new["chapters"]:
            return None
        if (new["entity_corrections"] or len(new["chapters"]) != 1
                or len(new["ingested_chapters"]) != 1):
            raise WorkingCopyMismatch("the chapter's commit is not one chapter")
        [written] = new["chapters"]
        [completed] = new["ingested_chapters"]
        source = self._text.get(self.ordinal)
        text = (source or {}).get("text") or ""
        if (source is None or written["revealed_at"] != self.ordinal
                or written["chapter_key"] != source["key"]
                or completed["chapter_key"] != source["key"]
                or written["content_hash"] != content_hash_of(text)
                or completed["content_hash"] != written["content_hash"]
                or any(row["chapter_key"] != source["key"] for row in new["chapter_summaries"])):
            raise WorkingCopyMismatch("the chapter's commit is not the chapter asked for")
        if any(row["src_entity"] == row["dst_entity"] for row in new["edges"]):
            raise SelfRelation("the chapter relates a person to themselves")

        entities = dict(self._uuids["entities"])
        for row in new["entities"]:
            entities[row["entity_id"]] = uuid.uuid4()
        states = dict(self._uuids["entity_state"])
        events = {row["event_id"]: uuid.uuid4() for row in new["events"]}
        chapter = {"key": written["chapter_key"], "ordinal": written["revealed_at"],
                   "href": written["href"], "fragment": written["fragment"],
                   "title": written["title"], "part_label": written["part_label"],
                   "kind": self._kind or written["kind"], "content_hash": _sha256(text),
                   "extractor_version": written["extractor_version"]}
        pinned = memory.pinned_identity() or {}
        receipt = {"extractor_model": pinned.get("extractor_model"),
                   "input_tokens": completed["input_tokens"],
                   "output_tokens": completed["output_tokens"], "usd": completed["usd"]}
        rows = {
            "summaries": [{"kind": row["kind"], "summary": row["summary"],
                           "revealed_at": row["revealed_at"],
                           "extractor_version": row["extractor_version"]}
                          for row in new["chapter_summaries"]],
            "entities": [{"id": entities[row["entity_id"]],
                          "canonical_name": row["canonical_name"], "entity_type": row["type"],
                          "revealed_at": row["revealed_at"], "invalid_at": row["invalid_at"],
                          "extractor_version": row["extractor_version"]}
                         for row in new["entities"]],
            "aliases": [{"entity_id": entities[row["entity_id"]],
                         "surface_form": row["surface_form"], "revealed_at": row["revealed_at"]}
                        for row in new["aliases"]],
            "states": [{"id": states.setdefault(row["state_id"], uuid.uuid4()),
                        "entity_id": entities[row["entity_id"]],
                        "status": json.loads(row["status_json"]),
                        "revealed_at": row["revealed_at"], "invalid_at": row["invalid_at"],
                        "extractor_version": row["extractor_version"]}
                       for row in new["entity_state"]],
            "closed_states": [{"id": states[key], "invalid_at": invalid_at}
                              for key, invalid_at in closed],
            "edges": [{"src_entity_id": entities[row["src_entity"]],
                       "dst_entity_id": entities[row["dst_entity"]],
                       "relationship_type": row["rel_type"], "label": row["label"],
                       "revealed_at": row["revealed_at"], "invalid_at": row["invalid_at"],
                       "extractor_version": row["extractor_version"]}
                      for row in new["edges"]],
            "events": [{"id": events[row["event_id"]], "order_idx": row["order_idx"],
                        "summary": row["summary"], "kind": row["kind"],
                        "revealed_at": row["revealed_at"], "invalid_at": row["invalid_at"],
                        "extractor_version": row["extractor_version"]}
                       for row in new["events"]],
            "participants": [{"event_id": events[row["event_id"]],
                              "entity_id": entities[row["entity_id"]], "role": row["role"],
                              "revealed_at": row["revealed_at"]}
                             for row in new["event_participants"]],
            "themes": [{"name": row["name"], "description": row["description"],
                        "revealed_at": row["revealed_at"], "invalid_at": row["invalid_at"],
                        "extractor_version": row["extractor_version"]}
                       for row in new["themes"]],
        }
        return chapter, receipt, rows


class _PublishingMemory(ContentMemory):
    """The working copy as the shared code sees it: local mode's ``ContentMemory`` (the same
    content scope over the same manifest), whose one commit also publishes the chapter."""

    def __init__(self, copy: _WorkingCopy) -> None:
        super().__init__(copy._memory, copy.manifest)
        self._copy = copy

    @contextmanager
    def transaction(self):
        try:
            with self._memory.transaction():
                yield
                self._copy.publish(self._memory)
        except StoryMemoryMoved:
            # Rolled back here, nothing written there: rebuild from the memory as it is now and
            # let the shared loop prepare the chapter again against it, as on a local correction.
            self._memory = self._copy.rebuild()
            raise StalePreparedRoster(
                "story memory changed after it was copied; prepare the chapter again") from None


UNREMEMBERED = "unremembered"


class _Unremembered(ChapterExtraction):
    """The shared extraction for a chapter that could not be done (ADR 0042 section 2): its
    model extraction is replaced by the empty extraction local mode keeps for a page that is not
    story, which asserts nothing, is cleared without a model call and costs nothing. Everything
    after it (preparation, the commit that stamps the chapter, the publish) is unchanged."""

    def _complete_extraction(self, book_id, ordinal, title, roster, text, **_profile):
        return _empty_extraction(text)


def remember_chapter(claim: ClaimedJob, repository: PostgresWorkerRepository, *, hold,
                     tenant_dsn: str, settings, client, storage, sources, ordinal: int,
                     unremembered: bool = False) -> bool:
    """Extract chapter ``ordinal`` of the claimed book for the claimed reader and publish it.

    ``hold`` is the job's lease check (``_Lease.hold`` of ``app.hosted.companion.jobs``): it is
    called before anything is spent and before every paid call, and raises ``LostLeaseError``
    once the lease is lost or the job cancelled; the publish checks the lease again in its own
    transaction. ``client`` is the platform's extraction client, embedding with the stub only.
    ``storage`` and ``sources`` are the object storage and the process's parse cache, as the
    companion dispatcher holds them.

    Returns True when this run published the chapter, False when it was already published (its
    deferred extraction calls, if a crash left any open, are settled now). Raises
    ``ChapterOutOfOrder`` when an earlier chapter has no memory yet, ``LookupError`` when the
    book is no longer the reader's, ``CostCeilingExceeded`` when the reader or the platform is at
    a cap at the chapter's first call (nothing is spent then), ``ExtractionGroundingRejected``
    when the chapter fails its review after the one repair, ``StalePreparedRoster`` when
    corrections kept arriving through three preparations, ``ChapterAlreadyPublished`` when
    another run published it meanwhile, ``ModelMigrationRequired`` when the platform's extraction
    model is not the one the book was begun with, ``SelfRelation`` when the chapter ties a person
    to themselves, and whatever the model calls raise.

    With ``unremembered`` the chapter is published as an unremembered chapter: no model call, the
    empty extraction, and ``chapters.kind`` = ``UNREMEMBERED`` as its marker.
    """
    if claim.book_id is None or claim.book_incarnation is None:
        raise ValueError("story memory is made for one book")
    if not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal < 1:
        raise ValueError("a chapter ordinal is a positive integer")
    if not client.embed_identity().startswith("stub:"):
        # Hosted mode stores no vectors (ADR 0042 section 8); the stub's identity is the pin.
        raise ValueError("hosted story memory is extracted with the stub embedder")
    hold()
    owner = OwnerId(claim.owner_id)
    book_id, incarnation = str(claim.book_id), str(claim.book_incarnation)
    catalog = ChapterCatalog(tenant_dsn, owner)
    book = catalog.get_book(book_id)
    if book is None or book["incarnation"] != incarnation:
        raise LookupError("unknown book")
    manifest, chapters = HostedBookSource(storage, catalog, sources).verified_source(book_id, book)
    if ordinal not in {chapter["ordinal"] for chapter in chapters}:
        raise ValueError("the book has no such chapter")
    copy = _WorkingCopy(_CopyReader(tenant_dsn, owner), repository, claim, book, manifest,
                        chapters, client, ordinal, kind=UNREMEMBERED if unremembered else None)
    try:
        if not set(range(1, ordinal)) <= copy.copied_ordinals:
            raise ChapterOutOfOrder("an earlier chapter has no story memory yet")
        if ordinal not in copy.copied_ordinals:
            # An attempt at this chapter that died (a crash, or a lease another worker took)
            # left its calls reserved. They were made, so they are settled now, once; the
            # shared loop would otherwise refuse the chapter for good.
            for purpose in CHAPTER_PURPOSES:
                for reservation in catalog.cost_reservation_ids(book_id, phase=purpose,
                                                                chapter_ordinal=ordinal):
                    catalog.settle_cost(book_id, reservation, phase="extraction-abandoned")

        def current(checked_book, checked_incarnation):
            if checked_book != book_id or checked_incarnation != incarnation:
                raise LookupError("unknown book")
            live = catalog.get_book(book_id)
            if live is None or live["incarnation"] != incarnation:
                raise LookupError("unknown book")

        # The chapter before is passed too, so its deferred calls are settled if a crash after
        # its publish left them open; every earlier chapter's run did the same for its own.
        extraction = _Unremembered if unremembered else ChapterExtraction
        extraction(catalog, client, settings, hold, time.sleep).ingest_through(
            book_id, [chapter for chapter in chapters if ordinal - 1 <= chapter["ordinal"]
                      <= ordinal],
            ordinal, store=copy, incarnation=incarnation, generation=0,
            require_incarnation=current,
            require_worker_incarnation=lambda checked, inc: current(checked, inc),
            publish_validated=lambda *_progress: None,
            book_lifecycle=lambda _book: nullcontext())
    finally:
        copy.close()
    return copy.published
