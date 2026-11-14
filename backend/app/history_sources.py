"""Reading history that cannot be read in the browser: the database files Kobo e-readers and Apple
Books keep, and a reader's Readwise account.

**Database files.** A Kobo keeps ``KoboReader.sqlite`` on the device; Apple Books on a Mac keeps a
library database and an annotation database. They are the reader's own files, holding titles,
progress and the passages they marked. None of this is a book or touches copy protection. The file
is someone else's SQLite database, so it is opened read-only, with the engine's defensive mode on
and a cap on the work a query may do, and only named columns of known tables are read.

**Readwise.** A reader who uses Readwise can paste their access token; their highlights are fetched
once from Readwise's export API, on a fixed host. The token is used for that one request chain and
is never stored or written to a log."""
import sqlite3
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

import httpx

MAX_ROWS = 200_000
_MAX_VM_STEPS = 400_000_000
_SQLITE_MAGIC = b"SQLite format 3\x00"
_APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=UTC)
READWISE_URL = "https://readwise.io/api/v2/export/"
READWISE_MAX_PAGES = 400


class UnreadableSource(Exception):
    """The file or account could not be read. The message is written for the reader."""


def is_sqlite(head):
    return head[:16] == _SQLITE_MAGIC


def _open(path):
    conn = sqlite3.connect(f"file:{quote(str(path))}?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    conn.setconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE, True)
    conn.execute("PRAGMA trusted_schema = OFF")
    steps = [0]

    def tick():
        steps[0] += 100_000
        return 1 if steps[0] > _MAX_VM_STEPS else 0

    conn.set_progress_handler(tick, 100_000)
    return conn


def _columns(conn, table):
    return {row["name"] for row in conn.execute(f'PRAGMA table_info("{table}")')}


def _clip(value, limit):
    return value.strip()[:limit] if isinstance(value, str) else ""


def _day(value):
    return value[:10] if isinstance(value, str) and len(value) >= 10 and value[4] == "-" and value[7] == "-" else None


def _apple_time(seconds):
    try:
        return (_APPLE_EPOCH + timedelta(seconds=float(seconds))).strftime("%Y-%m-%dT%H:%M:%S")
    except (TypeError, ValueError, OverflowError):
        return None


def _kobo(conn):
    content, marks = _columns(conn, "content"), _columns(conn, "Bookmark")
    if not {"ContentID", "Title", "ContentType"} <= content:
        raise UnreadableSource("This Kobo file is missing its book list.")
    pick = lambda name: name if name in content else "NULL"  # noqa: E731
    books = []
    for row in conn.execute(
            f"SELECT Title, {pick('Attribution')} AS author, {pick('ReadStatus')} AS state,"
            f" {pick('DateLastRead')} AS last_read, {pick('ISBN')} AS isbn FROM content"
            f" WHERE ContentType = 6 AND Title IS NOT NULL LIMIT {MAX_ROWS}"):
        title = _clip(row["Title"], 500)
        if not title:
            continue
        status = {2: "read", 1: "reading"}.get(row["state"])
        isbn = "".join(ch for ch in str(row["isbn"] or "") if ch.isdigit() or ch in "Xx")
        books.append({"title": title, "author": _clip(row["author"], 300),
                      "isbn": isbn if len(isbn) in (10, 13) else None, "status": status,
                      "finished_on": _day(row["last_read"]) if status == "read" else None})
    highlights = []
    if {"VolumeID", "Text"} <= marks:
        pick = lambda name: f"b.{name}" if name in marks else "NULL"  # noqa: E731
        for row in conn.execute(
                f"SELECT b.Text AS text, {pick('Annotation')} AS note, {pick('DateCreated')} AS made,"
                f" {pick('ChapterProgress')} AS progress, c.Title AS title,"
                f" {'c.Attribution' if 'Attribution' in content else 'NULL'} AS author"
                f" FROM Bookmark b JOIN content c ON c.ContentID = b.VolumeID"
                f" WHERE (b.Text IS NOT NULL OR {pick('Annotation')} IS NOT NULL) ORDER BY b.VolumeID, b.rowid LIMIT {MAX_ROWS}"):
            text, note = _clip(row["text"], 8000), _clip(row["note"], 8000) or None
            title = _clip(row["title"], 500)
            if title and (text or note):
                made = row["made"] if isinstance(row["made"], str) else ""
                highlights.append({"title": title, "author": _clip(row["author"], 300), "text": text, "note": note,
                                   "position": row["progress"] if isinstance(row["progress"], (int, float)) else None,
                                   "made_at": made[:19] if made[:4].isdigit() else None})
    return "kobo", books, highlights


def _apple_library(conn):
    columns = _columns(conn, "ZBKLIBRARYASSET")
    if not {"ZASSETID", "ZTITLE"} <= columns:
        raise UnreadableSource("This Apple Books library file is not laid out as we expected.")
    pick = lambda name: name if name in columns else "NULL"  # noqa: E731
    books, titles = [], {}
    for row in conn.execute(
            f"SELECT ZASSETID, ZTITLE, {pick('ZAUTHOR')} AS author, {pick('ZISFINISHED')} AS finished,"
            f" {pick('ZDATEFINISHED')} AS finished_at, {pick('ZREADINGPROGRESS')} AS progress"
            f" FROM ZBKLIBRARYASSET WHERE ZTITLE IS NOT NULL LIMIT {MAX_ROWS}"):
        title, author = _clip(row["ZTITLE"], 500), _clip(row["author"], 300)
        if not title:
            continue
        titles[row["ZASSETID"]] = (title, author)
        progress = row["progress"] if isinstance(row["progress"], (int, float)) else 0
        status = "read" if row["finished"] else "reading" if progress and progress > 0.02 else None
        finished = _apple_time(row["finished_at"]) if row["finished"] and row["finished_at"] else None
        books.append({"title": title, "author": author, "status": status,
                      "finished_on": finished[:10] if finished else None})
    return books, titles


def _apple_marks(conn, titles):
    columns = _columns(conn, "ZAEANNOTATION")
    if "ZANNOTATIONASSETID" not in columns:
        raise UnreadableSource("This Apple Books highlights file is not laid out as we expected.")
    pick = lambda name: name if name in columns else "NULL"  # noqa: E731
    alive = "COALESCE(ZANNOTATIONDELETED, 0) = 0" if "ZANNOTATIONDELETED" in columns else "1"
    highlights = []
    for row in conn.execute(
            f"SELECT ZANNOTATIONASSETID AS asset, {pick('ZANNOTATIONSELECTEDTEXT')} AS text,"
            f" {pick('ZANNOTATIONNOTE')} AS note, {pick('ZANNOTATIONCREATIONDATE')} AS made,"
            f" {pick('ZPLLOCATIONRANGESTART')} AS position FROM ZAEANNOTATION WHERE {alive}"
            f" ORDER BY ZANNOTATIONASSETID, position LIMIT {MAX_ROWS}"):
        book = titles.get(row["asset"])
        text, note = _clip(row["text"], 8000), _clip(row["note"], 8000) or None
        if book and (text or note):
            position = row["position"] if isinstance(row["position"], (int, float)) and row["position"] >= 0 else None
            highlights.append({"title": book[0], "author": book[1], "text": text, "note": note,
                               "position": position, "made_at": _apple_time(row["made"]) if row["made"] else None})
    return highlights


def read_databases(paths):
    """Reads Kobo and Apple Books database files. Returns ``[(source, books, highlights)]``."""
    found, apple_marks = [], []
    apple_books, apple_titles = None, {}
    for path in paths:
        try:
            conn = _open(path)
        except sqlite3.Error as error:
            raise UnreadableSource("One of these files could not be opened as a reading database.") from error
        try:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            if {"content", "Bookmark"} <= tables:
                found.append(_kobo(conn))
            elif "ZBKLIBRARYASSET" in tables:
                apple_books, apple_titles = _apple_library(conn)
            elif "ZAEANNOTATION" in tables:
                apple_marks.append(path)
            else:
                raise UnreadableSource(
                    "That database isn’t from Kobo or Apple Books. We read a Kobo’s KoboReader.sqlite and the"
                    " two Apple Books files from a Mac.")
        except sqlite3.Error as error:
            raise UnreadableSource("One of these files could not be read all the way through.") from error
        finally:
            conn.close()
    if apple_marks and apple_books is None:
        raise UnreadableSource(
            "That is the Apple Books highlights file. Bring the library file from the BKLibrary folder with it,"
            " so we know which book each highlight belongs to.")
    if apple_books is not None:
        highlights = []
        for path in apple_marks:
            conn = _open(path)
            try:
                highlights += _apple_marks(conn, apple_titles)
            except sqlite3.Error as error:
                raise UnreadableSource("The Apple Books highlights file could not be read all the way through.") from error
            finally:
                conn.close()
        found.append(("apple", apple_books, highlights))
    return found


def fetch_readwise(token, *, client=None):
    """Everything a reader's Readwise account holds: ``(books, highlights)``."""
    own = client is None
    client = client or httpx.Client(timeout=httpx.Timeout(40.0, connect=10.0), follow_redirects=False)
    books, highlights, cursor = [], [], None
    try:
        for _page in range(READWISE_MAX_PAGES):
            response = client.get(READWISE_URL, params={"pageCursor": cursor} if cursor else None,
                                  headers={"authorization": f"Token {token}"})
            if response.status_code in (401, 403):
                raise UnreadableSource("Readwise didn’t accept that access token. Copy it again from readwise.io/access_token.")
            if response.status_code == 429:
                raise UnreadableSource("Readwise asked us to slow down. Try again in a minute.")
            if response.status_code != 200:
                raise UnreadableSource("Readwise isn’t answering right now. Try again later.")
            page = response.json()
            for book in page.get("results") or []:
                title, author = _clip(book.get("title"), 500), _clip(book.get("author"), 300)
                if not title or book.get("category") not in (None, "books"):
                    continue  # articles, tweets and podcasts are not books
                books.append({"title": title, "author": author})
                for mark in book.get("highlights") or []:
                    text, note = _clip(mark.get("text"), 8000), _clip(mark.get("note"), 8000) or None
                    if not (text or note):
                        continue
                    location = mark.get("location")
                    kind = {"page": "Page", "location": "Location"}.get(mark.get("location_type"))
                    made = mark.get("highlighted_at")
                    highlights.append({
                        "title": title, "author": author, "text": text, "note": note,
                        "location": f"{kind} {location}" if kind and isinstance(location, int) else None,
                        "position": location if isinstance(location, (int, float)) and location >= 0 else None,
                        "made_at": made[:19] if isinstance(made, str) and made[:4].isdigit() else None})
            cursor = page.get("nextPageCursor")
            if not cursor:
                break
    except httpx.HTTPError as error:
        raise UnreadableSource("Couldn’t reach Readwise. Check your connection and try again.") from error
    except ValueError as error:
        raise UnreadableSource("Readwise sent something we couldn’t read. Try again later.") from error
    finally:
        if own:
            client.close()
    return books, highlights
