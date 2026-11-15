"""Where the reader is, said the way a friend reading along would know it."""
from app.reader_place import APP_HELP, place_line, spans


def item(*, here, whole=(), part=(), total=61, skimmed=(), asked=()):
    chapters = [{"ordinal": n, "whole": True} for n in whole] + [{"ordinal": n, "whole": False} for n in part]
    return {"book": {"title": "Pride and Prejudice", "author": "Jane Austen"},
            "chapters": sorted(chapters, key=lambda c: c["ordinal"]),
            "numbers": {n: n for n in range(1, total + 1)}, "point": {"atom": here},
            "skimmed": set(skimmed), "asked": set(asked)}


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
    # "1 through 4" is a range like "1 to 4"; read as only chapter 1, the button undercounts.
    assert offered(["I wasn’t with you for chapters 1 through 4."], reader) == [("p", [1, 2, 3, 4], "chapters 1 to 4")]
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


# --- chapters the two of them went through quickly (skimmed here), apart from chapters leapt past ---
QUICKLY = "The two of you went through {} quickly here, before where the reader is"
ALREADY = "You have already brought this up with the reader in this conversation: don't ask again."


def test_chapters_gone_through_quickly_here_are_told_apart_from_chapters_leapt_past():
    """Catches: skimmed chapters handed to the writer as "not read with you", so Mothli tells a
    reader who was right here turning pages that it was not with them."""
    line = place_line(item(here=4, whole=[1], part=[4], skimmed=[2, 3]))
    assert "the reader is in chapter 4 of 61 now. Read with you: chapter 1 in full, and part of chapter 4." in line
    assert QUICKLY.format("chapters 2 and 3") in line and "did not take them in" in line
    assert "Not read with you" not in line and "elsewhere" not in line and "not there" not in line
    # A leap leaves nothing skimmed: those chapters are still "not read with you", with no word
    # of going quickly.
    leapt = place_line(item(here=4, whole=[1], part=[4]))
    assert "Not read with you: chapters 2 and 3, which come before where the reader is." in leapt
    assert "quickly" not in leapt and "elsewhere" in leapt
    # Both in one book: each chapter is named once, under what happened to it.
    both = place_line(item(here=5, part=[5], skimmed=[3, 4]))
    assert "Not read with you: chapters 1 and 2, which" in both and QUICKLY.format("chapters 3 and 4") in both


def test_only_chapters_before_the_place_and_not_read_in_full_are_named_as_gone_through_quickly():
    """Catches: naming as gone through quickly a chapter since read in full, the chapter the
    reader is in, or one ahead of them (pages flicked forward and back again)."""
    line = place_line(item(here=4, whole=[1, 2], part=[4], skimmed=[1, 2, 3, 4, 6]))
    assert QUICKLY.format("chapter 3") in line and line.count("quickly") == 1
    # A chapter partly read and partly turned through quickly is both, as the reader app asks.
    line = place_line(item(here=4, whole=[1], part=[3, 4], skimmed=[2, 3]))
    assert "part of chapters 3 and 4" in line and QUICKLY.format("chapters 2 and 3") in line
    assert "quickly" not in place_line(item(here=0, skimmed=[1, 2]))            # nothing read, no place


def test_gone_through_quickly_is_said_in_chapter_numbers_not_in_the_books_own_page_order():
    """Catches: the position of a section in the file given as its chapter number. The first two
    sections here are a title page and a preface, so section 4 is chapter 2."""
    reader = {**item(here=6, part=[6], skimmed=[4, 5]), "numbers": {3: 1, 4: 2, 5: 3, 6: 4}}
    line = place_line(reader)
    assert "the reader is in chapter 4 of 4 now" in line and QUICKLY.format("chapters 2 and 3") in line
    assert "Not read with you: chapter 1, which" in line


def test_once_mothli_has_asked_about_those_chapters_it_is_told_not_to_ask_again():
    """Catches: the same question under every reply. The writer is given the place line each
    time; without this it is told each time to ask."""
    asked = place_line(item(here=4, whole=[1], part=[4], skimmed=[2, 3], asked=[2, 3]))
    assert QUICKLY.format("chapters 2 and 3") in asked and asked.endswith(ALREADY)
    assert "already" not in place_line(item(here=4, whole=[1], part=[4], skimmed=[2, 3]))
    # Asked about chapter 2 only, and chapter 3 has been gone through quickly since: still to ask.
    assert "already" not in place_line(item(here=4, whole=[1], part=[4], skimmed=[2, 3], asked=[2]))


