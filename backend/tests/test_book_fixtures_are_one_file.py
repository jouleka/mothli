"""The test books are one file however often they are made.

Story memory is kept once per exact file (ADR 0044): two readers share an edition only when
their files hash the same. The hosted tests give two readers "the same file" by making the test
book twice, so a builder that writes the clock into the zip (``writestr`` with a bare name
stamps every member with the time, to two seconds) made two files whenever the two makings fell
either side of a two-second mark: the second reader then had an edition of their own, and
``test_a_run_for_a_reader_at_n_reads_notes_through_n_asks_of_n_plus_1_and_nothing_further``
built parts 1 to 3 for reader A (three runs, not one) and
``test_second_reader_gets_first_readers_story_memory_as_missing_on_every_route`` handed reader B
an empty cast. Here the clock moves on between the two makings every time.
"""
import hashlib
import time
import types
import zipfile

import pytest

from tests._alice_shaped import alice_shaped_epub
from tests.api._epub import epub_ncx, three_chapter_book
from tests.hosted.test_companion_read_map_postgres import lamp_book
from tests.hosted.test_story_memory_door_postgres import long_lamps
from tests.hosted.test_story_memory_jobs_postgres import lamps
from tests.parity.test_story_memory_parity import harbour_book


@pytest.fixture()
def clock_moves_on(monkeypatch):
    """Every clock read the zip writer makes is ten seconds after the one before."""
    step = iter(range(1, 1_000_000))
    monkeypatch.setattr(zipfile, "time", types.SimpleNamespace(
        time=lambda: time.time() + 10 * next(step), localtime=time.localtime,
        gmtime=time.gmtime))


@pytest.mark.parametrize("make", [alice_shaped_epub, three_chapter_book, lamp_book, long_lamps,
                                  lamps, harbour_book])
def test_a_test_book_made_twice_is_one_file(clock_moves_on, make):
    """The break it catches: a test book whose bytes depend on when it was made, so two readers
    given "the same file" can get two files, two editions, and share nothing."""
    first, second = make(), make()
    assert hashlib.sha256(first).hexdigest() == hashlib.sha256(second).hexdigest()


def test_another_title_is_still_another_file(clock_moves_on):
    """The tests that need another edition of the same chapters make it with another title."""
    assert long_lamps() != long_lamps("The Long Lamp Book, Revised")
    assert epub_ncx([("c1.xhtml", "One", "One", "Mara.")], title="A") != epub_ncx(
        [("c1.xhtml", "One", "One", "Mara.")], title="B")
