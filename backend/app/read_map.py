"""The read map: the text of each book the reader has read, in whatever order they read it.

Only text inside the map reaches a model. A page enters it when the reader reports having read
it, with both ends verified against the source; whole chapters enter it when the reader says
they have read them. Where the reader is now is kept apart: it may move backward, and it only
tells Mothli what just happened.
"""
import hashlib
import json

from app.content_scope import is_content
from app.read_frontier import anchor_offset, snapshot, source_state
from app.reader_place import content_numbers
from app.retrieval_passages import normalize_source

PAGE_CHARS = 12_000        # the most one page report may add, so a faulty one cannot mark a book read
FINISHED_SHARE = 0.8       # a chapter counts as finished for the cast, recap and titles when this
                           # much of it was read and its ending was reached
GAP = " [...] "            # stands between two read stretches of one chapter with unread text between
END = 2**31 - 1


def covers(ranges, offset):
    """Whether the character before ``offset`` is inside the map (offset 0 always is)."""
    return offset <= 0 or any(start < offset <= end for start, end in ranges)


def _started(book_id, book, state, manifest, chapters, catalog):
    """A book's map begins, once, from what the older position record already held as read.

    Afterwards that record never feeds the map again: it still advances on a jump, and a jump
    is not reading.
    """
    if catalog.get_read_position(book_id, incarnation=book["incarnation"]) is not None:
        return
    point, permitted = snapshot(book_id, state, book, manifest, chapters,
                                catalog.get_conversation_frontier(book_id))
    for chapter in permitted:
        catalog.add_read_range(book_id, incarnation=book["incarnation"], atom=chapter["ordinal"],
                               start=0, end=len(chapter["text"]), source="told")
    catalog.set_read_position(book_id, incarnation=book["incarnation"], atom=point["atom"],
                              offset=point["offset"])


def read_snapshot(book_id, catalog, settings, worker):
    """(point, chapters, state, manifest, book, map) for the read map as it stands now.

    ``chapters`` holds, for each chapter with anything read, its read stretches joined by GAP.
    ``point`` names where the reader is; its id changes whenever the map or that place does.
    """
    state, book, manifest, chapters = source_state(book_id, catalog, settings, worker)
    _started(book_id, book, state, manifest, chapters, catalog)
    held = catalog.get_read_map(book_id, incarnation=book["incarnation"])
    atom, offset = catalog.get_read_position(book_id, incarnation=book["incarnation"])
    permitted = []
    # The last chapter up to which every chapter is read whole, and up to which every chapter
    # is finished. Pages that are not part of the book proper are passed over.
    whole = finished = 0
    all_whole = all_finished = True
    for chapter in chapters:
        if not is_content(manifest["atoms"][chapter["ordinal"] - 1]):
            whole, finished = (chapter["ordinal"] if all_whole else whole,
                               chapter["ordinal"] if all_finished else finished)
            continue
        text = normalize_source(chapter["text"])
        ranges = [(a, min(b, len(text))) for a, b in held.get(chapter["ordinal"], []) if a < len(text)]
        complete = ranges == [(0, len(text))] or not text
        share = sum(b - a for a, b in ranges) / len(text) if text else 1
        all_whole = all_whole and complete
        all_finished = all_finished and (complete or (share >= FINISHED_SHARE and covers(ranges, len(text))))
        whole, finished = (chapter["ordinal"] if all_whole else whole,
                           chapter["ordinal"] if all_finished else finished)
        if ranges and text.strip():
            entry = {**chapter, "chapter_key": chapter["key"], "revealed_at": chapter["ordinal"],
                     "text": GAP.join(text[a:b] for a, b in ranges), "whole": complete}
            if chapter["ordinal"] == atom:
                # What the reader has just read: the read stretch that ends where they are.
                entry["recent"] = next((text[a:offset] for a, b in ranges if a < offset <= b), "")
            permitted.append(entry)
    identity = [book["incarnation"], manifest["atom_set_version"], state["position_epoch"], atom, offset,
                sorted(held.items()), "read-map-v1"]
    point = {"id": hashlib.sha256(json.dumps(identity).encode()).hexdigest(), "atom": atom,
             "offset": offset, "position_epoch": state["position_epoch"],
             "available_chapter": atom if offset else max(0, atom - 1)}
    return point, permitted, {**state, "read_through": whole, "finished_through": finished}, manifest, book, held


