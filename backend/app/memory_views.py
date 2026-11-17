"""The story memory screens, built the same way in both modes (ADR 0042 section 9).

The cast (``/graph``), a person's card (``/character/{id}``), the story so far (``/notes``) and
the progress the app polls (``/ingest``). Moved from ``app/api/views.py`` and ``app/api/ingest.py``
without change: the local routes call these over a ``MemoryDB``, the hosted routes over a
``HostedBookStore`` snapshot, so the spoiler rules exist once.

THE CLAMP (``clamp``): a view shows the memory as of the smallest of the chapter asked for, the
reader's place and the last chapter whose memory is complete. Every read below then goes through
``mem.view(eff)``, whose rows are bounded by ``revealed_at <= eff`` and valid time.
"""

_IDENTITY_HONORIFICS = {"father", "elder", "abbot", "brother", "mother", "sister"}


def clamp(requested, place, durable):
    """The chapter a view is shown as of: never past the reader's ``place`` or the ``durable``
    (complete) memory, and never past what was ``requested`` (a scrubber; None for the place)."""
    high_water = min(place, durable)
    return high_water if requested is None else min(requested, high_water)


def _coalesce_honorific_characters(characters, relationships):
    """Repair legacy split identities in a view without mutating stored evidence.

    Only a two-token honorific plus an exact, unique one-token canonical name is folded; broad
    surname or fuzzy matching would risk merging distinct people.
    """
    single_names = {}
    for character in characters:
        name = character["canonical_name"].strip()
        if len(name.split()) == 1:
            single_names.setdefault(name.casefold(), []).append(character)

    remap = {}
    folded = {}
    for character in characters:
        tokens = character["canonical_name"].strip().split()
        if len(tokens) != 2 or tokens[0].casefold().rstrip(".") not in _IDENTITY_HONORIFICS:
            continue
        candidates = single_names.get(tokens[1].casefold(), [])
        if len(candidates) == 1 and candidates[0]["entity_id"] != character["entity_id"]:
            canonical = candidates[0]
            remap[character["entity_id"]] = canonical["entity_id"]
            folded.setdefault(canonical["entity_id"], []).append(character)

    if not remap:
        return characters, relationships

    merged_characters = []
    for character in characters:
        if character["entity_id"] in remap:
            continue
        variants = folded.get(character["entity_id"], [])
        aliases = list(character.get("aliases", []))
        for variant in variants:
            aliases.append(variant["canonical_name"])
            aliases.extend(variant.get("aliases", []))
            character["revealed_at"] = min(character["revealed_at"], variant["revealed_at"])
        character["aliases"] = list(dict.fromkeys(alias for alias in aliases
                                                   if alias != character["canonical_name"]))
        merged_characters.append(character)

    merged_relationships = []
    seen = set()
    for relationship in relationships:
        relationship["src_entity"] = remap.get(relationship["src_entity"], relationship["src_entity"])
        relationship["dst_entity"] = remap.get(relationship["dst_entity"], relationship["dst_entity"])
        if relationship["src_entity"] == relationship["dst_entity"]:
            continue
        key = (relationship["src_entity"], relationship["dst_entity"], relationship["rel_type"],
               relationship["label"], relationship["revealed_at"], relationship["invalid_at"])
        if key not in seen:
            seen.add(key)
            merged_relationships.append(relationship)
    return merged_characters, merged_relationships


def graph_payload(mem, eff):
    """The people graph as of ``eff``: the ``GET /graph`` body."""
    v = mem.view(eff)
    chars = []
    for row in v.characters():
        character = dict(row)
        character["aliases"] = [alias["surface_form"] for alias in v.aliases_of(row["entity_id"])]
        chars.append(character)
    rels = [dict(r) for r in v.relationships()]
    chars, rels = _coalesce_honorific_characters(chars, rels)
    # This endpoint is the people graph. Other visible entity types remain available through
    # cards/timeline, but every returned graph edge must resolve to a returned character.
    character_ids = {row["entity_id"] for row in chars}
    rels = [row for row in rels if row["src_entity"] in character_ids
            and row["dst_entity"] in character_ids]
    return {"as_of_chapter": eff, "characters": chars, "relationships": rels}


def character_payload(mem, eff, entity_id):
    """A person's card as of ``eff`` (the ``GET /character/{id}`` body), or None when they are not
    visible at ``eff``: the route answers that 404, the same as for an id that does not exist."""
    card = _character_card(mem, eff, entity_id)
    if card is None:
        return None
    return {"as_of_chapter": eff, **card}


