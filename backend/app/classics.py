"""Public-domain classics a reader can add with one tap.

The catalogue is a fixed, curated list shipped with the app (``classics_catalog.json``). A reader
can only ever ask for an entry by its catalogue id, so the one outbound request this module makes is
to a URL built from a vetted integer id on a fixed host: there is no reader-supplied URL anywhere,
and redirects are followed only within an allowlist. The downloaded file is then handed to the
ordinary import path and is validated exactly like an uploaded EPUB.

Copyright terms differ by country. To stay on the safe side for distribution, an entry is offered
only when its last-surviving author or translator died more than ``PUBLIC_DOMAIN_YEARS`` years ago.
"""
import json
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

CATALOG_PATH = Path(__file__).with_name("classics_catalog.json")
PUBLIC_DOMAIN_YEARS = 100
SOURCE_NAME = "Project Gutenberg"
_DOWNLOAD_URL = "https://www.gutenberg.org/ebooks/{pg}.epub3.images"
_ALLOWED_HOSTS = frozenset({"www.gutenberg.org", "gutenberg.org"})
_MAX_REDIRECTS = 4
_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
_USER_AGENT = "Mothli-reader (reader-initiated single download)"


class ClassicUnavailable(Exception):
    """The book could not be fetched right now. The message is safe to show a reader."""


@lru_cache(maxsize=1)
def _raw_catalog():
    data = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    books, seen = [], set()
    for entry in data["books"]:
        classic_id, pg = entry["id"], entry["pg"]
        if not isinstance(pg, int) or isinstance(pg, bool) or pg <= 0:
            raise ValueError(f"classics catalogue entry {classic_id!r} has an invalid Gutenberg id")
        if classic_id in seen:
            raise ValueError(f"classics catalogue has a duplicate id {classic_id!r}")
        seen.add(classic_id)
        books.append({"id": classic_id, "pg": pg, "title": entry["title"], "author": entry["author"],
                      "year": entry.get("year"), "died": int(entry["died"])})
    return tuple(books)


def source_tag(entry):
    """The catalog ``source`` value that marks a shelved book as this classic."""
    return f"gutenberg:{entry['pg']}"


def load_catalog(*, today=None):
    """Entries whose every contributor died more than PUBLIC_DOMAIN_YEARS years ago."""
    year = (today or datetime.now(UTC)).year
    return [dict(entry) for entry in _raw_catalog() if year - entry["died"] > PUBLIC_DOMAIN_YEARS]


def find_classic(classic_id, *, today=None):
    return next((entry for entry in load_catalog(today=today) if entry["id"] == classic_id), None)


def _checked(url):
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname not in _ALLOWED_HOSTS or parts.username or parts.port not in (None, 443):
        raise ClassicUnavailable("The book’s source sent us somewhere unexpected, so it was not downloaded.")
    return url


def fetch_classic(entry, *, max_bytes, client=None):
    """Download one catalogue entry's EPUB, bounded in size, time and destination."""
    own = client is None
    client = client or httpx.Client(timeout=_TIMEOUT, follow_redirects=False,
                                    headers={"user-agent": _USER_AGENT})
    try:
        url = _checked(_DOWNLOAD_URL.format(pg=int(entry["pg"])))
        for _hop in range(_MAX_REDIRECTS + 1):
            with client.stream("GET", url) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("location")
                    if not location:
                        raise ClassicUnavailable("This book isn’t available right now. Try again later.")
                    url = _checked(urljoin(url, location))
                    continue
                if response.status_code != 200:
                    raise ClassicUnavailable("This book isn’t available right now. Try again later.")
                chunks, total = [], 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise ClassicUnavailable("This book is too large to add.")
                    chunks.append(chunk)
                if not total:
                    raise ClassicUnavailable("This book isn’t available right now. Try again later.")
                return b"".join(chunks)
        raise ClassicUnavailable("This book isn’t available right now. Try again later.")
    except httpx.HTTPError as exc:
        raise ClassicUnavailable("Couldn’t reach the book’s source. Check your connection and try again.") from exc
    finally:
        if own:
            client.close()
