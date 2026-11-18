"""The free library in hosted mode (ADR 0041 part 11), against real PostgreSQL and the encrypted
filesystem store, through the hosted HTTP app on the RLS-enforced tenant role.

Adding a book goes through the hosted upload's own import, so it is checked against an upload of
the same file: the same rows, the same stored object, no job for either, the same limits. The network
is never reached: the source is an in-memory transport serving a tiny made-up EPUB, and a test
that forgot to give one fails instead of fetching.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from datetime import UTC, datetime

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from app.classics import find_classic
from app.hosted.auth.api import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE
from app.hosted.companion.store import HostedCompanionCatalog
from app.hosted.limits import update_limits
from app.hosted.storage import EPUB_MEDIA_TYPE
from app.hosted.tenant import classics as hosted_classics
from app.hosted.tenant.models import OwnerId
from app.hosted.tenant.repository import PostgresTenantRepository
from app.main import create_app
from tests.api._epub import epub_ncx
from tests.hosted import test_companion_read_map_postgres as read_map_gate
from tests.hosted.test_tenant_postgres import UnusedOIDCClient, _become

pytestmark = pytest.mark.postgres

admin_dsn = read_map_gate.admin_dsn
database = read_map_gate.database
tenant_dsn = read_map_gate.tenant_dsn
readers = read_map_gate.readers

PRIDE, ALICE = "1342", "11"          # two catalogue entries; their files here are made up
MIRROR = "https://gutenberg.pglaf.org/cache/epub/{pg}/pg{pg}-images-3.epub"
MAX_UPLOAD = 400_000                 # the app's upload limit in these tests


def made_up_book(canary: str = "LIBRARY_CANARY") -> bytes:
    return epub_ncx(
        [(f"c{n}.xhtml", f"Part {n}", f"Part {n}",
          " ".join(f"{canary} {n} line {i}." for i in range(1, 31))) for n in (1, 2)],
        title="A Made-Up Classic", author="Nobody Real")


BOOK = made_up_book()


class Source:
    """The catalogue's source, in memory: serves ``files[pg]`` from the mirror, and records every
    request. ``hold`` makes the first fetch wait until released."""

    def __init__(self, monkeypatch):
        self.files: dict[int, object] = {}
        self.seen: list[str] = []
        self.hold: threading.Event | None = None
        self.holding = threading.Event()
        self._lock = threading.Lock()
        monkeypatch.setattr(hosted_classics, "_transport",
                            lambda: httpx.MockTransport(self.handle))

    def handle(self, request: httpx.Request) -> httpx.Response:
        with self._lock:
            self.seen.append(str(request.url))
            hold, self.hold = self.hold, None
        if hold is not None:
            self.holding.set()
            assert hold.wait(10)
        if request.url.host != "gutenberg.pglaf.org":
            return httpx.Response(404)
        pg = int(request.url.path.split("/")[3])
        served = self.files.get(pg, BOOK)
        if callable(served):
            return served(request)
        return httpx.Response(200, content=served)


@pytest.fixture()
def source(monkeypatch) -> Source:
    return Source(monkeypatch)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse():
        pytest.fail("a test reached for the real network")

    monkeypatch.setattr(hosted_classics, "_transport", refuse)


@pytest.fixture()
def library_app(tenant_dsn: str, readers: dict, tmp_path, no_network):
    tokens, sessions = read_map_gate.signed_in(readers)
    objects = tmp_path / "objects"
    app = create_app(
        read_map_gate.hosted_settings(tenant_dsn, objects, epub_max_upload_bytes=MAX_UPLOAD),
        auth_repository=sessions, oidc_client=UnusedOIDCClient(),
        auth_clock=lambda: datetime(2026, 7, 16, 12, tzinfo=UTC))
    with TestClient(app, base_url="https://reader.example") as client:
        yield client, tokens, app, objects


def as_reader(library_app, label: str) -> dict:
    """Headers that sign one reader in for one request, so two readers can ask at once."""
    client, tokens, _app, _objects = library_app
    client.cookies.clear()
    session, csrf = tokens[label]
    return {"cookie": f"{SESSION_COOKIE}={session}; {CSRF_COOKIE}={csrf}", CSRF_HEADER: csrf}


def add(library_app, label: str, classic: str = PRIDE) -> httpx.Response:
    client = library_app[0]
    return client.post(f"/api/classics/{classic}", headers=as_reader(library_app, label))


def get(library_app, label: str, path: str) -> httpx.Response:
    return library_app[0].get(path, headers=as_reader(library_app, label))


def upload(library_app, label: str, blob: bytes) -> httpx.Response:
    client = library_app[0]
    return client.post("/api/books", files={"file": ("mine.epub", blob, EPUB_MEDIA_TYPE)},
                       headers=as_reader(library_app, label))


def rows(dsn: str, query: str, *params) -> list[dict]:
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        return conn.execute(query, params).fetchall()


def held(dsn: str, owner: uuid.UUID) -> dict:
    """Everything an import writes for one owner, read past row-level security."""
    return {
        "books": rows(dsn, "SELECT id,title,author,source_kind,file_hash,deleted_at FROM books "
                           "WHERE owner_id=%s ORDER BY created_at,id", owner),
        "objects": rows(dsn, "SELECT book_id,storage_key,byte_size,sha256,deleted_at "
                             "FROM source_objects WHERE owner_id=%s ORDER BY created_at", owner),
        "jobs": rows(dsn, "SELECT book_id,kind,state,credential_id FROM jobs WHERE owner_id=%s "
                          "ORDER BY created_at", owner),
        "search": rows(dsn, "SELECT book_id,ordinal FROM book_search_documents "
                            "WHERE owner_id=%s ORDER BY book_id,ordinal", owner),
        "state": rows(dsn, "SELECT book_id,bookmark FROM reading_state WHERE owner_id=%s", owner),
    }


def files(objects, owner: uuid.UUID) -> list[str]:
    folder = objects / owner.hex
    return sorted(path.name for path in folder.iterdir()) if folder.exists() else []


def catalogue_card(library_app, label: str, classic: str) -> dict:
    response = get(library_app, label, f"/api/classics/{classic}")
    assert response.status_code == 200, response.text
    return response.json()


# ---- adding a book ------------------------------------------------------------------------------

def test_adding_a_classic_puts_it_on_the_readers_shelf_through_the_upload_path(
        library_app, readers, database, source):
    """The break it catches: an add that writes anything an upload of the same file would not
    (another job state, no search text, a file stored in the clear or under another owner), a
    book the reader cannot open, a classic not marked as theirs, or a second fetch of a book
    already on the shelf."""
    _client, _tokens, _app, objects = library_app
    a = readers["a"]
    assert find_classic(PRIDE) is not None
    assert catalogue_card(library_app, "a", PRIDE)["book_id"] is None

    added = add(library_app, "a")
    assert added.status_code == 201, added.text
    book = added.json()
    assert book["book_id"] == book["id"] and book["title"] == "A Made-Up Classic"
    assert book["author"] == "Nobody Real" and book["atoms"] == 2
    assert source.seen == [MIRROR.format(pg=PRIDE)]
    assert added.headers["cache-control"] == "private, no-store"

    # On the shelf, readable, and the same bytes the source sent.
    shelf = get(library_app, "a", "/api/books").json()
    assert [item["id"] for item in shelf] == [book["id"]]
    epub = get(library_app, "a", f"/api/books/{book['id']}/epub")
    assert epub.status_code == 200 and epub.content == BOOK
    # The catalogue now knows it is theirs, everywhere it shows the book.
    assert catalogue_card(library_app, "a", PRIDE)["book_id"] == book["id"]
    listed = get(library_app, "a", "/api/classics?q=pride+prejudice+austen").json()
    assert {card["id"]: card["book_id"] for card in listed["books"]}[PRIDE] == book["id"]
    assert listed["covers"] is False
    assert get(library_app, "a", "/api/classics/front").json()["covers"] is False

    # Exactly what an upload of the same file writes, but the source tag.
    uploaded = upload(library_app, "a", BOOK)
    assert uploaded.status_code == 201, uploaded.text
    rows_a = held(database, a)
    by_id = {str(row["id"]): row for row in rows_a["books"]}
    library, own = by_id[book["id"]], by_id[uploaded.json()["id"]]
    assert library["source_kind"] == f"gutenberg:{PRIDE}" and own["source_kind"] == "upload"
    assert {key: library[key] for key in ("title", "author", "file_hash")} == {
        key: own[key] for key in ("title", "author", "file_hash")}
    # Neither makes a job (ADR 0043 step 7).
    assert rows_a["jobs"] == [] and "job" not in book and "job" not in uploaded.json()
    assert len([row for row in rows_a["search"] if str(row["book_id"]) == book["id"]]) == 2
    stored = {str(row["book_id"]): row for row in rows_a["objects"]}[book["id"]]
    assert stored["byte_size"] == len(BOOK)
    # The file is the reader's own object, under their own folder, encrypted.
    on_disk = objects / a.hex / f"{stored['storage_key']}.epub.enc"
    assert on_disk.exists() and BOOK not in on_disk.read_bytes()
    assert b"LIBRARY_CANARY" not in on_disk.read_bytes()
    audit = rows(database, "SELECT action FROM audit_events WHERE owner_id=%s AND target_id=%s",
                 a, uuid.UUID(book["id"]))
    assert [row["action"] for row in audit] == ["book.import"]

    # Asking again changes nothing and fetches nothing: the app is told which book it is.
    again = add(library_app, "a")
    assert again.status_code == 409
    assert again.json()["detail"] == {"code": "already_on_shelf", "book_id": book["id"],
                                      "message": hosted_classics.ON_SHELF}
    assert source.seen == [MIRROR.format(pg=PRIDE)]
    assert len(held(database, a)["books"]) == 2

    # Removed from the shelf, it can be added again.
    deleted = library_app[0].delete(f"/api/books/{book['id']}", headers=as_reader(library_app, "a"))
    assert deleted.status_code == 204
    assert catalogue_card(library_app, "a", PRIDE)["book_id"] is None
    assert add(library_app, "a").status_code == 201


def test_only_catalogue_numbers_are_fetched_and_nothing_the_reader_sends_is_followed(
        library_app, readers, database, source):
    """The break it catches: a reader steering the fetch with an id that is not a catalogue
    number (a URL, a path, a host) or with anything else in the request."""
    client = library_app[0]
    for classic in ("https:%2F%2Fevil.example%2Fbook.epub", "evil.example", "0", "-1", "1342.5",
                    "99999999", "1342%20", "%2e%2e"):
        response = client.post(f"/api/classics/{classic}", headers=as_reader(library_app, "a"))
        assert response.status_code in (404, 405), (classic, response.text)
    response = client.post(f"/api/classics/{PRIDE}?url=https://evil.example/x.epub",
                           headers=as_reader(library_app, "a"),
                           json={"url": "https://evil.example/x.epub", "host": "evil.example"})
    assert response.status_code == 201, response.text
    assert source.seen == [MIRROR.format(pg=PRIDE)]
    # A detail or a list never fetches anything.
    get(library_app, "a", f"/api/classics/{ALICE}")
    get(library_app, "a", "/api/classics/front")
    assert source.seen == [MIRROR.format(pg=PRIDE)]


@pytest.mark.parametrize("redirect", ["https://evil.example/pg.epub",
                                      "http://gutenberg.pglaf.org/cache/epub/1342/pg1342.epub",
                                      "http://127.0.0.1:8000/api/books"])
def test_a_redirect_off_the_reviewed_hosts_is_refused_and_adds_nothing(
        library_app, readers, database, source, redirect):
    """The break it catches: a response from the source sending the server to another host, a
    plain-HTTP address or the server's own network, and an add that leaves anything behind."""
    _client, _tokens, _app, objects = library_app
    source.files[int(PRIDE)] = lambda request: httpx.Response(302, headers={"location": redirect})
    response = add(library_app, "a")
    assert response.status_code == 502
    assert response.json()["detail"] == {"code": "source_unavailable",
                                         "message": hosted_classics.UNAVAILABLE}
    assert source.seen == [MIRROR.format(pg=PRIDE)]
    assert held(database, readers["a"]) == {"books": [], "objects": [], "jobs": [],
                                             "search": [], "state": []}
    assert files(objects, readers["a"]) == []


