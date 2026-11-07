from app.ask import ConversationMessage
from app.reader_reactions import reaction_clarification, reaction_copy


def test_bare_reaction_requests_a_scene_without_agreeing_or_inventing_book_facts():
    reply = reaction_clarification("That was so unfair!!", [], 2)
    assert reply == "Oof. Which part felt unfair to you?"
    assert reaction_clarification("OMG why did he do that?!", [], 2).endswith("which moment?")
    assert reaction_copy("Why did Napoleon leave Corsica?") is None
    assert reaction_copy('Explain the phrase "that was so unfair"') is None


def test_only_permitted_substantive_reader_context_can_replace_clarification():
    past = ConversationMessage(role="user", content="Why did Mara leave the key?", as_of_chapter=2)
    future = past.model_copy(update={"as_of_chapter": 3})
    assert reaction_clarification("omg", [future], 2)
    assert reaction_clarification("omg", [past], 2) is None
    reaction = ConversationMessage(role="user", content="omg", as_of_chapter=2)
    assistant = ConversationMessage(role="assistant", content="Which bit caught you off guard?", as_of_chapter=2)
    assert reaction_clarification("no way", [reaction, assistant], 2)
