import { describe, expect, test } from "vitest";
import { bookFileKind, unsupportedBookMessage } from "./bookFiles";
import { convertKindleBook, kindleKind, mobiToEpub, PROTECTED_MESSAGE, type KindleBook } from "./mobiToEpub";

function unzip(bytes: Uint8Array) {
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const files = new Map<string, Uint8Array>();
  let at = 0;
  while (view.getUint32(at, true) === 0x04034b50) {
    const size = view.getUint32(at + 18, true);
    const nameLength = view.getUint16(at + 26, true);
    const name = new TextDecoder().decode(bytes.subarray(at + 30, at + 30 + nameLength));
    files.set(name, bytes.subarray(at + 30 + nameLength, at + 30 + nameLength + size));
    at += 30 + nameLength + size;
  }
  return { names: [...files.keys()], text: (name: string) => new TextDecoder().decode(files.get(name)) };
}

const PNG = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 1, 2, 3, 4]);

/** A minimal, uncompressed, old-style MOBI file: one text record and one picture. */
function makeMobi(html: string, { encryption = 0, title = "A Small Book" } = {}) {
  const encoder = new TextEncoder();
  const text = encoder.encode(html);
  const titleBytes = encoder.encode(title);
  const record0 = new Uint8Array(248 + titleBytes.length);
  const r0 = new DataView(record0.buffer);
  r0.setUint16(0, 1); // no compression
  r0.setUint16(8, 1); // one text record
  r0.setUint16(10, 4096);
  r0.setUint16(12, encryption);
  record0.set(encoder.encode("MOBI"), 16);
  r0.setUint32(20, 232);
  r0.setUint32(24, 2);
  r0.setUint32(28, 65001); // UTF-8
  r0.setUint32(32, 12345);
  r0.setUint32(36, 6);
  r0.setUint32(84, 248);
  r0.setUint32(88, titleBytes.length);
  r0.setUint8(95, 9); // English
  r0.setUint32(108, 2); // first resource record
  r0.setUint32(244, 0xffffffff); // no index
  record0.set(titleBytes, 248);

  const records = [record0, text, PNG];
  const headerSize = 78 + records.length * 8 + 2;
  const file = new Uint8Array(headerSize + records.reduce((sum, record) => sum + record.length, 0));
  const view = new DataView(file.buffer);
  file.set(encoder.encode("A_Small_Book"), 0);
  file.set(encoder.encode("BOOKMOBI"), 60);
  view.setUint16(76, records.length);
  let offset = headerSize;
  records.forEach((record, index) => {
    view.setUint32(78 + index * 8, offset);
    file.set(record, offset);
    offset += record.length;
  });
  return file;
}

const bodyHtml =
  '<html><head><guide></guide><style>p{color:red}</style></head><body>' +
  '<h1>One</h1><p align="center">Tomas walked along the <i>harbour</i>.</p><script>alert(1)</script>' +
  '<p onclick="alert(2)">A <a href="javascript:alert(3)">bad link</a> and an <a href="https://example.org/">outside one</a>.</p>' +
  '<img recindex="1" alt="A map"/><mbp:pagebreak/>' +
  "<h1>Two</h1><p>Elena took the ferry. <a filepos=0000000067>Back to the start</a></p></body></html>";

describe("recognising Kindle files", () => {
  test("tells an open book from a copy-protected one", () => {
    expect(kindleKind(makeMobi(bodyHtml))).toBe("mobi");
    expect(kindleKind(makeMobi(bodyHtml, { encryption: 2 }))).toBe("protected");
    expect(kindleKind(makeMobi(bodyHtml, { encryption: 1 }))).toBe("protected");
    expect(kindleKind(new TextEncoder().encode("PK just a zip, not a kindle book ".repeat(5)))).toBe("unknown");
    expect(kindleKind(new Uint8Array(10))).toBe("unknown");
  });

  test("refuses a copy-protected book without trying to open it", async () => {
    const file = new File([makeMobi(bodyHtml, { encryption: 2 }) as BlobPart], "bought.azw3");
    await expect(mobiToEpub(file)).rejects.toThrow(PROTECTED_MESSAGE);
  });

  test("explains a file that only claims to be a Kindle book", async () => {
    await expect(mobiToEpub(new File(["not a book at all ".repeat(20)], "odd.mobi"))).rejects.toThrow(/can’t read/);
  });
});

