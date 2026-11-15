"""Named character codes (``&eacute;``, ``&nbsp;``, ``&mdash;``) in a book file are stored as the
letters they stand for (language audit W3). The five XML codes and numeric codes keep working; an
unknown code is kept as its literal text; nothing a book's own DOCTYPE declares is ever expanded."""
import io
import zipfile

import pytest

from app.ingest.extraction.chapter_text import chapter_texts

XHTML11 = ('<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" '
           '"http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">')
_CONTAINER = ('<?xml version="1.0"?><container version="1.0" '
              'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
              '<rootfile full-path="content.opf" media-type="application/oebps-package+xml"/>'
              '</rootfiles></container>')


def book(body, *, doctype=XHTML11, label="Chapter I"):
    """One-chapter EPUB whose chapter file holds ``body`` exactly as written (codes untouched)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", _CONTAINER)
        z.writestr("content.opf", (
            '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="2.0" '
            'unique-identifier="id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
            '<dc:title>T</dc:title><dc:language>fr</dc:language></metadata><manifest>'
            '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>'
            '<item id="d0" href="c1.xhtml" media-type="application/xhtml+xml"/></manifest>'
            '<spine toc="ncx"><itemref idref="d0"/></spine></package>'))
        z.writestr("toc.ncx", (
            '<?xml version="1.0"?><ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">'
            f'<navMap><navPoint id="n0"><navLabel><text>{label}</text></navLabel>'
            '<content src="c1.xhtml"/></navPoint></navMap></ncx>'))
        z.writestr("c1.xhtml", (
            f'<?xml version="1.0" encoding="utf-8"?>{doctype}'
            f'<html xmlns="http://www.w3.org/1999/xhtml"><body><h1>Chapter I</h1>{body}</body></html>'))
    return buf.getvalue()


def stored(body, **kw):
    [chapter] = chapter_texts(book(body, **kw), "b")
    return chapter["text"]


NAMED = ("<p>Anna entra au caf&eacute;&nbsp;; un na&iuml;ve gar&ccedil;on l&rsquo;attendait "
         "&mdash; d&eacute;j&agrave;&hellip;</p>")
EXPECTED = "Anna entra au café ; un naïve garçon l’attendait — déjà…"


@pytest.mark.parametrize("doctype", [XHTML11, ""], ids=["xhtml-doctype", "no-doctype"])
def test_named_codes_are_stored_as_the_letters_they_stand_for(doctype):
    text = stored(NAMED, doctype=doctype)
    assert EXPECTED in text
    assert "&" not in text and "caf " not in text


def test_xml_and_numeric_codes_keep_working():
    text = stored("<p>a &amp; b &lt;c&gt; &quot;d&quot; &apos;e&apos; caf&#233; caf&#xE9;</p>")
    assert "a & b <c> \"d\" 'e' café café" in text


def test_literal_markup_in_the_text_stays_text():
    text = stored("<p>He typed &lt;script&gt;alert(1)&lt;/script&gt; and &amp;eacute; there.</p>")
    assert "He typed <script>alert(1)</script> and &eacute; there." in text


@pytest.mark.parametrize("doctype", [XHTML11, ""], ids=["xhtml-doctype", "no-doctype"])
def test_an_unknown_code_is_kept_as_its_text_and_loses_nothing_around_it(doctype):
    text = stored("<p>before &notarealcode; after caf&eacute;</p>", doctype=doctype)
    assert "before &notarealcode; after café" in text


def test_a_code_the_book_declares_itself_is_never_expanded():
    huge = "&lol;" * 50
    doctype = ('<!DOCTYPE html [<!ENTITY lol "lollollollollollollollollollol">'
               f'<!ENTITY lol2 "{huge}"><!ENTITY eacute "PWNED">]>')
    text = stored("<p>start &lol2; middle caf&eacute; end</p>", doctype=doctype)
    assert "lollol" not in text and "PWNED" not in text
    assert "start &lol2; middle café end" in text


def test_named_codes_in_a_chapter_label_are_resolved_too():
    [chapter] = chapter_texts(book("<p>Some prose here.</p>", label="Chapitre&nbsp;I &mdash; Le caf&eacute;"),
                              "b")
    # A label is collapsed to single plain spaces, so its non-breaking space is one too.
    assert chapter["title"] == "Chapitre I \u2014 Le caf\u00e9"
