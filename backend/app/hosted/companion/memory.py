"""``HostedBookStore``: a hosted reader's per-book story memory, read-only, for the checks and
the story memory screens.

ADR 0041 section 4 (part 4) and ADR 0042 part 4. The companion modules use ``store.book(book_id)``
for ``completion_frontier``, the name groups (``known_names.same_person_names``) and the fixed check
``score_recap`` that removes a sentence naming someone the reader has not met. Locally that is a
``MemoryDB``; here it is an immutable snapshot of this owner's Postgres memory rows for the book's
live incarnation, offering exactly the calls those modules make, with the local semantics:

- ``completion_frontier(atoms)``: the atoms, in order, whose chapter has a live completion
  receipt for its current content at that ordinal (``MemoryDB.completion_frontier``).
- ``view(bm)``: ``characters``, ``entities_of_type``, ``aliases_of``, ``chapter_summaries``,
  ``timeline``, ``chapters``, bounded by the predicates of the parity suite
  (``tests/parity/_adapters.py``): ``revealed_at <= bm``, not retracted, valid at ``bm``, aliases
  only of an entity visible at ``bm``, summaries only of a live chapter at or before ``bm``.
- ``_audit_all("entities" | "aliases" | "themes")``: every row, future and retracted ones
  included, as locally: ``score_recap`` needs the future names to know what to forbid.
- ``content_language()``, as locally.
- for the screens (``app.memory_views``, through ``ContentSnapshot``, which leaves out the
  chapters that are not part of the book proper as the local ``ContentMemory`` does):
  ``view(bm).relationships``, ``current_state`` and ``bio``, with the parity suite's predicates
  for edges and entity state; an end (``invalid_at``) is never handed out.

Loading is one statement in one short transaction, so every table is read at one moment (a
receipt can never be seen without the rows published before it). One store is built per request;
it loads each book once and every later ``book()`` of that request, from any thread, gets the same
snapshot. Nothing outlives the store, so nothing is cached across requests. A snapshot that
cannot be loaded raises ``StoryMemoryUnavailable``: an empty snapshot would let every name through.
"""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from types import MappingProxyType

import psycopg

from app.hosted.companion import CompanionDatabaseBusy, OwnerBoundAdapter, as_uuid
from app.content_scope import excluded_ordinals
from app.hosted.tenant.models import OwnerId
from app.language import normalize_content_language

_ENTITY_TYPES = frozenset({"character", "place", "faction", "object"})

