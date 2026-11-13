"""Pick a classic: search the public-domain catalogue and add one book to the shelf.

Adding goes through the same import as an uploaded file (``import_epub_blob``), so a downloaded
book is segmented, validated and shelved under exactly the same rules. The shelved book's catalog
``source`` records which classic it is, which is how the list knows a classic is already on the
shelf."""
from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.books import import_epub_blob
from app.classics import (
    MAX_PAGE,
    SOURCE_NAME,
    SOURCE_URL,
    ClassicUnavailable,
    catalog_summary,
    fetch_classic,
    find_classic,
    search,
    source_tag,
)
from app.deps import get_catalog, get_settings, get_store

router = APIRouter(prefix="/api/classics", tags=["classics"])


def _public(entry, shelved):
    return {"id": entry["id"], "title": entry["title"], "author": entry["author"],
            "language": entry["language"], "book_id": shelved.get(source_tag(entry))}


@router.get("")
def list_classics(
    q: str = Query("", max_length=120),
    language: str = Query("", max_length=12, pattern=r"^[a-z-]*$"),
    offset: int = Query(0, ge=0, le=200_000),
    limit: int = Query(40, ge=1, le=MAX_PAGE),
    catalog=Depends(get_catalog),
    settings=Depends(get_settings),
):
    base = {"source": SOURCE_NAME, "source_url": SOURCE_URL}
    if not settings.classics_enabled:
        return {**base, "enabled": False, "total": 0, "matches": 0, "languages": [], "books": []}
    rule = settings.classics_min_years_since_death
    shelved = {book["source"]: book["book_id"] for book in catalog.list_books() if book.get("source")}
    matches, page = search(q, language=language or None, offset=offset, limit=limit, min_years_since_death=rule)
    return {**base, "enabled": True, **catalog_summary(min_years_since_death=rule), "matches": matches,
            "books": [_public(entry, shelved) for entry in page]}


@router.post("/{classic_id}", status_code=201)
def add_classic(classic_id: str, catalog=Depends(get_catalog), store=Depends(get_store),
                settings=Depends(get_settings)):
    entry = None
    if settings.classics_enabled:
        entry = find_classic(classic_id, min_years_since_death=settings.classics_min_years_since_death)
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
    return import_epub_blob(blob, filename=f"gutenberg-{entry['pg']}.epub", source=tag,
                            fallback_title=entry["title"], fallback_author=entry["author"],
                            catalog=catalog, store=store, settings=settings)
