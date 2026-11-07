"""Semantic content never moves saved EPUB reading coordinates."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from _epub import epub_ncx
from app.config import Settings
from app.content_scope import chapter_numbers, is_content
from app.main import create_app
from app.memory.dal import BookmarkView
from test_ingest import InlineExecutor
from test_views import env as env


@pytest.mark.parametrize("title", ["Acknowledgements", "List of Maps", "Map: Paris", "Index",
                                   "The Bonaparte Family Trees", "Notes and references"])
def test_supplementary_headings_are_not_companion_content(title):
    assert not is_content({"title": title})


@pytest.mark.parametrize("title", ["Introduction", "Preface", "Map of our lives", "Index of fear",
                                   "Notes from underground", "Contents of the letter", "序章"])
def test_substantive_and_unfamiliar_headings_are_kept(title):
    assert is_content({"title": title})


def test_chapter_numbers_ignore_front_matter_and_repeat_spine_parts():
    titles = ["List of Maps", "Introduction", "1: Corsica", "1: Corsica", "2: Revolution"]
    manifest = {"atoms": [{"ordinal": i, "title": t} for i, t in enumerate(titles, 1)]}
    assert chapter_numbers(manifest) == {3: 1, 4: 1, 5: 2}


def test_fresh_supplementary_material_keeps_receipts_and_text_without_ai_facts(tmp_path):
    settings = Settings(_env_file=None, allow_stub=True, data_dir=str(tmp_path / "data"))
    app = create_app(settings, ingest_executor=InlineExecutor())
    source = epub_ncx([
        ("a.xhtml", "List of Maps", "List of Maps", "Zebediah drew the maps. " * 30),
        ("b.xhtml", "Chapter I", "Chapter I", "Aldric the smith crossed the valley. " * 30),
        ("c.xhtml", "Chapter II", "Chapter II", "Corvus has an unread secret. " * 30),
    ])
    with TestClient(app) as api:
        bid = api.post("/api/books", files={"file": ("a.epub", source)}).json()["book_id"]
        manifest = json.loads((Path(settings.data_dir) / "books" / bid / "atoms.json").read_text())
        offset = manifest["atoms"][0]["char_len"] + 2
        assert api.put(f"/api/books/{bid}/position", json={"offset": offset, "cfi": "saved"}).status_code == 200
        assert api.get(f"/api/books/{bid}/ingest").json()["ingest_progress"] == 1
        assert api.get(f"/api/books/{bid}/graph").json()["characters"] == []
        recap = api.get(f"/api/books/{bid}/catch-me-up").json()
        assert recap["recap"] is None and recap["status"] == "paratext_only"
        assert api.get(f"/api/books/{bid}/notes").json()["chapters"] == []
        with app.state.store.book(bid) as mem:
            assert mem.completion_frontier(manifest["atoms"]) == 1
            assert "Zebediah" in mem.view(1).raw_text(manifest["atoms"][0]["key"])
            assert mem.view(1).characters() == []
            assert BookmarkView(mem, bid, 1, excluded_ordinals=(1,)).search([1.0] * 8) == []
        visible = api.get(f"/api/books/{bid}/manifest").json()
        assert visible["atom_set_version"] == manifest["atom_set_version"]
        assert [a["ordinal"] for a in visible["atoms"]] == [1, 2, 3]
        assert visible["content_chapters"] == 2
        assert visible["atoms"][0]["content_role"] == "supplementary"
        assert visible["atoms"][1]["chapter_number"] == 1
        assert visible["atoms"][2]["title"] == ""
        before = app.state.catalog.get_state(bid)
        assert api.post(f"/api/books/{bid}/ingest/retry").status_code == 200
        after = app.state.catalog.get_state(bid)
        assert all(after[k] == before[k] for k in ("bookmark", "cfi", "position_epoch", "ingest_progress"))


def test_legacy_supplementary_facts_hide_without_deleting_raw_evidence(env):
    api, bid, app, atoms = env
    path = Path(app.state.settings.data_dir) / "books" / bid / "atoms.json"
    manifest = json.loads(path.read_text())
    manifest["atoms"][0]["title"] = "Acknowledgements"
    path.write_text(json.dumps(manifest))
    graph = api.get(f"/api/books/{bid}/graph").json()
    assert all(c["canonical_name"] != "Aldric" for c in graph["characters"])
    assert any(c["canonical_name"] == "Berenice" for c in graph["characters"])
    assert [n["revealed_at"] for n in api.get(f"/api/books/{bid}/notes").json()["chapters"]] == [2]
    with app.state.store.book(bid) as mem:
        raw = mem.view(2)
        assert "Aldric" in {c["canonical_name"] for c in raw.characters()}
        filtered = BookmarkView(mem, bid, 2, excluded_ordinals=(1,))
        assert filtered.raw_text(atoms[0]["key"]) is None
        assert all(r["revealed_at"] == 2 for r in filtered.search([1.0] * 8))
        person = next(c["entity_id"] for c in raw.characters() if c["canonical_name"] == "Berenice")
        event = mem.add_event("acknowledges Berenice", 1, 99, participants=[(person, "subject")])
        assert filtered.participants_of(event) == []
        assert raw.participants_of(event)
    assert api.get(f"/api/books/{bid}/position").json()["bookmark"] == 2


@pytest.mark.parametrize("question", ["what happened so far?", "Catch me up", "a quick recap please", "What should I remember so far?"])
def test_recap_questions_reuse_the_reviewed_recap_flow(env, monkeypatch, question):
    api, bid, app, _ = env
    expected = api.get(f"/api/books/{bid}/catch-me-up").json()
    def no_more_model_calls(*args, **kwargs):
        raise AssertionError("cached recap must not call the fact-question generator")
    monkeypatch.setattr(app.state.client, "complete", no_more_model_calls)
    response = api.post(f"/api/books/{bid}/ask", json={"question": question}).json()
    assert response["answer_type"] == "recap" and response["status"] == "ready"
    assert response["recap"] == expected["recap"]
    assert response["claims"] == [] and response["cost"] is None


@pytest.mark.parametrize(("worker_status", "expected"), [
    ("running", "preparing"), ("error", "preparation_failed"),
    ("blocked", "preparation_failed"), ("idle", "preparation_failed"),
])
def test_pending_preparation_does_not_claim_missing_book_evidence(env, monkeypatch, worker_status, expected):
    api, bid, app, _ = env
    state = app.state.catalog.get_state(bid)
    monkeypatch.setattr(app.state.catalog, "get_state", lambda _: {**state, "ingest_progress": 1})
    monkeypatch.setattr(app.state.worker, "status", lambda _: {"status": worker_status})
    monkeypatch.setattr(app.state.client, "complete", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no AI call needed")))
    response = api.post(f"/api/books/{bid}/ask", json={"question": "Why?"}).json()
    assert response["status"] == expected and response["claims"] == []
    assert response["cost"]["calls"] == []


def test_retry_enqueues_only_the_saved_frontier(env, monkeypatch):
    api, bid, app, _ = env
    seen = []
    before = app.state.catalog.get_state(bid)
    monkeypatch.setattr(app.state.worker, "enqueue", lambda *args: seen.append(args))
    assert api.post(f"/api/books/{bid}/ingest/retry").status_code == 200
    assert seen == [(bid, 2)]
    assert app.state.catalog.get_state(bid) == before
    assert api.post("/api/books/unknown/ingest/retry").status_code == 404


def test_image_only_atoms_are_not_meaningful_companion_content():
    assert not is_content({"title": "(untitled)", "char_len": 1})
    assert not is_content({"title": "Illustration", "text": ""})


def test_hidden_supplementary_atoms_still_participate_in_geometry_validation(env):
    api, bid, app, _ = env
    path = Path(app.state.settings.data_dir) / "books" / bid / "atoms.json"
    manifest = json.loads(path.read_text())
    manifest["atoms"][0]["title"] = "Acknowledgements"
    from app.ingest.manifest import _version
    manifest["atoms"][0]["key"] = "wrong-key"
    manifest["atom_set_version"] = _version(manifest["atoms"])
    path.write_text(json.dumps(manifest))
    assert api.get(f"/api/books/{bid}/graph").status_code == 409


def test_answer_cache_changes_when_content_scope_changes(env):
    from app.answer_bank import answer_scope
    api, bid, app, _ = env
    manifest = api.get(f"/api/books/{bid}/manifest").json()
    def key():
        return answer_scope(bid, 2, app.state.catalog.get_state(bid), manifest,
                            catalog=app.state.catalog, store=app.state.store,
                            settings=app.state.settings, client=app.state.reader_client)
    before = key()
    manifest["atoms"][0]["title"] = "Acknowledgements"
    assert key() != before


def test_background_preparation_can_call_the_real_updated_answer_route(env, monkeypatch):
    from types import SimpleNamespace
    import app.api.views as views
    from app.reader_prepare import PreparationScope, prepare
    _, bid, app, _ = env
    app.state.settings.reader_prepare_enabled = app.state.settings.reader_answer_cache = True
    scope = PreparationScope.capture(app, bid, app.state.catalog.get_book(bid)["incarnation"])
    def plan(*args, **kwargs):
        if kwargs["schema"].__name__ == "PreparationReview":
            data = json.loads(kwargs["user"])
            return SimpleNamespace(value={"audits": [{"question_id": q["id"],
                "safe_to_prepare": True, "basis_ids": [1]} for q in data["questions"]]})
        return SimpleNamespace(value={"questions": [{"question": "Who is Berenice?",
            "kind": "character", "basis_ids": [1], "follow_up_to": None}]})
    actual = views.ask_the_book
    responses = []
    def answer(*args, **kwargs):
        value = actual(*args, **kwargs)
        responses.append(value)
        return value
    monkeypatch.setattr("app.reader_prepare.budgeted_completion", plan)
    monkeypatch.setattr(views, "ask_the_book", answer)
    prepare(app, scope)
    assert len(responses) == 1 and responses[0]["as_of_chapter"] == 2


def test_illustrations_do_not_split_a_publisher_chapter_but_new_parts_do():
    manifest = {"atoms": [
        {"ordinal": 1, "title": "Chapter I", "part_label": "Part One"},
        {"ordinal": 2, "title": "An illustration", "char_len": 1},
        {"ordinal": 3, "title": "Chapter I", "part_label": "Part One"},
        {"ordinal": 4, "title": "Chapter I", "part_label": "Part Two"},
    ]}
    assert chapter_numbers(manifest) == {1: 1, 3: 1, 4: 2}