# One statement: every subquery sees the same database snapshot. Each names the owner, and the
# transaction sets it for row-level security as well. Rows of a deleted book are never read.
_LOAD = """
SELECT book.content_language,
  COALESCE((SELECT json_agg(json_build_object(
              'key', chapter.chapter_key, 'ordinal', chapter.revealed_at, 'title', chapter.title,
              'part_label', chapter.part_label, 'live', chapter.retracted_at IS NULL)
            ORDER BY chapter.revealed_at, chapter.chapter_key, chapter.id)
            FROM public.chapters AS chapter
            WHERE chapter.owner_id=%(owner)s AND chapter.book_id=book.id
              AND chapter.book_incarnation=book.incarnation), '[]'::json) AS chapters,
  COALESCE((SELECT json_agg(json_build_object('key', chapter.chapter_key,
                                              'ordinal', chapter.revealed_at))
            FROM public.ingested_chapters AS receipt
            JOIN public.chapters AS chapter
              ON (chapter.owner_id, chapter.book_id, chapter.book_incarnation, chapter.id)
               = (receipt.owner_id, receipt.book_id, receipt.book_incarnation, receipt.chapter_id)
            WHERE receipt.owner_id=%(owner)s AND receipt.book_id=book.id
              AND receipt.book_incarnation=book.incarnation
              AND receipt.retracted_at IS NULL AND chapter.retracted_at IS NULL
              AND receipt.content_hash=chapter.content_hash), '[]'::json) AS completed,
  COALESCE((SELECT json_agg(json_build_object(
              'id', entity.id, 'name', entity.canonical_name, 'type', entity.entity_type,
              'revealed_at', entity.revealed_at, 'invalid_at', entity.invalid_at,
              'recorded_at', entity.recorded_at, 'retracted_at', entity.retracted_at)
            ORDER BY entity.recorded_at, entity.id)
            FROM public.entities AS entity
            WHERE entity.owner_id=%(owner)s AND entity.book_id=book.id
              AND entity.book_incarnation=book.incarnation), '[]'::json) AS entities,
  COALESCE((SELECT json_agg(json_build_object(
              'id', alias.id, 'entity_id', alias.entity_id, 'surface_form', alias.surface_form,
              'revealed_at', alias.revealed_at, 'recorded_at', alias.recorded_at,
              'retracted_at', alias.retracted_at)
            ORDER BY alias.recorded_at, alias.id)
            FROM public.aliases AS alias
            WHERE alias.owner_id=%(owner)s AND alias.book_id=book.id
              AND alias.book_incarnation=book.incarnation), '[]'::json) AS aliases,
  COALESCE((SELECT json_agg(json_build_object(
              'id', theme.id, 'name', theme.name, 'description', theme.description,
              'revealed_at', theme.revealed_at, 'invalid_at', theme.invalid_at,
              'recorded_at', theme.recorded_at, 'retracted_at', theme.retracted_at)
            ORDER BY theme.recorded_at, theme.id)
            FROM public.themes AS theme
            WHERE theme.owner_id=%(owner)s AND theme.book_id=book.id
              AND theme.book_incarnation=book.incarnation), '[]'::json) AS themes,
  COALESCE((SELECT json_agg(json_build_object(
              'id', event.id, 'revealed_at', event.revealed_at, 'invalid_at', event.invalid_at,
              'order_idx', event.order_idx, 'summary', event.summary, 'kind', event.kind,
              'live', event.retracted_at IS NULL)
            ORDER BY event.recorded_at, event.id)
            FROM public.events AS event
            WHERE event.owner_id=%(owner)s AND event.book_id=book.id
              AND event.book_incarnation=book.incarnation), '[]'::json) AS events,
  COALESCE((SELECT json_agg(json_build_object(
              'kind', summary.kind, 'summary', summary.summary,
              'revealed_at', summary.revealed_at, 'invalid_at', summary.invalid_at,
              'live', summary.retracted_at IS NULL, 'chapter_key', chapter.chapter_key,
              'chapter_ordinal', chapter.revealed_at,
              'chapter_live', chapter.retracted_at IS NULL)
            ORDER BY summary.recorded_at, summary.id)
            FROM public.chapter_summaries AS summary
            JOIN public.chapters AS chapter
              ON (chapter.owner_id, chapter.book_id, chapter.book_incarnation, chapter.id)
               = (summary.owner_id, summary.book_id, summary.book_incarnation,
                  summary.source_chapter_id)
            WHERE summary.owner_id=%(owner)s AND summary.book_id=book.id
              AND summary.book_incarnation=book.incarnation), '[]'::json) AS summaries,
  COALESCE((SELECT json_agg(json_build_object(
              'id', edge.id, 'src', edge.src_entity_id, 'dst', edge.dst_entity_id,
              'type', edge.relationship_type, 'label', edge.label,
              'revealed_at', edge.revealed_at, 'invalid_at', edge.invalid_at,
              'live', edge.retracted_at IS NULL)
            ORDER BY edge.recorded_at, edge.id)
            FROM public.edges AS edge
            WHERE edge.owner_id=%(owner)s AND edge.book_id=book.id
              AND edge.book_incarnation=book.incarnation), '[]'::json) AS edges,
  COALESCE((SELECT json_agg(json_build_object(
              'id', state.id, 'entity_id', state.entity_id, 'status', state.status,
              'revealed_at', state.revealed_at, 'invalid_at', state.invalid_at,
              'live', state.retracted_at IS NULL)
            ORDER BY state.recorded_at, state.id)
            FROM public.entity_state AS state
            WHERE state.owner_id=%(owner)s AND state.book_id=book.id
              AND state.book_incarnation=book.incarnation), '[]'::json) AS states
FROM public.books AS book
WHERE book.owner_id=%(owner)s AND book.id=%(book)s AND book.deleted_at IS NULL
"""


