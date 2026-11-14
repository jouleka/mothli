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
_COVER_URL = "https://gutenberg.pglaf.org/cache/epub/{pg}/pg{pg}.cover.medium.jpg"
MAX_COVER_BYTES = 1024 * 1024
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


# What a reader browses by. Order is the order the page shows them in.
GENRES = {
    "classics": "Classics",
    "adventure": "Adventure",
    "romance": "Romance",
    "mystery": "Mystery & crime",
    "scifi": "Science fiction & fantasy",
    "horror": "Horror & gothic",
    "historical": "Historical fiction",
    "humour": "Humour",
    "children": "Children’s",
    "stories": "Short stories",
    "poetry": "Poetry",
    "plays": "Plays",
    "myths": "Myths & folklore",
    "western": "Westerns",
    "philosophy": "Philosophy",
    "history": "History",
    "biography": "Biography",
    "travel": "Travel",
    "science": "Science & nature",
    "essays": "Essays & letters",
}
_STORYTELLING = frozenset({"classics", "adventure", "romance", "mystery", "scifi", "horror", "historical",
                           "humour", "children", "stories", "plays", "myths", "western"})
QUICK_READ_KB = 150  # roughly an evening: about 25,000 words


class Mood(NamedTuple):
    id: str
    label: str
    genres: frozenset
    quick: bool = False


# "How are you feeling?" Each mood is a plain, explainable filter over the genres above.
MOODS = (
    Mood("laugh", "Make me laugh", frozenset({"humour"})),
    Mood("love", "Sweep me off my feet", frozenset({"romance"})),
    Mood("chills", "Keep me up at night", frozenset({"horror"})),
    Mood("puzzle", "Give me a puzzle", frozenset({"mystery"})),
    Mood("away", "Take me far away", frozenset({"adventure", "travel"})),
    Mood("worlds", "Show me other worlds", frozenset({"scifi"})),
    Mood("think", "Make me think", frozenset({"philosophy", "essays"})),
    Mood("young", "Feel like a kid again", frozenset({"children"})),
    Mood("quick", "Just one evening", _STORYTELLING, quick=True),
)
_MOODS = {mood.id: mood for mood in MOODS}
SHELF_SIZE = 14
# The front page: the most-read books, then every mood, then a few genres that moods do not cover.
_FRONT_GENRES = ("classics", "historical", "poetry", "myths", "history")


class _Row(NamedTuple):
    pg: int
    language: str
    died: int  # latest contributor death year; 0 when it is not recorded
    kb: int  # size of the plain text, for a reading-time estimate; 0 when unknown
    genres: frozenset
    title: str
    author: str
    blurb: str
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
            pg, language, died, _downloads, kb, genres, title, author, blurb = line.rstrip("\n").split("\t")
            number = int(pg)
            if number <= 0 or number in seen:
                raise ValueError(f"classics catalogue has an invalid or duplicate book number {pg!r}")
            seen.add(number)
            rows.append(_Row(number, language, int(died), int(kb), frozenset(genres.split()) & GENRES.keys(),
                             title, author, blurb, fold(f"{title} {author}")))
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
            "author": row.author or "Unknown author", "language": row.language,
            "genres": [genre for genre in GENRES if genre in row.genres],
            # about 6 bytes a word, 250 words a minute
            "minutes": round(row.kb * 1024 / 6 / 250) if row.kb else None,
            "blurb": row.blurb}


def _wanted(language=None, genre=None, mood=None):
    """A row test for the browse filters, or None when a filter names nothing we know."""
    if genre and genre not in GENRES:
        return None
    feeling = _MOODS.get(mood) if mood else None
    if mood and feeling is None:
        return None

    def test(row):
        if language and row.language != language:
            return False
        if genre and genre not in row.genres:
            return False
        if feeling is not None:
            if not feeling.genres & row.genres:
                return False
            if feeling.quick and not 0 < row.kb <= QUICK_READ_KB:
                return False
        return True

    return test


def catalog_summary(*, min_years_since_death=0, today=None):
    """How many books are on offer, and in which languages (most books first)."""
    rows, _index, languages = _offer(min_years_since_death, today)
    return {"total": len(rows), "languages": list(languages),
            "genres": [{"id": genre, "label": label} for genre, label in GENRES.items()],
            "moods": [{"id": mood.id, "label": mood.label} for mood in MOODS]}


def front_page(*, language=None, min_years_since_death=0, today=None):
    """The shelves a reader sees before searching: most read, each mood, and a few genres."""
    rows, _index, _languages = _offer(min_years_since_death, today)
    in_language = _wanted(language)
    # reference works top the download counts; the opening shelf is for books people sit down and read
    plan = [("popular", "popular", "Most read right now", lambda row: bool(row.genres) and in_language(row))]
    plan += [("mood", mood.id, mood.label, _wanted(language, mood=mood.id)) for mood in MOODS]
    plan += [("genre", genre, GENRES[genre], _wanted(language, genre=genre)) for genre in _FRONT_GENRES]
    shelves, shown = [], set()
    for kind, shelf_id, title, test in plan:
        books = []
        for row in rows:
            # a book sits on one shelf only, so the page is not the same dozen titles over and over
            if row.pg not in shown and test(row):
                shown.add(row.pg)
                books.append(_entry(row))
                if len(books) == SHELF_SIZE:
                    break
        if len(books) >= 4:
            shelves.append({"kind": kind, "id": shelf_id, "title": title, "books": books})
    return shelves


def search(query="", *, language=None, genre=None, mood=None, offset=0, limit=40,
           min_years_since_death=0, today=None):
    """One page of books whose title or author contains every word of ``query``, within a language,
    genre and mood when those are given.

    Returns ``(matches, page)``. Rows keep the catalogue's order, most popular first."""
    rows, _index, _languages = _offer(min_years_since_death, today)
    test = _wanted(language, genre, mood)
    if test is None:
        return 0, []
    words = fold(query).split()[:8]
    offset, limit = max(int(offset), 0), min(max(int(limit), 1), MAX_PAGE)
    matches, page = 0, []
    for row in rows:
        if not test(row):
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


def fetch_cover(entry, *, client=None):
    """The book's cover image from the mirror, or None when it has none we can use."""
    own = client is None
    client = client or httpx.Client(timeout=httpx.Timeout(12.0, connect=6.0), follow_redirects=False,
                                    headers={"user-agent": _USER_AGENT})
    try:
        blob = _download(client, _COVER_URL.format(pg=int(entry["pg"])), MAX_COVER_BYTES)
    except (_Missing, ClassicUnavailable, httpx.HTTPError):
        return None
    finally:
        if own:
            client.close()
    return blob if image_type(blob) else None


def image_type(blob):
    if blob[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if blob[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    return None


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
