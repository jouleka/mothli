"""Source layout preserves admitted characters, locations and citation ownership."""
import pytest

from app.ask_evidence import AskEvidenceError
from app.conversation_jev import cited_bodies
from app.retrieval_passages import normalize_source, recent_scene_windows


@pytest.mark.parametrize("text", ["", "A short sentence.", "word " * 2000,
    "x" * 7000, ('“Who is there?” Nell asked. “Mara!” came the reply. ' * 200)])
def test_tail_slices_are_bounded_exact_and_cover_through_the_confirmed_end(text):
    normalized = normalize_source(text)
    windows = list(recent_scene_windows(text))
    assert len(windows) <= 4
    for start, end, body in windows:
        assert body == normalized[start:end] and 0 < len(body) <= 1500
    if windows:
        assert windows[-1][1] == len(normalized)
        for left, right in zip(windows, windows[1:]):
            assert left[0] < right[0] <= left[1] < right[1]


def test_no_sentence_completion_or_unread_context_is_added():
    prefix = 'Nell waited. “I think Mara'
    assert list(recent_scene_windows(prefix))[-1][2] == prefix


def source(offset, text, chapter="one"):
    return dict(ordinal=1, chapter_key=chapter, source_offset=offset, text=text)


def test_reorders_and_coalesces_only_exact_cited_overlap():
    assert cited_bodies([source(5, "kept the key."), source(0, "Mara kept the")]) == [
        "Mara kept the key."]


def test_keeps_uncited_gaps_and_other_chapters_separate():
    assert cited_bodies([source(40, "A different occasion."), source(0, "Mara left."),
                         source(0, "A different chapter.", "two")]) == [
        "Mara left.", "A different occasion.", "A different chapter."]


def test_conflicting_offsets_are_not_silently_reconciled():
    with pytest.raises(AskEvidenceError):
        cited_bodies([source(0, "Mara kept the"), source(5, "lost the key.")])
