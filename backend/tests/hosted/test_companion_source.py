"""HostedBookSource reads the same book the local import reads (ADR 0041 part 1).

The read map counts a page only when both ends are found in the chapter text, and a page in no
chapter counts for nothing. Hosted mode runs the same ``read_map`` code, so it is exactly as safe
as local mode only if its chapters are the local chapters: same keys, same text, same manifest
atoms. These tests settle that (go-live map B12, first open question) without a database: the
catalog stand-in returns what the hosted upload records, built the way ``upload_book`` builds it.
"""

from __future__ import annotations

import base64
import hashlib
import uuid

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.hosted.companion.source import HostedBookSource, SourceParseCache
from app.hosted.storage import EPUB_MEDIA_TYPE, EncryptedFilesystemStorage, new_source_ref
from app.hosted.tenant.models import OwnerId
from app.ingest.extraction.chapter_text import segment_for_ingest
from app.main import create_app
from tests._alice_shaped import alice_shaped_epub
from tests.api._epub import epub_ncx


def lamp_book(verb: str = "moved") -> bytes:
    people = {1: "Mara", 2: "Corvus", 3: "Vex"}
    return epub_ncx(
        [(f"c{n}.xhtml", f"Part {n}", f"Part {n}",
          " ".join(f"{people[n]} {verb} lamp {i} in part {n}." for i in range(1, 61)))
         for n in (1, 2, 3)],
        title="The Lamp Book", author="Ada Writer")


class _NoExtraction:
    def submit(self, fn, *args, **kwargs):
        return None

    def shutdown(self, wait=True):
        return None


def local_import(tmp_path, blob):
    """(manifest, chapters) exactly as the local app's read map gets them for this EPUB."""
    settings = Settings(_env_file=None, allow_stub=True, reader_page_context=True,
                        reader_memory_ingest=False, data_dir=str(tmp_path / "local"))
    app = create_app(settings, ingest_executor=_NoExtraction())
    with TestClient(app) as api:
        posted = api.post("/api/books", files={"file": ("b.epub", blob, EPUB_MEDIA_TYPE)})
        assert posted.status_code in (200, 201), posted.text
        book_id = posted.json()["book_id"]
        book = app.state.catalog.get_book(book_id)
        return app.state.worker.verified_source(book_id, book)


class UploadedCatalog:
    """The two calls HostedBookSource makes on the catalog, answered as the hosted upload stores
    them (``tenant/api.py`` ``upload_book``: object, checksum, and one search document per
    chapter with its file and length)."""

    def __init__(self, owner: OwnerId, storage, blob: bytes) -> None:
        self.book_id = str(uuid.uuid4())
        self.incarnation = str(uuid.uuid4())
        checksum = hashlib.sha256(blob).hexdigest()
        self.ref = new_source_ref(owner)
        storage.put(self.ref, blob, media_type=EPUB_MEDIA_TYPE, expected_sha256=checksum)
        _result, chapters = segment_for_ingest(blob, self.book_id)
        self.book = {"book_id": self.book_id, "incarnation": self.incarnation,
                     "file_hash": checksum}
        self.record = {"ref": self.ref, "incarnation": self.incarnation,
                       "provider": storage.provider, "media_type": EPUB_MEDIA_TYPE,
                       "byte_size": len(blob), "sha256": checksum,
                       "atoms": [(chapter["ordinal"], chapter["href"], max(1, len(chapter["text"])))
                                 for chapter in chapters]}

    def get_book(self, book_id):
        return dict(self.book) if book_id == self.book_id else None

    def source_record(self, book_id):
        return dict(self.record) if book_id == self.book_id else None


@pytest.fixture()
def storage(tmp_path):
    store = EncryptedFilesystemStorage(root=tmp_path / "objects",
                                       encryption_key=base64.b64decode(base64.b64encode(b"k" * 32)),
                                       max_object_bytes=16 * 1024 * 1024)
    yield store
    store.close()


def _unowned(key):
    return key.partition(":")[2]


def _atoms(manifest):
    return [{**atom, "key": _unowned(atom["key"])} for atom in manifest["atoms"]]


def _comparable_manifest(manifest):
    # ``atom_set_version`` hashes the keys, so it differs with the book id, as between two local
    # imports of one file; what it hashes is compared through ``_atoms``.
    return {key: manifest[key] for key in ("mode", "flags", "content_language")}


