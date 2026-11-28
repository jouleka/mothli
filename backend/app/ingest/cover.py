"""The cover picture an EPUB carries, for the shelf.

An EPUB names its cover in its package file in one of a few ways: EPUB 3 marks a manifest item
``properties="cover-image"``; EPUB 2 points at one with ``<meta name="cover" content="item-id">``;
some older files do neither and simply call an image "cover". Each is tried in that order.

The book is an untrusted upload, so the same guarded readers as the segmenter are used, the picture
is bounded in size, and only a real raster image is returned (decided by its first bytes, never by
what the file says it is). An SVG is not served."""
import io
import posixpath
import zipfile

from PIL import Image

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
    items, guides, cover_id = [], [], None
    for el in root.iter():
        tag = _ln(el.tag)
        if tag == "item" and el.get("href"):
            items.append(el)
        elif tag == "reference" and "cover" in (el.get("type") or "").split() and el.get("href"):
            guides.append(el)
        elif tag == "meta" and (el.get("name") or "").lower() == "cover" and el.get("content"):
            cover_id = cover_id or el.get("content")
    images = [el for el in items if (el.get("media-type") or "").lower().startswith("image/")]
    marked = [el for el in items if "cover-image" in (el.get("properties") or "").split()]
    named = [el for el in items if cover_id is not None and el.get("id") == cover_id]
    guessed = [el for el in images
               if "cover" in (el.get("id") or "").lower() or "cover" in posixpath.basename(el.get("href")).lower()]
    seen, hrefs = set(), []
    for el in (*marked, *named, *guides, *guessed):
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
                    path = _join(base, href.partition("#")[0])
                    blob = _read(zf, path, MAX_COVER_BYTES)
                except ValueError:
                    continue
                kind = image_type(blob)
                if kind:
                    return blob, kind
                # Some editions mark an XHTML or SVG cover page containing a local raster.
                if len(blob) <= MAX_METADATA_BYTES and blob.lstrip().startswith(b"<"):
                    page = _parse(blob)
                    for node in page.iter() if page is not None else ():
                        if _ln(node.tag) not in ("img", "image"):
                            continue
                        src = node.get("src") or node.get("href") or node.get("{http://www.w3.org/1999/xlink}href")
                        if src:
                            try:
                                raster = _read(zf, _join(posixpath.dirname(path), src), MAX_COVER_BYTES)
                            except ValueError:
                                break
                            if image_type(raster):
                                return raster, image_type(raster)
                        break
    except (ValueError, OSError, zipfile.BadZipFile, RuntimeError, NotImplementedError):
        return None
    return None


def extract_thumbnail(source):
    """Small single-frame shelf artwork, or empty bytes for a book without a usable cover."""
    found = extract_cover(source)
    if found is None:
        return b""
    try:
        with Image.open(io.BytesIO(found[0]), formats=("JPEG", "PNG", "GIF", "WEBP")) as image:
            if image.width * image.height > 24_000_000:
                return b""
            image.thumbnail((320, 480))
            out = io.BytesIO()
            image.convert("RGB").save(out, format="WEBP", quality=80)
            return out.getvalue()
    except (ValueError, OSError, Image.DecompressionBombError):
        return b""