def skimmed_chapters(book_id, book, catalog):
    """The chapters with pages turned through quickly here.

    Which chapters is all that may leave that table: where in them the skimmed text lies is not
    passed on, and the text itself is not in the map, so none of it reaches a model.
    """
    return set(catalog.get_skimmed(book_id, incarnation=book["incarnation"]))


def sync_counter(book_id, catalog, settings, worker, *, with_place=False):
    """Make the older chapter counter say what the map says, and return the map's point.

    With ``with_place``, also what the reader's place looks like (for asking about chapters
    they went past): returns (point, place).

    The cast, recap, chapter closeout and chapter titles follow that counter. It is the last
    chapter up to which every chapter is finished, so those views never run ahead of the reader.
    """
    point, chapters, state, manifest, book, _held = read_snapshot(book_id, catalog, settings, worker)
    if state["bookmark"] != state["finished_through"]:
        catalog.set_bookmark(book_id, state["finished_through"])
    if with_place:
        return point, {"book": book, "chapters": chapters, "point": point,
                       "numbers": content_numbers(manifest),
                       "skimmed": skimmed_chapters(book_id, book, catalog)}
    return point


def record_page(book_id, start, end, catalog, settings, worker, *, skimmed=False):
    """Add the page between two verified anchors to the map and move the reader's place to its end.

    Returns whether a chapter the page touches is now read from start to finish.

    With ``skimmed`` the page was turned quickly on the way somewhere: it is checked the same way
    but kept out of the map, so it never reaches a model, and the reader's place does not move.
    """
    state, book, manifest, chapters = source_state(book_id, catalog, settings, worker)
    if not (start.position_epoch == end.position_epoch == state["position_epoch"]):
        raise RuntimeError("reading pass changed")
    if not (1 <= start.atom <= end.atom <= len(chapters)) or end.atom - start.atom > 1:
        raise ValueError("a page lies within one chapter or across two neighbours")
    _started(book_id, book, state, manifest, chapters, catalog)
    first, last = chapters[start.atom - 1], chapters[end.atom - 1]
    begin, finish = anchor_offset(first["text"], start), anchor_offset(last["text"], end)
    length = len(normalize_source(first["text"]))
    stretches = ([(start.atom, begin, finish)] if start.atom == end.atom
                 else [(start.atom, begin, length), (end.atom, 0, finish)])
    if any(b < a for _, a, b in stretches) or sum(b - a for _, a, b in stretches) > PAGE_CHARS:
        raise ValueError("page anchors do not bound one page")
    if skimmed:
        for atom, a, b in stretches:
            catalog.add_skimmed_range(book_id, incarnation=book["incarnation"], atom=atom, start=a, end=b)
        return False
    for atom, a, b in stretches:
        catalog.add_read_range(book_id, incarnation=book["incarnation"], atom=atom, start=a, end=b,
                               source="read")
    catalog.set_read_position(book_id, incarnation=book["incarnation"], atom=end.atom, offset=finish)
    held = catalog.get_read_map(book_id, incarnation=book["incarnation"])
    return any(held.get(atom) == [(0, len(normalize_source(chapters[atom - 1]["text"])))]
               for atom, _, _ in stretches)


def mark_chapters(book_id, atoms, *, read, source, catalog, settings, worker):
    """The reader's word that they have, or have not, read whole chapters."""
    state, book, manifest, chapters = source_state(book_id, catalog, settings, worker)
    _started(book_id, book, state, manifest, chapters, catalog)
    by_atom = {chapter["ordinal"]: chapter for chapter in chapters}
    if not atoms or any(atom not in by_atom for atom in atoms):
        raise ValueError("unknown chapter")
    for atom in atoms:
        if read:
            catalog.add_read_range(book_id, incarnation=book["incarnation"], atom=atom, start=0,
                                   end=len(normalize_source(by_atom[atom]["text"])), source=source)
        else:
            catalog.clear_read_map(book_id, atom=atom)


