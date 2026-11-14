"""Books whose chapters do not line up with their files (the Project Gutenberg layout).

One file can hold front matter AND the opening chapters, a chapter can continue across a file
split, and a file can have no ToC entry at all. None of that may lose prose, and no prose may
move to an EARLIER atom than the one whose anchor precedes it.
"""
import io
import zipfile

from app.ingest.extraction.chapter_text import segment_for_ingest

_CONTAINER = ('<?xml version="1.0"?><container version="1.0" '
              'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
              '<rootfile full-path="content.opf" media-type="application/oebps-package+xml"/>'
              '</rootfiles></container>')
WORDS = " The wind crossed the valley and nothing else happened that day." * 6


def _epub(files, toc):
    """files = [(name, body_html)], toc = [(name, fragment, label)]; EPUB2 with an NCX."""
    items = "".join(f'<item id="d{i}" href="{n}" media-type="application/xhtml+xml"/>'
                    for i, (n, _b) in enumerate(files))
    spine = "".join(f'<itemref idref="d{i}"/>' for i in range(len(files)))
    points = "".join(f'<navPoint id="n{i}"><navLabel><text>{lab}</text></navLabel>'
                     f'<content src="{n}#{fr}"/></navPoint>' for i, (n, fr, lab) in enumerate(toc))
    opf = ('<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="2.0" '
           'unique-identifier="id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
           '<dc:title>Split</dc:title></metadata><manifest>'
           '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>'
           f'{items}</manifest><spine toc="ncx">{spine}</spine></package>')
    ncx = ('<?xml version="1.0"?><ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">'
           f'<navMap>{points}</navMap></ncx>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", _CONTAINER)
        z.writestr("content.opf", opf)
        z.writestr("toc.ncx", ncx)
        for name, body in files:
            z.writestr(name, '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml">'
                             f'<body>{body}</body></html>')
    return buf.getvalue()


def _gutenberg_like():
    files = [
        ("f0.html",
         "<p>The Project Gutenberg eBook of Split</p><p>*** START OF THE PROJECT GUTENBERG EBOOK SPLIT ***</p>"
         '<h1 id="t">SPLIT: A NOVEL</h1><p>PREFACE_SPOILER the heroine marries the villain.</p>'
         f'<h2 id="c1">CHAPTER I.</h2><p>ONE_OPENING Mara arrived.{WORDS}</p>'
         f'<h2 id="c2">CHAPTER II.</h2><p>TWO_START Jonas waited.{WORDS}</p>'),
        ("f1.html",
         f"<p>TWO_CONTINUED across the file split.{WORDS}</p>"
         f'<h2 id="c3">CHAPTER III.</h2><p>THREE_START Nell counted coins.{WORDS}</p>'),
        ("f2.html", f"<p>THREE_WHOLE_FILE with no table of contents entry.{WORDS}</p>"),
        ("f3.html",
         f"<p>THREE_TAIL before the next heading.{WORDS}</p>"
         f'<h2 id="c4">CHAPTER IV.</h2><p>FOUR_START the last chapter.{WORDS}</p>'),
    ]
    toc = [("f0.html", "t", "SPLIT: A NOVEL"), ("f0.html", "c1", "CHAPTER I."),
           ("f0.html", "c2", "CHAPTER II."), ("f1.html", "c3", "CHAPTER III."),
           ("f3.html", "c4", "CHAPTER IV.")]
    return _epub(files, toc)


def test_opening_chapters_in_the_front_matter_file_are_kept_and_front_matter_is_not():
    result, chapters = segment_for_ingest(_gutenberg_like(), "b")
    assert result.mode == "anchor-driven"
    assert [c["title"] for c in chapters] == ["CHAPTER I.", "CHAPTER II.", "CHAPTER III.", "CHAPTER IV."]
    assert "ONE_OPENING" in chapters[0]["text"]
    everything = " ".join(c["text"] for c in chapters)
    assert "PREFACE_SPOILER" not in everything and "Project Gutenberg" not in everything
    assert not [f for f in result.flags if "coverage gap" in f.lower() or "anchor resolution" in f.lower()]
    assert any("ahead of the first chapter" in f for f in result.flags)


def test_a_chapter_continuing_across_files_stays_whole_and_never_moves_earlier():
    _result, chapters = segment_for_ingest(_gutenberg_like(), "b")
    two, three, four = chapters[1]["text"], chapters[2]["text"], chapters[3]["text"]
    assert "TWO_START" in two and "TWO_CONTINUED" in two
    assert two.index("TWO_START") < two.index("TWO_CONTINUED")
    assert "THREE_START" in three and "THREE_WHOLE_FILE" in three and "THREE_TAIL" in three
    assert three.index("THREE_START") < three.index("THREE_WHOLE_FILE") < three.index("THREE_TAIL")
    assert "FOUR_START" in four
    # No atom holds prose that follows a later atom's anchor.
    assert "THREE" not in two and "FOUR" not in three and "TWO" not in chapters[0]["text"]


def test_a_file_with_no_toc_entry_is_spanned_by_the_preceding_atom():
    result, _chapters = segment_for_ingest(_gutenberg_like(), "b")
    assert result.atoms[2].source_files == ("f1.html", "f2.html")
    assert any("joined 1 body file" in f for f in result.flags)


def test_prose_ahead_of_the_first_anchor_is_kept_when_the_file_is_not_front_matter():
    files = [("a.html", f'<p>PROLOGUE_TEXT before any heading.{WORDS}</p>'
                        f'<h2 id="c1">CHAPTER I.</h2><p>ONE.{WORDS}</p>'
                        f'<h2 id="c2">CHAPTER II.</h2><p>TWO.{WORDS}</p>')]
    _result, chapters = segment_for_ingest(_epub(files, [("a.html", "c1", "CHAPTER I."),
                                                         ("a.html", "c2", "CHAPTER II.")]), "b")
    assert chapters[0]["text"].index("PROLOGUE_TEXT") < chapters[0]["text"].index("ONE.")
    assert "PROLOGUE_TEXT" not in chapters[1]["text"]
