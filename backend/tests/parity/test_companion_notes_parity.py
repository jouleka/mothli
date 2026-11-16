"""Chapter notes come out the same locally and hosted (ADR 0041 part 6, section 9.3).

The same book, the same reading and the same stand-in writer and checker: locally the unchanged
``build_missing`` runs on the local ``Catalog`` after each step; hosted, the routes add durable
jobs and the worker's dispatcher runs the same ``build_missing`` over the hosted adapters. After
every step the unchanged ``usable_notes`` must give a reply the same notes in both modes, the
stored notes must match, and the writer and the checker must have been given the same text.
"""

from __future__ import annotations

import pytest

from app.chapter_notes import build_missing, usable_notes
from app.hosted.companion.jobs import CompanionJobs
from app.hosted.companion.source import HostedBookSource
from app.hosted.companion.store import HostedCompanionCatalog
from app.hosted.jobs import PostgresWorkerRepository
from app.hosted.tenant.models import OwnerId
from app.hosted.worker import DurableWorker
from app.read_map import read_snapshot
from tests.hosted.test_companion_notes_postgres import NotesModels
from tests.hosted.test_companion_read_map_postgres import page
from tests.hosted.test_companion_stream_postgres import CHAT
from tests.hosted.test_companion_stream_postgres import restore_caps  # noqa: F401  (autouse)
from tests.parity import test_companion_stream_parity as stream_parity
from tests.parity.test_companion_stream_parity import LocalChat

pytestmark = pytest.mark.postgres

postgres_database = stream_parity.postgres_database

STEPS = [
    ("PUT", "/read-chapters", {"atoms": [1], "read": True}),          # chapter 1 read
    ("PUT", "/read-page", page(2, 1, 5)),                              # inside chapter 2
    ("PUT", "/read-page", page(1, 60, 60, to_chapter=2)),              # chapter 2 finished
    ("PUT", "/read-page", page(3, 10, 12)),                            # a leap into chapter 3
    ("PUT", "/read-chapters", {"atoms": [2], "read": False}),          # "I haven't read this"
    ("PUT", "/read-chapters", {"atoms": [2, 3], "read": True}),        # both read after all
]


def local_view(local):
    state = local.app.state
    _point, chapters, *_rest = read_snapshot(local.book, state.catalog, local.settings,
                                             state.worker)
    return (usable_notes(local.book, chapters, state.catalog),
            state.catalog.get_chapter_notes(local.book))


def hosted_view(hosted):
    catalog = HostedCompanionCatalog(hosted.app.state.companion_dsn, OwnerId(hosted.owner))
    source = HostedBookSource(hosted.app.state.object_storage, catalog,
                              hosted.app.state.companion_sources)
    _point, chapters, *_rest = read_snapshot(hosted.book, catalog, hosted.app.state.settings,
                                             source)
    return usable_notes(hosted.book, chapters, catalog), catalog.get_chapter_notes(hosted.book)


def given(models: NotesModels, book: str) -> tuple:
    return ([prompt.replace(book, "BOOK") for prompt in models.notes_prompts],
            sorted(str(state).replace(book, "BOOK") for state in models.checked))


def test_the_same_reading_gives_the_same_usable_notes_locally_and_hosted(
        tmp_path, postgres_database, monkeypatch):
    """The break it catches: hosted notes written for a chapter local mode would not write
    (looser is the danger: notes from a chapter not read in full), kept with other sentences,
    or given to a reply where local mode gives none."""
    monkeypatch.setattr(stream_parity, "CHAT", {**CHAT, "reader_story_notes": True})
    local = LocalChat(tmp_path)
    hosted = stream_parity.HostedChat(tmp_path, postgres_database)
    try:
        local_models = NotesModels(local.client, monkeypatch)
        hosted_models = NotesModels(hosted.client, monkeypatch)
        state = hosted.app.state
        work = DurableWorker(
            PostgresWorkerRepository(postgres_database),
            CompanionJobs(tenant_dsn=state.companion_dsn, settings=state.settings,
                          client=state.platform_client, storage=state.object_storage,
                          sources=state.companion_sources),
            "parity-worker")
        seen = []
        for method, suffix, body in STEPS:
            for side in (local, hosted):
                assert side.request(method, suffix, body).status_code == 200, suffix
            build_missing(local.book, local.app.state.catalog, local.app.state.store,
                          local.settings, local.client, local.app.state.worker)
            while work.run_once():
                pass
            mine, theirs = local_view(local), hosted_view(hosted)
            assert mine == theirs, (suffix, body)
            assert given(local_models, local.book) == given(hosted_models, hosted.book), suffix
            seen.append(mine)
        # The comparison covered what it is for: no notes, notes withheld from a reply while
        # their chapter is not read in full, and notes for every chapter read.
        usable = [[note["ordinal"] for note in notes] for notes, _stored in seen]
        stored = [[note["atom"] for note in held] for _notes, held in seen]
        assert usable == [[1], [1], [1, 2], [1, 2], [1], [1, 2, 3]]
        assert stored == [[1], [1], [1, 2], [1, 2], [1, 2], [1, 2, 3]]
        assert all(note["dropped"] == 1 for note in seen[-1][1])
        assert len(local_models.notes_prompts) == 3
    finally:
        local.close()
        hosted.close()