class StoryMemoryUnavailable(Exception):
    """A book's story memory could not be read. The reply stops; nothing is let through unchecked.

    The message names no book, person or row.
    """


def _ordinal(value, *, positive=True):
    if not isinstance(value, int) or isinstance(value, bool) or (positive and value < 1):
        raise ValueError("a story memory ordinal is malformed")
    return value


def _optional_ordinal(value):
    return None if value is None else _ordinal(value)


def _text(value, *, optional=False):
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise ValueError("a story memory text is malformed")
    return value


def _flag(value):
    if not isinstance(value, bool):
        raise ValueError("a story memory flag is malformed")
    return value


def _status(value):
    """How a person stands, as the local ``status_json`` decodes: a JSON object (the schema's own
    check). Copied, so a caller's change never reaches the snapshot."""
    if not isinstance(value, dict):
        raise ValueError("a story memory state is malformed")
    return json.loads(json.dumps(value))


def _rows(value):
    if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
        raise ValueError("a story memory table is malformed")
    return value


def _frozen(rows):
    return tuple(MappingProxyType(row) for row in rows)


class StoryMemorySnapshot:
    """One book's story memory as of one moment, immutable, safe to share between threads."""

    __slots__ = ("_language", "_chapters", "_completed", "_entities", "_aliases", "_themes",
                 "_events", "_summaries", "_edges", "_states")

    def __init__(self, payload) -> None:
        values = {}
        values["_language"] = payload["content_language"]
        values["_chapters"] = _frozen(
            {"chapter_key": _text(row["key"]), "revealed_at": _ordinal(row["ordinal"]),
             "title": _text(row["title"], optional=True),
             "part_label": _text(row["part_label"], optional=True), "live": _flag(row["live"])}
            for row in _rows(payload["chapters"]))
        # A chapter key is unique within a book incarnation (``chapters`` constraint).
        values["_completed"] = MappingProxyType(
            {_text(row["key"]): _ordinal(row["ordinal"]) for row in _rows(payload["completed"])})
        entities = []
        for row in _rows(payload["entities"]):
            kind = _text(row["type"])
            if kind not in _ENTITY_TYPES:
                raise ValueError("a story memory entity type is malformed")
            entities.append({
                "entity_id": _text(row["id"]), "canonical_name": _text(row["name"]), "type": kind,
                "revealed_at": _ordinal(row["revealed_at"]),
                "invalid_at": _optional_ordinal(row["invalid_at"]),
                "recorded_at": _text(row["recorded_at"]),
                "retracted_at": _text(row["retracted_at"], optional=True)})
        values["_entities"] = _frozen(entities)
        values["_aliases"] = _frozen(
            {"alias_id": _text(row["id"]), "entity_id": _text(row["entity_id"]),
             "surface_form": _text(row["surface_form"]),
             "revealed_at": _ordinal(row["revealed_at"]),
             "recorded_at": _text(row["recorded_at"]),
             "retracted_at": _text(row["retracted_at"], optional=True)}
            for row in _rows(payload["aliases"]))
        values["_themes"] = _frozen(
            {"theme_id": _text(row["id"]), "name": _text(row["name"]),
             "description": _text(row["description"], optional=True),
             "revealed_at": _ordinal(row["revealed_at"]),
             "invalid_at": _optional_ordinal(row["invalid_at"]),
             "recorded_at": _text(row["recorded_at"]),
             "retracted_at": _text(row["retracted_at"], optional=True)}
            for row in _rows(payload["themes"]))
        values["_events"] = _frozen(
            {"event_id": _text(row["id"]), "revealed_at": _ordinal(row["revealed_at"]),
             "invalid_at": _optional_ordinal(row["invalid_at"]),
             "order_idx": _ordinal(row["order_idx"], positive=False),
             "summary": _text(row["summary"]), "kind": _text(row["kind"], optional=True),
             "live": _flag(row["live"])}
            for row in _rows(payload["events"]))
        values["_summaries"] = _frozen(
            {"kind": _text(row["kind"]), "summary": _text(row["summary"]),
             "revealed_at": _ordinal(row["revealed_at"]),
             "invalid_at": _optional_ordinal(row["invalid_at"]), "live": _flag(row["live"]),
             "chapter_key": _text(row["chapter_key"]),
             "chapter_ordinal": _ordinal(row["chapter_ordinal"]),
             "chapter_live": _flag(row["chapter_live"])}
            for row in _rows(payload["summaries"]))
        values["_edges"] = _frozen(
            {"edge_id": _text(row["id"]), "src_entity": _text(row["src"]),
             "dst_entity": _text(row["dst"]), "rel_type": _text(row["type"]),
             "label": _text(row["label"], optional=True),
             "revealed_at": _ordinal(row["revealed_at"]),
             "invalid_at": _optional_ordinal(row["invalid_at"]), "live": _flag(row["live"])}
            for row in _rows(payload["edges"]))
        values["_states"] = _frozen(
            {"state_id": _text(row["id"]), "entity_id": _text(row["entity_id"]),
             "status": _status(row["status"]), "revealed_at": _ordinal(row["revealed_at"]),
             "invalid_at": _optional_ordinal(row["invalid_at"]), "live": _flag(row["live"])}
            for row in _rows(payload["states"]))
        for name, value in values.items():
            object.__setattr__(self, name, value)

    def __setattr__(self, name, value):
        raise AttributeError("a story memory snapshot cannot be changed")

    def __delattr__(self, name):
        raise AttributeError("a story memory snapshot cannot be changed")

    def __repr__(self) -> str:
        return "StoryMemorySnapshot(<one book>)"

    def completion_frontier(self, atoms):
        """The last ordinal of the leading atoms whose chapter memory is complete (0 if none)."""
        frontier = 0
        for atom in atoms:
            if self._completed.get(atom["key"]) != atom["ordinal"]:
                break
            frontier = atom["ordinal"]
        return frontier

    def content_language(self):
        language = normalize_content_language(self._language)
        if language != self._language:
            raise RuntimeError("content language metadata is malformed")
        return language

    def view(self, bookmark):
        return SnapshotView(self, bookmark)

    def has_rows(self):
        """Whether anything of this book's story memory was ever written (a chapter of it)."""
        return bool(self._chapters)

    def _audit_all(self, table):
        """Every row of one of the three tables ``score_recap`` reads, future ones included.

        As locally, these are only the blocklist of names not yet revealed; never shown.
        """
        rows = {"entities": self._entities, "aliases": self._aliases,
                "themes": self._themes}.get(table)
        if rows is None:
            raise ValueError("this table is not part of the hosted story memory")
        return [dict(row) for row in rows]


