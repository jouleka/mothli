"""Known name forms stay inside the reading boundary and prove nothing but identity."""
import json

from app.ask import AskDraft
from app.conversation_jev import review_packet
from app.conversation_pipeline import SAME_PERSON_REVIEW, review_escalation
from app.known_names import names_note, same_person_names


class View:
    def __init__(self, rows):
        self.rows = rows

    def characters(self):
        return [{"entity_id": i, "canonical_name": names[0]} for i, names in enumerate(self.rows)]

    def aliases_of(self, entity_id):
        return [{"surface_form": name} for name in self.rows[entity_id][1:]]


class Memory:
    def __init__(self, rows):
        self.rows, self.frontiers = rows, []

    def view(self, frontier):
        self.frontiers.append(frontier)
        return View(self.rows)


ROWS = [["Alexey Fyodorovitch Karamazov", "Alyosha", "Alexey", "Lyosha"], ["Dmitri", "Mitya"], ["Ivan"]]
READ = {"alexey", "fyodorovitch", "karamazov", "alyosha", "dmitri", "mitya", "ivan"}


def test_groups_are_read_at_the_frontier_and_limited_to_the_reader_message():
    mem = Memory(ROWS)
    groups = same_person_names(mem, 4, READ, {"who", "is", "alyosha"})
    assert mem.frontiers == [4]
    # "Lyosha" is not in the read prefix; Dmitri is not what the reader asked about.
    assert groups == [["Alexey Fyodorovitch Karamazov", "Alyosha", "Alexey"]]


def test_no_memory_before_the_first_completed_chapter_and_no_single_form_groups():
    assert same_person_names(Memory(ROWS), 0, READ, {"alyosha"}) == []
    assert same_person_names(Memory(ROWS), 3, READ, {"ivan"}) == []
    # A form the reader has not seen cannot complete a group.
    assert same_person_names(Memory(ROWS), 3, READ - {"mitya"}, {"dmitri"}) == []
    assert names_note([]) == ""


DRAFT = AskDraft.model_validate({"insufficient_evidence": False, "claims": [
    {"text": "Alyosha is the third son.", "citation_ids": [1]}]})
SOURCES = [{"id": 1, "ordinal": 1, "chapter_key": "one", "title": "I",
            "text": "Alexey Fyodorovitch Karamazov was the third son."}]


def test_typed_review_receives_names_only_when_known():
    state, questions = review_packet(DRAFT, SOURCES, question="Who is Alyosha?", frontier=1)
    assert "same_person_names" not in state
    assert "same_person_names" not in questions["c0_relation"]["instructions"]
    state, questions = review_packet(DRAFT, SOURCES, question="Who is Alyosha?", frontier=1,
                                    identities=[["Alexey Fyodorovitch Karamazov", "Alyosha"]])
    assert state["same_person_names"] == [["Alexey Fyodorovitch Karamazov", "Alyosha"]]
    assert "establishes nothing else" in questions["c0_relation"]["instructions"]
    # The qualification checks are unchanged.
    assert "same_person_names" not in questions["c0_unestablished_cause"]["instructions"]


def test_reasoning_review_receives_names_as_data_with_a_narrow_rule():
    seen = {}

    def complete(system, user, *, schema, phase, tier):
        seen.update(system=system, packet=json.loads(user.split("\n", 1)[1]))
        return {"references_future": False, "unsupported_claims": [], "missing_details": [],
                "claim_audits": [{"claim_id": 1, "units": [
                    {"unit_id": u["unit_id"], "supported": True, "diagnostic": None,
                     "evidence": [{"source_id": 1, "passage_ids": [
                         seen["packet"]["claims"][0]["sources"][0]["passages"][0]["id"]]}]}
                    for u in seen["packet"]["claims"][0]["units"]]}]}

    review_escalation(DRAFT, SOURCES, question="Who is Alyosha?", complete=complete,
                      max_output_tokens=4096, identities=[["Alexey Fyodorovitch Karamazov", "Alyosha"]])
    assert seen["system"].endswith(SAME_PERSON_REVIEW)
    assert seen["packet"]["established_same_person_names"] == [["Alexey Fyodorovitch Karamazov", "Alyosha"]]
    review_escalation(DRAFT, SOURCES, question="Who is Alyosha?", complete=complete, max_output_tokens=4096)
    assert SAME_PERSON_REVIEW not in seen["system"] and "established_same_person_names" not in seen["packet"]
