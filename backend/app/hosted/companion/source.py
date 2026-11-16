"""``HostedBookSource``: a hosted reader's book text, verified against its upload (ADR 0041 part 1).

The hosted counterpart of the local worker's ``verified_source`` and ``source_chapters``: the EPUB
comes from object storage (the storage adapter checks its checksum), is segmented by the same
``segment_for_ingest`` the upload used, and must still split into the chapters recorded at upload
(ordinal, file and length) or nothing is served, as locally a source that no longer matches its
manifest serves nothing. The parse is cached per process, bounded, and keyed by the owner's object
reference, the book, its incarnation and the file's checksum (ADR 0022): one reader's parse can
never answer for another's, nor for a re-uploaded book.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import threading
from collections import OrderedDict

from app.hosted.storage import ObjectStorageError
from app.ingest.extraction.chapter_text import segment_for_ingest
from app.ingest.manifest import build_manifest, position_length

# The local worker's fail-closed segmentation gates (``app.ingest.worker._GATING``).
_GATING = ("coverage gap", "anchor resolution")


class SourceParseCache:
    """A small thread-safe LRU of verified parses, built once per process in the hosted lifespan."""

    def __init__(self, max_entries: int = 8) -> None:
        if max_entries < 1:
            raise ValueError("the source parse cache bound must be positive")
        self._max_entries = max_entries
        self._entries: OrderedDict = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            value = self._entries.get(key)
            if value is not None:
                self._entries.move_to_end(key)
            return value

    def put(self, key, value) -> None:
        with self._lock:
            self._entries[key] = value
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


class HostedBookSource:
    """The ``worker`` the companion modules are given in hosted mode, for one owner's catalog."""

    __slots__ = ("_storage", "_catalog", "_cache")

    def __init__(self, storage, catalog, cache: SourceParseCache) -> None:
        self._storage = storage
        self._catalog = catalog
        self._cache = cache

    def verified_source(self, book_id, book):
        """(manifest, chapters) of a live book whose stored EPUB still matches its upload."""
        record = self._catalog.source_record(book_id)
        if record is None or record["provider"] != self._storage.provider:
            raise ValueError("source is unavailable")
        if record["incarnation"] != book["incarnation"]:
            raise ValueError("source does not belong to the requested book")
        if not book.get("file_hash") or record["sha256"] != book["file_hash"]:
            raise ValueError("source no longer matches its import")
        key = (record["ref"], book["book_id"], book["incarnation"], record["sha256"])
        verified = self._cache.get(key)
        if verified is None:
            verified = self._parse(book_id, book, record)
            self._cache.put(key, verified)
        manifest, chapters = verified
        return copy.deepcopy(manifest), copy.deepcopy(chapters)

    def source_chapters(self, book_id):
        """Exact canonical source; callers MUST apply a read boundary."""
        book = self._catalog.get_book(book_id)
        if book is None:
            raise LookupError("unknown book")
        return self.verified_source(book_id, book)[1]

    def _parse(self, book_id, book, record):
        try:
            stored = self._storage.get(record["ref"])
        except ObjectStorageError as exc:
            raise ValueError("source is unavailable") from exc
        digest = hashlib.sha256(stored.data).hexdigest()
        if (not hmac.compare_digest(digest, book["file_hash"])
                or stored.sha256 != record["sha256"] or stored.byte_size != record["byte_size"]
                or stored.media_type != record["media_type"]):
            raise ValueError("source no longer matches its import")
        try:
            result, chapters = segment_for_ingest(stored.data, book_id)
        except Exception as exc:
            raise ValueError("source is unavailable") from exc
        if any(gate in str(flag).lower() for flag in result.flags for gate in _GATING):
            raise ValueError("source no longer matches its import manifest")
        fresh = [(chapter["ordinal"], chapter.get("href", ""), position_length(chapter.get("text")))
                 for chapter in chapters]
        if fresh != record["atoms"]:
            raise ValueError("source no longer matches its import manifest")
        return build_manifest(book_id, result, chapters), chapters