def test_what_counts_as_already_asked_is_read_from_mothlis_own_earlier_sentences():
    """Catches: "already asked" worked out from anything but what Mothli said; and an older
    "I wasn't with you for chapters 2 and 3" taken for the question, so that it is never asked."""
    from app.reader_place import note_asked
    pride = book("p", "Pride and Prejudice", here=4, whole=[1], part=[4], skimmed=[2, 3])
    note_asked([pride], ["I wasn’t with you for chapters 2 and 3.", "Jane is ill in chapter 4."])
    assert pride["asked"] == set() and "already" not in place_line(pride)
    note_asked([pride], ["We went through chapters 2 and 3 quickly.", "Did you read them?"])
    assert pride["asked"] == {2, 3} and place_line(pride).endswith(ALREADY)
    other = book("k", "The Brothers Karamazov", here=3, part=[3], total=5, skimmed=[1, 2])
    note_asked([pride, other], ["Did you read chapters 1 and 2 of The Brothers Karamazov?"])
    assert (pride["asked"], other["asked"]) == (set(), {1, 2})


def test_the_speaking_rules_ask_about_skimmed_chapters_and_never_say_what_is_in_them():
    """Catches: the rule for skimmed chapters dropped or turned into "you haven't read them"; the
    voice rewritten on the way."""
    from app.reader_place import FRIEND_VOICE, SPEAK_AS_A_FRIEND
    rules = " ".join(SPEAK_AS_A_FRIEND.split())
    assert "went through" in rules and "quickly" in rules and "ask whether they read them" in rules
    assert "never that they haven't read them" in rules and "nothing about what is in them" in rules
    assert "don't ask again" in rules
    # The app's own account no longer calls quickly turned pages ones Mothli "was not there for".
    assert "was not there for them" not in APP_HELP
    assert FRIEND_VOICE.startswith("HOW TO TALK: you are the friend reading this alongside them")


def test_the_offer_appears_when_mothli_says_the_two_of_them_went_through_chapters_quickly():
    """Catches: no "I've read..." button under the new wording, which never says "wasn't with you"."""
    pride = book("p", "Pride and Prejudice", here=4, whole=[1], part=[4], skimmed=[2, 3])
    for said in ("We went through chapters 2 and 3 pretty quickly, so I didn’t take them in.",
                 "You went through chapters 2 and 3 quickly.",
                 "Honestly, we flipped through chapters 2 and 3.",
                 "We skimmed chapters 2 and 3, didn’t we?",
                 "Skimmed past chapters 2 and 3, the pair of us.",
                 "You and I raced through chapters 2 to 3 back there.",
                 "I think we went a bit fast through chapters 2 and 3 for me to follow.",
                 "I didn’t really take in chapters 2 and 3.",
                 "Did you read chapters 2 and 3?",
                 "Have you already read chapters 2 and 3 somewhere?"):
        assert offered([said], pride) == [("p", [2, 3], "chapters 2 and 3")], said
    # A chapter leapt past is offered under either wording, and both may stand in one sentence.
    leapt = book("p", "Pride and Prejudice", here=4, part=[4], skimmed=[2, 3])
    assert offered(["I wasn’t with you for chapter 1, and we flipped through chapters 2 and 3."], leapt) == [
        ("p", [1, 2, 3], "chapters 1 to 3")]


def test_quick_wording_offers_exactly_the_named_chapters_gone_past_and_nothing_otherwise():
    """Catches: a button that counts as read chapters Mothli did not ask about, or chapters never
    gone past. Pressing it puts their text in front of the models."""
    pride = book("p", "Pride and Prejudice", here=4, whole=[1], part=[4], skimmed=[2, 3])
    assert offered(["We went through chapters 1 to 6 quickly."], pride) == [("p", [2, 3], "chapters 2 and 3")]
    assert offered(["We flipped through chapter 3."], pride) == [("p", [3], "chapter 3")]
    assert offered(["We flipped through chapters 5 and 6."], pride) == []        # ahead of the reader
    assert offered(["We raced through chapter 1, didn’t we?"], pride) == []      # read in full
    assert offered(["We went through chapters 2 and 3 together."], pride) == []  # nothing says quickly
    assert offered(["We flipped through those pages quickly."], pride) == []     # no chapter named
    # Someone in the book hurrying, or the chapter standing in another part of the sentence.
    assert offered(["Lydia raced through her story in chapter 3."], pride) == []
    assert offered(["We flipped through the early pages, and chapter 3 has the ball."], pride) == []
    assert offered(["I didn’t take it in, though chapter 3 is where the ball is."], pride) == []
    other = book("k", "The Brothers Karamazov", here=3, part=[3], total=5, skimmed=[1, 2])
    assert offered(["We flipped through chapters 2 and 3."], pride, other) == []     # which book?
    assert offered(["We flipped through chapters 1 and 2 of The Brothers Karamazov."], pride, other) == [
        ("k", [1, 2], "chapters 1 and 2")]
