"""Reading history from Kobo and Apple Books database files and from a Readwise account.

The database files are someone else's SQLite: they are read defensively and only for known columns.
Readwise is never contacted here: the fetcher runs against an in-memory transport."""
import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient

from app import history_sources
from app.api import history as history_api
from app.config import Settings
from app.main import create_app


@pytest.fixture
def c(tmp_path):
    settings = Settings(_env_file=None, allow_stub=True, data_dir=str(tmp_path / "data"))
    with TestClient(create_app(settings)) as client:
        yield client


def _db(path, script):
    conn = sqlite3.connect(path)
    conn.executescript(script)
    conn.commit()
    conn.close()
    return path.read_bytes()


def kobo(tmp_path):
    return _db(tmp_path / "KoboReader.sqlite", """
        CREATE TABLE content (ContentID TEXT, ContentType INTEGER, Title TEXT, Attribution TEXT,
                              ReadStatus INTEGER, DateLastRead TEXT, ISBN TEXT);
        CREATE TABLE Bookmark (BookmarkID TEXT, VolumeID TEXT, ContentID TEXT, Text TEXT, Annotation TEXT,
                               DateCreated TEXT, ChapterProgress REAL, Type TEXT);
        INSERT INTO content VALUES ('vol1', 6, 'Pride and Prejudice', 'Jane Austen', 2, '2022-03-04T10:00:00Z', '9780141439518');
        INSERT INTO content VALUES ('vol2', 6, 'Piranesi', 'Susanna Clarke', 1, '2024-01-02T10:00:00Z', NULL);
        INSERT INTO content VALUES ('vol3', 6, 'Unopened', 'Somebody', 0, NULL, NULL);
        INSERT INTO content VALUES ('vol1!ch1', 9, 'Chapter 1', NULL, 0, NULL, NULL);
        INSERT INTO Bookmark VALUES ('b1', 'vol1', 'vol1!ch1', 'It is a truth universally acknowledged', NULL, '2022-03-01T09:00:00.000', 0.1, 'highlight');
        INSERT INTO Bookmark VALUES ('b2', 'vol1', 'vol1!ch1', 'My dear Mr. Bennet', 'So it begins', '2022-03-01T09:05:00.000', 0.2, 'note');
        INSERT INTO Bookmark VALUES ('b3', 'vol2', 'vol2!ch1', NULL, NULL, '2024-01-01T09:00:00.000', 0.5, 'dogear');
        INSERT INTO Bookmark VALUES ('b4', 'gone', 'gone!ch1', 'orphan', NULL, '2020-01-01T09:00:00.000', 0.5, 'highlight');
    """)


def apple(tmp_path):
    library = _db(tmp_path / "BKLibrary-1.sqlite", """
        CREATE TABLE ZBKLIBRARYASSET (ZASSETID TEXT, ZTITLE TEXT, ZAUTHOR TEXT, ZISFINISHED INTEGER,
                                      ZDATEFINISHED REAL, ZREADINGPROGRESS REAL);
        INSERT INTO ZBKLIBRARYASSET VALUES ('A1', 'Dracula', 'Bram Stoker', 1, 700000000, 1.0);
        INSERT INTO ZBKLIBRARYASSET VALUES ('A2', 'Middlemarch', 'George Eliot', 0, NULL, 0.4);
        INSERT INTO ZBKLIBRARYASSET VALUES ('A3', 'A Sample', 'Nobody', 0, NULL, 0.0);
    """)
    marks = _db(tmp_path / "AEAnnotation_v1.sqlite", """
        CREATE TABLE ZAEANNOTATION (ZANNOTATIONASSETID TEXT, ZANNOTATIONSELECTEDTEXT TEXT, ZANNOTATIONNOTE TEXT,
                                    ZANNOTATIONCREATIONDATE REAL, ZANNOTATIONDELETED INTEGER, ZPLLOCATIONRANGESTART INTEGER);
        INSERT INTO ZAEANNOTATION VALUES ('A1', 'I am Dracula; and I bid you welcome', 'Chilling', 699000000, 0, 12);
        INSERT INTO ZAEANNOTATION VALUES ('A1', 'deleted one', NULL, 699000001, 1, 13);
        INSERT INTO ZAEANNOTATION VALUES ('A2', NULL, NULL, 699000002, 0, 5);
        INSERT INTO ZAEANNOTATION VALUES ('ZZ', 'a book no longer in the library', NULL, 699000003, 0, 1);
    """)
    return library, marks


