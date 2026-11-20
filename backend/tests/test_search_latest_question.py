"""Passages are found for the latest question first, and "just happened" means what was just read.

A made-up book reproduces what a real reader hit 30 chapters into a novel (2026-11-20): the
names in Mothli's last reply outranked the name in the new question, and a chapter the reader
had only opened (its heading read, nothing else) stood in for what had just happened. The
search below is the one the streamed chat runs, with no database and no model.
"""
from collections import deque

from app.ask import ConversationMessage, book_names_in, conversation_retrieval_question, retrieve_ask_sources
from app.ask_context import complete_ask_context
from app.conversation_pipeline import compact_candidates
from app.retrieval_passages import passage_windows, recent_reading
from app.stream_conversation import MAX_PASSAGES

FILLER = ("The weather held fair through the week, and the lanes were dry enough for walking. "
          "Nobody in the village spoke of anything but the coming season and its many dinners. ")

# Chapters 1 and 2: the quarrel Mothli's last reply was about (Mara, Corvus and Ilse together).
# Chapter 3: Vex's story about Corvus, which the new question asks about.
# Chapter 4: an older scene with Juno. Chapter 5: what just happened to Juno, finished.
# Chapter 6: opened, only its heading read; the rest of it is not read and must never be used.
BOOK = {
    1: "Mara met Corvus and Ilse at the harbour. Mara said Corvus had lied about the harbour boats, "
       "and Ilse watched Corvus closely while the quarrel grew loud. " + FILLER,
    2: "Mara, Corvus and Ilse argued again at the market about the boats and the lie. Ilse took "
       "the side of Mara, and Corvus left the market angry. " + FILLER,
    3: "Vex told Wren a long story about Corvus over the card table. Vex said Corvus had cheated "
       "him of the mill his father meant him to have, and Wren listened with all her heart. "
       + FILLER,
    4: "Juno walked with Wren in the orchard and talked of dancing, of ribbons and of the officers. "
       "Juno laughed at everything Wren said. " + FILLER,
    5: FILLER + "Then Juno accepted Ansel, to the astonishment of Wren. Juno said she was not "
       "romantic and asked only a comfortable home, and Wren could not answer her.",
    6: "CHAPTER VI.",
}
READ_TEXT = "\n".join(BOOK.values())
UNREAD_REST_OF_6 =" Juno wrote that the parsonage was cold and that Ansel was often away."


def chapters(book=BOOK):
    rows = []
    for ordinal, text in book.items():
        row = {"key": f"c{ordinal}", "chapter_key": f"c{ordinal}", "ordinal": ordinal,
               "revealed_at": ordinal, "text": text}
        if ordinal == max(book):
            row["recent"] = text            # where the reader is now
        rows.append(row)
    return rows


def search(question, history, read=None):
    """The streamed chat's passage search (stream_conversation.stream_read_prefix)."""
    read = read or chapters()
    reach = max(ch["ordinal"] for ch in read)
    texts = {ch["key"]: ch["text"] for ch in read}
    query = conversation_retrieval_question(question, history, reach)
    latest, recent_text = recent_reading(read)
    hits = [(1.0, text, latest["ordinal"], latest["key"])
            for _, _, text in reversed(deque(passage_windows(recent_text), maxlen=4))]
    atoms = {ch["key"]: {"href": "", "title": ""} for ch in read}
    pool = compact_candidates(retrieve_ask_sources(
        query, bookmark=reach, chapters=read, read_chapter=texts.get, semantic_hits=hits,
        atoms=atoms, candidate_pool=True, prefer_recent=True,
        lead_names=book_names_in(question, "\n".join(texts.values()))))
    core = complete_ask_context(query, pool[:6], pool, allow_named_fallback=True) if pool else []
    chosen = {s["id"] for s in core}
    return core + [s for s in pool if s["id"] not in chosen][:MAX_PASSAGES - len(core)]


def ranked(question, history, *, lead=True):
    """Plain ranking of the read passages, no recent-text slots: ordinals, best first.

    ``lead=False`` ranks the way it was before this fix: every name in the search text alike.
    """
    texts = {ch["key"]: ch["text"] for ch in chapters()}
    lead_names = book_names_in(question, READ_TEXT) if lead else ()
    return [s["ordinal"] for s in retrieve_ask_sources(
        conversation_retrieval_question(question, history, 6), bookmark=6, chapters=chapters(),
        read_chapter=texts.get, semantic_hits=[], atoms={}, limit=6, lead_names=lead_names)]


def turns(asked, replied):
    return [ConversationMessage(role="user", content=asked, as_of_chapter=6),
            ConversationMessage(role="assistant", content=replied, as_of_chapter=6)]


QUARREL = turns("what's going on with Mara and Corvus?", (
    "Mara and Corvus quarrelled at the harbour, and again at the market, with Ilse taking "
    "Mara's side. Mara thinks Corvus lied about the boats."))
