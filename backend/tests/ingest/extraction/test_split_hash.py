"""The split fingerprint (ADR 0044 section 1, step 1): SHA-256 over the ordered list of (chapter
number, the file part of the chapter key, SHA-256 of the chapter's text), the book id left out.

Story memory is stamped by chapter number, so two copies of one file whose chapters are numbered
differently must never count as one book. The break these catch: a fingerprint that differs
between two imports of one file (so the shared notes would never be found), or one that stays the
same when the chapters are cut, named, ordered or filled differently (so another split's "chapter
7" would be handed out as this reader's chapter 7: a spoiler). ADR 0044 lists as unverified that
``segment_for_ingest`` splits one file the same way every time with one version of the code: the
separate-process test checks it, with Python's string hashing seeded differently each time.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.ingest.extraction.chapter_text import segment_for_ingest, split_hash_of
from tests.api._epub import epub_ncx
from tests.ingest.extraction.test_chapter_text import _PROSE, _epub_anchor_raw_body, _sec

BACKEND = Path(__file__).resolve().parents[3]


def _file_driven(parts: int = 4, *, names: str = "c{n}.xhtml", text: str = "Part {n} prose.") -> bytes:
    return epub_ncx([(names.format(n=n), f"Part {n}", f"Part {n}",
                      " ".join([text.format(n=n)] * 40)) for n in range(1, parts + 1)],
                    title="The Split Book", author="Ada Writer")


def _anchor_driven(frags=("c1", "c2", "c3")) -> bytes:
    body = "".join(_sec(frag, f"SECTION_{frag.upper()} " + _PROSE) for frag in frags)
    return _epub_anchor_raw_body(body, list(frags))


def _hash(epub: bytes, book_id: str = "book-a") -> str:
    return split_hash_of(segment_for_ingest(epub, book_id)[1], book_id)


def test_the_fingerprint_is_the_sha256_of_the_ordered_number_file_part_text_hash_list() -> None:
    """The break it catches: a fingerprint over something other than the record's list (the
    book id in it, the 16-character content hash instead of the text's own SHA-256, the order
    lost), which would make two readers' copies of one file differ, or two splits agree."""
    result, chapters = segment_for_ingest(_file_driven(), "book-a")
    assert result.mode != "none" and len(chapters) == 4
    listed = [[chapter["ordinal"], chapter["key"].split(":", 1)[1],
               hashlib.sha256(chapter["text"].encode("utf-8")).hexdigest()]
              for chapter in chapters]
    assert [item[:2] for item in listed] == [[n, f"c{n}.xhtml"] for n in range(1, 5)]
    expected = hashlib.sha256(json.dumps(listed, ensure_ascii=False,
                                         separators=(",", ":")).encode("utf-8")).hexdigest()
    assert split_hash_of(chapters, "book-a") == expected
    assert len(expected) == 64 and set(expected) <= set("0123456789abcdef")


@pytest.mark.parametrize("epub", [_file_driven(), _anchor_driven()], ids=["files", "anchors"])
def test_two_imports_of_one_file_have_one_fingerprint_whatever_the_book_id(epub) -> None:
    """The break it catches: the book id (new for every import) or anything else of the import
    leaking into the fingerprint, so a second reader's copy of one file never matches."""
    assert _hash(epub, "book-a") == _hash(epub, "book-b") == _hash(bytes(epub), "book-a")


