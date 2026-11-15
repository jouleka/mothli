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
