"""Pick a classic: list the curated public-domain catalogue and add one entry to the shelf.

Adding goes through the same import as an uploaded file (``import_epub_blob``), so a downloaded
book is segmented, validated and shelved under exactly the same rules. The shelved book's catalog
``source`` records which classic it is, which is how the list knows a classic is already on the
shelf."""
from fastapi import APIRouter, Depends, HTTPException

from app.api.books import import_epub_blob
from app.classics import (
    SOURCE_NAME,
    ClassicUnavailable,
    fetch_classic,
    find_classic,
    load_catalog,
    source_tag,
)
from app.deps import get_catalog, get_settings, get_store

router = APIRouter(prefix="/api/classics", tags=["classics"])


def _public(entry, shelved):
    return {"id": entry["id"], "title": entry["title"], "author": entry["author"],
            "year": entry["year"], "book_id": shelved.get(source_tag(entry))}


@router.get("")
def list_classics(catalog=Depends(get_catalog), settings=Depends(get_settings)):
    if not settings.classics_enabled:
        return {"enabled": False, "source": SOURCE_NAME, "books": []}
    shelved = {book["source"]: book["book_id"] for book in catalog.list_books() if book.get("source")}
    return {"enabled": True, "source": SOURCE_NAME,
            "books": [_public(entry, shelved) for entry in load_catalog()]}


@router.post("/{classic_id}", status_code=201)
def add_classic(classic_id: str, catalog=Depends(get_catalog), store=Depends(get_store),
                settings=Depends(get_settings)):
    entry = find_classic(classic_id) if settings.classics_enabled else None
    if entry is None:
        raise HTTPException(404, "unknown classic")
    tag = source_tag(entry)
    existing = next((book for book in catalog.list_books() if book.get("source") == tag), None)
    if existing:
        raise HTTPException(409, f"book already imported as {existing['book_id']!r}")
    try:
        blob = fetch_classic(entry, max_bytes=settings.epub_max_upload_bytes)
    except ClassicUnavailable as exc:
        raise HTTPException(502, str(exc)) from exc
    return import_epub_blob(blob, filename=f"{entry['id']}.epub", source=tag,
                            fallback_title=entry["title"], fallback_author=entry["author"],
                            catalog=catalog, store=store, settings=settings)
