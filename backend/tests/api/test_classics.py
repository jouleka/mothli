"""Pick a classic: the public-domain catalogue and its one bounded outbound download.

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


def test_catalogue_is_the_whole_public_domain_library_most_loved_first(env):
    c, _calls = env
    body = c.get("/api/classics").json()
    assert body["enabled"] is True and body["source"] == "Project Gutenberg"
    assert body["total"] > 50_000 and body["matches"] == body["total"]
    assert len(body["books"]) == 40 and body["books"][0]["title"] == "Pride and Prejudice"
    assert body["languages"][0]["code"] == "en"
    assert all(book["book_id"] is None for book in body["books"])
    # nothing internal (death years, download counts) is exposed to the page
    assert set(body["books"][0]) == {"id", "title", "author", "language", "book_id"}


def test_search_matches_every_word_ignoring_case_and_accents_and_pages(env):
    c, _calls = env
    found = c.get("/api/classics", params={"q": "  BRONTE jane "}).json()
    assert found["books"][0]["title"].startswith("Jane Eyre") and found["matches"] < 40
    assert c.get("/api/classics", params={"q": "zzqqxxnothing"}).json()["books"] == []
    french = c.get("/api/classics", params={"language": "fr", "limit": 5}).json()
    assert len(french["books"]) == 5 and {book["language"] for book in french["books"]} == {"fr"}
    first = c.get("/api/classics", params={"q": "dickens", "limit": 10}).json()
    second = c.get("/api/classics", params={"q": "dickens", "limit": 10, "offset": 10}).json()
    assert first["matches"] == second["matches"] > 20
    assert not {book["id"] for book in first["books"]} & {book["id"] for book in second["books"]}
    for bad in ({"limit": 1000}, {"offset": -1}, {"language": "../x"}, {"q": "x" * 500}):
        assert c.get("/api/classics", params=bad).status_code == 422


def test_united_states_rule_by_default_and_an_optional_death_date_rule():
    # published 1925, author died 1940: public domain in the United States, not yet everywhere
    assert classics.find_classic("64317")["title"] == "The Great Gatsby"
    assert classics.find_classic("64317", min_years_since_death=70, today=datetime(2010, 12, 31, tzinfo=UTC)) is None
    assert classics.find_classic("64317", min_years_since_death=70, today=datetime(2011, 1, 1, tzinfo=UTC)) is not None
    everywhere = classics.catalog_summary(min_years_since_death=100)["total"]
    assert 0 < everywhere < classics.catalog_summary()["total"]
    assert classics.find_classic("1342", min_years_since_death=100) is not None


def test_the_death_date_rule_applies_to_listing_and_adding(tmp_path, monkeypatch):
    settings = Settings(_env_file=None, allow_stub=True, data_dir=str(tmp_path / "data"),
                        classics_min_years_since_death=150)
    monkeypatch.setattr(classics_api, "fetch_classic", lambda *a, **k: pytest.fail("must not download"))
    with TestClient(create_app(settings)) as c:
        assert c.get("/api/classics", params={"q": "great gatsby fitzgerald"}).json()["books"] == []
        assert c.post("/api/classics/64317").status_code == 404
        assert c.get("/api/classics", params={"q": "pride and prejudice"}).json()["books"]


def test_adding_a_classic_shelves_it_like_any_upload_and_marks_it_in_the_list(env):
    c, calls = env
    added = c.post("/api/classics/1342")
    assert added.status_code == 201
    book_id = added.json()["book_id"]
    assert calls == ["1342"]
    shelf = c.get("/api/books").json()
    assert [(book["book_id"], book["source"]) for book in shelf] == [(book_id, "gutenberg:1342")]
    assert c.get(f"/api/books/{book_id}/manifest").status_code == 200
    listed = {book["id"]: book["book_id"] for book in c.get("/api/classics", params={"q": "jane austen"}).json()["books"]}
    assert listed["1342"] == book_id and listed["158"] is None


def test_a_classic_already_on_the_shelf_is_not_downloaded_again(env):
    c, calls = env
    assert c.post("/api/classics/158").status_code == 201
    again = c.post("/api/classics/158")
    assert again.status_code == 409 and "already imported" in again.json()["detail"]
    assert calls == ["158"]


def test_only_catalogue_ids_can_be_requested(env):
    c, calls = env
    for bad in ("not-a-book", "pride-and-prejudice", "0", "01342", "99999999", "1342x", "..%2f..%2fetc", "https:%2F%2Fexample.org%2Fbook.epub"):
        assert c.post(f"/api/classics/{bad}").status_code == 404
    assert calls == []


def test_a_failed_download_is_reported_without_shelving_anything(env, monkeypatch):
    c, _calls = env

    def unavailable(entry, *, max_bytes, client=None):
        raise classics.ClassicUnavailable("This book isn’t available right now. Try again later.")

    monkeypatch.setattr(classics_api, "fetch_classic", unavailable)
    failed = c.post("/api/classics/345")
    assert failed.status_code == 502 and "isn’t available" in failed.json()["detail"]
    assert c.get("/api/books").json() == []


def test_a_download_that_is_not_a_book_is_rejected_by_the_ordinary_import(env, monkeypatch):
    c, _calls = env
    monkeypatch.setattr(classics_api, "fetch_classic", lambda entry, *, max_bytes, client=None: b"<html>blocked</html>")
    rejected = c.post("/api/classics/345")
    assert rejected.status_code == 422 and "not a readable EPUB" in rejected.json()["detail"]
    assert c.get("/api/books").json() == []


def test_classics_can_be_switched_off(tmp_path, monkeypatch):
    settings = Settings(_env_file=None, allow_stub=True, data_dir=str(tmp_path / "data"), classics_enabled=False)
    monkeypatch.setattr(classics_api, "fetch_classic", lambda *a, **k: pytest.fail("must not download"))
    with TestClient(create_app(settings)) as c:
        off = c.get("/api/classics").json()
        assert off["enabled"] is False and off["books"] == [] and off["total"] == 0
        assert c.post("/api/classics/158").status_code == 404


ENTRY = {"id": "158", "pg": 158}
MIRROR = "https://gutenberg.pglaf.org/cache/epub/158/pg158-images-3.epub"


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)


def test_fetch_takes_the_book_from_the_mirror():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, content=b"EPUBBYTES")

    assert classics.fetch_classic(ENTRY, max_bytes=1000, client=_client(handler)) == b"EPUBBYTES"
    assert seen == [MIRROR]


def test_fetch_falls_back_to_the_main_site_and_follows_its_redirect():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if request.url.host == "gutenberg.pglaf.org":
            return httpx.Response(404)
        if request.url.path == "/ebooks/158.epub3.images":
            return httpx.Response(302, headers={"location": "/cache/epub/158/pg158-images-3.epub"})
        return httpx.Response(200, content=b"EPUBBYTES")

    assert classics.fetch_classic(ENTRY, max_bytes=1000, client=_client(handler)) == b"EPUBBYTES"
    assert seen == [MIRROR, "https://www.gutenberg.org/ebooks/158.epub3.images",
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

    loop = _client(lambda request: httpx.Response(302, headers={"location": request.url.path}))
    with pytest.raises(classics.ClassicUnavailable, match="isn’t available"):
        classics.fetch_classic(ENTRY, max_bytes=1000, client=loop)

    for status in (403, 404, 429, 500):
        with pytest.raises(classics.ClassicUnavailable, match="isn’t available"):
            classics.fetch_classic(ENTRY, max_bytes=1000, client=_client(lambda request, s=status: httpx.Response(s)))

    def offline(request):
        raise httpx.ConnectError("no route", request=request)

    with pytest.raises(classics.ClassicUnavailable, match="Couldn’t reach"):
        classics.fetch_classic(ENTRY, max_bytes=1000, client=_client(offline))
