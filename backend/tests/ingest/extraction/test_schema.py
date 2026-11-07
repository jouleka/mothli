"""Production extraction contract: round-trip, rejected extra fields and provider compatibility."""
import pytest
from openai.lib._pydantic import to_strict_json_schema

from app.ingest.extraction.schema import (
    Entity, Extraction,
)


def _gold_obj():
    """A fully-formed extraction object — exactly the shape ``client.complete(schema=Extraction)``
    emits (every property present, per the OpenAI-strict ``required`` list)."""
    return {
        "chapter_summary": "Fyodor introduces his sons; Alyosha goes to the monastery.",
        "entities": [
            {"canonical_name": "Fyodor Pavlovitch Karamazov", "type": "character",
             "aliases": ["Fyodor Pavlovitch"], "matched_roster": False, "state": "a landowner"},
            {"canonical_name": "the monastery", "type": "place",
             "aliases": [], "matched_roster": False, "state": None},
        ],
        "relationships": [
            {"src": "Fyodor Pavlovitch Karamazov", "dst": "Alexey Fyodorovitch Karamazov",
             "rel_type": "family", "label": "father of"},
        ],
        "events": [
            {"summary": "Alyosha enters the monastery.", "participants": ["Alexey Fyodorovitch Karamazov"]},
        ],
        "themes": [
            {"name": "faith", "description": "Alyosha's religious calling."},
            {"name": "family", "description": None},
        ],
    }


def test_gold_object_validates_and_round_trips_to_the_same_dict():
    """An accepted object is unchanged by the new validation path: model_dump(mode="json") == input,
    so resolve.py / pipeline.py / the gate keep their dict-of-strings contract (D-A5)."""
    obj = _gold_obj()
    dumped = Extraction.model_validate(obj).model_dump(mode="json")
    assert dumped == obj                                     # no transformation, no added/dropped keys


def test_extra_keys_are_forbidden():
    """ConfigDict(extra="forbid") — a hallucinated extra field is rejected (a tightening over the spike,
    which silently ignored extras), so an over-eager model output fails closed rather than smuggling a key."""
    with pytest.raises(Exception):
        Entity.model_validate({"canonical_name": "X", "type": "character", "aliases": [],
                               "matched_roster": False, "state": None, "secret": "leak"})


def test_schema_is_openai_strict_compatible():
    """The models are AUTHORED strict-compatible (extra='forbid' + all-required-nullable): every object
    node has additionalProperties:false and EVERY property in `required` (incl. nullable ones). This holds
    on the RAW model_json_schema() too — so to_strict_json_schema is a no-op here, not a repair (the
    honest framing of ADR 0007 P2-3, where the transform is the net for the general non-strict case)."""
    def _walk(node):
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                assert node.get("additionalProperties") is False
                assert set(node["required"]) == set(node["properties"]), node["properties"].keys()
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for v in node:
                _walk(v)

    strict = to_strict_json_schema(Extraction)
    _walk(strict)
    _walk(Extraction.model_json_schema())                    # the RAW schema already satisfies strict
    # the nullable optionals (state, description) must remain REQUIRED (nullable), not dropped
    ent = strict["$defs"]["Entity"] if "$defs" in strict else None
    if ent:
        assert "state" in ent["required"]