def _valid(row, bookmark):
    return row["invalid_at"] is None or row["invalid_at"] > bookmark


def _ordered(rows, *keys):
    """Rows sorted on ``keys``; equal keys keep the load order (insertion), as local ids do."""
    return sorted(rows, key=lambda row: tuple(row[key] for key in keys))


class SnapshotView:
    """The snapshot as of a chapter ordinal: the reads of a local ``BookmarkView`` the modules use.

    ``excluded_ordinals`` is the local ``BookmarkView``'s presentation scope (``ContentMemory``):
    rows stamped at a chapter that is not part of the book proper (a contents page, Project
    Gutenberg's own pages) are left out of every read, and so are the entities and chapters the
    reads close over. The checks pass none, as locally.
    """

    __slots__ = ("_memory", "bookmark", "_excluded")

    def __init__(self, memory: StoryMemorySnapshot, bookmark, *, excluded_ordinals=()) -> None:
        # As locally: an int ordinal only, or a malformed counter would show the whole book.
        if not isinstance(bookmark, int) or isinstance(bookmark, bool):
            raise ValueError(f"bookmark must be an int chapter ordinal, got {bookmark!r}")
        object.__setattr__(self, "_memory", memory)
        object.__setattr__(self, "bookmark", bookmark)
        object.__setattr__(self, "_excluded", frozenset(excluded_ordinals))

    def __setattr__(self, name, value):
        raise AttributeError("a story memory view cannot be changed")

    def _shown(self, row):
        """The funnel every read passes: revealed by the bookmark and not an excluded chapter."""
        return row["revealed_at"] <= self.bookmark and row["revealed_at"] not in self._excluded

    def _visible_entities(self):
        bookmark = self.bookmark
        return [row for row in self._memory._entities
                if self._shown(row) and row["retracted_at"] is None and _valid(row, bookmark)]

    def chapters(self):
        rows = [row for row in self._memory._chapters if self._shown(row) and row["live"]]
        return [{key: row[key] for key in ("chapter_key", "revealed_at", "title", "part_label")}
                for row in _ordered(rows, "revealed_at", "chapter_key")]

    def entities_of_type(self, entity_type):
        rows = [row for row in self._visible_entities() if row["type"] == entity_type]
        return [{key: row[key] for key in ("entity_id", "canonical_name", "type", "revealed_at")}
                for row in _ordered(rows, "revealed_at")]

    def characters(self):
        return self.entities_of_type("character")

    def aliases_of(self, entity_id):
        if entity_id not in {row["entity_id"] for row in self._visible_entities()}:
            return []
        rows = [row for row in self._memory._aliases
                if row["entity_id"] == entity_id and self._shown(row)
                and row["retracted_at"] is None]
        return [{"surface_form": row["surface_form"], "revealed_at": row["revealed_at"]}
                for row in _ordered(rows, "revealed_at")]

    def relationships(self):
        """Edges valid at the bookmark between two visible entities. As locally, an edge's
        ``invalid_at`` is never handed out: when a tie ends is news about that later chapter."""
        bookmark = self.bookmark
        visible = {row["entity_id"] for row in self._visible_entities()}
        rows = [row for row in self._memory._edges
                if self._shown(row) and row["live"] and _valid(row, bookmark)
                and row["src_entity"] in visible and row["dst_entity"] in visible]
        return [{**{key: row[key] for key in ("edge_id", "src_entity", "dst_entity", "rel_type",
                                              "label", "revealed_at")}, "invalid_at": None}
                for row in _ordered(rows, "revealed_at")]

    def current_state(self, entity_id):
        """How a visible entity stands at the bookmark: its latest state still valid there (the
        last recorded among those revealed at the same chapter, as the local highest id), with no
        word of when it ends."""
        if entity_id not in {row["entity_id"] for row in self._visible_entities()}:
            return None
        bookmark = self.bookmark
        latest = None
        for row in self._memory._states:
            if (row["entity_id"] == entity_id and self._shown(row) and row["live"]
                    and _valid(row, bookmark)
                    and (latest is None or row["revealed_at"] >= latest["revealed_at"])):
                latest = row
        if latest is None:
            return None
        return {"state_id": latest["state_id"], "revealed_at": latest["revealed_at"],
                "status": json.loads(json.dumps(latest["status"]))}

    def bio(self, entity_id):
        """A visible entity's identity at the bookmark, or None: the fields of the local ``bio``
        that a person's card reads (not ``appears_in_events``: the snapshot loads no event
        participants, and nothing hosted asks for them)."""
        entity = next((row for row in self._visible_entities() if row["entity_id"] == entity_id),
                      None)
        if entity is None:
            return None
        state = self.current_state(entity_id)
        return {"name": entity["canonical_name"], "type": entity["type"],
                "first_seen": entity["revealed_at"],
                "aliases": [row["surface_form"] for row in self.aliases_of(entity_id)],
                "state": state["status"] if state else None}

    def chapter_summaries(self):
        bookmark = self.bookmark
        rows = [row for row in self._memory._summaries
                if row["kind"] == "chapter" and self._shown(row) and row["live"]
                and _valid(row, bookmark) and row["chapter_live"]
                and row["chapter_ordinal"] <= bookmark
                and row["chapter_ordinal"] not in self._excluded]
        return [{key: row[key] for key in ("chapter_key", "revealed_at", "summary")}
                for row in _ordered(rows, "revealed_at")]

    def timeline(self):
        bookmark = self.bookmark
        rows = [row for row in self._memory._events
                if self._shown(row) and row["live"] and _valid(row, bookmark)]
        return [{key: row[key] for key in ("event_id", "revealed_at", "order_idx", "summary",
                                            "kind")}
                for row in _ordered(rows, "revealed_at", "order_idx")]