STORY = turns("should I trust Vex's story about Corvus?",
              "Vex told Wren that Corvus cheated him of the mill his father meant him to have.")


def test_the_names_in_the_last_reply_no_longer_outrank_the_one_asked_about():
    question = "should I trust Vex's story about Corvus?"
    # The pattern, as it was: every name in question plus last reply alike puts the quarrel first.
    assert sorted(ranked(question, QUARREL, lead=False)[:2]) == [1, 2]
    # Now the names the question asks about lead; the reply's names only break ties.
    assert ranked(question, QUARREL)[0] == 3
    assert 3 in [s["ordinal"] for s in search(question, QUARREL)[:6]]


def test_a_named_follow_up_finds_what_the_turn_before_was_about():
    # "why did Corvus do that?" names Corvus, who is in chapters 1, 2 and 3 alike: the turn
    # before decides which. The same question after two different replies finds two places.
    question = "why did Corvus do that?"
    assert ranked(question, QUARREL)[:2] in ([1, 2], [2, 1])
    assert ranked(question, STORY)[0] == 3
    # In the chat's own search, past the recent-text slots (chapter 5, just finished).
    def first_earlier(history):
        return next(s["ordinal"] for s in search(question, history) if s["ordinal"] != 5)
    assert first_earlier(QUARREL) in {1, 2}
    assert first_earlier(STORY) == 3
    # A passage without the name asked about never outranks one with it, however many of the
    # reply's words it shares: Juno's chapters never lead a question about Corvus.
    juno = turns("what about Juno?", "Juno walked with Wren in the orchard and talked of dancing, "
                 "of ribbons and of the officers, and then accepted Ansel.")
    assert ranked(question, juno)[0] in {1, 2, 3}


def test_a_follow_up_with_no_name_of_its_own_still_finds_the_turn_before():
    for question in ("what about him?", "and then?", "Interesting, why would he say that?"):
        assert ranked(question, STORY)[0] == 3
        assert 3 in [s["ordinal"] for s in search(question, STORY)[:6]]
        # The other direction: the same follow-up after the quarrel finds the quarrel, not Vex.
        assert ranked(question, QUARREL)[0] in {1, 2}


def test_a_name_counts_only_if_the_read_text_has_it_as_a_name():
    assert book_names_in("should I trust Vex's story about Corvus?", READ_TEXT) == ["vex", "corvus"]
    assert book_names_in("Ok so what did Juno do?", READ_TEXT) == ["juno"]
    # Ordinary words at the start of a message are not names, and neither is a name the
    # reader has not met in what is read.
    assert book_names_in("Interesting, and then?", READ_TEXT) == []
    assert book_names_in("what about Zed?", READ_TEXT) == []


def test_just_happened_means_the_chapter_just_finished_when_only_a_heading_is_read():
    latest, text = recent_reading(chapters())
    assert latest["ordinal"] == 5 and text == BOOK[5]
    found = search("what just happened with Juno?", QUARREL)
    assert found[0]["ordinal"] == 5 and "accepted Ansel" in found[0]["text"]
    # No slot is spent on the bare heading.
    assert all(s["text"].strip() != "CHAPTER VI." for s in found)


def test_the_recent_text_never_goes_past_what_is_read():
    for found in (search("what just happened with Juno?", QUARREL), search("and then?", QUARREL)):
        assert all(s["ordinal"] <= 6 for s in found)
        assert all("parsonage" not in s["text"] for s in found)
    # Once the reader has read a real passage of chapter 6, that is the recent text.
    read_on = {**BOOK, 6: BOOK[6] + UNREAD_REST_OF_6 + " " + FILLER}
    latest, text = recent_reading(chapters(read_on))
    assert latest["ordinal"] == 6
    assert search("what just happened with Juno?", [], chapters(read_on))[0]["ordinal"] == 6


def test_the_search_weighs_the_latest_chapter_with_a_real_passage_as_recent():
    texts = {ch["key"]: ch["text"] for ch in chapters()}
    found = retrieve_ask_sources("Juno", bookmark=6, chapters=chapters(), read_chapter=texts.get,
                                 semantic_hits=[], atoms={}, candidate_pool=True, prefer_recent=True)
    assert found[0]["ordinal"] == 5
    # A heading-only chapter beyond the bookmark is never read, recent or not.
    beyond = chapters({**BOOK, 7: FILLER + " Juno " + FILLER})
    texts = {ch["key"]: ch["text"] for ch in beyond}
    found = retrieve_ask_sources("Juno", bookmark=6, chapters=beyond, read_chapter=texts.get,
                                 semantic_hits=[], atoms={}, candidate_pool=True, prefer_recent=True)
    assert found[0]["ordinal"] == 5 and all(s["ordinal"] <= 6 for s in found)