def test_a_failed_too_large_or_unreadable_fetch_leaves_nothing_and_can_be_tried_again(
        library_app, readers, database, source):
    """The break it catches: a failed fetch that leaves a row, a stored file or a job behind, a
    refusal the reader cannot understand, or a reader who cannot simply try again."""
    _client, _tokens, _app, objects = library_app
    a = readers["a"]

    def down(request):
        raise httpx.ConnectError("unreachable", request=request)

    failures = [
        (down, 502, "source_unavailable", hosted_classics.UNAVAILABLE),
        (lambda request: httpx.Response(503), 502, "source_unavailable",
         hosted_classics.UNAVAILABLE),
        (b"x" * (MAX_UPLOAD + 1), 413, "too_large", hosted_classics.TOO_LARGE),
        (b"<html>Sorry, we're down</html>", 422, "not_a_book", hosted_classics.NOT_A_BOOK),
        (epub_ncx([("c1.xhtml", "", "", "")], title="Empty"), 422, "not_a_book",
         hosted_classics.NOT_A_BOOK),
    ]
    for served, status, code, message in failures:
        source.files[int(PRIDE)] = served
        response = add(library_app, "a")
        assert response.status_code == status, (code, response.text)
        assert response.json()["detail"] == {"code": code, "message": message}
        assert held(database, a) == {"books": [], "objects": [], "jobs": [], "search": [],
                                     "state": []}
        assert files(objects, a) == []
    # Nothing went wrong for good: the next try adds the book.
    source.files[int(PRIDE)] = BOOK
    assert add(library_app, "a").status_code == 201
    assert len(held(database, a)["books"]) == 1 and len(files(objects, a)) == 1


