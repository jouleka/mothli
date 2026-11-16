"""Books Mothli may suggest that are not on the shelf: chosen by fixed rules from the free
library's catalogue, known only by their catalogue line, and offered to be added only when a
sentence the reader was shown names one. Each test says what it catches."""
import json
import re
import sys
from math import ceil
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "api"))
from _epub import three_chapter_book  # noqa: E402
from test_chapter_notes import models  # noqa: E402
from test_home_conversation import ask, book_ids, shelf as shelf  # noqa: E402,F401
from test_skimmed_pages import everything_sent  # noqa: E402

from app import home_conversation, home_suggestions  # noqa: E402
from app.api import classics as classics_api  # noqa: E402
from app.classics import find_classic, title_key  # noqa: E402
from app.config import Settings  # noqa: E402
from app.classics import rows_on_offer  # noqa: E402
from app.home_suggestions import LIMIT, short_title, suggested_in, suggestion_line, suggestions  # noqa: E402

ON = Settings(_env_file=None, allow_stub=True)
OFF = Settings(_env_file=None, allow_stub=True, classics_enabled=False)


def on_shelf(title, author, *, read=0, of=20, language="und", source=None):
    """One shelf book as ``shelf_books`` gives it: ``read`` of its ``of`` chapters read in full."""
    return {"book": {"book_id": title_key(title).replace(" ", "-"), "title": title, "author": author, "source": source},
            "chapters": [{"ordinal": n, "whole": True} for n in range(1, read + 1)],
            "numbers": {n: n for n in range(1, of + 1)}, "manifest": {"content_language": language}}


def elsewhere(title, author, status="read", classic_id=None):
    return {"title": title, "author": author, "status": status, "classic_id": classic_id,
            "rating": None, "finished_on": None}


def titles(found):
    return [book["title"] for book in found]


OWNER = [on_shelf("Pride and Prejudice", "Jane Austen", read=61, of=61),
         on_shelf("The Brothers Karamazov", "Fyodor Dostoevsky", read=30, of=96)]
FINISHED = [OWNER[0], on_shelf("The Brothers Karamazov", "Fyodor Dostoevsky", read=96, of=96)]
REVIEWED = Path(home_suggestions.__file__).with_name("classics_reviewed.tsv")


def _forget():
    for cached in ("_pool", "_surnames", "_spellings", "reviewed"):
        if hasattr(getattr(home_suggestions, cached, None), "cache_clear"):
            getattr(home_suggestions, cached).cache_clear()


@pytest.fixture
def fresh():
    """The candidate pool built again for this test, and again after it."""
    _forget()
    yield
    _forget()


@pytest.fixture
def every_blurb_allowed(monkeypatch, fresh):
    """Every catalogue blurb counted as read and clean, for a test of a rule that has nothing to do
    with the review: the books it needs (a French novel, another hand's sequel) are far down the
    catalogue, where nothing has been read, and so would never be candidates at all."""
    monkeypatch.setattr(home_suggestions, "_clean", lambda: frozenset(row.pg for row in rows_on_offer()))


def the_file():
    """The reviewed file as written: [(number, verdict, why)], comments left out."""
    rows = []
    for line in REVIEWED.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#"):
            number, verdict, *why = line.split("\t")
            rows.append((int(number), verdict, why[0] if why else ""))
    return rows


# --- which books -----------------------------------------------------------------------------------
def test_a_suggestion_is_in_a_language_of_the_readers_own_books(every_blurb_allowed):
    """Catches: an English novel offered to someone whose shelf is French; the catalogue's most
    read being offered whatever the reader reads. (No French blurb has been read, so with the real
    file a French shelf gets nothing; here every blurb is let through to test the language rule.)"""
    french = suggestions([on_shelf("Germinal", "Émile Zola", language="fr-FR")], [], ON)
    assert len(french) == LIMIT and {find_classic(book["classic_id"])["language"] for book in french} == {"fr"}
    both = suggestions([on_shelf("Germinal", "Émile Zola", language="fr"), on_shelf("Emma", "Jane Austen", language="en")],
                       [], ON, limit=200)
    assert {find_classic(book["classic_id"])["language"] for book in both} == {"fr", "en"}
    # A book added from the free library is in the language the catalogue gives it.
    finnish = suggestions([on_shelf("Morgue kadun kauhu", "Edgar Allan Poe", source="gutenberg:76496")], [], ON)
    assert {find_classic(book["classic_id"])["language"] for book in finnish} == {"fi"}
    # Nothing on the shelf says its language: English, the language Mothli and the blurbs are in.
    unknown = suggestions([on_shelf("The Lamp Book", "Ada Writer")], [], ON)
    assert {find_classic(book["classic_id"])["language"] for book in unknown} == {"en"}


