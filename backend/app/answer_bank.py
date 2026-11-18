"""What a complete, cited answer must look like, and the error a busy answer route raises.

The answer cache that used to live here was removed with the other solo speed-ups (ADR 0043 R3).
"""
from app.ask import AskDraft


class AnswerBusy(RuntimeError):
    pass


def valid_answer(payload):
    try:
        eff = payload["as_of_chapter"]
        draft = {k: payload[k] for k in ("insufficient_evidence", "claims")}
        checked = AskDraft.model_validate(draft, strict=True)
        citations = payload["citations"]
        ids = [c["id"] for c in citations]
        return (type(eff) is int and eff > 0 and not checked.insufficient_evidence
                and checked.model_dump(mode="json") == draft
                and isinstance(citations, list) and 1 <= len(citations) <= 6
                and len(set(ids)) == len(ids)
                and all(type(c["id"]) is int and c["id"] > 0
                        and type(c["ordinal"]) is int and 1 <= c["ordinal"] <= eff
                        and all(isinstance(c.get(k), str) for k in ("chapter_key", "href", "title", "excerpt"))
                        and 0 < len(c["excerpt"]) <= 6000 for c in citations)
                and all(set(claim.citation_ids) <= set(ids) for claim in checked.claims))
    except (ValueError, TypeError, KeyError, AttributeError):
        return False