def _upload(c, *files):
    return c.post("/api/history/import-databases", files=[("files", (name, blob, "application/octet-stream")) for name, blob in files])


def test_a_kobo_brings_its_library_progress_and_highlights(c, tmp_path):
    device = kobo(tmp_path)
    result = _upload(c, ("KoboReader.sqlite", device))
    assert result.status_code == 200
    body = result.json()
    assert body["sources"] == [{"source": "kobo", "books": 3, "highlights": 2}]
    assert body["books_added"] == 3 and body["highlights_added"] == 2
    entries = {entry["title"]: entry for entry in c.get("/api/history").json()["entries"]}
    assert entries["Pride and Prejudice"]["status"] == "read" and entries["Pride and Prejudice"]["finished_on"] == "2022-03-04"
    assert entries["Pride and Prejudice"]["classic_id"] == "1342" and entries["Pride and Prejudice"]["sources"] == ["kobo"]
    assert entries["Piranesi"]["status"] == "reading" and entries["Unopened"]["status"] is None
    marks = c.get(f"/api/history/{entries['Pride and Prejudice']['id']}").json()["marks"]
    assert [(mark["text"], mark["note"]) for mark in marks] == [
        ("It is a truth universally acknowledged", None), ("My dear Mr. Bennet", "So it begins")]
    # the same file again changes nothing, and no temporary copy is left behind
    assert _upload(c, ("KoboReader.sqlite", device)).json()["highlights_added"] == 0
    assert not [name for name in (tmp_path / "data").iterdir() if name.name.startswith("history-")]


def test_apple_books_needs_both_files_and_joins_them(c, tmp_path):
    library, marks = apple(tmp_path)
    alone = _upload(c, ("AEAnnotation_v1.sqlite", marks))
    assert alone.status_code == 422 and "BKLibrary" in alone.json()["detail"]
    body = _upload(c, ("AEAnnotation_v1.sqlite", marks), ("BKLibrary-1.sqlite", library)).json()
    assert body["sources"] == [{"source": "apple", "books": 3, "highlights": 1}]
    entries = {entry["title"]: entry for entry in c.get("/api/history").json()["entries"]}
    assert entries["Dracula"]["status"] == "read" and entries["Dracula"]["finished_on"] == "2023-03-08"
    assert entries["Middlemarch"]["status"] == "reading" and entries["A Sample"]["status"] is None
    mark = c.get(f"/api/history/{entries['Dracula']['id']}").json()["marks"][0]
    assert mark["text"].startswith("I am Dracula") and mark["note"] == "Chilling" and mark["made_at"].startswith("2023-02-")


def test_files_that_are_not_reading_databases_are_refused(c, tmp_path):
    assert _upload(c, ("notes.sqlite", b"hello, I am not a database")).status_code == 422
    other = _db(tmp_path / "other.sqlite", "CREATE TABLE secrets (x TEXT); INSERT INTO secrets VALUES ('y');")
    refused = _upload(c, ("other.sqlite", other))
    assert refused.status_code == 422 and "isn’t from Kobo or Apple Books" in refused.json()["detail"]
    broken = kobo(tmp_path)[:2048]
    assert _upload(c, ("KoboReader.sqlite", broken)).status_code == 422
    assert c.post("/api/history/import-databases", content=b"x", headers={"content-type": "multipart/form-data; boundary=x"}).status_code == 422
    assert c.get("/api/history").json()["books"] == 0


def test_a_database_cannot_run_code_or_reach_other_files(c, tmp_path):
    # a view standing in for a table, and a trigger, are the usual tricks; only plain reads happen
    hostile = _db(tmp_path / "hostile.sqlite", """
        CREATE TABLE real_content (ContentID TEXT, ContentType INTEGER, Title TEXT);
        INSERT INTO real_content VALUES ('v', 6, 'Hostile');
        CREATE VIEW content AS SELECT ContentID, ContentType, Title FROM real_content;
        CREATE TABLE Bookmark (VolumeID TEXT, Text TEXT);
    """)
    result = _upload(c, ("KoboReader.sqlite", hostile))
    assert result.status_code == 422  # "content" is not a table in this file
    assert c.get("/api/history").json()["books"] == 0


