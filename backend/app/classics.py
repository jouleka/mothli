"""Public-domain books a reader can add with one tap.

The catalogue is Project Gutenberg's own: every text it marks "Public domain in the USA" that has an
EPUB edition, built from its offline catalogue feed by ``scripts/build_classics_catalog.py`` and
shipped with the app (``classics_catalog.tsv.gz``). Project Gutenberg asks tools to use that feed
rather than crawl the site, and to fetch books from a mirror, so that is what happens here.

A reader can only ever ask for a book by its catalogue number, so the one outbound request this
module makes is to a URL built from a vetted integer on a fixed host: there is no reader-supplied
URL anywhere, and redirects are followed only within an allowlist. The downloaded file is then
handed to the ordinary import path and is validated exactly like an uploaded EPUB. It is shelved
unmodified, with Project Gutenberg's licence page intact.

Which rule decides "public domain": United States law, which is what Project Gutenberg, Standard
Ebooks and Wikisource publish under, with a note to readers elsewhere to check their own country's
rules. An operator who wants a stricter, death-date rule (for example 70 for most of the world) can
set ``CLASSICS_MIN_YEARS_SINCE_DEATH``; books with an unrecorded death year are then left out.
"""
import gzip
import re
import unicodedata
from collections import Counter
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urljoin, urlsplit

import httpx

CATALOG_PATH = Path(__file__).with_name("classics_catalog.tsv.gz")
SOURCE_NAME = "Project Gutenberg"
SOURCE_URL = "https://www.gutenberg.org/"
# The mirror first (Project Gutenberg's request for anything beyond light use), the main site as a
# fallback for a book the mirror has not picked up yet.
_DOWNLOAD_URLS = (
    "https://gutenberg.pglaf.org/cache/epub/{pg}/pg{pg}-images-3.epub",
    "https://www.gutenberg.org/ebooks/{pg}.epub3.images",
)
_ALLOWED_HOSTS = frozenset({"gutenberg.pglaf.org", "www.gutenberg.org", "gutenberg.org"})
_MAX_REDIRECTS = 4
_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
_USER_AGENT = "Mothli-reader (reader-initiated single download)"
_ID = re.compile(r"[1-9][0-9]{0,6}")
MAX_PAGE = 60
_UNAVAILABLE = "This book isn’t available right now. Try again later."


class ClassicUnavailable(Exception):
    """The book could not be fetched right now. The message is safe to show a reader."""


class _Missing(Exception):
    """This address does not have the book; another one may."""


class _Row(NamedTuple):
    pg: int
    language: str
    died: int  # latest contributor death year; 0 when it is not recorded
    title: str
    author: str
    hay: str


def fold(text):
    """Case- and accent-insensitive form used for matching: ``Brontë`` finds ``bronte``."""
    plain = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in plain if not unicodedata.combining(ch)).casefold()


@lru_cache(maxsize=1)
def _rows():
    rows, seen = [], set()
    with gzip.open(CATALOG_PATH, "rt", encoding="utf-8", newline="\n") as handle:
        next(handle)  # header
        for line in handle:
            pg, language, died, _downloads, title, author = line.rstrip("\n").split("\t")
            number = int(pg)
            if number <= 0 or number in seen:
                raise ValueError(f"classics catalogue has an invalid or duplicate book number {pg!r}")
            seen.add(number)
            rows.append(_Row(number, language, int(died), title, author, fold(f"{title} {author}")))
    return tuple(rows)


@lru_cache(maxsize=8)
def _eligible(min_years_since_death, year):
    """Rows on offer, most popular first, with their index by catalogue number."""
    rows = _rows()
    if min_years_since_death > 0:
        rows = tuple(row for row in rows if row.died and year - row.died > min_years_since_death)
    languages = tuple({"code": code, "count": count} for code, count in Counter(row.language for row in rows).most_common())
    return rows, {row.pg: row for row in rows}, languages


def _offer(min_years_since_death, today):
    return _eligible(max(int(min_years_since_death), 0), (today or datetime.now(UTC)).year)


def _entry(row):
    return {"id": str(row.pg), "pg": row.pg, "title": row.title,
            "author": row.author or "Unknown author", "language": row.language}


def catalog_summary(*, min_years_since_death=0, today=None):
    """How many books are on offer, and in which languages (most books first)."""
    rows, _index, languages = _offer(min_years_since_death, today)
    return {"total": len(rows), "languages": list(languages)}


def search(query="", *, language=None, offset=0, limit=40, min_years_since_death=0, today=None):
    """One page of books whose title or author contains every word of ``query``.

    Returns ``(matches, page)``. Rows keep the catalogue's order, most popular first."""
    rows, _index, _languages = _offer(min_years_since_death, today)
    words = fold(query).split()[:8]
    offset, limit = max(int(offset), 0), min(max(int(limit), 1), MAX_PAGE)
    matches, page = 0, []
    for row in rows:
        if language and row.language != language:
            continue
        if words and not all(word in row.hay for word in words):
            continue
        if offset <= matches < offset + limit:
            page.append(_entry(row))
        matches += 1
    return matches, page


def find_classic(classic_id, *, min_years_since_death=0, today=None):
    if not isinstance(classic_id, str) or not _ID.fullmatch(classic_id):
        return None
    _rows_, index, _languages = _offer(min_years_since_death, today)
    row = index.get(int(classic_id))
    return _entry(row) if row else None


def source_tag(entry):
    """The catalog ``source`` value that marks a shelved book as this classic."""
    return f"gutenberg:{entry['pg']}"


def _checked(url):
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname not in _ALLOWED_HOSTS or parts.username or parts.port not in (None, 443):
        raise ClassicUnavailable("The book’s source sent us somewhere unexpected, so it was not downloaded.")
    return url


def _download(client, url, max_bytes):
    url = _checked(url)
    for _hop in range(_MAX_REDIRECTS + 1):
        with client.stream("GET", url) as response:
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("location")
                if not location:
                    raise _Missing
                url = _checked(urljoin(url, location))
                continue
            if response.status_code != 200:
                raise _Missing
            chunks, total = [], 0
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    raise ClassicUnavailable("This book is too large to add.")
                chunks.append(chunk)
            if not total:
                raise _Missing
            return b"".join(chunks)
    raise _Missing


def fetch_classic(entry, *, max_bytes, client=None):
    """Download one catalogue entry's EPUB, bounded in size, time and destination."""
    own = client is None
    client = client or httpx.Client(timeout=_TIMEOUT, follow_redirects=False,
                                    headers={"user-agent": _USER_AGENT})
    try:
        unreachable = None
        for template in _DOWNLOAD_URLS:
            try:
                return _download(client, template.format(pg=int(entry["pg"])), max_bytes)
            except _Missing:
                continue
            except httpx.HTTPError as exc:
                unreachable = exc
        if unreachable is not None:
            raise ClassicUnavailable(
                "Couldn’t reach the book’s source. Check your connection and try again.") from unreachable
        raise ClassicUnavailable(_UNAVAILABLE)
    finally:
        if own:
            client.close()