def test_a_book_already_on_the_shelf_is_never_suggested():
    """Catches: "add Pride and Prejudice" for a reader who has their own copy of it; the same
    work coming back because the reader's file spells the author differently (Dostoevsky) or was
    added from the library under another title."""
    plain = titles(suggestions([on_shelf("The Lamp Book", "Ada Writer")], [], ON))
    assert plain[:2] == ["Pride and Prejudice", "Frankenstein"]
    have = [on_shelf("Pride and Prejudice", "Austen, Jane"),
            on_shelf("A Modern Prometheus (1818 text)", "M. Shelley", source="gutenberg:84"),
            on_shelf("The Brothers Karamazov", "Fyodor Dostoevsky", read=96, of=96)]
    found = suggestions(have, [], ON, limit=60)
    assert not {"Pride and Prejudice", "Frankenstein", "The Brothers Karamazov"} & set(titles(found))
    assert "84" not in {book["classic_id"] for book in found}
    assert "Crime and Punishment" in titles(found)             # the author's other books still are


def test_a_book_read_in_another_app_is_not_suggested_but_one_only_wanted_may_be():
    """Catches: suggesting as new a book the reading history says was read, is being read or was
    set aside; and treating "wants to read" as read, which would hide the best suggestion."""
    for status in ("read", "reading", "abandoned"):
        found = suggestions([on_shelf("The Lamp Book", "Ada Writer")],
                            [elsewhere("Frankenstein", "Mary Shelley", status),
                             elsewhere("Moby-Dick", "Herman Melville", status, classic_id="2701")], ON, limit=30)
        assert not {"Frankenstein", "Moby Dick"} & set(titles(found)), status
        assert "Pride and Prejudice" in titles(found)
    wanted = suggestions([on_shelf("The Lamp Book", "Ada Writer")], [elsewhere("Frankenstein", "Mary Shelley", "want")], ON)
    assert "Frankenstein" in titles(wanted)


def test_the_order_is_finished_authors_then_shared_genres_then_the_most_read():
    """Catches: the ranking drifting from what is written down. Books by an author of a finished
    book first, then the number of genres shared with the books finished or well begun, then the
    catalogue's order; at most two by one author."""
    found = suggestions(FINISHED, [], ON)
    assert titles(found) == ["Sense and Sensibility", "Northanger Abbey", "Crime and Punishment", "The Idiot",
                             "Middlemarch", "Jane Eyre"]
    assert [book["author"] for book in found[:4]] == ["Jane Austen"] * 2 + ["Fyodor Dostoyevsky"] * 2
    liked = {"Classics", "Romance"}                            # Pride and Prejudice's and the Karamazovs'
    assert all(liked <= set(book["genres"]) for book in found[4:])
    # Without anything read, nothing is preferred: the catalogue's own order.
    assert titles(suggestions([on_shelf("The Lamp Book", "Ada Writer")], [], ON)) == [
        "Pride and Prejudice", "Frankenstein", "Moby Dick", "Alice's Adventures in Wonderland",
        "The Adventures of Sherlock Holmes", "Dracula"]


def test_an_author_is_the_same_person_not_a_surname_that_looks_alike():
    """Catches: Ostrovsky's plays put first for a reader of Dostoevsky, John V. Allcott's
    "Colonial Homes in North Carolina" for a reader of Louisa May Alcott, T. S. Eliot for George."""
    far = suggestions(FINISHED, [], ON, limit=40)
    assert not [book for book in far[:12] if "Ostrovsky" in book["author"]]
    alcott = suggestions([on_shelf("Little Women", "Alcott, Louisa May", read=47, of=47)], [], ON)
    assert [book["author"] for book in alcott[:3]] == ["Louisa May Alcott", "Louisa May Alcott", "Lewis Carroll"]
    eliot = suggestions([on_shelf("Middlemarch", "George Eliot", read=86, of=86)], [], ON)
    assert [book["author"] for book in eliot[:2]] == ["George Eliot"] * 2 and "T. S. Eliot" not in {
        book["author"] for book in eliot}


