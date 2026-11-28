"""The free library in hosted mode (ADR 0041 part 11), without a database: which routes each mode
mounts, and the fence around the one outbound fetch. The network is never reached: every fetch here
goes to an in-memory transport, and a test that forgot to give one fails instead of fetching."""

from __future__ import annotations

import time

import httpx
import pytest

from app import classics
from app.api import classics as local_classics
from app.config import Settings
from app.hosted.tenant import classics as hosted_classics
from app.main import create_app
from tests.hosted.test_companion_read_map_postgres import hosted_settings

ENTRY = {"id": "158", "pg": 158}
MIRROR = "https://gutenberg.pglaf.org/cache/epub/158/pg158-images-3.epub"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse():
        pytest.fail("a test reached for the real network")

    monkeypatch.setattr(hosted_classics, "_transport", refuse)


def network(monkeypatch, handler) -> list[str]:
    seen = []

    def recorded(request):
        seen.append(str(request.url))
        return handler(request)

    monkeypatch.setattr(hosted_classics, "_transport", lambda: httpx.MockTransport(recorded))
    return seen


def classics_routes(app) -> dict[tuple[str, str], tuple[str, ...]]:
    """{(method, path): the route's tags} for every free-library route the app serves: the local
    router is tagged ``classics``, the hosted one ``hosted-library`` (the tenant inventory's tag)."""
    return {(method.upper(), path): tuple(operation["tags"])
            for path, operations in app.openapi()["paths"].items() if path.startswith("/api/classics")
            for method, operation in operations.items()}


def test_local_and_hosted_catalogue_routes_keep_separate_cover_storage(tmp_path):
    local = create_app(Settings(_env_file=None, allow_stub=True, data_dir=str(tmp_path / "data")))
    local_tag, hosted_tag = tuple(local_classics.router.tags), tuple(hosted_classics.router.tags)
    assert local_tag == ("classics",) and hosted_tag == ("hosted-library",)
    assert classics_routes(local) == {
        ("GET", "/api/classics"): local_tag,
        ("GET", "/api/classics/front"): local_tag,
        ("GET", "/api/classics/{classic_id}"): local_tag,
        ("GET", "/api/classics/{classic_id}/cover"): local_tag,
        ("POST", "/api/classics/covers/warm"): local_tag,
        ("POST", "/api/classics/{classic_id}"): local_tag,
    }
    hosted = create_app(hosted_settings("postgresql://unused.invalid/db", tmp_path / "objects"))
    assert classics_routes(hosted) == {
        ("GET", "/api/classics"): hosted_tag,
        ("GET", "/api/classics/front"): hosted_tag,
        ("GET", "/api/classics/{classic_id}"): hosted_tag,
        ("GET", "/api/classics/{classic_id}/cover"): hosted_tag,
        ("POST", "/api/classics/{classic_id}"): hosted_tag,
    }


def test_the_hosted_fetch_takes_the_book_from_the_mirror_only(monkeypatch):
    seen = network(monkeypatch, lambda request: httpx.Response(200, content=b"EPUBBYTES"))
    assert hosted_classics._download(ENTRY, 1000) == b"EPUBBYTES"
    assert seen == [MIRROR]


@pytest.mark.parametrize("location", [
    "https://evil.example/book.epub",
    "http://www.gutenberg.org/cache/epub/158/pg158.epub",
    "https://www.gutenberg.org:8443/x.epub",
    "https://user@www.gutenberg.org/x.epub",
    "https://gutenberg.org.evil.example/x.epub",
    "http://169.254.169.254/latest/meta-data/",
    "http://127.0.0.1:8000/api/books",
    "file:///etc/passwd",
])
def test_a_redirect_anywhere_but_the_reviewed_hosts_is_never_followed(monkeypatch, location):
    """The break it catches: a response from the source sending the server to another host, a
    plain-HTTP address, another port, an address with credentials, or the server's own network."""
    seen = network(monkeypatch, lambda request: httpx.Response(302, headers={"location": location}))
    with pytest.raises(classics.ClassicUnavailable, match="somewhere unexpected"):
        hosted_classics._download(ENTRY, 1000)
    assert seen == [MIRROR]


def test_an_address_pointing_off_the_reviewed_hosts_is_refused_before_any_request(monkeypatch):
    """The break it catches: a catalogue address (or a later edit of one) that is not on the
    reviewed hosts being fetched at all."""
    seen = network(monkeypatch, lambda request: httpx.Response(200, content=b"EPUBBYTES"))
    monkeypatch.setattr(classics, "_DOWNLOAD_URLS", ("https://books.evil.example/{pg}.epub",
                                                     "http://www.gutenberg.org/{pg}.epub"))
    with pytest.raises(classics.ClassicUnavailable, match="somewhere unexpected"):
        hosted_classics._download(ENTRY, 1000)
    assert seen == []


def test_the_hosted_fetch_uses_no_proxy_from_the_environment(monkeypatch):
    """The break it catches: an HTTPS_PROXY in the server's environment carrying the fetch past
    the client's own transport (and past the deadline), to a host nobody reviewed."""
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")
    seen = network(monkeypatch, lambda request: httpx.Response(200, content=b"EPUBBYTES"))
    assert hosted_classics._download(ENTRY, 1000) == b"EPUBBYTES"
    assert seen == [MIRROR]


def test_the_hosted_fetch_is_bounded_in_size_and_in_time(monkeypatch):
    """The break it catches: a download read past the upload limit, or one the source trickles
    out for ever, holding a server thread."""
    network(monkeypatch, lambda request: httpx.Response(200, content=b"x" * 2000))
    with pytest.raises(classics.ClassicTooLarge):
        hosted_classics._download(ENTRY, 1000)

    def trickle(request):
        def chunks():
            for _ in range(20):
                time.sleep(0.02)
                yield b"x"
        return httpx.Response(200, content=chunks())

    seen = network(monkeypatch, trickle)
    monkeypatch.setattr(hosted_classics, "FETCH_SECONDS", 0.1)
    started = time.monotonic()
    with pytest.raises(classics.ClassicUnavailable):
        hosted_classics._download(ENTRY, 1000)
    assert time.monotonic() - started < 1.0
    assert seen == [MIRROR]           # the deadline also stops the fallback address
