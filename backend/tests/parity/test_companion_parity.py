"""The read map gives the same answers in local and hosted mode (ADR 0041 part 1, section 9.3).

One sequence of page reports, skims, chapter marks and refused reports runs through the local app
(SQLite catalog, temporary data directory) and through the hosted app (Postgres adapters, object
storage), at the same paths with the same bodies. The unchanged ``app.read_map`` decides both, so
every answer must match: each response, then ``read_snapshot``'s point, map and permitted text,
and ``chapter_fractions``. Only the point's id differs, because it hashes the book's own identity.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import conninfo

from app.config import Settings
from app.hosted.auth.api import CSRF_HEADER
from app.hosted.companion.source import HostedBookSource
from app.hosted.companion.store import HostedCompanionCatalog
from app.hosted.migrations import apply_migrations
from app.hosted.storage import EPUB_MEDIA_TYPE
from app.hosted.tenant.models import OwnerId
from app.hosted.tenant.repository import PostgresTenantRepository
from app.main import create_app
from app.read_map import chapter_fractions, read_snapshot
from tests._alice_shaped import HEADER, CONTENTS, alice_shaped_epub, line as alice_line
from tests.hosted.test_companion_read_map_postgres import (
    anchor,
    hosted_settings,
    lamp_book,
    line,
    page,
    signed_in,
)
from tests.hosted.test_tenant_postgres import UnusedOIDCClient, _become

pytestmark = pytest.mark.postgres


@pytest.fixture()
def postgres_database():
    admin_dsn = os.environ.get("TEST_POSTGRES_DSN")
    if not admin_dsn:
        pytest.skip("TEST_POSTGRES_DSN is required for the SQLite/PostgreSQL parity gate")
    database_name = f"companion_{uuid.uuid4().hex}"
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{database_name}"')
    dsn = conninfo.make_conninfo(admin_dsn, dbname=database_name)
    try:
        apply_migrations(dsn)
        yield dsn
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (database_name,),
            )
            admin.execute(f'DROP DATABASE "{database_name}"')


class _NoExtraction:
    def submit(self, fn, *args, **kwargs):
        return None

    def shutdown(self, wait=True):
        return None


class _ParityTenantRepository(PostgresTenantRepository):
    """The real repository on the parity database's own (superuser) role: parity compares
    answers, and isolation is proven on the tenant role by ``tests/hosted``."""

    async def check_runtime_role(self) -> None:
        return None


class LocalReader:
    def __init__(self, tmp_path, blob: bytes) -> None:
        self.settings = Settings(_env_file=None, allow_stub=True, reader_page_context=True,
                                 reader_memory_ingest=False, data_dir=str(tmp_path / "local"))
        self.app = create_app(self.settings, ingest_executor=_NoExtraction())
        self._client = TestClient(self.app)
        self._client.__enter__()
        # The read map without streamed replies is a hosted-only setting; locally it is switched
        # on beside them after start, as the local read-map tests do.
        self.settings.reader_read_map = True
        posted = self._client.post("/api/books",
                                   files={"file": ("b.epub", blob, EPUB_MEDIA_TYPE)})
        self.book = posted.json()["book_id"]

    def request(self, method, suffix, body):
        return self._client.request(method, f"/api/books/{self.book}{suffix}", json=body)

    def module_view(self):
        state = self.app.state
        return (read_snapshot(self.book, state.catalog, self.settings, state.worker),
                chapter_fractions(self.book, state.catalog, self.settings, state.worker))

    def close(self):
        self._client.__exit__(None, None, None)


class HostedReader:
    def __init__(self, tmp_path, dsn: str, blob: bytes) -> None:
        self.owner = uuid.uuid4()
        with psycopg.connect(dsn) as conn:
            conn.execute("INSERT INTO users (id,display_name) VALUES (%s,'Parity reader')",
                         (self.owner,))
        self.tokens, sessions = signed_in({"a": self.owner})
        self.settings = hosted_settings(dsn, tmp_path / "objects")
        self.app = create_app(
            self.settings,
            auth_repository=sessions,
            tenant_repository=_ParityTenantRepository(dsn),
            oidc_client=UnusedOIDCClient(),
            auth_clock=lambda: datetime(2026, 7, 16, 12, tzinfo=UTC),
        )
        self._client = TestClient(self.app, base_url="https://reader.example")
        self._client.__enter__()
        self.csrf = _become(self._client, self.tokens, "a")
        posted = self._client.post("/api/books",
                                   files={"file": ("b.epub", blob, EPUB_MEDIA_TYPE)},
                                   headers={CSRF_HEADER: self.csrf})
        assert posted.status_code == 201, posted.text
        self.book = posted.json()["id"]

    def request(self, method, suffix, body):
        return self._client.request(method, f"/api/books/{self.book}{suffix}", json=body,
                                    headers={CSRF_HEADER: self.csrf})

    def module_view(self):
        catalog = HostedCompanionCatalog(self.app.state.companion_dsn, OwnerId(self.owner))
        source = HostedBookSource(self.app.state.object_storage, catalog,
                                  self.app.state.companion_sources)
        return (read_snapshot(self.book, catalog, self.settings, source),
                chapter_fractions(self.book, catalog, self.settings, source))

    def close(self):
        self._client.__exit__(None, None, None)


def _answer(response):
    body = response.json()
    if isinstance(body, dict):
        body = {key: value for key, value in body.items() if key != "id"}
    return response.status_code, body


def _view(module_view):
    (point, permitted, state, _manifest, _book, held), fractions = module_view
    return {
        "point": {key: value for key, value in point.items() if key != "id"},
        "permitted": [{key: chapter.get(key) for key in ("ordinal", "title", "text", "whole",
                                                          "recent", "revealed_at")}
                      for chapter in permitted],
        "counter": {key: state[key] for key in ("bookmark", "read_through", "finished_through",
                                                "position_epoch")},
        "held": held,
        "fractions": fractions,
    }


def _run_both(tmp_path, dsn, blob, sequence):
    local = LocalReader(tmp_path, blob)
    hosted = HostedReader(tmp_path, dsn, blob)
    try:
        answers = []
        for method, suffix, body, expected in sequence:
            here, there = local.request(method, suffix, body), hosted.request(method, suffix, body)
            assert _answer(here) == _answer(there), (method, suffix, body)
            assert here.status_code == expected, (method, suffix, here.text)
            answers.append(_answer(there))
        local_view, hosted_view = _view(local.module_view()), _view(hosted.module_view())
        assert local_view == hosted_view
        return answers, hosted_view
    finally:
        local.close()
        hosted.close()


def test_the_same_reading_gives_the_same_read_map_locally_and_hosted(tmp_path, postgres_database):
    """The break it catches: a hosted adapter that stores, merges, clears or seeds ranges
    differently from the local catalog (so hosted Mothli would be given more, or other, text than
    local Mothli for the same reading), or hosted chapter text that verifies a page local mode
    refuses. Covers jumping around, a page across two chapters, a skim, "I've read this" and
    "I haven't read this", and the refused reports (not neighbours, backwards, a stale pass, an
    unknown chapter)."""
    sequence = [
        ("GET", "/read-frontier", None, 200),
        ("PUT", "/read-page", page(2, 30, 34), 200),
        ("PUT", "/read-page", page(1, 5, 6), 200),
        ("PUT", "/read-page", page(1, 7, 8), 200),
        ("PUT", "/read-page", page(3, 10, 12, skimmed=True), 200),
        ("PUT", "/read-page", page(1, 1, 60, to_chapter=3), 422),
        ("PUT", "/read-page", page(2, 9, 3), 422),
        ("PUT", "/read-page", {"start": anchor(1, 9, after=False, epoch=1),
                               "end": anchor(1, 10, after=True, epoch=1)}, 409),
        ("PUT", "/read-chapters", {"atoms": [3], "read": True}, 200),
        ("GET", "/read-map", None, 200),
        ("PUT", "/read-chapters", {"atoms": [3], "read": False}, 200),
        ("PUT", "/read-page", page(1, 58, 2, to_chapter=2), 200),
        ("PUT", "/read-page", page(1, 1, 60), 200),
        ("PUT", "/read-chapters", {"atoms": [9], "read": True}, 422),
        ("PUT", "/read-chapters", {"atoms": [2], "read": True, "source": "import"}, 200),
        ("PUT", "/read-frontier", anchor(3, 1, after=True), 200),
        ("GET", "/read-map", None, 200),
        ("GET", "/read-frontier", None, 200),
    ]
    _answers, view = _run_both(tmp_path, postgres_database, lamp_book(), sequence)
    # A sanity floor under the comparison: the reading did land, and only what was read.
    assert view["counter"]["finished_through"] == 2
    assert [chapter["ordinal"] for chapter in view["permitted"]] == [1, 2]
    assert line(3, 10) not in " ".join(chapter["text"] for chapter in view["permitted"])


def test_pages_of_no_chapter_count_for_nothing_in_both_modes(tmp_path, postgres_database):
    """The break it catches: a page of the cover or of Project Gutenberg's own page (its header,
    which the server keeps out of every chapter, or the book's contents) refused in one mode, or
    counted as read text of a chapter in either."""
    def mark(atom, quote, prefix=""):
        return {"atom": atom, "quote": quote, "prefix": prefix, "cfi": "epubcfi(/6/4)",
                "position_epoch": 0}

    opening = "CHAPTER I. Down the Lamp-Hole " + alice_line(1, 1)
    sequence = [
        ("GET", "/read-map", None, 200),
        ("GET", "/read-frontier", None, 200),
        ("PUT", "/read-page", {"start": mark(2, HEADER), "end": mark(2, HEADER, HEADER)}, 200),
        ("PUT", "/read-page", {"start": mark(2, CONTENTS), "end": mark(2, CONTENTS, CONTENTS)},
         200),
        ("GET", "/read-map", None, 200),
        ("GET", "/read-frontier", None, 200),
        ("PUT", "/read-page", {"start": mark(3, opening),
                               "end": mark(3, alice_line(1, 20), alice_line(1, 20))}, 200),
        ("GET", "/read-map", None, 200),
        ("GET", "/read-frontier", None, 200),
    ]
    answers, view = _run_both(tmp_path, postgres_database, alice_shaped_epub(), sequence)
    # The two front-matter pages changed nothing: map and point as when the book was opened.
    assert answers[4] == answers[0] and answers[5] == answers[1]
    assert answers[0][1]["chapters"] == [{"atom": 3, "read": 0.0}, {"atom": 4, "read": 0.0},
                                         {"atom": 5, "read": 0.0}]
    assert [chapter["ordinal"] for chapter in view["permitted"]] == [3]
    text = view["permitted"][0]["text"]
    assert HEADER not in text and CONTENTS not in text and alice_line(1, 21) not in text