def test_nothing_by_the_author_of_a_book_the_reader_has_not_finished():
    """Catches: a sequel's blurb telling how the book in progress comes out. The catalogue's line
    for Little Men is 'This sequel to "Little Women" follows Jo Bhaer and her husband'; for The
    Grand Inquisitor, what is in a chapter far into The Brothers Karamazov. Both are by the
    author of the book on the shelf, which is how they came first before this rule."""
    for read in (0, 1, 20, 46):
        found = suggestions([on_shelf("Little Women", "Louisa May Alcott", read=read, of=47)], [], ON, limit=400)
        assert not [book for book in found if "Alcott" in book["author"]], read
    in_progress = suggestions(OWNER, [], ON, limit=400)
    assert not [book for book in in_progress if "Dostoyevsky" in book["author"] or "Ostrovsky" in book["author"]]
    assert titles(in_progress)[:2] == ["Sense and Sensibility", "Northanger Abbey"]       # Austen is finished
    # Finished with Mothli, the author's other books are the first to be suggested.
    done = suggestions([on_shelf("Little Women", "Louisa May Alcott", read=47, of=47)], [], ON)
    assert [book["author"] for book in done[:2]] == ["Louisa May Alcott"] * 2
    # One unfinished book by an author keeps that author out, whatever else of theirs is finished.
    mixed = [on_shelf("Emma", "Jane Austen", read=55, of=55), on_shelf("Persuasion", "Jane Austen", read=3, of=24)]
    assert not [book for book in suggestions(mixed, [], ON, limit=400) if "Austen" in book["author"]]


def test_nothing_whose_line_names_a_book_the_reader_has_not_finished(every_blurb_allowed):
    """Catches: another hand's sequel or adaptation, which the author rule cannot see. "Edmond
    Dantès" by Edmund Flagg is 'a sequel to the famous "The Count of Monte-Cristo"'; there is a
    play "founded on Jane Austen's novel" that carries the novel's own title. (Those books' blurbs
    have not been read; every blurb is let through here so that this rule is still tried.)"""
    monte = [on_shelf("The Count of Monte Cristo", "Alexandre Dumas", read=60, of=117)]
    found = suggestions(monte, [], ON, limit=5000)
    assert len(found) > 3000 and not {"Edmond Dantès", "The Son of Monte-Cristo", "Monte-Cristo's Daughter"} & set(
        titles(found))
    assert not [book for book in found if "count of monte cristo" in title_key(book["blurb"])]
    pride = suggestions([on_shelf("Pride and Prejudice", "Jane Austen", read=5, of=61)], [], ON, limit=5000)
    assert not [book for book in pride if "pride and prejudice" in title_key(book["title"] + " " + book["blurb"])]
    # Once the book is finished such a book gives nothing away, and may be suggested.
    done = suggestions([on_shelf("The Count of Monte Cristo", "Alexandre Dumas", read=117, of=117)], [], ON, limit=5000)
    assert "Edmond Dantès" in titles(done)


def test_a_book_only_just_opened_says_nothing_of_the_readers_taste():
    """Catches: one page of a book steering every suggestion. "Well into" is a quarter of the
    chapters read in full, or ten of them."""
    plain = suggestions([on_shelf("The Lamp Book", "Ada Writer")], [], ON, limit=8)
    opened = suggestions([on_shelf("Dracula", "Bram Stoker", read=2, of=27)], [], ON)
    assert titles(opened) == [title for title in titles(plain) if title != "Dracula"][:LIMIT]
    for read, of in ((10, 96), (ceil(27 / 4), 27), (1, 3)):
        begun = suggestions([on_shelf("Dracula", "Bram Stoker", read=read, of=of)], [], ON)
        assert titles(begun)[0] == "The strange case of Dr. Jekyll and Mr. Hyde", (read, of)   # all four of its genres
        assert set(begun[0]["genres"]) == {"Classics", "Mystery & crime", "Science fiction & fantasy", "Horror & gothic"}


def test_with_the_free_library_switched_off_there_is_nothing_to_suggest():
    """Catches: suggesting a book the reader then cannot add."""
    assert suggestions(OWNER, [], OFF) == []


# --- only blurbs read and judged clean -------------------------------------------------------------
# The 40 numbers of the deny list this file replaced (SPOILING_BLURBS, 15 November 2026).
OLD_DENY_LIST = frozenset({
    1513, 1777, 1533, 2264, 13868, 6975, 16893, 27761, 2265, 1122, 1787, 10606, 56454, 15032, 25667, 7276,
    15632, 59828, 151, 910, 271, 11860, 78051, 78086, 76899, 76280, 22908, 69576, 1097, 41085, 393, 10676,
    81, 2788, 52900, 108, 221, 544, 68, 561})
ANYONE = [on_shelf("The Lamp Book", "Ada Writer")]


