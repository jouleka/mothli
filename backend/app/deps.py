"""FastAPI dependencies (ADR 0007 D-A11): the per-process singletons — Store (sole memory.db owner),
Catalog (the one shared writer), LLMClient — built once in the lifespan and read off ``app.state``.
Routes NEVER construct these; the Store's sole-owner guarantee rests on there being exactly one."""
import asyncio
import os

from fastapi import Request

from app.catalog.catalog import Catalog
from app.config import Settings
from app.llm.client import LLMClient
from app.llm.factory import build_platform_clients
from app.memory import migrations
from app.memory.store import Store


def build_state(settings: Settings) -> dict:
    """Construct the service singletons. RAISES (refusing startup) if the LLM resolves to the stub
    without ALLOW_STUB — the D-A7 fail-loud predicate lives in the client's default-deny."""
    os.makedirs(settings.data_dir, exist_ok=True)
    # The writer and Jev, built by the one construction both modes share (ADR 0041 section 1).
    client = build_platform_clients(settings)
    reader = client
    from app.answer_bank import AnswerBank
    reader.answers = AnswerBank(persist_path=(os.path.join(settings.data_dir, "reviewed-answers-v2.json")
                                             if settings.reader_answer_cache else None))
    catalog = Catalog(os.path.join(settings.data_dir, "catalog.db"),
                      schema_version_default=migrations.CURRENT_VERSION)
    return {
        "settings": settings,
        "store": Store(
            data_dir=settings.data_dir,
            max_handles=settings.store_max_handles,
            vector_backend=settings.vector_backend,
            schema_version_callback=catalog.set_schema_version,
        ),
        "catalog": catalog,
        "client": client,
        "reader_client": reader,
    }


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_store(request: Request) -> Store:
    return request.app.state.store


def get_content_store(request: Request):
    from app.content_scope import ContentStore
    return ContentStore(request.app.state.store, request.app.state.settings.data_dir)


def get_catalog(request: Request) -> Catalog:
    return request.app.state.catalog


def get_client(request: Request) -> LLMClient:
    return request.app.state.client


def get_reader_client(request: Request) -> LLMClient:
    return request.app.state.reader_client


def get_worker(request: Request):
    return request.app.state.worker


class _BookRequestLockEntry:
    """One event-loop-confined per-book request lock and all of its holders/waiters."""

    def __init__(self, lock=None, refcount=0):
        self.lock = lock or asyncio.Lock()
        self.position_lock = asyncio.Lock()
        self.refcount = refcount
        self.readers = 0
        self.readers_done = asyncio.Event()
        self.readers_done.set()


async def book_lifecycle(book_id: str, request: Request):
    """Protect the book lifecycle without making provider IO block reading or progress.

    The lock registry is created by the app lifespan and is intentionally scoped to that app's one
    event loop. Tests must acquire these locks on the TestClient lifespan loop rather than carrying
    them across clients/loops. Cancellation while queued is handled natively by ``asyncio.Lock``.
    """
    locks = request.app.state.book_request_locks
    entry = locks.get(book_id)
    if entry is None:
        entry = locks[book_id] = _BookRequestLockEntry()
    entry.refcount += 1
    try:
        path = getattr(getattr(request, "url", None), "path", "")
        method = getattr(request, "method", "")
        position_write = method == "PUT" and path.endswith(("/position", "/read-frontier", "/read-page", "/read-chapters"))
        shared_read = (getattr(request, "recap_preparation", False)
                       or method == "GET"
                       or (method == "POST" and path.endswith(("/ask", "/ask/stream", "/chapter-closeout", "/selection-action", "/people", "/ingest/retry")))
                       or position_write)
        if shared_read:
            async with entry.lock:
                entry.readers += 1
                entry.readers_done.clear()
            try:
                if position_write:
                    # Progress keeps its incarnation/reset lease through validation, commit and
                    # enqueue, but only other progress reports need to wait for that transition.
                    async with entry.position_lock:
                        yield
                else:
                    yield
            finally:
                entry.readers -= 1
                if entry.readers == 0:
                    entry.readers_done.set()
        else:
            # Holding the entry lock prevents new readers from passing a queued mutation.
            async with entry.lock:
                await entry.readers_done.wait()
                yield
    finally:
        entry.refcount -= 1
        if entry.refcount == 0 and locks.get(book_id) is entry:
            del locks[book_id]
