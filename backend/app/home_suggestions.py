"""Books Mothli may suggest that are not on the reader's shelf.

Only books from the free library's catalogue, and of each only its catalogue line: title, author,
genres and blurb. Mothli has read none of them, so that line is everything it may say. The books
are chosen here by fixed rules, never by a model, and the same list is given to the writer and,
line by line, to the checks on every sentence.

Which books (``suggestions``):
  - in a language of the books on the shelf (English when no book says its language);
  - not on the shelf, and not one the reading history says was read, is being read or was set
    aside in another app;
  - nothing by the author of a shelf book the reader has not finished with Mothli, and nothing
    whose line names such a book. A sequel's blurb tells how the book before it came out ("Jo
    and her husband"), and an excerpt's or an adaptation's blurb tells what is in the book
    itself: either would take the reader past their place in a book on the shelf;
  - one line a work, never one volume of a set, and only a book with a blurb and a genre;
  - only a book whose blurb has been read by hand and judged clean: its number is in
    ``classics_reviewed.tsv`` with the verdict "clean". A book judged "no", and every book whose
    blurb nobody has read, is never suggested. The owner's rule: for unread books use only a
    spoiler-free blurb.
In which order: a book the reader has named in their own words (``named``) comes first, then
books by an author of a book the reader has finished, then books
sharing more genres with the books the reader has finished or is well into, then the catalogue's
own order (most read first). At most two by one author. "Well into" is a quarter of a book's
chapters read in full with Mothli, or ten of them.
"""
import re
from datetime import UTC, datetime
from difflib import SequenceMatcher
from functools import lru_cache
from math import ceil
from pathlib import Path

from app.classics import GENRES, author_key, find_classic, fold, match_title, rows_on_offer, title_key

LIMIT = 6                 # books in one reply's list
PER_AUTHOR = 2
WELL_INTO = 10            # chapters read in full that count as well into a book, however long it is
SAME_SURNAME = 0.84       # as the reading history matches a title to the shelf: Dostoevsky, Dostoyevsky
# The blurbs read by hand, one line a book: number, "clean" or "no", and for "no" why. The file's
# own head says what the rule was and what was read. Another edition of a work whose blurb is
# clean stands in for one whose blurb is not.
REVIEWED_PATH = Path(__file__).with_name("classics_reviewed.tsv")
_VERDICTS = ("clean", "no")
_ASIDE = re.compile(r"\([^)]*\)|\[[^\]]*\]")
_SUBTITLE = re.compile(r"\s*[:;]\s+|\s+[-–—]\s+|,\s+complete$", re.I)
_NOT_WORD = re.compile(r"[^\w]+", re.UNICODE)
# "Vol. 5 of 9", "Tome I", "Tome Second", "Volume 2 (of 3)", "1. Band", "(2/3)", "[2]": one part
# of a set is not a book to suggest. A pattern, so a set numbered some other way gets through.
_ONE_OF_A_SET = re.compile(
    r"\b(?:vol|volume|tome|tomo|band|deel|part|parte)\b\.?\s*"
    r"(?:\d|[ivxlc]+\b|(?:premier|second|deuxi[eè]me|troisi[eè]me|first|third|one|two|three)\b)"
    r"|\b\d+\.\s*band\b|\(\s*(?:of|de|van)\s+\d+\s*\)|\(\d+/\d+\)|\[\d+\]", re.I)


def short_title(title):
    """The title as a person says it: no subtitle, series note or cataloguer's mark."""
    plain = " ".join(_ASIDE.sub(" ", title or "").split())
    return _SUBTITLE.split(plain, maxsplit=1)[0].strip(" .,") or (title or "").strip()


def _work(title):
    """What two editions of a work share: the catalogue's title key, without "Complete"."""
    return title_key(title).removesuffix(" complete")


def _words(text):
    """Folded words between single spaces, with one at each end, so a phrase can be looked for whole."""
    return f" {_NOT_WORD.sub(' ', fold(text)).strip()} "


@lru_cache(maxsize=4)
def reviewed(path=None):
    """{catalogue number: (verdict, why)} from the reviewed file. A line that is not a number, a
    known verdict and, for "no", a reason, or a number given twice, is an error, not a guess."""
    found = {}
    for line in Path(path or REVIEWED_PATH).read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        number, verdict, *why = line.split("\t")
        why = why[0].strip() if why else ""
        if not number.isdigit() or verdict not in _VERDICTS or (verdict == "no" and not why) or int(number) in found:
            raise ValueError(f"reviewed blurbs: bad or repeated line {line!r}")
        found[int(number)] = (verdict, why)
    return found


