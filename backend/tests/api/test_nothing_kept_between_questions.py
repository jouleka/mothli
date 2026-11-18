"""Nothing is kept between two questions (ADR 0043, R3 to R6).

The local app once had switches that kept a reviewed answer in a file and served it again, wrote
suggested questions and catch-up summaries ahead of time, kept those summaries in a file, and paid
for quicker replies. They are gone. These tests stand in for the spoiler tests that went with them:
whatever an old environment file still says, two identical questions are each answered fresh, and
nothing about them is written to the data folder. Unknown setting names are silently ignored by
``Settings`` (``extra="ignore"``), so a forgotten line cannot switch anything back on.
"""
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from _epub import three_chapter_book
from app.config import Settings
from app.main import create_app
from test_ingest import InlineExecutor
from test_views import env as env

# Every setting the removed speed-ups read, as an old environment file would have set it.
OLD_SETTINGS = {
    "READER_ANSWER_CACHE": "true",
    "READER_REUSE_ENABLED": "true",
    "READER_REUSE_FAST_REVIEW": "true",
    "READER_REUSE_MIN_PROBABILITY": "0.99",
    "READER_REUSE_MIN_CONFIDENCE": "0.9",
    "READER_REUSE_TIMEOUT_SECONDS": "0.65",
    "READER_PREPARE_ENABLED": "true",
    "READER_PREPARE_MAX_QUESTIONS": "8",
    "READER_PREPARE_DELAY_SECONDS": "0",
    "READER_PREPARE_MAX_USD_PER_BOOK": "0.25",
    "RECAP_PREWARM": "true",
    "RECAP_DISK_CACHE": "true",
    "READER_FAST_MODE": "true",
}
KEPT_FILES = ("reviewed-answers-v2.json", "reviewed-recaps-v1.json")
FACT = "Berenice met Aldric at the forge and they spoke."


def _files(root):
    return {str(p.relative_to(root)) for p in Path(root).rglob("*")
            if p.is_file() and not p.name.endswith(("-wal", "-shm", "-journal"))}


@pytest.mark.parametrize("old_settings", [
    pytest.param(False, id="no-old-settings"),
    pytest.param(True, id="every-old-setting"),
])
def test_two_identical_questions_are_each_answered_fresh_and_nothing_is_kept(
        tmp_path, monkeypatch, old_settings):
    for name in OLD_SETTINGS:
        monkeypatch.delenv(name, raising=False)
    if old_settings:
        for name, value in OLD_SETTINGS.items():
            monkeypatch.setenv(name, value)
    settings = Settings(_env_file=None, allow_stub=True, data_dir=str(tmp_path / "data"))
    app = create_app(settings, ingest_executor=InlineExecutor())
    with TestClient(app) as api:
        bid = api.post("/api/books", files={"file": (
            "b.epub", three_chapter_book(), "application/epub+zip")}).json()["book_id"]
        with open(os.path.join(settings.data_dir, "books", bid, "atoms.json"), encoding="utf-8") as f:
            atoms = json.load(f)["atoms"]
        api.put(f"/api/books/{bid}/position",
                json={"cfi": "x", "offset": atoms[0]["char_len"] + atoms[1]["char_len"] + 2})
        client = app.state.reader_client
        client.jev = SimpleNamespace(model="jev-1.14.0", close=lambda: None)
        calls = []
        original = client.complete

        def stand_in(*args, **kwargs):
            fields = getattr(kwargs.get("schema"), "model_fields", {})
            calls.append(sorted(fields))
            if "insufficient_evidence" in fields:
                found = FACT in args[1]
                return {"insufficient_evidence": not found,
                        "claims": [{"text": FACT, "citation_ids": [1]}] if found else []}, {"in": 20, "out": 12}
            if "references_future" in fields:
                return {"references_future": False, "unsupported_claims": []}, {"in": 12, "out": 3}
            return original(*args, **kwargs)

        monkeypatch.setattr(client, "complete", stand_in)
        question = {"question": "Where did Berenice meet Aldric?", "bookmark": 999}
        before = _files(settings.data_dir)

        first = api.post(f"/api/books/{bid}/ask", json=question)
        assert first.status_code == 200, first.text
        first = first.json()
        assert first["claims"] and not first["insufficient_evidence"] and not first.get("cached")
        after_first = len(calls)
        assert after_first > 0

        second = api.post(f"/api/books/{bid}/ask", json=question)
        assert second.status_code == 200, second.text
        second = second.json()
        # Answered fresh: the model stand-in was asked again and nothing was served from the first.
        assert len(calls) > after_first and calls[after_first:] == calls[:after_first]
        assert not second.get("cached") and second["cost"]["calls"]
        assert second["claims"] == first["claims"]
        assert _files(settings.data_dir) == before

        # The catch-up summary keeps only its in-memory cache; nothing goes to a file.
        recap = api.get(f"/api/books/{bid}/catch-me-up")
        assert recap.status_code == 200, recap.text
        for name in KEPT_FILES:
            assert not (Path(settings.data_dir) / name).exists()
        assert app.state.reader_client is app.state.client  # no separately paid "fast" reader
    for name in KEPT_FILES:
        assert not (Path(settings.data_dir) / name).exists()


def test_prepared_questions_route_is_gone(env):
    api, bid, _app, _atoms = env
    assert api.get(f"/api/books/{bid}/prepared-questions").status_code == 404


def test_old_setting_names_are_ignored_and_switch_nothing_on(tmp_path, monkeypatch):
    env_file = tmp_path / "old.env"
    env_file.write_text("".join(f"{name}={value}\n" for name, value in OLD_SETTINGS.items()),
                        encoding="utf-8")
    from_file = Settings(_env_file=str(env_file), allow_stub=True)
    for name, value in OLD_SETTINGS.items():
        monkeypatch.setenv(name, value)
    from_environment = Settings(_env_file=None, allow_stub=True)
    for settings in (from_file, from_environment):
        for name in OLD_SETTINGS:
            assert not hasattr(settings, name.lower()), name
