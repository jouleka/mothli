"""Where the reader is, said the way a friend reading along would know it."""
from app.reader_place import APP_HELP, place_line, spans


def item(*, here, whole=(), part=(), total=61):
    chapters = [{"ordinal": n, "whole": True} for n in whole] + [{"ordinal": n, "whole": False} for n in part]
    return {"book": {"title": "Pride and Prejudice", "author": "Jane Austen"},
            "chapters": sorted(chapters, key=lambda c: c["ordinal"]),
            "numbers": {n: n for n in range(1, total + 1)}, "point": {"atom": here}}


def test_chapters_are_named_as_a_person_would():
    assert spans([4]) == "chapter 4"
    assert spans([1, 2]) == "chapters 1 and 2"
    assert spans([1, 2, 3, 5]) == "chapters 1 to 3 and 5"


def test_a_reader_who_went_quickly_to_chapter_three_is_in_chapter_three_not_nowhere():
    line = place_line(item(here=3, part=[3]))
    assert line.startswith("“Pride and Prejudice” by Jane Austen: the reader is in chapter 3 of 61 now.")
    assert "Read with you: part of chapter 3." in line
    assert "Not read with you: chapters 1 and 2, which come before where the reader is." in line
    assert "may have read them" in line and "finished" not in line


def test_reading_in_order_names_no_gap_and_an_unopened_book_says_only_that():
    line = place_line(item(here=7, whole=range(1, 7), part=[7]))
    assert "the reader is in chapter 7 of 61 now" in line and "chapters 1 to 6 in full" in line
    assert "Not read with you" not in line
    assert place_line(item(here=0)) == (
        "“Pride and Prejudice” by Jane Austen: not started. You know nothing of what is in it.")
    assert place_line(item(here=5, whole=range(1, 6), total=5)).endswith("read all 5 chapters with you.")


def test_chapters_are_counted_plainly_whatever_they_are_called():
    from app.reader_place import content_numbers
    manifest = {"atoms": [{"ordinal": 1, "title": "", "char_len": 1},            # an empty page, not a chapter
                          {"ordinal": 2, "title": "Part 1", "char_len": 9000},
                          {"ordinal": 3, "title": "", "char_len": 9000}]}
    assert content_numbers(manifest) == {2: 1, 3: 2}


def test_what_mothli_can_say_about_the_app_names_the_real_controls():
    assert "Contents" in APP_HELP and "'not read'" in APP_HELP and "read it fresh" in APP_HELP


def test_chapters_told_as_read_with_no_place_yet_name_the_next_one_to_read():
    # "I've read chapters 1 to 4" from the contents list: Mothli may say chapter 5 is next,
    # but not that the reader is in it.
    line = place_line(item(here=0, whole=range(1, 5), total=5))
    assert "Read with you: chapters 1 to 4 in full." in line
    assert "The next chapter not read with you is chapter 5." in line and "is in chapter" not in line


def test_chapters_gone_past_before_the_readers_place_can_be_offered_as_read():
    from app.reader_place import skipped_before
    assert skipped_before(item(here=3, part=[3])) == {"atoms": [1, 2], "chapters": "chapters 1 and 2", "skimmed": []}
    assert skipped_before(item(here=4, whole=[1], part=[2, 4])) == {"atoms": [2, 3], "chapters": "chapters 2 and 3", "skimmed": []}
    assert skipped_before(item(here=7, whole=range(1, 7), part=[7])) is None       # read in order
    assert skipped_before(item(here=0)) is None                                     # not started


def offered(sentences, *items):
    from app.reader_place import offers_for
    return [(offer["book_id"], offer["atoms"], offer["chapters"]) for offer in offers_for(sentences, list(items))]


def book(book_id, title, **place):
    return {**item(**place), "book": {"book_id": book_id, "title": title, "author": ""}}


def test_the_offer_counts_exactly_the_chapters_mothli_said_it_was_not_there_for():
    # The reader's page ends chapter 3 and starts chapter 4: chapter 3 was only partly read.
    pride = book("p", "Pride and Prejudice", here=4, part=[3, 4])
    said = ["I wasn’t with you for chapters 1 and 2 of Pride and Prejudice.", "In chapter 4, Jane tells Elizabeth."]
    assert offered(said, pride) == [("p", [1, 2], "chapters 1 and 2")]


def test_a_partly_read_chapter_the_reply_calls_read_together_is_not_offered():
    karamazov = book("k", "The Brothers Karamazov", here=3, part=[1, 3], total=5)
    said = ["I wasn’t with you for chapter 2, and we’ve only read parts of chapters 1 and 3 together."]
    assert offered(said, karamazov) == [("k", [2], "chapter 2")]


def test_ranges_and_lists_are_read_and_nothing_outside_the_gone_past_chapters_is_counted():
    reader = book("p", "Pride and Prejudice", here=7, part=[7])
    assert offered(["I wasn’t with you for chapters 1 to 3 and 5."], reader) == [("p", [1, 2, 3, 5], "chapters 1 to 3 and 5")]
    # Chapter 9 is ahead of the reader and chapter 7 is where they are: neither may be counted.
    assert offered(["I wasn’t with you for chapters 6, 7 and 9."], reader) == [("p", [6], "chapter 6")]


def test_no_offer_when_the_reply_names_no_chapter_or_no_book():
    reader = book("p", "Pride and Prejudice", here=4, part=[4])
    assert offered(["I wasn’t with you for the first few chapters."], reader) == []
    assert offered(["To tell me, open Contents and press the label."], reader) == []
    assert offered(["Lydia laughed."], reader) == []
    other = book("k", "The Brothers Karamazov", here=3, part=[3], total=5)
    # Two books gone past and the sentence names neither: which one is meant cannot be known.
    assert offered(["I wasn’t with you for chapters 1 and 2."], reader, other) == []
    assert offered(["I wasn’t with you for chapters 1 and 2 of The Brothers Karamazov."], reader, other) == [
        ("k", [1, 2], "chapters 1 and 2")]