def test_a_storage_failure_after_the_fetch_leaves_nothing(library_app, readers, database,
                                                          source, monkeypatch):
    """The break it catches: the add's rows written although the file could not be stored, or a
    stored file left behind when the rows could not be written."""
    _client, _tokens, app, objects = library_app
    a = readers["a"]
    repository = app.state.tenant_repository

    async def refused(*args, **kwargs):
        raise RuntimeError("database went away")

    monkeypatch.setattr(type(repository), "create_uploaded_book", refused)
    response = add(library_app, "a")
    assert response.status_code == 503
    assert response.json()["detail"] == {"code": "not_saved", "message": hosted_classics.NOT_SAVED}
    assert source.seen == [MIRROR.format(pg=PRIDE)]
    assert held(database, a) == {"books": [], "objects": [], "jobs": [], "search": [], "state": []}
    assert files(objects, a) == []


# ---- limits ----------------------------------------------------------------------------------

def refusal(response: httpx.Response) -> tuple[int, dict]:
    detail = dict(response.json()["detail"])
    detail.pop("message", None)
    return response.status_code, detail


def test_the_limits_on_books_and_bytes_refuse_an_add_exactly_as_they_refuse_an_upload(
        library_app, readers, database, source):
    """The break it catches: an add that gets past a limit an upload is held to, refuses with a
    different status or code, fetches from the source for a shelf that is already full, or leaves
    anything behind when the fetched file turns out not to fit."""
    _client, _tokens, _app, objects = library_app
    a = readers["a"]
    assert upload(library_app, "a", made_up_book("FIRST")).status_code == 201
    before = held(database, a)

    # Books: a full shelf is refused before anything is fetched.
    update_limits(database, a, {"max_books": 1})
    by_upload, by_add = upload(library_app, "a", BOOK), add(library_app, "a")
    assert refusal(by_add) == refusal(by_upload)
    assert refusal(by_add) == (409, {"code": "book_quota_exceeded", "limit": 1,
                                     "retry_after_seconds": None,
                                     "action": "Delete an existing book or ask an operator to "
                                               "raise the book limit."})
    assert by_add.json()["detail"]["message"] == hosted_classics.LIMITS["book_quota_exceeded"]
    assert source.seen == []

    # Bytes: known only once the file is fetched, then refused by the same check, nothing kept.
    update_limits(database, a, {"max_books": 10, "max_library_bytes": before["objects"][0][
        "byte_size"] + len(BOOK) - 1})
    by_upload, by_add = upload(library_app, "a", BOOK), add(library_app, "a")
    assert refusal(by_add) == refusal(by_upload)
    assert refusal(by_add)[1]["code"] == "library_storage_exceeded"
    assert by_add.json()["detail"]["message"] == hosted_classics.LIMITS["library_storage_exceeded"]
    assert source.seen == [MIRROR.format(pg=PRIDE)]

    # One file larger than the reader may upload.
    update_limits(database, a, {"max_library_bytes": 10**9, "max_upload_bytes": len(BOOK) - 1})
    by_upload, by_add = upload(library_app, "a", BOOK), add(library_app, "a")
    assert refusal(by_add) == refusal(by_upload)
    assert refusal(by_add)[0] == 413 and refusal(by_add)[1]["code"] == "upload_size_exceeded"

    assert held(database, a) == before
    assert len(files(objects, a)) == 1


