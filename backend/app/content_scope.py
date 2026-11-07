"""Meaningful companion content, independent of immutable EPUB reading coordinates."""
import re
from contextlib import contextmanager

from app.ingest.manifest import assert_matches_store, load_manifest
from app.memory.dal import BookmarkView

CONTENT_POLICY_VERSION = "content-scope-v1"
_PARATEXT = re.compile(
    r"(?:acknowledg(?:e)?ments?|list of (?:maps|illustrations|figures|tables)|"
    r"table of contents|contents|copyright|bibliography|index|family\s*trees?|"
    r"the .+ family trees?|notes(?: and references)?)", re.I,
)
_MAP = re.compile(r"map\s*:", re.I)



def is_content(atom):
    # Titles, never arbitrary prose, determine these conservative structural exclusions.
    # Introductions and prefaces remain substantive; an unfamiliar heading stays included.
    if atom.get("char_len") == 1 or ("text" in atom and not (atom["text"] or "").strip()):
        return False
    title = " ".join(str(atom.get("title", "")).split()).strip(" .:")
    return not bool(_PARATEXT.fullmatch(title) or _MAP.match(title))


def excluded_ordinals(manifest):
    return tuple(a["ordinal"] for a in manifest["atoms"] if not is_content(a))


def chapter_numbers(manifest):
    """Publisher chapter labels, not a count of spine documents or illustrations."""
    numbers, last, number = {}, None, 0
    for atom in manifest["atoms"]:
        title = " ".join(atom.get("title", "").split())
        if is_content(atom) and re.match(r"^(?:chapter\s+(?:\d+|[ivxlcdm]+)\b|\d+\s*[:.\-]\s*\w)", title, re.I):
            label = (atom.get("part_label", ""), title)
            if label != last:
                number += 1
            numbers[atom["ordinal"]] = number
            last = label
        elif is_content(atom):
            last = None
    return numbers


class ContentMemory:
    def __init__(self, memory, manifest):
        self._memory = memory
        self._excluded = excluded_ordinals(manifest)

    def __getattr__(self, name):
        return getattr(self._memory, name)

    def view(self, bookmark):
        return BookmarkView(self._memory, self._memory._book_id, bookmark,
                            excluded_ordinals=self._excluded)


class ContentStore:
    """Presentation/ingestion scope only; backup, receipts and geometry keep the original Store."""
    def __init__(self, store, data_dir):
        self._store, self._data_dir = store, data_dir

    def __getattr__(self, name):
        return getattr(self._store, name)

    @contextmanager
    def book(self, book_id, *args, **kwargs):
        manifest = load_manifest(self._data_dir, book_id)
        with self._store.book(book_id, *args, **kwargs) as memory:
            # Validate all stored coordinates before applying a narrower presentation view.
            assert_matches_store(manifest, memory)
            yield ContentMemory(memory, manifest)