describe("MOBI to EPUB", () => {
  test("converts a real MOBI container end to end", async () => {
    expect(bodyHtml.indexOf("<h1>One</h1>")).toBe(67); // the filepos link above points here
    const epubFile = await mobiToEpub(new File([makeMobi(bodyHtml) as BlobPart], "small.mobi"));
    expect(epubFile.name).toBe("small.epub");
    expect(epubFile.type).toBe("application/epub+zip");
    const bytes = await new Promise<Uint8Array>((resolve) => {
      const reader = new FileReader();
      reader.onload = () => resolve(new Uint8Array(reader.result as ArrayBuffer));
      reader.readAsArrayBuffer(epubFile);
    });
    const zip = unzip(bytes);
    expect(zip.names[0]).toBe("mimetype");
    const opf = zip.text("OEBPS/content.opf");
    expect(opf).toContain("<dc:title>A Small Book</dc:title>");
    expect(opf).toContain("<dc:language>en</dc:language>");
    expect((opf.match(/<itemref /g) ?? []).length).toBe(2);
    const one = zip.text("OEBPS/sec0001.xhtml");
    expect(one).toContain("<h1>One</h1>");
    expect(one).toContain('<p class="center">Tomas walked along the <em>harbour</em>.</p>');
    expect(one).toContain('<img src="images/img0001.png" alt="A map"/>');
    expect(one).toContain('<a href="https://example.org/">outside one</a>');
    const two = zip.text("OEBPS/sec0002.xhtml");
    expect(two).toContain('<a href="sec0001.xhtml#filepos0000000067">Back to the start</a>');
    expect(one).toContain('id="filepos0000000067"');
    expect(zip.names).toContain("OEBPS/images/img0001.png");
    // no contents in the file: every page becomes an entry, named by its heading
    const nav = zip.text("OEBPS/nav.xhtml");
    expect(nav).toContain('<a href="sec0001.xhtml">One</a>');
    expect(nav).toContain('<a href="sec0002.xhtml">Two</a>');
    // nothing executable crosses over, and every page is well-formed
    const pages = zip.names.filter((name) => name.endsWith(".xhtml"));
    const all = pages.map(zip.text).join("\n");
    expect(all).not.toMatch(/<script|<style|onclick|javascript:|color:red/i);
    for (const name of pages) {
      const parsed = new DOMParser().parseFromString(zip.text(name).replace(/<!DOCTYPE html>\n/, ""), "application/xml");
      expect(parsed.getElementsByTagName("parsererror").length, name).toBe(0);
    }
  });

  test("uses the book's own contents when it has one, and skips empty pages", async () => {
    const page = (html: string) => new DOMParser().parseFromString(`<html><body>${html}</body></html>`, "text/html");
    const docs = [page("<h2>Prologue</h2><p id='start'>It began.</p>"), page("   "), page("<p>Chapter text <span id='mid'>here</span>.</p>")];
    const book: KindleBook = {
      sections: docs.map((doc) => ({ createDocument: () => doc })),
      metadata: { title: "Contents Book", author: ["Ada North", "Maya West"], language: ["en-GB"] },
      toc: [
        { label: "Prologue", href: "p:0", subitems: [{ label: "Midway", href: "p:2:mid" }] },
        { label: "Blank", href: "p:1" },
        { label: "", href: "p:2" },
      ],
      resolveHref: (href) => {
        const [, index, id] = href.split(":");
        return { index: Number(index), anchor: (doc) => (id ? doc.getElementById(id) : null) };
      },
      getCover: () => null,
    };
    const { epub, title, author } = await convertKindleBook(book, async () => null, { identifier: "urn:test" });
    expect(title).toBe("Contents Book");
    expect(author).toBe("Ada North, Maya West");
    const zip = unzip(epub);
    expect(zip.names).not.toContain("OEBPS/sec0002.xhtml");
    const nav = zip.text("OEBPS/nav.xhtml");
    expect(nav).toContain('<li><a href="sec0001.xhtml">Prologue</a><ol><li><a href="sec0003.xhtml#mid">Midway</a></li></ol></li>');
    expect(nav).not.toContain("Blank");
    expect(zip.text("OEBPS/content.opf")).toContain("<dc:language>en-GB</dc:language>");
  });

  test("a book with no text is refused plainly", async () => {
    const empty = new DOMParser().parseFromString("<html><body> </body></html>", "text/html");
    await expect(
      convertKindleBook({ sections: [{ createDocument: () => empty }] }, async () => null, { identifier: "urn:test" }),
    ).rejects.toThrow("This book has no text in it.");
  });
});

describe("which files the library accepts", () => {
  test("sorts files by kind", () => {
    expect(bookFileKind({ name: "a.EPUB", type: "" })).toBe("epub");
    expect(bookFileKind({ name: "a.fb2", type: "" })).toBe("fb2");
    for (const name of ["a.mobi", "a.azw", "a.AZW3", "a.prc"]) expect(bookFileKind({ name, type: "" })).toBe("kindle");
    expect(bookFileKind({ name: "a.pdf", type: "" })).toBe("pdf");
    expect(bookFileKind({ name: "a.fb2.zip", type: "" })).toBe("zipped");
    expect(bookFileKind({ name: "a.kfx", type: "" })).toBe("kfx");
    expect(bookFileKind({ name: "a.docx", type: "" })).toBe("unknown");
  });

  test("explains each kind it cannot take, in plain words", () => {
    for (const kind of ["epub", "fb2", "kindle", "zipped"] as const) expect(unsupportedBookMessage(kind)).toBeNull();
    expect(unsupportedBookMessage("pdf")).toContain("PDFs aren’t supported yet");
    expect(unsupportedBookMessage("kfx")).toContain("outside Kindle");
    expect(unsupportedBookMessage("unknown")).toContain("EPUB, FB2, MOBI or AZW3");
  });
});