def test_a_book_makes_no_job_and_the_shelf_has_no_job_limit(
        library_app, readers, database, source):
    """The break it catches: every upload or add making an extraction job that nothing can run,
    so a limit on "active" jobs refuses the reader's fourth book for good. The job and its limit
    are gone (ADR 0043 step 7)."""
    a = readers["a"]
    for number in range(1, 5):
        uploaded = upload(library_app, "a", made_up_book(f"OWN{number}"))
        assert uploaded.status_code == 201, uploaded.text
        assert "job" not in uploaded.json()
    added = add(library_app, "a")
    assert added.status_code == 201, added.text
    assert "job" not in added.json()
    assert held(database, a)["jobs"] == []
    limits = get(library_app, "a", "/api/limits").json()
    assert limits["books"] == 5 and not ({"active_jobs", "max_active_jobs"} & set(limits))
    # Each is readable as it is.
    for book in get(library_app, "a", "/api/books").json():
        assert get(library_app, "a", f"/api/books/{book['book_id']}/epub").status_code == 200
        assert get(library_app, "a", f"/api/books/{book['book_id']}/manifest").status_code == 200


def test_the_shelf_names_each_book_book_id_as_the_app_and_local_mode_do(library_app, source):
    """The break it catches: hosted answering a book's id only as ``id`` where the app reads
    ``book_id`` (``Book`` in frontend/src/api.ts, as local ``GET /api/books`` answers), so a hosted
    reader's click on a book on the shelf, or the app after an upload, opens ``?book=undefined``."""
    uploaded = upload(library_app, "a", BOOK)
    assert uploaded.status_code == 201, uploaded.text
    book_id = uploaded.json()["book_id"]
    assert book_id == uploaded.json()["id"]
    added = add(library_app, "a").json()
    shelf = get(library_app, "a", "/api/books").json()
    assert {item["book_id"] for item in shelf} == {book_id, added["book_id"]}
    for item in shelf:
        assert {"book_id", "title", "author"} <= set(item)
        assert item["book_id"] == item["id"]
    assert get(library_app, "a", f"/api/books/{book_id}").json()["book_id"] == book_id


