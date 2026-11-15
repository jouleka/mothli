"""The reader across their books: where they are in each, what they said they felt, and what they
have read elsewhere. Read straight from stored state on each request; no model writes any of it."""
import json

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from app.api.history import _history, _with_shelf
from app.content_scope import is_content
from app.deps import get_catalog, get_reader_client, get_settings, get_store, get_worker
from app.home_conversation import HomeAsk, home_turns, read_maps, shelf_books, stream_home
from app.ingest.manifest import load_manifest
from app.read_map import read_snapshot, visible_turns

router = APIRouter(prefix="/api/reader", tags=["reader"])
REACTIONS = 5
END_OF_CHAPTER = 2**31 - 1


def _mapped(book, catalog, settings, worker):
    """Status from the read map: what was read, wherever in the book it was."""
    _point, chapters, _state, manifest, _book, held = read_snapshot(book["book_id"], catalog, settings, worker)
    content = sum(is_content(atom) for atom in manifest["atoms"])
    read = sum(chapter["whole"] for chapter in chapters)
    reactions = visible_turns(book["book_id"], book, held, catalog, limit=REACTIONS, reactions=True)
    return {"book_id": book["book_id"], "title": book["title"], "author": book["author"],
            "status": "finished" if content and read == content else "reading" if chapters else "unread",
            "chapters_read": read, "chapters": content,
            "reactions": [turn["content"] for turn in reactions]}


def reader_profile(catalog, settings, history, worker=None):
    books = []
    for book in catalog.list_books():
        if settings.reader_read_map and settings.reader_page_context:
            try:
                books.append(_mapped(book, catalog, settings, worker))
            except (LookupError, OSError, ValueError, RuntimeError):
                pass                                # a book whose source cannot be verified is left out
            continue
        state = catalog.get_state(book["book_id"])
        try:
            atoms = load_manifest(settings.data_dir, book["book_id"])["atoms"]
        except (OSError, ValueError):
            continue                                # a book whose source cannot be verified is left out
        content = [ordinal for ordinal, atom in enumerate(atoms, 1) if is_content(atom)]
        read = sum(ordinal <= state["bookmark"] for ordinal in content)
        point = catalog.get_conversation_frontier(book["book_id"])
        inside = (point["atom"], point["source_offset"]) if (
            point and point["position_epoch"] == state["position_epoch"]) else (0, 0)
        atom, offset = max((state["bookmark"], END_OF_CHAPTER), inside) if state["bookmark"] else inside
        reactions = catalog.get_turns(book["book_id"], incarnation=book["incarnation"], atom=atom,
                                      offset=offset, limit=REACTIONS, reactions=True)
        books.append({
            "book_id": book["book_id"], "title": book["title"], "author": book["author"],
            "status": ("finished" if content and read == len(content)
                       else "reading" if (atom, offset) > (0, 0) else "unread"),
            "chapters_read": read, "chapters": len(content),
            "reactions": [turn["content"] for turn in reactions]})
    _total, entries = history.list(limit=500)
    elsewhere = [{key: entry[key] for key in ("title", "author", "status", "rating", "finished_on")}
                 for entry in _with_shelf(entries, catalog, settings) if not entry["book_id"]]
    return {"books": books, "elsewhere": elsewhere}


def _home_enabled(settings):
    if not (settings.reader_read_map and settings.reader_page_context and settings.reader_conversation_stream):
        raise HTTPException(404, "the home conversation is not enabled")


@router.post("/ask/stream")
def ask_home(body: HomeAsk, catalog=Depends(get_catalog), store=Depends(get_store),
             settings=Depends(get_settings), client=Depends(get_reader_client), worker=Depends(get_worker)):
    """Server-sent events, as for a book: one ``sentence`` per checked sentence, then ``done``."""
    _home_enabled(settings)
    _total, entries = _history(settings).list(limit=500)
    elsewhere = [entry for entry in _with_shelf(entries, catalog, settings) if not entry["book_id"]]
    events = stream_home(body, catalog, store, settings, client, worker, elsewhere)
    return StreamingResponse(
        ("data: " + json.dumps(event, ensure_ascii=False) + "\n\n" for event in events),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@router.get("/conversation")
def home_conversation(catalog=Depends(get_catalog), settings=Depends(get_settings), worker=Depends(get_worker)):
    """What was said on the home screen and may be shown again: see ``home_turns``."""
    _home_enabled(settings)
    turns = home_turns(catalog, read_maps(shelf_books(catalog, settings, worker)), limit=80)
    return {"turns": [{"role": turn["role"], "content": turn["content"], "created_at": turn["created_at"]}
                      for turn in turns]}


@router.delete("/conversation", status_code=204)
def clear_home_conversation(catalog=Depends(get_catalog), settings=Depends(get_settings)):
    _home_enabled(settings)
    catalog.clear_turns(None)


@router.get("/profile")
def profile(catalog=Depends(get_catalog), settings=Depends(get_settings), worker=Depends(get_worker)):
    return reader_profile(catalog, settings, _history(settings), worker)
