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


def test_browsing_by_genre_and_by_mood(env):
    c, _calls = env
    summary = c.get("/api/classics", params={"limit": 1}).json()
    assert {"id": "mystery", "label": "Mystery & crime"} in summary["genres"]
    assert {"id": "laugh", "label": "Make me laugh"} in summary["moods"]
    mystery = c.get("/api/classics", params={"genre": "mystery", "limit": 5}).json()
    assert mystery["books"][0]["title"] == "The Adventures of Sherlock Holmes" and 1000 < mystery["matches"] < 5000
    chills = c.get("/api/classics", params={"mood": "chills", "q": "stoker"}).json()
    assert [book["title"] for book in chills["books"]][:1] == ["Dracula"]
    both = c.get("/api/classics", params={"mood": "quick", "genre": "horror", "limit": 60}).json()
    assert 0 < both["matches"] < mystery["matches"]
    for book in both["books"]:
        detail = c.get(f"/api/classics/{book['id']}").json()
        assert detail["minutes"] and detail["minutes"] <= 110
        assert "horror" in {genre["id"] for genre in detail["genres"]}
    assert c.get("/api/classics", params={"genre": "nonsense"}).json()["books"] == []
    assert c.get("/api/classics", params={"mood": "Robert'); DROP"}).status_code == 422


def test_front_page_has_a_shelf_for_the_most_read_each_mood_and_some_genres(env):
    c, _calls = env
    assert c.post("/api/classics/1342").status_code == 201
    shelves = c.get("/api/classics/front").json()["shelves"]
    assert shelves[0]["kind"] == "popular" and shelves[0]["books"][0]["title"] == "Pride and Prejudice"
    assert shelves[0]["books"][0]["book_id"] is not None
    assert {shelf["id"] for shelf in shelves if shelf["kind"] == "mood"} == {mood.id for mood in classics.MOODS}
    assert all(4 <= len(shelf["books"]) <= classics.SHELF_SIZE for shelf in shelves)
    french = c.get("/api/classics/front", params={"language": "fr"}).json()["shelves"]
    assert french and {book["language"] for shelf in french for book in shelf["books"]} == {"fr"}


def test_a_book_page_has_a_description_a_length_and_its_genres(env):
    c, _calls = env
    detail = c.get("/api/classics/1342").json()
    assert detail["title"] == "Pride and Prejudice" and detail["book_id"] is None
    assert "Elizabeth Bennet" in detail["blurb"] and len(detail["blurb"]) <= 300
    assert 300 < detail["minutes"] < 900
    assert {"id": "romance", "label": "Romance"} in detail["genres"]
    assert detail["source_url"] == "https://www.gutenberg.org/ebooks/1342"
    assert c.get("/api/classics/not-a-book").status_code == 404


JPEG = b"\xff\xd8\xff\xe0" + b"0" * 64


def test_cover_art_is_fetched_once_kept_on_disk_and_served_as_an_image(env, monkeypatch, tmp_path):
    c, _calls = env
    asked = []

    def fake_cover(entry, *, client=None):
        asked.append(entry["pg"])
        return JPEG if entry["pg"] == 1342 else None

    monkeypatch.setattr(classics_api, "fetch_cover", fake_cover)
    classics_api._missing_covers.clear()
    first = c.get("/api/classics/1342/cover")
    assert first.status_code == 200 and first.content == JPEG
    assert first.headers["content-type"] == "image/jpeg" and first.headers["x-content-type-options"] == "nosniff"
    assert "max-age" in first.headers["cache-control"]
    assert c.get("/api/classics/1342/cover").content == JPEG
    assert asked == [1342]
    assert list((tmp_path / "data" / "classics-covers").iterdir())[0].name == "1342.img"
    # a book with no usable cover is a plain 404, and the mirror is not asked again
    assert c.get("/api/classics/345/cover").status_code == 404
    assert c.get("/api/classics/345/cover").status_code == 404
    assert asked == [1342, 345]
    assert c.get("/api/classics/not-a-book/cover").status_code == 404


def test_fetch_cover_accepts_only_a_real_image_from_the_mirror():
    seen = []

    def image(request):
        seen.append(str(request.url))
        return httpx.Response(200, content=JPEG)

    assert classics.fetch_cover(ENTRY, client=_client(image)) == JPEG
    assert seen == ["https://gutenberg.pglaf.org/cache/epub/158/pg158.cover.medium.jpg"]
    assert classics.fetch_cover(ENTRY, client=_client(lambda r: httpx.Response(200, content=b"<html>no</html>"))) is None
    assert classics.fetch_cover(ENTRY, client=_client(lambda r: httpx.Response(404))) is None
    huge = _client(lambda r: httpx.Response(200, content=b"\xff\xd8\xff" + b"0" * (classics.MAX_COVER_BYTES + 1)))
    assert classics.fetch_cover(ENTRY, client=huge) is None
    elsewhere = _client(lambda r: httpx.Response(302, headers={"location": "https://evil.example/x.jpg"}))
    assert classics.fetch_cover(ENTRY, client=elsewhere) is None
