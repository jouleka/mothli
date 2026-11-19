"""A small EPUB laid out the way Project Gutenberg ships "Alice's Adventures in Wonderland" (book 11).

A cover page, then a file of Gutenberg's own (its header, the START line, the book's title page
and its contents), then one file per chapter, then Gutenberg's licence. The app splits it into
the cover, a piece titled "The Project Gutenberg eBook of ...", and the chapters.
"""
import io
import zipfile

_CONTAINER = ('<?xml version="1.0"?><container version="1.0" '
              'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
              '<rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
              '</rootfiles></container>')

TITLE = "Lamp Tales"
# Text on the Gutenberg page before its START line: the server keeps it out of every chapter.
HEADER = "This eBook is for the use of anyone anywhere in the United States at no cost."
# The book's own title page and contents, after the START line.
CONTENTS = "THE LANTERN EDITION 1.0"
CHAPTERS = [("CHAPTER I.", "Down the Lamp-Hole"), ("CHAPTER II.", "The Pool of Oil"),
            ("CHAPTER III.", "A Wick-Race")]
PEOPLE = ["Mara", "Corvus", "Vex"]


def line(chapter, number):
    """Sentence ``number`` of chapter ``chapter`` (1-based): every one is different."""
    return f"{PEOPLE[chapter - 1]} moved lamp {number} in part {chapter}."


def member(name):
    """A zip member stamped with one fixed time, so the book made twice is one file (one file
    hash, one edition: ADR 0044). ``writestr`` with a bare name stamps the clock, to two seconds;
    two readers' copies made a moment apart were then two files, and shared nothing."""
    info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
    info.external_attr = 0o600 << 16
    return info


def _page(title, body):
    return ('<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml" lang="en">'
            f"<head><title>{title}</title></head><body>{body}</body></html>")


def alice_shaped_epub():
    contents = "".join(f"<p>{number} {name}</p>" for number, name in CHAPTERS)
    files = [
        ("wrap0000.xhtml", _page("Cover", '<div class="x-ebookmaker-cover"><img src="cover.jpg" alt=""/></div>')),
        ("pg-0.xhtml", _page(TITLE, (
            f'<header id="pg-header"><h2>The Project Gutenberg eBook of <span>{TITLE}</span></h2>'
            f"<div>{HEADER}</div><p>Title: {TITLE}</p>"
            f"<div><span>*** START OF THE PROJECT GUTENBERG EBOOK {TITLE.upper()} ***</span>\n</div></header>\n"
            f'<h1 id="t0">{TITLE}</h1><h4 id="t1">{CONTENTS}</h4><h2 id="t2">Contents</h2>{contents}'))),
    ]
    for n, (number, name) in enumerate(CHAPTERS, 1):
        body = " ".join(line(n, i) for i in range(1, 61))
        files.append((f"pg-{n}.xhtml", _page(f"{number} {name}", (
            f'<div class="chapter" id="c{n}"><h2>{number}<br/>\n{name}</h2><p>{body}</p></div>'))))
    files.append((f"pg-{len(CHAPTERS) + 1}.xhtml", _page(TITLE, (
        f"<p>THE END</p><section><div>*** END OF THE PROJECT GUTENBERG EBOOK {TITLE.upper()} ***</div>"
        "<h2>THE FULL PROJECT GUTENBERG LICENSE</h2><p>Section 1. General Terms of Use and Redistributing "
        "Project Gutenberg electronic works.</p></section>"))))
    toc = [("pg-0.xhtml", "t0", TITLE), ("pg-0.xhtml", "t1", CONTENTS), ("pg-0.xhtml", "t2", "Contents")]
    toc += [(f"pg-{n}.xhtml", f"c{n}", f"{number} {name}") for n, (number, name) in enumerate(CHAPTERS, 1)]
    toc.append((f"pg-{len(CHAPTERS) + 1}.xhtml", "", "THE FULL PROJECT GUTENBERG LICENSE"))
    items = "".join(f'<item id="d{i}" href="{name}" media-type="application/xhtml+xml"/>'
                    for i, (name, _body) in enumerate(files))
    spine = "".join(f'<itemref idref="d{i}"/>' for i in range(len(files)))
    points = "".join(f'<navPoint id="n{i}" playOrder="{i + 1}"><navLabel><text>{label}</text></navLabel>'
                     f'<content src="{name}{"#" + frag if frag else ""}"/></navPoint>'
                     for i, (name, frag, label) in enumerate(toc))
    opf = ('<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="2.0" '
           'unique-identifier="id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
           f"<dc:title>{TITLE}</dc:title><dc:creator>Ada Writer</dc:creator><dc:language>en</dc:language>"
           '</metadata><manifest><item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>'
           f'{items}</manifest><spine toc="ncx">{spine}</spine></package>')
    ncx = ('<?xml version="1.0"?><ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">'
           f"<navMap>{points}</navMap></ncx>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(member("mimetype"), "application/epub+zip")
        z.writestr(member("META-INF/container.xml"), _CONTAINER)
        z.writestr(member("OEBPS/content.opf"), opf)
        z.writestr(member("OEBPS/toc.ncx"), ncx)
        for name, body in files:
            z.writestr(member(f"OEBPS/{name}"), body)
    return buf.getvalue()
