import { describe, expect, test } from "vitest";
import { convertFb2, decodeFb2, Fb2Error, isFb2 } from "./fb2ToEpub";
import { crc32, zipStore } from "./zipStore";

/** Read back a stored (uncompressed) zip, as written by zipStore. */
function unzip(bytes: Uint8Array) {
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const files = new Map<string, Uint8Array>();
  const order: string[] = [];
  let at = 0;
  while (view.getUint32(at, true) === 0x04034b50) {
    const size = view.getUint32(at + 18, true);
    const nameLength = view.getUint16(at + 26, true);
    const name = new TextDecoder().decode(bytes.subarray(at + 30, at + 30 + nameLength));
    const data = bytes.subarray(at + 30 + nameLength, at + 30 + nameLength + size);
    expect(view.getUint16(at + 8, true)).toBe(0); // stored
    expect(view.getUint32(at + 14, true)).toBe(crc32(data));
    files.set(name, data);
    order.push(name);
    at += 30 + nameLength + size;
  }
  return { order, text: (name: string) => new TextDecoder().decode(files.get(name)), has: (name: string) => files.has(name) };
}

const PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==";
const book = `<?xml version="1.0" encoding="utf-8"?>
<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0" xmlns:l="http://www.w3.org/1999/xlink">
<description><title-info>
  <author><first-name>Anton</first-name><last-name>Chekhov</last-name></author>
  <book-title>The Lady &amp; the Dog</book-title><lang>en</lang>
  <coverpage><image l:href="#cover.png"/></coverpage>
</title-info></description>
<body>
  <title><p>The Lady &amp; the Dog</p></title>
  <section>
    <title><p>Part One</p></title>
    <section id="one"><title><p>I</p></title>
      <p>It was said that a <emphasis>new person</emphasis> had appeared on the sea-front.<a l:href="#n1" type="note">[1]</a></p>
      <empty-line/>
      <p onclick="alert(1)">Dmitri &lt;script&gt;alert(1)&lt;/script&gt; Gurov was interested.</p>
      <image l:href="#cover.png"/>
    </section>
    <section><title><p>II</p></title>
      <p>A week had passed. See <a l:href="#one">the beginning</a> or <a l:href="javascript:alert(1)">this</a>.</p>
      <poem><stanza><v>First line</v><v>Second line</v></stanza></poem>
    </section>
  </section>
  <section><p>An untitled closing section with enough words to count as text.</p></section>
</body>
<body name="notes"><section id="n1"><title><p>1</p></title><p>Yalta, on the Black Sea.</p></section></body>
<binary id="cover.png" content-type="image/png">${PNG}</binary>
<binary id="evil.svg" content-type="image/svg+xml">PHN2Zy8+</binary>
</FictionBook>`;

const convert = (text: string) => convertFb2(new TextEncoder().encode(text));

