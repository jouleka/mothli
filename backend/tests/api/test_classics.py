"""Pick a classic: the curated public-domain catalogue and its one bounded outbound download.

The network is never touched here: the download is replaced with a synthetic EPUB, and the
fetcher itself is exercised against an in-memory transport."""
from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from _epub import three_chapter_book
from app import classics
from app.api import classics as classics_api
from app.config import Settings
from app.main import create_app


@pytest.fixture
def env(tmp_path, monkeypatch):
    settings = Settings(_env_file=None, allow_stub=True, data_dir=str(tmp_path / "data"))
    calls = []

    def fake_fetch(entry, *, max_bytes, client=None):
        calls.append(entry["id"])
        return three_chapter_book()

    monkeypatch.setattr(classics_api, "fetch_classic", fake_fetch)
    with TestClient(create_app(settings)) as c:
        yield c, calls


def test_catalogue_lists_only_long_public_domain_books(env):
    c, _calls = env
    body = c.get("/api/classics").json()
    assert body["enabled"] is True and body["source"] == "Project Gutenberg"
    titles = {book["title"] for book in body["books"]}
    assert "Pride and Prejudice" in titles and len(body["books"]) >= 30
    assert all(book["book_id"] is None for book in body["books"])
    # nothing internal (source ids, death years) is exposed to the page
    assert set(body["books"][0]) == {"id", "title", "author", "year", "book_id"}


def test_an_entry_is_withheld_until_a_century_after_its_last_contributor_died():
    conrad = next(entry for entry in classics._raw_catalog() if entry["id"] == "heart-of-darkness")
    assert conrad["died"] == 1924
    assert classics.find_classic("heart-of-darkness", today=datetime(2024, 12, 31, tzinfo=UTC)) is None
    assert classics.find_classic("heart-of-darkness", today=datetime(2025, 1, 1, tzinfo=UTC)) is not None
    ids = [entry["id"] for entry in classics._raw_catalog()]
    assert len(ids) == len(set(ids))


def test_adding_a_classic_shelves_it_like_any_upload_and_marks_it_in_the_list(env):
    c, calls = env
    added = c.post("/api/classics/pride-and-prejudice")
    assert added.status_code == 201
    book_id = added.json()["book_id"]
    assert calls == ["pride-and-prejudice"]
    shelf = c.get("/api/books").json()
    assert [(book["book_id"], book["source"]) for book in shelf] == [(book_id, "gutenberg:1342")]
    assert c.get(f"/api/books/{book_id}/manifest").status_code == 200
    listed = {book["id"]: book["book_id"] for book in c.get("/api/classics").json()["books"]}
    assert listed["pride-and-prejudice"] == book_id and listed["emma"] is None


def test_a_classic_already_on_the_shelf_is_not_downloaded_again(env):
    c, calls = env
    assert c.post("/api/classics/emma").status_code == 201
    again = c.post("/api/classics/emma")
    assert again.status_code == 409 and "already imported" in again.json()["detail"]
    assert calls == ["emma"]


def test_only_catalogue_ids_can_be_requested(env):
    c, calls = env
    for bad in ("not-a-book", "1342", "..%2f..%2fetc", "https:%2F%2Fexample.org%2Fbook.epub"):
        assert c.post(f"/api/classics/{bad}").status_code == 404
    assert calls == []


def test_a_failed_download_is_reported_without_shelving_anything(env, monkeypatch):
    c, _calls = env

    def unavailable(entry, *, max_bytes, client=None):
        raise classics.ClassicUnavailable("This book isn’t available right now. Try again later.")

    monkeypatch.setattr(classics_api, "fetch_classic", unavailable)
    failed = c.post("/api/classics/dracula")
    assert failed.status_code == 502 and "isn’t available" in failed.json()["detail"]
    assert c.get("/api/books").json() == []


def test_a_download_that_is_not_a_book_is_rejected_by_the_ordinary_import(env, monkeypatch):
    c, _calls = env
    monkeypatch.setattr(classics_api, "fetch_classic", lambda entry, *, max_bytes, client=None: b"<html>blocked</html>")
    rejected = c.post("/api/classics/dracula")
    assert rejected.status_code == 422 and "not a readable EPUB" in rejected.json()["detail"]
    assert c.get("/api/books").json() == []


def test_classics_can_be_switched_off(tmp_path, monkeypatch):
    settings = Settings(_env_file=None, allow_stub=True, data_dir=str(tmp_path / "data"), classics_enabled=False)
    monkeypatch.setattr(classics_api, "fetch_classic", lambda *a, **k: pytest.fail("must not download"))
    with TestClient(create_app(settings)) as c:
        assert c.get("/api/classics").json() == {"enabled": False, "source": "Project Gutenberg", "books": []}
        assert c.post("/api/classics/emma").status_code == 404


ENTRY = {"id": "emma", "pg": 158}


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)


def test_fetch_follows_the_source_redirect_and_returns_the_file():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if request.url.path == "/ebooks/158.epub3.images":
            return httpx.Response(302, headers={"location": "/cache/epub/158/pg158-images-3.epub"})
        return httpx.Response(200, content=b"EPUBBYTES")

    assert classics.fetch_classic(ENTRY, max_bytes=1000, client=_client(handler)) == b"EPUBBYTES"
    assert seen == ["https://www.gutenberg.org/ebooks/158.epub3.images",
                    "https://www.gutenberg.org/cache/epub/158/pg158-images-3.epub"]


@pytest.mark.parametrize("location", [
    "https://evil.example/book.epub",
    "http://www.gutenberg.org/cache/epub/158/pg158.epub",
    "https://www.gutenberg.org:8443/x.epub",
    "https://user@www.gutenberg.org/x.epub",
    "http://169.254.169.254/latest/meta-data/",
    "file:///etc/passwd",
])
def test_fetch_refuses_to_follow_a_redirect_off_the_source(location):
    requests = []

    def handler(request):
        requests.append(str(request.url))
        return httpx.Response(302, headers={"location": location})

    with pytest.raises(classics.ClassicUnavailable, match="somewhere unexpected"):
        classics.fetch_classic(ENTRY, max_bytes=1000, client=_client(handler))
    assert len(requests) == 1


def test_fetch_is_bounded_in_size_hops_and_failure():
    big = _client(lambda request: httpx.Response(200, content=b"x" * 2000))
    with pytest.raises(classics.ClassicUnavailable, match="too large"):
        classics.fetch_classic(ENTRY, max_bytes=1000, client=big)

    loop = _client(lambda request: httpx.Response(302, headers={"location": "/ebooks/158.epub3.images"}))
    with pytest.raises(classics.ClassicUnavailable, match="isn’t available"):
        classics.fetch_classic(ENTRY, max_bytes=1000, client=loop)

    for status in (403, 404, 429, 500):
        with pytest.raises(classics.ClassicUnavailable, match="isn’t available"):
            classics.fetch_classic(ENTRY, max_bytes=1000, client=_client(lambda request, s=status: httpx.Response(s)))

    def offline(request):
        raise httpx.ConnectError("no route", request=request)

    with pytest.raises(classics.ClassicUnavailable, match="Couldn’t reach"):
        classics.fetch_classic(ENTRY, max_bytes=1000, client=_client(offline))