def test_a_book_whose_blurb_was_never_read_is_never_suggested_however_popular():
    """Catches: the old way round, where a blurb counted as safe unless someone had found it
    spoiling, so 76,000 blurbs nobody had read went to Mothli. Villette is the most read English
    book whose blurb was not read; it was a candidate before."""
    clean = {number for number, verdict, _why in the_file() if verdict == "clean"}
    found = suggestions(ANYONE, [], ON, limit=100_000)
    assert found and {int(book["classic_id"]) for book in found} <= clean
    assert 9182 not in {number for number, _verdict, _why in the_file()}
    assert "9182" not in {book["classic_id"] for book in found}
    # In French nothing has been read, so a French shelf is offered nothing at all.
    assert suggestions([on_shelf("Germinal", "Émile Zola", language="fr")], [], ON, limit=100_000) == []


def test_a_book_whose_blurb_was_read_and_judged_not_clean_is_never_suggested():
    """Catches: a "no" in the file being ignored or read as "clean". The Monk ("a dark tale of
    lust, murder, and demonic pacts") was fourth of six for the owner with Pride and Prejudice
    finished and The Brothers Karamazov well begun; Anna Karenina's line tells the affair's course."""
    refused = {number for number, verdict, _why in the_file() if verdict == "no"}
    found = suggestions(ANYONE, [], ON, limit=100_000)
    assert not refused & {int(book["classic_id"]) for book in found}
    assert "The Monk" not in titles(suggestions(OWNER, [], ON))
    assert "Anna Karenina" not in titles(suggestions(OWNER, [], ON, limit=100_000))


def test_a_book_judged_clean_can_be_suggested_and_the_file_is_the_only_list(monkeypatch, tmp_path, fresh):
    """Catches: the file not being what decides; a second list in the code still keeping a book
    out. With a file of three lines, the one clean book is the only candidate, even a number the
    old deny list in the code held."""
    own = tmp_path / "reviewed.tsv"
    own.write_text("# a test file\n1342\tclean\n84\tno\tbecause\n1513\tclean\n", encoding="utf-8")
    monkeypatch.setattr(home_suggestions, "REVIEWED_PATH", own)
    assert sorted(book["classic_id"] for book in suggestions(ANYONE, [], ON, limit=100)) == ["1342", "1513"]
    assert not hasattr(home_suggestions, "SPOILING_BLURBS")


def test_the_reviewed_file_is_whole_and_says_what_it_is():
    """Catches: a typo'd verdict silently keeping a book out or letting it in; a number twice with
    two verdicts; a number that is no book; a "no" with no reason anyone can check."""
    rows = the_file()
    numbers = [number for number, _verdict, _why in rows]
    assert len(numbers) == len(set(numbers)) >= 640
    assert all(find_classic(str(number)) for number in numbers)
    assert {verdict for _number, verdict, _why in rows} == {"clean", "no"}
    assert all(why.strip() for _number, verdict, why in rows if verdict == "no")
    loaded = home_suggestions.reviewed()
    assert {number: verdict for number, (verdict, _why) in loaded.items()} == {
        number: verdict for number, verdict, _why in rows}
    head = " ".join(" ".join(line.lstrip("# ") for line in REVIEWED.read_text(encoding="utf-8").splitlines()
                             if line.startswith("#")).split())
    assert "spoiler-free blurb" in head and "16 November 2026" in head
    read = [verdict for number, verdict, _why in rows if number not in OLD_DENY_LIST]
    assert f"{len(read)} blurbs read, {read.count('clean')} clean, {read.count('no')} no" in head


def test_the_file_refuses_a_bad_line(tmp_path):
    """Catches: a loader that would let a duplicate or an unknown verdict through."""
    for text in ("1342\tclean\n1342\tno\twhy\n", "1342\tmaybe\n", "84\tno\n"):
        bad = tmp_path / "bad.tsv"
        bad.write_text(text, encoding="utf-8")
        with pytest.raises(ValueError):
            home_suggestions.reviewed(bad)


def test_every_book_of_the_old_deny_list_is_in_the_file_as_no():
    """Catches: the deny list lost when it was moved into the file."""
    verdicts = {number: verdict for number, verdict, _why in the_file()}
    assert {number: verdicts.get(number) for number in OLD_DENY_LIST} == dict.fromkeys(OLD_DENY_LIST, "no")


