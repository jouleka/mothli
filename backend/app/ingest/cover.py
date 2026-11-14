"""The cover picture an EPUB carries, for the shelf.

An EPUB names its cover in its package file in one of a few ways: EPUB 3 marks a manifest item
``properties="cover-image"``; EPUB 2 points at one with ``<meta name="cover" content="item-id">``;
some older files do neither and simply call an image "cover". Each is tried in that order.

The book is an untrusted upload, so the same guarded readers as the segmenter are used, the picture
is bounded in size, and only a real raster image is returned (decided by its first bytes, never by
what the file says it is). An SVG is not served."""
import posixpath
import zipfile

from app.ingest.segmentation.epub_segmenter import (
    MAX_METADATA_BYTES,
    _join,
    _ln,
    _opf_path,
    _parse,
    _read,
)

MAX_COVER_BYTES = 8 * 1024 * 1024


def image_type(blob):
    """The media type of a raster image, from its signature; None for anything else."""
    if blob[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if blob[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if blob[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return "image/webp"
    return None


def _candidates(root):
    """Manifest hrefs that may be the cover, most trustworthy first."""
    items, cover_id = [], None
    for el in root.iter():
        tag = _ln(el.tag)
        if tag == "item" and el.get("href"):
            items.append(el)
        elif tag == "meta" and (el.get("name") or "").lower() == "cover" and el.get("content"):
            cover_id = cover_id or el.get("content")
    images = [el for el in items if (el.get("media-type") or "").lower().startswith("image/")]
    marked = [el for el in items if "cover-image" in (el.get("properties") or "").split()]
    named = [el for el in items if cover_id is not None and el.get("id") == cover_id]
    guessed = [el for el in images
               if "cover" in (el.get("id") or "").lower() or "cover" in posixpath.basename(el.get("href")).lower()]
    seen, hrefs = set(), []
    for el in (*marked, *named, *guessed):
        href = el.get("href")
        if href not in seen:
            seen.add(href)
            hrefs.append(href)
    return hrefs


def extract_cover(path):
    """``(bytes, media_type)`` for the EPUB's cover picture, or None when it has none we can show."""
    try:
        with zipfile.ZipFile(path) as zf:
            opf = _opf_path(zf)
            root = _parse(_read(zf, opf, MAX_METADATA_BYTES))
            if root is None:
                return None
            base = posixpath.dirname(opf)
            for href in _candidates(root)[:6]:
                try:
                    blob = _read(zf, _join(base, href), MAX_COVER_BYTES)
                except ValueError:
                    continue
                kind = image_type(blob)
                if kind:
                    return blob, kind
    except (ValueError, OSError, zipfile.BadZipFile, RuntimeError, NotImplementedError):
        return None
    return None
