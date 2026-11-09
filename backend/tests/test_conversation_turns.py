"""Routing never authorizes uncited book assertions."""
import pytest
from app.ask_evidence import AskEvidenceError
from app.conversation_turns import classify_turn, answer_dialogue
from app.llm.jev import JevUnavailable


@pytest.mark.parametrize("values,expected", [
    ((.99,.01,.01),"evidence"), ((.99,.99,.01),"recent_recap"),
    ((.99,.01,.99),"clarify_person"), ((.01,.01,.01),"dialogue"),
    ((.5,.5,.5),"evidence"),
])
def test_routing_truth_table_has_an_evidence_path_for_uncertainty(values, expected):
    def run(state, questions):
        assert state["latest_message"] == "untrusted reader text"
        assert state["recent_dialogue"] == [] and "currently open book" in state["conversation_setting"]
        return {**{k:{"noul":p} for k,p in zip(list(questions)[:5],(*values,.01,.99 if values[2] >= .7 else .01),strict=True)},
                "clarification_kind":{"choice":"person"}}
    assert classify_turn("untrusted reader text",run=run) == expected


@pytest.mark.parametrize("bad", [True, None, float("nan"), 1.1])
def test_invalid_routing_is_not_a_no_evidence_decision(bad):
    with pytest.raises(JevUnavailable):
        classify_turn("reader text",run=lambda state,q:{k:{"noul":bad} for k in q})


def test_ambiguous_followup_with_history_is_resolved_only_against_book_evidence():
    def run(state,questions):
        return {**{k:{"noul":.99} for k in questions if k!="clarification_kind"},
                "clarification_kind":{"choice":"person"}}
    assert classify_turn("Who was that?",run=run,history=[
        {"role":"assistant","content":"Nell counted three coins."}]) == "evidence"


@pytest.mark.parametrize("risk,allowed", [(.01,True),(.5,False),(.99,False),(True,False)])
def test_uncited_dialogue_must_pass_its_own_no_book_fact_check(risk, allowed):
    phases=[]
    def complete(system,user,**kwargs):
        phases.append(kwargs["phase"])
        return {"message":"Do you mean Lea or Arun?"}
    def invoke():
        return answer_dialogue("Who was that?",route="clarification",complete=complete,
            run=lambda state,q:{"book_assertion":{"noul":risk}})
    if allowed:
        assert invoke() == "Do you mean Lea or Arun?"
    else:
        with pytest.raises(AskEvidenceError):
            invoke()
    assert phases == (["dialogue"] if allowed or type(risk) is bool else ["dialogue","dialogue-repair"])