def test_a_blurb_that_gives_something_away_keeps_its_book_out_and_a_clean_edition_stands_in(monkeypatch, fresh):
    """Catches: a refused blurb reaching Mothli, which repeats what it is given; and the work
    being lost when another edition's blurb is clean."""
    monkeypatch.setattr(home_suggestions, "PER_AUTHOR", 1000)      # so that every play of Shakespeare's is in reach
    found = suggestions(ANYONE, [], ON, limit=1000)
    assert not OLD_DENY_LIST & {int(book["classic_id"]) for book in found}
    by_title = {book["title"]: book for book in found}
    assert by_title["Romeo and Juliet"]["classic_id"] == "1112" and "marry" not in by_title["Romeo and Juliet"]["blurb"]
    assert "murdered" not in by_title["Hamlet"]["blurb"]
    assert "White Fang" not in by_title and "Mrs. Warren's Profession" not in by_title     # no clean edition


def test_one_line_a_work_never_one_volume_of_a_set_and_always_something_to_say():
    """Catches: three editions of Moby Dick filling the list; "Vol. 5 of 9" offered as a book to
    read; a book with no blurb, of which Mothli could say nothing."""
    found = suggestions(OWNER + [on_shelf("Henry V", "William Shakespeare", read=5, of=5)], [], ON, limit=300)
    keys = [(title_key(book["title"]), book["author"]) for book in found]
    assert len(keys) == len(set(keys)) == 300
    whole = [find_classic(book["classic_id"])["title"] for book in found]
    assert not [title for title in whole if re.search(r"\bVol(?:ume)?\b\.? ?\d|\bTome [IVX\d]|\(of \d+\)", title)]
    assert all(book["blurb"] and book["genres"] for book in found)
    assert suggestions(OWNER, [], ON) == suggestions(list(OWNER), [], ON)   # the same list every time


def test_no_single_volume_of_a_french_set_either(every_blurb_allowed):
    """Catches: "Tome I" or "(2/3)" offered as a book. No French blurb has been read, so every
    blurb is let through here to try the pattern on the sets the catalogue numbers in French."""
    plain = [find_classic(book["classic_id"])["title"] for book in suggestions(
        [on_shelf("Les Misérables", "Victor Hugo", language="fr", read=20)], [], ON, limit=40)]
    assert plain and not [title for title in plain if "Tome" in title or "/3)" in title]


def test_the_title_mothli_is_given_is_the_one_a_person_would_say():
    """Catches: "Frankenstein; or, The modern Prometheus" or "Peter Pan : $b [Peter and Wendy]"
    as the name on the button and the name a reply would have to repeat to be recognised."""
    assert short_title("Frankenstein; or, The modern Prometheus") == "Frankenstein"
    assert short_title("Peter Pan : $b [Peter and Wendy]") == "Peter Pan"
    assert short_title("Jane Eyre: An Autobiography") == "Jane Eyre"
    assert short_title("The Portrait of a Lady — Volume 1") == "The Portrait of a Lady"
    assert short_title("Emma") == "Emma" and short_title("Moby-Dick; or, The Whale") == "Moby-Dick"


# --- what a reply names ----------------------------------------------------------------------------
EMMA = {"classic_id": "158", "title": "Emma", "author": "Jane Austen", "genres": ["Romance", "Humour"],
        "blurb": find_classic("158")["blurb"]}
CALL = {"classic_id": "215", "title": "The call of the wild", "author": "Jack London", "genres": ["Adventure"],
        "blurb": find_classic("215")["blurb"]}
SHE = {"classic_id": "3155", "title": "She", "author": "H. Rider Haggard", "genres": ["Adventure"], "blurb": "x"}


def named(*sentences, candidates=(EMMA, CALL, SHE)):
    return [book["title"] for book in suggested_in(list(sentences), list(candidates))]


def test_a_reply_offers_to_add_exactly_the_listed_books_its_shown_sentences_name():
    """Catches: a button for a book the reply did not name; none for one it did; a button
    decided by anything but the sentences the reader saw."""
    assert named("You might like The Call of the Wild by Jack London.") == ["The call of the wild"]
    assert named("Try Emma, by Jane Austen.", "Or The Call of the Wild.") == ["Emma", "The call of the wild"]
    assert named("Honestly, anything by Jane Austen might suit you.") == []
    assert named("You might like Middlemarch.") == []                        # not on the list
    assert suggested_in(["Try Emma, by Jane Austen."], [EMMA]) == [
        {"classic_id": "158", "title": "Emma", "author": "Jane Austen"}]
    assert named() == [] and named("Try Emma by Jane Austen.", candidates=()) == []