def test_one_file_splits_the_same_way_in_separate_processes(tmp_path) -> None:
    """ADR 0044's unverified point 3: ``segment_for_ingest`` gives the same split every time with
    one version of the code. Each run is a fresh interpreter with its own string-hash seed, so a
    split that leaned on set or dict ordering of strings would show up here."""
    script = ("import sys; from app.ingest.extraction.chapter_text import segment_for_ingest, "
              "split_hash_of; data = open(sys.argv[1], 'rb').read(); "
              "print(split_hash_of(segment_for_ingest(data, sys.argv[2])[1], sys.argv[2]))")
    for epub in (_file_driven(9), _anchor_driven(("c1", "c2", "c3", "c4", "c5"))):
        expected = _hash(epub)
        path = tmp_path / f"{len(epub)}.epub"
        path.write_bytes(epub)
        seen = set()
        for seed in ("0", "1", "4242", "random"):
            done = subprocess.run(
                [sys.executable, "-c", script, str(path), f"book-{seed}"],
                cwd=BACKEND, capture_output=True, text=True, timeout=120, check=True,
                env={**os.environ, "PYTHONHASHSEED": seed})
            seen.add(done.stdout.strip())
        assert seen == {expected}


def test_a_different_split_of_the_same_words_gives_another_fingerprint() -> None:
    """The break it catches: two copies whose chapters are cut, named or ordered differently
    agreeing, so one copy's chapter numbers would be read against the other's notes."""
    base = _hash(_file_driven(4))
    words = [" ".join([f"Part {n} prose."] * 40) for n in range(1, 5)]
    # The same words, the last two parts in one chapter: one boundary fewer.
    merged = epub_ncx([("c1.xhtml", "Part 1", "Part 1", words[0]),
                       ("c2.xhtml", "Part 2", "Part 2", words[1]),
                       ("c3.xhtml", "Part 3", "Part 3", words[2] + " " + words[3])],
                      title="The Split Book", author="Ada Writer")
    # The same chapters under other file names.
    renamed = _file_driven(4, names="part{n}.xhtml")
    # The same chapters in another order.
    reordered = epub_ncx([(f"c{n}.xhtml", f"Part {n}", f"Part {n}", words[n - 1])
                          for n in (2, 1, 3, 4)], title="The Split Book", author="Ada Writer")
    # One letter of one chapter's text.
    edited = epub_ncx([(f"c{n}.xhtml", f"Part {n}", f"Part {n}",
                        words[n - 1] + ("!" if n == 3 else "")) for n in range(1, 5)],
                      title="The Split Book", author="Ada Writer")
    found = [base, *(_hash(epub) for epub in (merged, renamed, reordered, edited))]
    assert len(set(found)) == len(found)
    # An anchor-driven book cut at one more anchor.
    assert _hash(_anchor_driven(("c1", "c2", "c3"))) != _hash(_anchor_driven(("c1", "c2")))


def test_a_stand_in_split_that_moves_one_boundary_gives_another_fingerprint() -> None:
    """The chapter list itself changed by hand, as another version of the splitter could change
    it: a chapter boundary moved, a chapter renumbered, a chapter's file part changed. Each
    changes the fingerprint; a title, part label or word count (not part of the split) does not."""
    _result, chapters = segment_for_ingest(_file_driven(4), "book-a")
    base = split_hash_of(chapters, "book-a")

    moved = copy.deepcopy(chapters)
    cut = len(moved[1]["text"]) // 2
    moved[0]["text"] += moved[1]["text"][:cut]
    moved[1]["text"] = moved[1]["text"][cut:]
    renumbered = copy.deepcopy(chapters)
    for chapter in renumbered:
        chapter["ordinal"] += 1
    refiled = copy.deepcopy(chapters)
    refiled[2]["key"] = "book-a:c3.xhtml#part"
    dropped = copy.deepcopy(chapters)[:3]
    for changed in (moved, renumbered, refiled, dropped):
        assert split_hash_of(changed, "book-a") != base

    cosmetic = copy.deepcopy(chapters)
    for chapter in cosmetic:
        chapter.update(title="Another title", part_label="Another part", words=0)
    assert split_hash_of(cosmetic, "book-a") == base


def test_a_chapter_key_of_another_book_is_refused() -> None:
    """The break it catches: a chapter list of one book fingerprinted under another book's id,
    which would silently hash the whole key (book id included) and never match anything."""
    _result, chapters = segment_for_ingest(_file_driven(2), "book-a")
    with pytest.raises(ValueError):
        split_hash_of(chapters, "book-b")