class ContentSnapshot:
    """The hosted ``ContentMemory`` (``app/content_scope.py``): what the story memory screens
    read, with the chapters that are not part of the book proper left out, as locally."""

    __slots__ = ("_memory", "_excluded")

    def __init__(self, memory: StoryMemorySnapshot, manifest) -> None:
        self._memory = memory
        self._excluded = excluded_ordinals(manifest)

    def view(self, bookmark):
        return SnapshotView(self._memory, bookmark, excluded_ordinals=self._excluded)


# A ``story_memory`` job of this book still to run (ADR 0042 section 6). A capped reader's job is
# moved to the next UTC midnight by the claim (``app/hosted/jobs.py``): that one is waiting for
# tomorrow; any other is under way or about to be.
_MEMORY_JOBS = """
SELECT job.state,
       job.run_after >= (date_trunc('day', now() AT TIME ZONE 'UTC') + interval '1 day')
                        AT TIME ZONE 'UTC' AS tomorrow
FROM public.jobs AS job
JOIN public.books AS book
  ON (book.owner_id, book.id, book.incarnation)
   = (job.owner_id, job.book_id, job.book_incarnation)
WHERE job.owner_id=%s AND job.book_id=%s AND job.kind='story_memory'
  AND job.state IN ('pending', 'leased', 'running') AND book.deleted_at IS NULL
"""