def test_a_one_word_title_counts_only_beside_its_authors_name():
    """Catches: "Add Emma to my shelf" under a reply about Emma Bovary, and "Add She to my
    shelf" under any sentence with the word "she"."""
    assert named("Emma has just married Charles in Madame Bovary.") == []
    assert named("She walked to Meryton in the rain.") == []
    assert named("She, by H. Rider Haggard, might be fun.") == ["She"]
    assert named("How about Emma?", "It’s Jane Austen again.") == ["Emma"]
    assert named("Emmaline moved the lamp, said Austen.") == []               # a whole word, not a part of one


# --- in the home conversation ----------------------------------------------------------------------
def block(prompt):
    return prompt.split("BOOKS YOU CAN SUGGEST")[1].split("\n\nWHAT YOU HAVE READ TOGETHER")[0].splitlines()[1:]


def test_the_list_reaches_the_writer_and_the_checker_and_only_the_catalogue_line_of_each_book(shelf, monkeypatch):
    """Catches: the writer told to suggest with nothing to suggest from; a sentence about a
    listed book that no check could hold to its line; anything of a book beyond its catalogue
    line (or, for the unread book on the shelf, beyond its title) reaching any model."""
    api, app, _ = shelf
    seen = models(app, monkeypatch, reply="You might like Frankenstein by Mary Wollstonecraft Shelley.")
    sent = everything_sent(app, monkeypatch)
    ask(api, "Can you suggest something new to read?")
    prompt, lines = seen["reply"][-1], block(seen["reply"][-1])
    assert len(lines) == LIMIT
    for line in lines:
        title = line.split("“")[1].split("”")[0]
        number = next(str(n) for n in (1342, 84, 2701, 11, 1661, 345) if short_title(find_classic(str(n))["title"]) == title)
        entry = find_classic(number)
        labels = {"classics": "Classics", "romance": "Romance", "horror": "Horror & gothic", "adventure": "Adventure",
                  "scifi": "Science fiction & fantasy", "children": "Children’s", "mystery": "Mystery & crime",
                  "stories": "Short stories"}
        genres = ", ".join(labels[genre] for genre in entry["genres"])
        assert line == f"- “{title}” by {entry['author']} ({genres}): {entry['blurb']}"       # the line and no more
    # The checker is given the same lines, each as a passage of its own.
    passages = seen["checked"][-1]["passages"]
    for line in lines:
        assert sum(passage.endswith(line[2:]) for passage in passages) == 1
    # The unread book on the shelf is still a title and nothing else: no blurb for it anywhere.
    everything = "\n".join(sent)
    assert "“Middlemarch” by George Eliot: not started" in prompt
    assert "Midlands" not in everything and "UNREAD_SHELF_CANARY" not in everything
    assert find_classic("145")["blurb"] not in everything
    # And it is not offered as new: it is on the shelf.
    assert "Middlemarch" not in "\n".join(lines)


def test_a_reply_that_names_a_listed_book_carries_the_offer_to_add_it_and_no_other(shelf, monkeypatch):
    """Catches: no button under a suggestion; a button for every listed book; a button under a
    reply that suggested nothing."""
    api, app, _ = shelf
    models(app, monkeypatch, reply="You might like Frankenstein by Mary Wollstonecraft Shelley. It’s a Gothic novel.")
    answer = ask(api, "Suggest something new?")[-1]["answer"]
    assert answer["suggested"] == [{"classic_id": "84", "title": "Frankenstein", "author": "Mary Wollstonecraft Shelley"}]
    models(app, monkeypatch, reply="Jonas pushed cart 7 in section 1.")
    assert "suggested" not in ask(api, "What did Jonas do with cart 7?")[-1]["answer"]


def test_the_offer_comes_from_the_sentences_shown_never_from_one_that_was_cut(shelf, monkeypatch):
    """Catches: a button for a book whose sentence the checks removed, so the reader is offered
    a book nothing on the screen mentions; and a claim beyond the line getting through with it."""
    api, app, _ = shelf
    models(app, monkeypatch, unsupported=["dies"],
           reply="You might like Dracula by Bram Stoker, where the Count dies at the end. Or Moby Dick by Herman Melville.")
    events = ask(api, "Suggest something new?")
    assert [e["text"] for e in events if e["type"] == "sentence"] == ["Or Moby Dick by Herman Melville."]
    assert [book["title"] for book in events[-1]["answer"]["suggested"]] == ["Moby Dick"]