def chapter_fractions(book_id, catalog, settings, worker):
    """For each chapter of the book proper, the share of its text inside the map (0 to 1)."""
    state, book, manifest, chapters = source_state(book_id, catalog, settings, worker)
    _started(book_id, book, state, manifest, chapters, catalog)
    held = catalog.get_read_map(book_id, incarnation=book["incarnation"])
    shares = []
    for chapter in chapters:
        if not is_content(manifest["atoms"][chapter["ordinal"] - 1]):
            continue
        length = len(normalize_source(chapter["text"]))
        read = sum(min(b, length) - a for a, b in held.get(chapter["ordinal"], []) if a < length)
        shares.append({"atom": chapter["ordinal"], "read": round(read / length, 4) if length else 0})
    return shares


def import_offer(book_id, entries, catalog, settings, worker):
    """What another app recorded about this book, while nothing of it has been read here.

    ``entries`` are the reader's imported records of this title. A finished book is offered
    whole. Otherwise the furthest imported highlight found in this text places the reader at
    the start of its chapter: rounded down, so Mothli is behind the reader, never ahead.
    Nothing enters the map until the reader says yes.
    """
    state, book, manifest, chapters = source_state(book_id, catalog, settings, worker)
    _started(book_id, book, state, manifest, chapters, catalog)
    if not entries or catalog.get_read_map(book_id, incarnation=book["incarnation"]):
        return None
    texts = {chapter["ordinal"]: normalize_source(chapter["text"]) for chapter in chapters
             if is_content(manifest["atoms"][chapter["ordinal"] - 1])}
    sources = sorted({name for entry in entries for name in entry["sources"]})
    finished = next((entry for entry in entries if entry["status"] == "read"), None)
    if finished:
        return {"kind": "finished", "sources": sources, "finished_on": finished["finished_on"],
                "chapter": None, "atoms": list(texts)}
    quotes = [normalize_source(mark["text"] or "") for entry in entries for mark in entry.get("marks", [])]
    furthest = max((ordinal for ordinal, text in texts.items()
                    if any(len(quote) >= 20 and quote in text for quote in quotes)), default=0)
    behind = [ordinal for ordinal in texts if ordinal < furthest]
    if not behind:
        return None
    return {"kind": "progress", "sources": sources, "finished_on": None, "chapter": furthest, "atoms": behind}


def still_read(then, held):
    """Whether every stretch of the stored map ``then`` (chapters as JSON keys) is inside ``held``."""
    return all(any(a <= start and end <= b for a, b in held.get(int(atom), []))
               for atom, stretches in then.items() for start, end in stretches)


def written_from_read(turn, read):
    """Whether everything a stored turn was written from is still read.

    A turn is stored with ``drew_on``: {book_id: [incarnation, read map]} as it was when the
    turn's prompt was built, one book for a turn inside a book and every read book for a home
    turn. ``read`` is the same thing now. The turn holds only read text while every book it
    names is there, in the same shelf lifetime, with every stretch read then still read. A turn
    stored without the record (before it was kept) cannot be shown to be safe.
    """
    return turn["drew_on"] is not None and all(
        book_id in read and read[book_id][0] == incarnation and still_read(then, read[book_id][1])
        for book_id, (incarnation, then) in turn["drew_on"].items())


def book_record(book_id, book, held):
    """What a turn inside a book is stored with, where ``held`` is the map its prompt was built from."""
    return {book_id: [book["incarnation"], held]}


def prefix_map(atom, offset):
    """The map of a reader counted as having read everything up to (atom, offset) and no further.

    This is what "read" means with the read map switched off; chapter lengths are not needed.
    """
    return {**{earlier: [(0, END)] for earlier in range(1, atom)}, **({atom: [(0, offset)]} if offset else {})}


def visible_turns(book_id, book, held, catalog, *, limit, reactions=False):
    """One book's stored turns that may be said and shown again, oldest first; ``limit`` keeps the latest.

    Every reader of a book's turns comes through here: the book's conversation, the reactions of
    the reader profile, and what the writer is given with the next question. ``held`` is what is
    read of the book now (``prefix_map`` when the read map is off). "Read it fresh" and "I haven't
    read this" hide the turns written from that text, wherever the reader was when they asked;
    reading it again brings them back.
    """
    read = book_record(book_id, book, held)
    turns = catalog.get_turns(book_id, incarnation=book["incarnation"], reactions=reactions)
    return [turn for turn in turns
            if turn["drew_on"] and book_id in turn["drew_on"] and written_from_read(turn, read)][-limit:]
