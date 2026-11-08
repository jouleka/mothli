"""Exact source-prefix, persistence, reset, cache and no-embedding boundaries; no live model."""
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from _epub import epub_ncx
from app.config import Settings
from app.main import create_app
from test_ingest import InlineExecutor


@pytest.fixture
def page_env(tmp_path):
    settings = Settings(_env_file=None, allow_stub=True, reader_page_context=True,
        reader_answer_cache=True, reader_conversation_pipeline=True, data_dir=str(tmp_path / "data"))
    app = create_app(settings, ingest_executor=InlineExecutor())
    with TestClient(app) as api:
        text = ("Mara put the cobalt key in a red tin. Jonas waited beside the forge. "
                + "The wind crossed the valley. " * 150
                + "A cart rattled past the forge. "
                + "The wind crossed the valley. " * 150
                + "UNREAD_CANARY_CORVUS poisoned Mara at midnight. ")
        epub = epub_ncx([("one.xhtml", "Chapter I", "Chapter I", text),
                         ("two.xhtml", "Chapter II", "Chapter II", "FUTURE_CHAPTER_CANARY. " * 150)])
        bid = api.post("/api/books", files={"file": ("b.epub", epub, "application/epub+zip")}).json()["book_id"]
        yield api, bid, app, settings


def anchor(quote="Jonas waited beside the forge.", **overrides):
    return {"atom": 1, "quote": quote, "prefix": quote, "cfi": "epubcfi(/6/2!/4/2/1:60)",
            "position_epoch": 0, **overrides}


def confirm(api, bid, value=None):
    response = api.put(f"/api/books/{bid}/read-frontier", json=value or anchor())
    assert response.status_code == 200, response.text
    return response.json()


def test_current_chapter_prefix_is_available_before_any_chapter_extraction(page_env, monkeypatch):
    api, bid, app, _ = page_env
    assert api.get(f"/api/books/{bid}/position").json()["bookmark"] == 0
    point = confirm(api, bid)
    assert point["available_chapter"] == 1 and point["offset"] > 0
    calls = []
    client = app.state.client
    client.provider = "openai-compatible"
    monkeypatch.setattr(client, "embed", lambda _: pytest.fail("source-prefix chat must not call embeddings"))

    def complete(system, user, tier="cheap", schema=None):
        calls.append(user)
        assert "UNREAD_CANARY" not in user and "FUTURE_CHAPTER_CANARY" not in user
        if schema.__name__ == "ConversationDraft":
            source_id = int(re.search(r"SOURCE \[(\d+)\]", user)[1])
            result = {"clarification": None, "insufficient_evidence": False, "claims": [
                {"text": "Mara put the cobalt key in a red tin.", "citation_ids": [source_id]}]}
        elif schema.__name__ == "ConversationVerdict":
            result = {"references_future": False, "unsupported_claims": [], "missing_details": []}
        else:
            packet = json.loads(user.split("\n", 1)[1])
            result = {"claim_audits": [{"claim_id": c["claim_id"], "units": [
                {"unit_id": u["unit_id"], "supported": True, "diagnostic": None,
                 "evidence": [{"source_id": c["sources"][0]["source_id"], "passage_ids": [1]}]}
                for u in c["units"]]} for c in packet["claims"]]}
        return result, {"in": 10, "out": 10}
    monkeypatch.setattr(client, "complete", complete)
    body = {"question": "Where is the cobalt key?", "bookmark": 0, "read_frontier": point}
    response = api.post(f"/api/books/{bid}/ask", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["read_frontier"] == point
    assert "red tin" in response.text and "UNREAD_CANARY" not in response.text
    paid = len(calls)
    assert api.post(f"/api/books/{bid}/ask", json=body).json()["cached"]
    assert len(calls) == paid
    newer = confirm(api, bid, anchor("A cart rattled past the forge."))
    assert newer["offset"] > point["offset"] and newer["id"] != point["id"]
    # Old in-flight snapshots stay narrow and keep their own cache namespace.
    assert api.post(f"/api/books/{bid}/ask", json=body).json()["read_frontier"] == point
    response = api.post(f"/api/books/{bid}/ask", json={**body, "read_frontier": newer})
    assert response.status_code == 200 and len(calls) > paid
    assert api.get(f"/api/books/{bid}/position").json()["bookmark"] == 0


@pytest.mark.parametrize("value", [anchor("The wind crossed the valley."), anchor("invented quote"),
    anchor(prefix="forged"), anchor(atom=3), anchor(position_epoch=1)])
def test_unverifiable_repeated_future_or_stale_anchors_never_advance(page_env, value):
    api, bid, _, _ = page_env
    before = confirm(api, bid)
    assert api.put(f"/api/books/{bid}/read-frontier", json=value).status_code in (409, 422)
    assert api.get(f"/api/books/{bid}/read-frontier").json() == before


def test_frontier_survives_catalog_reopen_and_reset_invalidates_old_requests(page_env):
    from app.catalog.catalog import Catalog
    api, bid, _, settings = page_env
    point = confirm(api, bid)
    other = Catalog(str(Path(settings.data_dir) / "catalog.db"))
    try:
        assert other.get_conversation_frontier(bid)["source_offset"] == point["offset"]
    finally:
        other.close()
    assert api.post(f"/api/books/{bid}/position/reset", json={"position_epoch": 0}).status_code == 200
    reset = api.get(f"/api/books/{bid}/read-frontier").json()
    assert reset["position_epoch"] == 1 and reset["available_chapter"] == 0
    response = api.post(f"/api/books/{bid}/ask", json={"question": "Where is the key?", "read_frontier": point})
    assert response.status_code == 409 and "red tin" not in response.text


def test_forged_larger_boundary_is_withheld_without_a_model_call(page_env, monkeypatch):
    api, bid, app, _ = page_env
    point = confirm(api, bid)
    monkeypatch.setattr(app.state.client, "complete", lambda *a, **kw: pytest.fail("forged boundary reached model"))
    for forged in ({**point, "offset": point["offset"] + 1}, {**point, "id": "0" * 64}):
        response = api.post(f"/api/books/{bid}/ask", json={"question": "What happened?", "read_frontier": forged})
        assert response.status_code == 409


def test_source_replacement_invalidates_even_an_already_cached_parse(page_env, monkeypatch):
    api, bid, app, settings = page_env
    point = confirm(api, bid)
    source = Path(settings.data_dir) / "books" / bid / "source.epub"
    source.write_bytes(source.read_bytes() + b"source changed")
    monkeypatch.setattr(app.state.client, "complete", lambda *a, **kw: pytest.fail("changed source reached model"))
    assert api.get(f"/api/books/{bid}/read-frontier").status_code == 409
    assert api.post(f"/api/books/{bid}/ask", json={"question":"Why?", "read_frontier":point}).status_code == 409
