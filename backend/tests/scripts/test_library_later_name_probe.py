"""The real-model later-name probe (ADR 0042 section 3) calls nothing unless told to."""

from scripts import library_later_name_probe as probe
from tests.api._epub import three_chapter_book


def test_without_run_it_says_what_it_would_do_and_calls_nothing(tmp_path, monkeypatch, capsys):
    """The break it catches: a probe that reaches a model (paid, or the owner's subscription)
    when nobody asked it to."""
    book = tmp_path / "book.epub"
    book.write_bytes(three_chapter_book())

    def refuse(*_args, **_kwargs):
        raise AssertionError("the probe ran without --run")

    monkeypatch.setattr(probe, "probe", refuse)
    assert probe.main(["--epub", str(book), "--place", "1", "--question", "Who is next?",
                       "--later", "Wickham"]) == 0
    assert "nothing called" in capsys.readouterr().out


def test_a_later_name_is_found_as_a_whole_word_only():
    assert probe.holds("Wickham smiled at Lydia.", ["Wickham", "Darcy"]) == ["Wickham"]
    assert probe.holds("Wickhamshire is far.", ["Wickham"]) == []