@pytest.mark.parametrize("make_book", [lamp_book, alice_shaped_epub], ids=["lamp", "gutenberg"])
def test_hosted_source_gives_the_local_imports_chapters_and_manifest_atoms(tmp_path, storage,
                                                                         make_book):
    """The break it catches: hosted chapters that differ from local ones (a key, a text, an atom
    or its length), which would make a page verified locally unverifiable in hosted mode, or the
    reverse, and let hosted count text as read that local mode would not.
    """
    blob = make_book()
    local_manifest, local_chapters = local_import(tmp_path, blob)
    catalog = UploadedCatalog(OwnerId(uuid.uuid4()), storage, blob)
    source = HostedBookSource(storage, catalog, SourceParseCache(4))
    manifest, chapters = source.verified_source(catalog.book_id, catalog.book)

    # A chapter key is "<book id>:<file>" in both modes, so keys are equal past their own book id.
    local_id = local_manifest["book_id"]
    assert all(c["key"].startswith(f"{catalog.book_id}:") for c in chapters)
    assert all(c["key"].startswith(f"{local_id}:") for c in local_chapters)
    assert [(c["ordinal"], _unowned(c["key"]), c["text"]) for c in chapters] == [
        (c["ordinal"], _unowned(c["key"]), c["text"]) for c in local_chapters]
    assert [(c.get("href"), c.get("title"), c.get("part_label")) for c in chapters] == [
        (c.get("href"), c.get("title"), c.get("part_label")) for c in local_chapters]
    assert _atoms(manifest) == _atoms(local_manifest)
    assert _comparable_manifest(manifest) == _comparable_manifest(local_manifest)
    assert source.source_chapters(catalog.book_id) == chapters


def test_hosted_source_refuses_a_changed_object_a_changed_split_and_another_incarnation(
    tmp_path, storage
):
    """The break it catches: chapter text served from bytes that are not the uploaded book, from
    a parse that no longer splits into the chapters recorded at upload (so read offsets would
    land in other chapters), or for a re-uploaded book under the old one's identity."""
    owner = OwnerId(uuid.uuid4())
    blob = lamp_book()

    replaced = UploadedCatalog(owner, storage, blob)
    other = lamp_book("shoved")                    # same lengths, other words
    storage.delete(replaced.ref)
    storage.put(replaced.ref, other, media_type=EPUB_MEDIA_TYPE,
                expected_sha256=hashlib.sha256(other).hexdigest())
    with pytest.raises(ValueError, match="no longer matches"):
        HostedBookSource(storage, replaced, SourceParseCache(4)).verified_source(
            replaced.book_id, replaced.book)

    resplit = UploadedCatalog(owner, storage, blob)
    resplit.record["atoms"] = resplit.record["atoms"][:-1]
    with pytest.raises(ValueError, match="import manifest"):
        HostedBookSource(storage, resplit, SourceParseCache(4)).verified_source(
            resplit.book_id, resplit.book)

    reuploaded = UploadedCatalog(owner, storage, blob)
    with pytest.raises(ValueError, match="does not belong"):
        HostedBookSource(storage, reuploaded, SourceParseCache(4)).verified_source(
            reuploaded.book_id, {**reuploaded.book, "incarnation": str(uuid.uuid4())})

    missing = UploadedCatalog(owner, storage, blob)
    storage.delete(missing.ref)
    with pytest.raises(ValueError, match="unavailable"):
        HostedBookSource(storage, missing, SourceParseCache(4)).verified_source(
            missing.book_id, missing.book)
    with pytest.raises(LookupError):
        HostedBookSource(storage, missing, SourceParseCache(4)).source_chapters(str(uuid.uuid4()))


def test_one_readers_cached_parse_never_answers_for_another_reader(tmp_path, storage):
    """The break it catches: a process-wide parse cache keyed by book id alone, so a second reader
    naming the same book id (whose object does not exist for them) is served the first reader's
    text from the cache."""
    cache = SourceParseCache(4)
    blob = lamp_book()
    first = UploadedCatalog(OwnerId(uuid.uuid4()), storage, blob)
    HostedBookSource(storage, first, cache).verified_source(first.book_id, first.book)
    assert len(cache) == 1

    second = UploadedCatalog(OwnerId(uuid.uuid4()), storage, blob)
    storage.delete(second.ref)
    second.book_id = first.book_id
    second.book = {**first.book}
    second.record = {**second.record, "incarnation": first.incarnation}
    with pytest.raises(ValueError, match="unavailable"):
        HostedBookSource(storage, second, cache).verified_source(first.book_id, second.book)


def test_the_parse_cache_is_bounded():
    cache = SourceParseCache(2)
    for key in range(5):
        cache.put(key, ("manifest", []))
    assert len(cache) == 2 and cache.get(0) is None and cache.get(4) is not None