# ---- the source is not hammered -----------------------------------------------------------------

def test_one_fetch_in_flight_per_reader_and_a_bounded_number_in_all(library_app, readers,
                                                                     database, source,
                                                                     monkeypatch):
    """The break it catches: a reader pressing "add" again and again making the server fetch
    again and again at once, or many readers together making it open any number of fetches. A
    second reader is not held up by the first's fetch while there is room."""
    hold = source.hold = threading.Event()
    first: list[httpx.Response] = []
    started = threading.Thread(target=lambda: first.append(add(library_app, "a")))
    started.start()
    try:
        assert source.holding.wait(10)
        again = add(library_app, "a", ALICE)
        assert again.status_code == 429 and again.headers["retry-after"] == "5"
        assert again.json()["detail"]["code"] == "already_adding"
        assert again.json()["detail"]["message"] == hosted_classics.ADDING
        # Another reader may add while there is room; with none left, they are told to wait.
        assert add(library_app, "b", ALICE).status_code == 201
        monkeypatch.setattr(hosted_classics, "MAX_FETCHES", 1)
        busy = add(library_app, "b")
        assert busy.status_code == 503 and busy.json()["detail"]["code"] == "library_busy"
        assert busy.json()["detail"]["message"] == hosted_classics.BUSY
    finally:
        hold.set()
        started.join(10)
    assert first[0].status_code == 201, first[0].text
    assert sorted(source.seen) == sorted([MIRROR.format(pg=PRIDE), MIRROR.format(pg=ALICE)])
    # Once it is done, the reader may add the next one.
    assert add(library_app, "a", ALICE).status_code == 201
    assert hosted_classics._fetching == set()


# ---- two readers --------------------------------------------------------------------------------