def _clean():
    """The catalogue numbers whose blurb was read and judged clean: the only books to suggest."""
    return frozenset(number for number, (verdict, _why) in reviewed(REVIEWED_PATH).items() if verdict == "clean")


@lru_cache(maxsize=4)
def _pool(rule, year):
    """{language: [(place in the catalogue, row, work, author's surname)]}, one entry a work."""
    found, seen, clean = {}, set(), _clean()
    for place, row in enumerate(rows_on_offer(rule, year)):
        if not (row.blurb and row.genres) or row.pg not in clean or _ONE_OF_A_SET.search(row.title):
            continue
        work = (_work(row.title), author_key(row.author), row.language)
        if not work[0] or work in seen:
            continue
        seen.add(work)
        found.setdefault(row.language, []).append((place, row, work[0], work[1]))
    return found


def _alike(surname, other):
    return surname == other or not surname or not other or SequenceMatcher(None, surname, other).ratio() >= SAME_SURNAME


@lru_cache(maxsize=512)
def _spellings(surname, rule, year, strict):
    """The catalogue's spellings of one surname. ``strict`` asks the first letter to agree too:
    for an author to prefer, so that Dostoevsky does not bring in Ostrovsky. For an author to
    keep out it is left loose, which only ever keeps out more."""
    if not surname:
        return frozenset()
    match, found = SequenceMatcher(None), {surname}
    match.set_seq2(surname)
    for name in _surnames(rule, year):
        if strict and name[0] != surname[0]:
            continue
        if 2 * min(len(name), len(surname)) < SAME_SURNAME * (len(name) + len(surname)):
            continue                                # too different in length to be alike: skip the slow part
        match.set_seq1(name)
        if match.quick_ratio() >= SAME_SURNAME and match.ratio() >= SAME_SURNAME:
            found.add(name)
    return frozenset(found)


@lru_cache(maxsize=4)
def _surnames(rule, year):
    return frozenset(name for books in _pool(rule, year).values() for _place, _row, _title, name in books if name)


def _initial(author):
    """The first letter of the first author's given name: "Jane Austen", "Austen, Jane" and
    "Leo Tolstoy, graf" give j, j and l. Empty when the name does not say."""
    first = re.split(r"\s*(?:;|&| and |\|)\s*", author or "", maxsplit=1)[0]
    before, _comma, after = first.partition(",")
    names = _NOT_WORD.sub(" ", fold(_ASIDE.sub(" ", before))).split()
    given = names[:-1] or _NOT_WORD.sub(" ", fold(after)).split()
    return given[0][0] if given else ""


def _read_in_full(item):
    numbers = item["numbers"]
    return sum(1 for chapter in item["chapters"] if chapter["whole"] and chapter["ordinal"] in numbers), len(numbers)


def _named_in(texts, row):
    """Whether one of ``texts`` names this book: its title as a person says it (``short_title``),
    as whole words, whatever the capitals and accents. A title of one word ("Emma", "She") may be
    a person in a book or an ordinary word, so it counts only with the author's surname in the
    same text, as ``suggested_in`` counts it."""
    title = _words(short_title(row.title))
    if not title.strip():
        return False
    surname = author_key(row.author)
    return any(title in said and (len(title.split()) > 1 or bool(surname) and f" {surname} " in said)
               for said in map(_words, texts))