PAGES = {
    None: {"count": 3, "nextPageCursor": "c2", "results": [
        {"title": "Deep Work", "author": "Cal Newport", "category": "books", "highlights": [
            {"text": "Clarity about what matters", "note": "", "location": 900, "location_type": "location",
             "highlighted_at": "2024-03-04T07:30:00.000Z"},
            {"text": "", "note": "", "location": 1, "location_type": "location"}]},
        {"title": "Some blog post", "author": "A Blogger", "category": "articles", "highlights": [{"text": "not a book"}]}]},
    "c2": {"count": 3, "nextPageCursor": None, "results": [
        {"title": "Pride and Prejudice", "author": "Jane Austen", "category": "books", "highlights": [
            {"text": "It is a truth", "note": "famous", "location": 3, "location_type": "page", "highlighted_at": None}]}]},
}


def test_readwise_is_paged_through_with_the_readers_token_and_books_only():
    seen = []

    def handler(request):
        seen.append((request.url.host, request.url.path, request.url.params.get("pageCursor"), request.headers["authorization"]))
        return httpx.Response(200, json=PAGES[request.url.params.get("pageCursor")])

    client = httpx.Client(transport=httpx.MockTransport(handler))
    books, highlights = history_sources.fetch_readwise("tok_" + "a" * 30, client=client)
    assert seen == [("readwise.io", "/api/v2/export/", None, "Token tok_" + "a" * 30),
                    ("readwise.io", "/api/v2/export/", "c2", "Token tok_" + "a" * 30)]
    assert [book["title"] for book in books] == ["Deep Work", "Pride and Prejudice"]
    assert highlights == [
        {"title": "Deep Work", "author": "Cal Newport", "text": "Clarity about what matters", "note": None,
         "location": "Location 900", "position": 900, "made_at": "2024-03-04T07:30:00"},
        {"title": "Pride and Prejudice", "author": "Jane Austen", "text": "It is a truth", "note": "famous",
         "location": "Page 3", "position": 3, "made_at": None}]


@pytest.mark.parametrize("status, words", [(401, "didn’t accept"), (429, "slow down"), (500, "isn’t answering")])
def test_readwise_problems_are_explained(status, words):
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(status)))
    with pytest.raises(history_sources.UnreadableSource, match=words):
        history_sources.fetch_readwise("t" * 40, client=client)


def test_the_readwise_route_imports_and_never_keeps_the_token(c, tmp_path, monkeypatch, caplog):
    token = "SECRETtoken" + "9" * 30
    used = []

    def fake(given, *, client=None):
        used.append(given)
        return ([{"title": "Deep Work", "author": "Cal Newport"}],
                [{"title": "Deep Work", "author": "Cal Newport", "text": "Clarity", "note": None, "location": None,
                  "position": 1, "made_at": None}])

    monkeypatch.setattr(history_api, "fetch_readwise", fake)
    body = c.post("/api/history/readwise", json={"token": token}).json()
    assert used == [token] and body["books_added"] == 1 and body["highlights_added"] == 1
    assert body["sources"] == [{"source": "readwise", "books": 1, "highlights": 1}]
    assert c.get("/api/history").json()["entries"][0]["sources"] == ["readwise"]
    for path in (tmp_path / "data").rglob("*"):
        if path.is_file():
            assert token.encode() not in path.read_bytes()
    assert token not in caplog.text
    assert c.post("/api/history/readwise", json={"token": "short"}).status_code == 422
    assert c.post("/api/history/readwise", json={"token": "has spaces and/slashes " * 2}).status_code == 422

    def refused(given, *, client=None):
        raise history_sources.UnreadableSource("Readwise didn’t accept that access token.")

    monkeypatch.setattr(history_api, "fetch_readwise", refused)
    failed = c.post("/api/history/readwise", json={"token": token})
    assert failed.status_code == 502 and "didn’t accept" in failed.json()["detail"]