def test_with_the_free_library_off_mothli_is_told_it_has_none_and_offers_none(shelf, monkeypatch):
    """Catches: Mothli offering ideas it does not have, the owner's own complaint; a button that
    would lead to a 404."""
    api, app, settings = shelf
    settings.classics_enabled = False
    seen = models(app, monkeypatch, reply="I don’t have any new books to suggest right now. Frankenstein by Mary "
                                          "Wollstonecraft Shelley is one I can’t add.")
    answer = ask(api, "Suggest something new?")[-1]["answer"]
    assert block(seen["reply"][-1]) == ["(none: you have no new books to suggest)"]
    passages = seen["checked"][-1]["passages"]
    assert "Books Mothli may suggest that are not on the reader’s shelf: none." in " ".join(passages)
    assert "Frankenstein" not in " ".join(passages) and "suggested" not in answer


def test_a_suggested_title_that_is_also_a_name_the_reader_has_not_met_is_cut_by_the_fixed_gate(shelf, monkeypatch):
    """The "Emma" question. Catches: the fixed gate loosened for suggestions (a listed book's
    words counted as read, so the name of a person the reader has not met in a shelf book gets
    through); and a button for a book whose sentence was cut."""
    api, app, _ = shelf
    monkeypatch.setattr(home_conversation, "suggestions", lambda *args, **kwargs: [EMMA])
    reply = "You might like Emma by Jane Austen. It follows a young woman who fancies herself a matchmaker."
    models(app, monkeypatch, reply=reply)
    events = ask(api, "Suggest something new?")
    assert len([e for e in events if e["type"] == "sentence"]) == 2
    assert [book["title"] for book in events[-1]["answer"]["suggested"]] == ["Emma"]
    # Now a person called Emma comes into The Lamp Book in part 3; the reader is in part 2.
    with app.state.store.book(book_ids(app)["The Lamp Book"]) as memory:
        memory.add_entity("Emma", "character", revealed_at=3)
    models(app, monkeypatch, reply=reply)
    events = ask(api, "Suggest something else?")
    assert [e["text"] for e in events if e["type"] == "sentence"] == [
        "It follows a young woman who fancies herself a matchmaker."]
    assert events[-1]["answer"]["dropped_sentences"] == 1 and "suggested" not in events[-1]["answer"]


def test_pressing_the_offer_adds_the_book_and_it_is_not_suggested_again(shelf, monkeypatch):
    """Catches: the button's id not being one the add-classic call accepts; a book suggested
    again once it is on the shelf; a book just added treated as anything but not started."""
    api, app, _ = shelf
    fetched = []
    monkeypatch.setattr(classics_api, "fetch_classic",
                        lambda entry, *, max_bytes, client=None: fetched.append(entry["id"]) or three_chapter_book())
    seen = models(app, monkeypatch, reply="You might like Frankenstein by Mary Wollstonecraft Shelley.")
    [offer] = ask(api, "Suggest something new?")[-1]["answer"]["suggested"]
    added = api.post(f"/api/classics/{offer['classic_id']}")
    assert added.status_code == 201 and fetched == ["84"]
    sources = {item["book_id"]: item["source"] for item in api.get("/api/books").json()}
    assert sources[added.json()["book_id"]] == "gutenberg:84"
    ask(api, "And another?")
    assert "Frankenstein" not in "\n".join(block(seen["reply"][-1])) and len(block(seen["reply"][-1])) == LIMIT
    assert json.dumps(seen["checked"][-1]["passages"]).count("may suggest") == LIMIT


def test_mothli_is_told_to_suggest_at_once_from_the_list_and_only_its_line():
    """Catches: the instruction that made Mothli ask "are you looking for ideas?" twice and then
    say it had none; the old line that it may only point at books on the shelf."""
    told = home_conversation.HOME_SYSTEM
    assert "BOOKS YOU CAN SUGGEST" in told and "straight away" in told
    assert "never ask it again" in told and "only that book’s own line" in told
    assert "however famous" in told and "you have no new books to suggest" in told
    assert suggestion_line(EMMA) == "“Emma” by Jane Austen (Romance, Humour): " + EMMA["blurb"]


# --- a book the reader has named (6ah) --------------------------------------------------------------
def test_a_reviewed_clean_book_the_reader_names_is_the_first_candidate():
    """Catches (6af, failed before 6ah): "maybe jane eyre", and Jane Eyre, reviewed clean and in
    the library, is not among the six, so Mothli says it knows too little of it. Named in the
    reader's words (any capitals, its subtitle left off), it comes first; the rest keep their order."""
    shelf = [on_shelf("The Lamp Book", "Ada Writer")]
    plain = suggestions(shelf, [], ON)
    assert "Jane Eyre" not in titles(plain)
    found = suggestions(shelf, [], ON, named=["i think after this one i want something lighter, maybe jane eyre"])
    assert titles(found)[0] == "Jane Eyre" and found[0]["classic_id"] == "1260"
    assert titles(found)[1:] == titles(plain)[:LIMIT - 1]
    # Named nowhere, or in nothing at all: exactly the list as before.
    assert suggestions(shelf, [], ON, named=["something lighter please", ""]) == plain
    assert suggestions(shelf, [], ON, named=[]) == plain