def notes_payload(mem, eff):
    """LIT-31 codex 'the story broken down': per-chapter summary + highlights (who first appears +
    that chapter's events) + the visible cast for name-chip wrapping. Highlights are grouped by
    ``revealed_at`` through the funnel (an entity/event stamped later than eff is never read), so the
    breakdown is spoiler-safe by construction — no gate needed (structured reads only)."""
    v = mem.view(eff)
    titles = {c["chapter_key"]: c["title"] for c in v.chapters()}
    new_by_ch: dict[int, list] = {}
    for e in v.characters():                                  # type='character', revealed_at<=eff
        new_by_ch.setdefault(e["revealed_at"], []).append(
            {"entity_id": e["entity_id"], "name": e["canonical_name"]})
    events_by_ch: dict[int, list] = {}
    for ev in v.timeline():                                   # revealed_at<=eff, order-stable
        events_by_ch.setdefault(ev["revealed_at"], []).append(ev["summary"])
    chapters = [{"chapter_key": s["chapter_key"], "revealed_at": s["revealed_at"],
                 "title": titles.get(s["chapter_key"], ""), "summary": s["summary"],
                 "new_characters": new_by_ch.get(s["revealed_at"], []),
                 "events": events_by_ch.get(s["revealed_at"], [])}
                for s in v.chapter_summaries()]
    cast = _visible_cast(mem, eff)
    return {"as_of_chapter": eff, "cast": cast, "chapters": chapters}


def ingest_payload(progress, durable, status):
    """The ``GET /ingest`` body: how many chapters' memory is complete, and how building it goes."""
    return {"ingest_progress": min(progress, durable),
            "status": status["status"],
            "flags": status.get("flags") or [], "error": status.get("error")}


def _visible_cast(mem, eff):
    """The bookmark-bounded cast for the recap's clickable name affordances. Each visible surface form
    (a character's canonical name + each visible alias) paired with its ``entity_id`` (LIT-30), so a
    chip on 'Mitya' and one on 'Dmitri' both resolve to the ONE character card. All ``revealed_at <=
    eff`` — the client wraps ONLY these, so a name it makes clickable can never be a future entity.
    Deduped by name (a clean surface-form -> id map); read under the per-book lock."""
    v = mem.view(eff)
    out, seen = [], set()
    for ch in v.characters():
        eid = ch["entity_id"]
        for name in [ch["canonical_name"], *[a["surface_form"] for a in v.aliases_of(eid)]]:
            if name not in seen:
                seen.add(name)
                out.append({"name": name, "entity_id": eid})
    return out


def _character_card(mem, eff, entity_id):
    """LIT-30 name card for a character AS OF ``eff``: identity (``bio``) + visible ties (relationships
    touching the entity, resolved to the other endpoint's name). Returns None when the entity is not
    visible at ``eff`` (a future / unknown id) so the route can 404 — indistinguishable from
    nonexistent, no leak. Every read is funnel-bounded (``bio`` and ``relationships`` both apply the
    ``revealed_at <= eff`` filter + referential closure), so ties can only reach visible-both-endpoint
    edges — a tie to a not-yet-met character is absent. Structured data only; run under the per-book
    lock. No LLM/gate on this path."""
    v = mem.view(eff)
    bio = v.bio(entity_id)
    if bio is None:
        return None
    id2name = {e["entity_id"]: e["canonical_name"]
               for t in ("character", "place", "faction", "object")
               for e in v.entities_of_type(t)}
    ties, seen = [], set()
    for e in v.relationships():                       # already restricted to visible-both-endpoints,
        if e["src_entity"] == entity_id and e["dst_entity"] in id2name:   # ordered by revealed_at
            other, direction = e["dst_entity"], "out"
        elif e["dst_entity"] == entity_id and e["src_entity"] in id2name:
            other, direction = e["src_entity"], "in"
        else:
            continue
        if other in seen:
            continue                                  # one line per related person — the extraction
        seen.add(other)                               # emits overlapping edges; keep the first (earliest)
        ties.append({"entity_id": other, "name": id2name[other], "rel_type": e["rel_type"],
                     "label": e["label"], "direction": direction})
    return {"entity_id": entity_id, "name": bio["name"], "type": bio["type"],
            "aliases": bio["aliases"], "first_seen": bio["first_seen"],
            "status": bio["state"], "ties": ties}