def test_two_readers_adding_the_same_classic_each_get_their_own_book(library_app, readers,
                                                                    database, tenant_dsn,
                                                                    source):
    """The break it catches: one reader's added book shown to, counted against, reused for or
    deleted by another; the catalogue telling a reader that someone else has a book; or two
    readers' copies sharing a row or a stored file. Through the routes on the RLS-enforced role,
    in the database and storage afterwards, and through the repository and the companion adapter
    on a superuser connection, where only their own owner predicates keep readers apart."""
    client, _tokens, _app, objects = library_app
    a, b = readers["a"], readers["b"]
    book_a = add(library_app, "a").json()["book_id"]
    a_before = held(database, a)

    # Before B adds it: nothing of A's shows anywhere B looks.
    assert catalogue_card(library_app, "b", PRIDE)["book_id"] is None
    for path in ("/api/classics?q=pride+prejudice+austen", "/api/classics/front"):
        assert book_a not in get(library_app, "b", path).text
    assert get(library_app, "b", "/api/books").json() == []
    assert get(library_app, "b", "/api/limits").json()["books"] == 0
    missing = uuid.uuid4()
    for method, suffix in (("GET", ""), ("GET", "/epub"), ("DELETE", "")):
        hidden = client.request(method, f"/api/books/{book_a}{suffix}",
                                headers=as_reader(library_app, "b"))
        absent = client.request(method, f"/api/books/{missing}{suffix}",
                                headers=as_reader(library_app, "b"))
        assert (hidden.status_code, hidden.json()) == (absent.status_code, absent.json())
        assert hidden.status_code == 404

    # B adds the same book: B's own row, object and file, fetched for B.
    added = add(library_app, "b")
    assert added.status_code == 201, added.text
    book_b = added.json()["book_id"]
    assert book_b != book_a
    assert source.seen == [MIRROR.format(pg=PRIDE)] * 2
    b_rows = held(database, b)
    assert [str(row["id"]) for row in b_rows["books"]] == [book_b]
    assert b_rows["objects"][0]["storage_key"] != a_before["objects"][0]["storage_key"]
    assert files(objects, a) == [f"{a_before['objects'][0]['storage_key']}.epub.enc"]
    assert files(objects, b) == [f"{b_rows['objects'][0]['storage_key']}.epub.enc"]
    assert catalogue_card(library_app, "b", PRIDE)["book_id"] == book_b
    assert catalogue_card(library_app, "a", PRIDE)["book_id"] == book_a
    assert held(database, a) == a_before

    # A deletes theirs: B's is still B's and still opens.
    assert client.delete(f"/api/books/{book_a}",
                         headers=as_reader(library_app, "a")).status_code == 204
    epub = get(library_app, "b", f"/api/books/{book_b}/epub")
    assert epub.status_code == 200 and epub.content == BOOK
    assert catalogue_card(library_app, "b", PRIDE)["book_id"] == book_b
    assert held(database, b) == b_rows

    # The owner predicates alone, with row-level security bypassed.
    with psycopg.connect(database) as conn:
        assert conn.execute("SELECT rolsuper FROM pg_roles WHERE rolname=current_user").fetchone()[0]
    superuser = PostgresTenantRepository(database)
    assert asyncio.run(superuser.shelved_classics(OwnerId(b))) == {f"gutenberg:{PRIDE}": book_b}
    assert asyncio.run(superuser.shelved_classics(OwnerId(a))) == {}
    assert [book["book_id"] for book in
            HostedCompanionCatalog(database, OwnerId(b)).list_books()] == [book_b]
    assert HostedCompanionCatalog(database, OwnerId(b)).get_book(book_a) is None
    # And through the role, row-level security alone hides A's books from B.
    with psycopg.connect(tenant_dsn, autocommit=True) as conn:
        conn.execute("SELECT set_config('app.owner_id', %s, false)", (str(b),))
        assert {row[0] for row in conn.execute("SELECT id::text FROM books")} == {book_b}
        assert conn.execute("SELECT count(*) FROM source_objects WHERE book_id=%s",
                            (book_a,)).fetchone()[0] == 0


def test_every_free_library_route_needs_a_signed_in_reader_and_adding_needs_csrf(library_app,
                                                                                 source):
    """The break it catches: the catalogue or the add reachable without a session, or an add
    that a page on another site could trigger without the CSRF token."""
    client, tokens, _app, _objects = library_app
    client.cookies.clear()
    for method, path in (("GET", "/api/classics"), ("GET", "/api/classics/front"),
                         ("GET", f"/api/classics/{PRIDE}"), ("POST", f"/api/classics/{PRIDE}")):
        assert client.request(method, path).status_code == 401, path
    _become(client, tokens, "a")
    assert client.post(f"/api/classics/{PRIDE}").status_code == 403
    assert source.seen == []


def test_the_tenant_role_needs_no_new_grant_for_the_free_library(tenant_dsn: str) -> None:
    """The break it catches: the library needing a privilege the reviewed allow-list does not
    hold. The fixture grants exactly the allow-list and the startup check refuses one more or one
    fewer (``test_tenant_role_is_rls_enforced_and_privilege_allow_list_is_exact``)."""
    asyncio.run(PostgresTenantRepository(tenant_dsn).check_runtime_role())