class HostedBookStore(OwnerBoundAdapter):
    """The companion's ``store`` for one hosted reader. Build one per request (or per job):
    ``HostedBookStore(dsn, owner_id)``; ``with store.book(book_id) as mem:`` as locally."""

    __slots__ = ("_snapshots", "_loading")

    def __init__(self, dsn: str, owner_id: OwnerId) -> None:
        super().__init__(dsn, owner_id)
        object.__setattr__(self, "_snapshots", {})
        object.__setattr__(self, "_loading", threading.Lock())

    @contextmanager
    def book(self, book_id):
        """Yield this reader's story memory of one of their live books.

        Raises ``LookupError`` for a book that is not theirs or not live, and
        ``StoryMemoryUnavailable`` when it cannot be read; never an empty stand-in.
        """
        yield self._snapshot(book_id)

    def memory_job(self, book_id):
        """How building this book's story memory stands: ``"running"`` while a job is under way
        or about to be, ``"waiting"`` when the only one waits for the reader's next day, None
        when there is none. A book that is not this reader's has none."""
        try:
            book = as_uuid(book_id)
        except LookupError:
            return None
        with self._transaction() as conn:
            jobs = conn.execute(_MEMORY_JOBS, (self._owner.value, book)).fetchall()
        if not jobs:
            return None
        return "waiting" if all(job["state"] == "pending" and job["tomorrow"] for job in jobs) \
            else "running"

    def _snapshot(self, book_id) -> StoryMemorySnapshot:
        book = as_uuid(book_id)
        with self._loading:
            snapshot = self._snapshots.get(book)
            if snapshot is None:
                snapshot = self._snapshots[book] = self._load(book)
            return snapshot

    def _load(self, book) -> StoryMemorySnapshot:
        try:
            with self._transaction() as conn:
                payload = conn.execute(_LOAD, {"owner": self._owner.value, "book": book}).fetchone()
        except (CompanionDatabaseBusy, psycopg.Error) as exc:
            raise StoryMemoryUnavailable("the story memory could not be read") from exc
        if payload is None:
            raise LookupError("unknown book")
        try:
            return StoryMemorySnapshot(payload)
        except (KeyError, TypeError, ValueError) as exc:
            raise StoryMemoryUnavailable("the story memory is malformed") from exc