def test_a_one_word_title_alone_names_no_book():
    """Catches: "Emma" in "Emma Bovary was awful" or "she" in any sentence putting a book first.
    With the author's surname in the same text it counts, as for the add button."""
    plain = suggestions(OWNER, [], ON)
    for text in ("maybe emma next", "she was so lighthearted", "dracula?", "Emma Bovary bored me"):
        assert suggestions(OWNER, [], ON, named=[text]) == plain, text
    # The surname in another text is not enough: one text must name both.
    assert suggestions(OWNER, [], ON, named=["maybe emma", "something by austen"]) == plain
    found = suggestions(OWNER, [], ON, named=["maybe emma, the austen one"])
    assert titles(found)[0] == "Emma" and found[0]["author"] == "Jane Austen"
    assert titles(suggestions([on_shelf("The Lamp Book", "Ada Writer")], [], ON,
                              named=["dracula by bram stoker"]))[0] == "Dracula"


def test_a_named_book_that_breaks_a_rule_is_not_a_candidate():
    """Catches: naming a book let it past a rule. On the shelf already (Pride and Prejudice);
    by the author of a shelf book not finished (Crime and Punishment, while the Karamazovs are
    in progress); its blurb judged not clean (Anna Karenina) or never read; read in another app."""
    plain = suggestions(OWNER, [], ON)
    for text in ("maybe pride and prejudice again", "crime and punishment next?", "maybe anna karenina",
                 "what about the possessed by dostoyevsky"):
        found = suggestions(OWNER, [], ON, named=[text])
        assert found == plain, text
    history = [elsewhere("Jane Eyre", "Charlotte Brontë")]
    assert "Jane Eyre" not in titles(suggestions(OWNER, history, ON, named=["maybe jane eyre"]))
    # A book on the shelf not finished keeps out a book whose line names it, named or not.
    little = [on_shelf("Little Women", "Louisa May Alcott", read=3, of=47)]
    assert not [book for book in suggestions(little, [], ON, named=["little men by alcott"])
                if "Alcott" in book["author"]]


def test_a_named_book_whose_line_names_a_book_in_progress_is_not_a_candidate(every_blurb_allowed):
    """Catches: naming another hand's sequel ("Edmond Dantès" by Edmund Flagg, 'a sequel to the
    famous "The Count of Monte-Cristo"') putting it in front of a reader still in the Count."""
    monte = [on_shelf("The Count of Monte Cristo", "Alexandre Dumas", read=60, of=117)]
    assert "Edmond Dantès" not in titles(suggestions(monte, [], ON, named=["maybe edmond dantès next"]))
    done = [on_shelf("The Count of Monte Cristo", "Alexandre Dumas", read=117, of=117)]
    assert titles(suggestions(done, [], ON, named=["maybe edmond dantès next"]))[0] == "Edmond Dantès"


def test_named_books_still_make_at_most_six_and_two_by_one_author():
    """Catches: naming many books growing the list, or three Austens filling it."""
    found = suggestions(OWNER, [], ON, named=["mansfield park, persuasion or northanger abbey, then jane eyre"])
    assert len(found) == LIMIT
    assert set(titles(found)[:2]) < {"Mansfield Park", "Persuasion", "Northanger Abbey"}
    assert titles(found)[2] == "Jane Eyre"
    assert sum(book["author"] == "Jane Austen" for book in found) == 2
    assert len(suggestions(OWNER, [], ON, limit=3, named=["jane eyre", "middlemarch"])) == 3


@pytest.mark.parametrize("language", ["en", "en-GB", "fr"])
def test_the_language_of_a_shelf_book_is_its_main_tag(language, every_blurb_allowed):
    """Catches: "en-GB" matching no catalogue language, so a British file gets nothing. (Every
    blurb let through, so that "fr" has something to find.)"""
    found = suggestions([on_shelf("The Lamp Book", "Ada Writer", language=language)], [], ON)
    assert {find_classic(book["classic_id"])["language"] for book in found} == {language.split("-")[0]}