def suggestions(books, elsewhere, settings, *, limit=LIMIT, year=None, named=()):
    """The books Mothli may suggest now, best first: see the module's own account of the rules.

    ``books`` is ``shelf_books``; ``elsewhere`` the reading-history entries that are not on the
    shelf. Each book is ``{classic_id, title, author, genres, blurb}``, all from the catalogue.
    ``named`` is the reader's own words that may name a book they want (their message, the plans
    they told Mothli): a book named there that passes every rule above comes first. Where several
    editions answer to one title, the one these rules put first.
    """
    if not settings.classics_enabled:
        return []
    rule, year = max(int(settings.classics_min_years_since_death), 0), year or datetime.now(UTC).year
    languages, taken, numbers = set(), {}, set()
    liked, genres = set(), set()          # authors of finished books; genres of those finished or well begun
    unfinished, avoid = set(), set()      # works on the shelf not read to the end, and their authors
    for item in books:
        book = item["book"]
        title, author = book.get("title") or "", book.get("author") or ""
        source, surname = book.get("source") or "", author_key(author)
        own = find_classic(source.removeprefix("gutenberg:"), min_years_since_death=rule) if source.startswith(
            "gutenberg:") else None
        language = item["manifest"].get("content_language", "und").split("-", 1)[0]
        if language != "und" or own:
            languages.add(language if language != "und" else own["language"])
        if own:
            numbers.add(own["pg"])
        taken.setdefault(_work(title), []).append(surname)
        whole, chapters = _read_in_full(item)
        if chapters and whole == chapters:
            liked |= {(name, _initial(author)) for name in _spellings(surname, rule, year, True)}
        else:
            avoid |= _spellings(surname, rule, year, False)
            unfinished |= {_work(title), _work(own["title"]) if own else ""} - {""}
        if chapters and whole >= min(WELL_INTO, ceil(chapters / 4)):
            known = own or find_classic(match_title(title, author, min_years_since_death=rule) or "",
                                        min_years_since_death=rule)
            genres |= set(known["genres"]) if known else set()
    for entry in elsewhere:
        if entry.get("status") == "want":
            continue                                # not read: free to suggest
        if entry.get("classic_id"):
            numbers.add(int(entry["classic_id"]))
        taken.setdefault(_work(entry.get("title") or ""), []).append(author_key(entry.get("author") or ""))

    pool = _pool(rule, year)
    by_surname = {}
    for name, initial in liked:
        by_surname.setdefault(name, set()).add(initial)

    def same_author(row, surname):
        # The same surname, give or take a spelling, and the same first initial where both say:
        # Louisa May Alcott is not John V. Allcott, nor George Eliot T. S. Eliot.
        initials = by_surname.get(surname)
        return bool(initials) and ("" in initials or _initial(row.author) in initials | {""})

    free = [(not same_author(row, surname), -len(row.genres & genres), place, row, surname)
            for language in languages or {"en"} for place, row, work, surname in pool.get(language, ())
            if row.pg not in numbers and surname not in avoid
            and not any(_alike(surname, other) for other in taken.get(work, ()))]
    free.sort(key=lambda found: found[:3])
    texts = [text for text in named if text and text.strip()]
    if texts:
        first, titles = set(), set()
        for found in free:
            title = _words(short_title(found[3].title))
            if title not in titles and _named_in(texts, found[3]):
                titles.add(title)
                first.add(found[3].pg)
        free.sort(key=lambda found: found[3].pg not in first)        # stable: otherwise as before
    chosen, by_author = [], {}
    for _author, _genres, _place, row, surname in free:
        if len(chosen) == limit:
            break
        if surname and by_author.get(surname, 0) == PER_AUTHOR:
            continue
        line = _words(f"{row.title} {row.blurb}")
        if any(f" {work} " in line for work in unfinished):
            continue                              # an adaptation of, a sequel to or a study of a book in progress
        by_author[surname] = by_author.get(surname, 0) + 1
        chosen.append({"classic_id": str(row.pg), "title": short_title(row.title), "author": row.author,
                       "genres": [label for genre, label in GENRES.items() if genre in row.genres],
                       "blurb": row.blurb})
    return chosen


def suggestion_line(book):
    """Everything Mothli knows of one book it may suggest, as the writer and the checker read it."""
    return (f"“{book['title']}” by {book['author'] or 'an unknown author'} "
            f"({', '.join(book['genres'])}): {book['blurb']}")


def _said(text):
    return " ".join(fold(text).replace("’", "'").split())


def suggested_in(sentences, candidates):
    """The listed books these shown sentences name: what a reply may offer to add to the shelf.

    A book counts when its title, as listed, stands in a sentence as whole words, whatever the
    capitals. A title of one word ("Emma", "She") counts only when the author's surname is in
    the reply too. Decided here from what the reader was shown, never by the writer. At most three.
    """
    reply = [_said(sentence) for sentence in sentences]
    whole = " ".join(reply)
    named = []
    for book in candidates:
        title = _said(book["title"])
        if not title or not any(re.search(rf"(?<!\w){re.escape(title)}(?!\w)", sentence) for sentence in reply):
            continue
        surname = author_key(book["author"])
        if " " not in title and not (surname and re.search(rf"(?<!\w){re.escape(surname)}(?!\w)", whole)):
            continue
        named.append({"classic_id": book["classic_id"], "title": book["title"], "author": book["author"]})
    return named[:3]