describe("FB2 to EPUB", () => {
  test("produces a well-formed EPUB container with the book's metadata", () => {
    const { epub, title, author } = convert(book);
    expect(title).toBe("The Lady & the Dog");
    expect(author).toBe("Anton Chekhov");
    const zip = unzip(epub);
    expect(zip.order[0]).toBe("mimetype");
    expect(zip.text("mimetype")).toBe("application/epub+zip");
    expect(zip.text("META-INF/container.xml")).toContain('full-path="OEBPS/content.opf"');
    const opf = zip.text("OEBPS/content.opf");
    expect(opf).toContain("<dc:title>The Lady &amp; the Dog</dc:title>");
    expect(opf).toContain("<dc:creator>Anton Chekhov</dc:creator>");
    expect(opf).toContain("<dc:language>en</dc:language>");
    expect(opf).toContain('properties="nav"');
    expect(opf).toContain('properties="cover-image"');
    expect((opf.match(/<itemref /g) ?? []).length).toBe(4); // I, II, the untitled section, Notes
  });

  test("makes one chapter per section with text and keeps parts as groups in the contents", () => {
    const zip = unzip(convert(book).epub);
    const nav = zip.text("OEBPS/nav.xhtml");
    expect(nav).toContain('<li><span>Part One</span><ol><li><a href="ch0001.xhtml">I</a></li><li><a href="ch0002.xhtml">II</a></li></ol></li>');
    expect(nav).toContain('<a href="ch0003.xhtml">Chapter 3</a>');
    expect(nav).toContain('<a href="notes.xhtml">Notes</a>');
    const first = zip.text("OEBPS/ch0001.xhtml");
    expect(first).toContain("<h2>I</h2>");
    expect(first).toContain("<em>new person</em>");
    expect(first).toContain('<img src="images/img0001.png" alt=""/>');
    expect(zip.text("OEBPS/ch0002.xhtml")).toContain('<p class="verse">First line</p>');
    expect(zip.text("OEBPS/ch0003.xhtml")).toContain("<h1>Chapter 3</h1>");
  });

  test("links footnotes and cross-references to the file they landed in", () => {
    const zip = unzip(convert(book).epub);
    expect(zip.text("OEBPS/ch0001.xhtml")).toContain('<a href="notes.xhtml#n1">[1]</a>');
    expect(zip.text("OEBPS/ch0002.xhtml")).toContain('<a href="ch0001.xhtml#one">the beginning</a>');
    expect(zip.text("OEBPS/notes.xhtml")).toContain('id="n1"');
  });

  test("carries nothing executable across from a hostile file", () => {
    const zip = unzip(convert(book).epub);
    const all = zip.order.filter((name) => /\.(xhtml|opf)$/.test(name)).map(zip.text).join("\n");
    expect(all).not.toMatch(/<script/i);
    expect(all).not.toMatch(/onclick/i);
    expect(all).not.toMatch(/javascript:/i);
    expect(all).toContain("&lt;script&gt;alert(1)&lt;/script&gt;");
    expect(zip.order.some((name) => name.endsWith(".svg"))).toBe(false);
    // every page is well-formed XML
    for (const name of zip.order.filter((item) => item.endsWith(".xhtml") || item.endsWith(".opf"))) {
      const parsed = new DOMParser().parseFromString(zip.text(name).replace(/<!DOCTYPE html>\n/, ""), "application/xml");
      expect(parsed.getElementsByTagName("parsererror").length, name).toBe(0);
    }
  });

  test("the same file always converts to the same bytes, so a re-import is recognised", () => {
    expect(convert(book).epub).toEqual(convert(book).epub);
  });

  test("honours the declared text encoding", () => {
    const head = '<?xml version="1.0" encoding="windows-1251"?><FictionBook><description><title-info><book-title>';
    const tail = "</book-title></title-info></description><body><section><p>text</p></section></body></FictionBook>";
    const bytes = new Uint8Array([...new TextEncoder().encode(head), 0xc4, 0xee, 0xec, ...new TextEncoder().encode(tail)]);
    expect(decodeFb2(bytes)).toContain("Дом");
    expect(convertFb2(bytes).title).toBe("Дом");
  });

  test("a body without sections becomes a single chapter", () => {
    const { epub } = convert("<FictionBook><description><title-info><book-title>Short</book-title></title-info></description><body><p>Only this.</p></body></FictionBook>");
    const zip = unzip(epub);
    expect(zip.text("OEBPS/ch0001.xhtml")).toContain("<p>Only this.</p>");
  });

  test("refuses files that are not FB2 books", () => {
    expect(() => convert("<html><body>nope</body></html>")).toThrow(Fb2Error);
    expect(() => convert("not xml at all <")).toThrow(Fb2Error);
    expect(() => convert("<FictionBook><description/></FictionBook>")).toThrow(/no text/);
  });

  test("recognises FB2 files by name or type", () => {
    expect(isFb2({ name: "Book.FB2", type: "" })).toBe(true);
    expect(isFb2({ name: "book", type: "application/x-fictionbook+xml" })).toBe(true);
    expect(isFb2({ name: "book.epub", type: "application/epub+zip" })).toBe(false);
  });
});

describe("the zip writer", () => {
  test("stores entries in order with correct checksums", () => {
    expect(crc32(new TextEncoder().encode("123456789"))).toBe(0xcbf43926);
    const zip = unzip(zipStore([{ name: "a.txt", data: new TextEncoder().encode("hello") }, { name: "dir/b.txt", data: new Uint8Array(0) }]));
    expect(zip.order).toEqual(["a.txt", "dir/b.txt"]);
    expect(zip.text("a.txt")).toBe("hello");
  });
});
