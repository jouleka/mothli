"""Offline checks for the companion evaluation's position handling. No provider calls."""
from scripts import companion_eval as ev


def test_positions_run_in_reading_order_because_the_read_position_only_moves_forward():
    book = {"read_through_atom": 5, "cases": [{"id": "end"}], "positions": [
        {"name": "inside-5", "atom": 5, "quote": "q5", "cases": [{"id": "b"}]},
        {"name": "inside-3", "atom": 3, "quote": "q3", "cases": [{"id": "a"}]}]}
    plan = ev.positions(book)
    assert [(p["name"], p["atom"], p.get("quote")) for p in plan] == [
        ("inside-3", 3, "q3"), ("inside-5", 5, "q5"), ("through-5", 5, None)]
    assert [c["id"] for p in plan for c in p["cases"]] == ["a", "b", "end"]


def test_text_is_split_at_the_exact_offset_inside_a_chapter():
    chapters = [{"ordinal": 1, "text": "Mara  hid the key.\nJonas waited."},
                {"ordinal": 2, "text": "Corvus arrived. He poisoned Mara."},
                {"ordinal": 3, "text": "Later, Vex ruled."}]
    read, later = ev.read_and_unread(chapters, {"atom": 2, "offset": len("Corvus arrived.")})
    assert read == "Mara hid the key. Jonas waited. Corvus arrived."
    assert later == " He poisoned Mara. Later, Vex ruled."


def test_a_name_first_met_later_in_the_same_chapter_counts_as_unread():
    read = "Mara hid the key while old Jonas waited by the forge."
    later = "That night Mara met Corvus, and Corvus poisoned her. Then Jonas wept."
    assert ev.unread_name_tokens(read, later) == {"corvus"}


def test_positions_reached_by_skipping_come_after_the_plain_ones_each_from_an_empty_map():
    book = {"read_through_atom": 5, "cases": [{"id": "end"}], "positions": [
        {"name": "skip", "pages": [{"atom": 5, "from": "a", "to": "b"}], "cases": []},
        {"name": "inside-3", "atom": 3, "quote": "q", "cases": []}]}
    assert [p["name"] for p in ev.positions(book)] == ["inside-3", "through-5", "skip"]


def test_text_is_split_by_the_read_map_wherever_the_read_stretches_are():
    chapters = [{"ordinal": 1, "text": "Mara hid the key. Jonas waited."}, {"ordinal": 2, "text": "Corvus arrived."}]
    read, unread = ev.read_and_unread_by_map(chapters, {1: [(18, 31)]})
    assert read == "Jonas waited."
    assert "Mara hid the key." in unread and "Corvus arrived." in unread and "Jonas" not in unread


def test_a_position_given_by_chapter_number_is_entered_on_the_read_map(tmp_path):
    """Through chapter N, and inside chapter N after a quote, become what the reader app would
    have recorded: whole chapters before, and the stretch up to the quote with the place there."""
    import sys
    from pathlib import Path

    from fastapi.testclient import TestClient

    from app.config import Settings
    from app.main import create_app
    sys.path.insert(0, str(Path(__file__).parents[1] / "api"))
    from _epub import epub_ncx
    from test_ingest import InlineExecutor
    settings = Settings(_env_file=None, allow_stub=True, reader_page_context=True,
                        reader_conversation_pipeline=True, data_dir=str(tmp_path / "data"))
    app = create_app(settings, ingest_executor=InlineExecutor())
    settings.reader_conversation_jev = settings.reader_conversation_stream = settings.reader_read_map = True
    lines = {n: " ".join(f"Line {i} of part {n}." for i in range(1, 41)) for n in (1, 2, 3)}
    with TestClient(app) as api:
        epub = epub_ncx([(f"c{n}.xhtml", f"Part {n}", f"Part {n}", lines[n]) for n in (1, 2, 3)])
        bid = api.post("/api/books", files={"file": ("b.epub", epub, "application/epub+zip")}).json()["book_id"]
        chapters = app.state.worker.source_chapters(bid)
        point, read, unread = ev.enter_by_map(api, app, bid, {"name": "through-2", "atom": 2}, chapters)
        assert "Line 40 of part 2." in read and "part 3" not in read
        point, read, unread = ev.enter_by_map(
            api, app, bid, {"name": "inside-3", "atom": 3, "quote": "Line 7 of part 3."}, chapters)
        assert "Line 40 of part 2." in read and "Line 7 of part 3." in read and "Line 8 of part 3." not in read
        assert (point["atom"], point["available_chapter"]) == (3, 3)
        assert "Line 8 of part 3." in unread
