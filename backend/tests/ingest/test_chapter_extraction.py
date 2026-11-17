"""The per-chapter extraction runs from its own module, with no ``IngestWorker`` driving it (ADR 0042)."""
from contextlib import contextmanager

from fastapi.testclient import TestClient

from tests.api._epub import epub_ncx
from app.config import Settings
from app.content_scope import ContentStore
from app.ingest.chapter_extraction import ChapterExtraction
from app.ingest.extraction.chapter_text import segment_for_ingest
from app.ingest.extraction.pipeline import content_hash_of
from app.ingest.manifest import source_epub_path
from app.main import create_app


class DiscardingExecutor:
    """Nothing the app enqueues runs: only the direct call below extracts."""

    def submit(self, fn, *args, **kwargs):
        return None

    def shutdown(self, wait=True, cancel_futures=False):
        return None


def test_one_chapter_is_extracted_and_committed_without_the_worker(tmp_path):
    settings = Settings(
        _env_file=None, allow_stub=True, data_dir=str(tmp_path / "data"),
        openai_api_key=None, anthropic_api_key=None, openai_base_url=None,
        embed_api_key=None, embed_base_url=None, embed_provider="stub",
        embed_model="lexical-stub-256",
    )
    book = epub_ncx([
        ("one.xhtml", "Chapter I", "Chapter I", "Aldric worked at the forge with Berenice. " * 12),
        ("two.xhtml", "Chapter II", "Chapter II", "Corvus concealed the future verdict. " * 12),
    ])
    app = create_app(settings, ingest_executor=DiscardingExecutor())
    with TestClient(app) as api:
        imported = api.post("/api/books", files={
            "file": ("synthetic.epub", book, "application/epub+zip"),
        })
        assert imported.status_code == 201
        book_id = imported.json()["book_id"]
        catalog, client = app.state.catalog, app.state.client
        incarnation = catalog.get_book(book_id)["incarnation"]
        _result, chapters = segment_for_ingest(
            source_epub_path(settings.data_dir, book_id), book_id)
        assert catalog.get_state(book_id)["ingest_progress"] == 0

        checks, published, stops = [], [], []

        @contextmanager
        def lifecycle(bid):
            checks.append(("lifecycle", bid))
            yield

        extraction = ChapterExtraction(catalog, client, settings,
                                       lambda: stops.append(1), lambda _delay: None)
        validated = extraction.ingest_through(
            book_id, chapters, 1, store=ContentStore(app.state.store, settings.data_dir),
            incarnation=incarnation, generation=7,
            require_incarnation=lambda bid, inc: checks.append(("current", bid, inc)),
            require_worker_incarnation=lambda bid, inc: checks.append(("worker", bid, inc)),
            publish_validated=lambda bid, inc, gen, ordinal: published.append((gen, ordinal)),
            book_lifecycle=lifecycle,
        )

        assert validated == 1
        assert published == [(7, 1)]
        assert ("lifecycle", book_id) in checks and ("worker", book_id, incarnation) in checks
        assert stops, "the caller's stop check is consulted between calls"
        assert catalog.get_state(book_id)["ingest_progress"] == 1
        assert catalog.get_cost_reservations(book_id) == []
        first = chapters[0]
        with app.state.store.book(book_id) as mem:
            receipt = mem.chapter_completion(first["key"], 1, content_hash_of(first["text"]))
            assert receipt is not None
            assert mem.chapter_completion(chapters[1]["key"], 2,
                                          content_hash_of(chapters[1]["text"])) is None
