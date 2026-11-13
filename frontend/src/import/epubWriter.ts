/** Assemble an EPUB from already-safe parts. Every converter (FB2, MOBI/AZW3) ends here, so the
 * container layout the server's hardened import sees is identical whatever the source format was.
 * Output is deterministic: no timestamps, no random ids. */
import { zipStore, type ZipEntry } from "./zipStore";

/** A reason a book could not be added, worded for the reader. */
export class BookImportError extends Error {}

export type EpubChapter = { file: string; title: string; body: string };
export type EpubTocNode = { label: string; href: string; children: EpubTocNode[]; heading?: boolean };
export type EpubImage = { file: string; type: string; data: Uint8Array };

export const esc = (value: string) =>
  value.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
export const squash = (value: string | null | undefined) => (value ?? "").replace(/\s+/g, " ").trim();
/** A source id made safe to use as an XML id and a URL fragment. */
export const safeId = (value: string) => value.replace(/[^\p{L}\p{N}_.-]/gu, "_").replace(/^(?=[\d.-])/, "_");
export const safeLanguage = (value: string | null | undefined) =>
  /^[A-Za-z]{2,3}(-[A-Za-z0-9]+)*$/.test(squash(value)) ? squash(value) : "und";

/** Only real raster pictures are carried into a converted book. */
export function sniffImage(data: Uint8Array): { type: string; ext: string } | null {
  if (data[0] === 0xff && data[1] === 0xd8) return { type: "image/jpeg", ext: "jpg" };
  if (data[0] === 0x89 && data[1] === 0x50 && data[2] === 0x4e && data[3] === 0x47) return { type: "image/png", ext: "png" };
  if (data[0] === 0x47 && data[1] === 0x49 && data[2] === 0x46 && data[3] === 0x38) return { type: "image/gif", ext: "gif" };
  return null;
}

const BOOK_CSS =
  "h1,h2,h3{text-align:center;line-height:1.2}\n.gap{height:1em}\n.verse{margin:0;text-indent:0}\n.stanza{margin:1em 0}\n" +
  ".author,.date{text-align:right;font-style:italic}\n.epigraph{font-style:italic;margin-left:30%}\n.picture{text-align:center}\n" +
  ".center{text-align:center}\n.right{text-align:right}\nimg{max-width:100%}\n";

export function buildEpub({
  identifier,
  title,
  author,
  language,
  chapters,
  toc,
  images,
  cover,
}: {
  identifier: string;
  title: string;
  author: string;
  language: string;
  chapters: EpubChapter[];
  toc: EpubTocNode[];
  images: EpubImage[];
  cover?: EpubImage;
}): Uint8Array {
  const page = (pageTitle: string, body: string) =>
    `<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="${esc(language)}" lang="${esc(language)}"><head><meta charset="utf-8"/><title>${esc(pageTitle)}</title><link rel="stylesheet" type="text/css" href="book.css"/></head><body>${body}</body></html>\n`;

  // A part that only groups chapters is a plain heading: linking it to its first chapter would
  // make the server label that chapter with the part's name.
  const navList = (nodes: EpubTocNode[]): string =>
    `<ol>${nodes
      .map((node) =>
        node.heading && node.children.length
          ? `<li><span>${esc(node.label)}</span>${navList(node.children)}</li>`
          : `<li><a href="${esc(node.href)}">${esc(node.label)}</a>${node.children.length ? navList(node.children) : ""}</li>`,
      )
      .join("")}</ol>`;
  const nav = `<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="${esc(language)}" lang="${esc(language)}"><head><meta charset="utf-8"/><title>${esc(title)}</title></head><body><nav epub:type="toc"><h1>Contents</h1>${navList(toc)}</nav></body></html>\n`;

  const opf =
    `<?xml version="1.0" encoding="utf-8"?>\n<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid" xml:lang="${esc(language)}">` +
    `<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="bookid">${esc(identifier)}</dc:identifier><dc:title>${esc(title)}</dc:title>` +
    (author ? `<dc:creator>${esc(author)}</dc:creator>` : "") +
    `<dc:language>${esc(language)}</dc:language><meta property="dcterms:modified">2000-01-01T00:00:00Z</meta></metadata>` +
    `<manifest><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/><item id="css" href="book.css" media-type="text/css"/>` +
    chapters.map((chapter, index) => `<item id="c${index + 1}" href="${esc(chapter.file)}" media-type="application/xhtml+xml"/>`).join("") +
    images.map((image, index) => `<item id="i${index + 1}" href="${esc(image.file)}" media-type="${esc(image.type)}"${image === cover ? ' properties="cover-image"' : ""}/>`).join("") +
    `</manifest><spine>${chapters.map((_, index) => `<itemref idref="c${index + 1}"/>`).join("")}</spine></package>\n`;

  const encoder = new TextEncoder();
  const text = (name: string, value: string): ZipEntry => ({ name, data: encoder.encode(value) });
  return zipStore([
    text("mimetype", "application/epub+zip"),
    text("META-INF/container.xml", `<?xml version="1.0" encoding="utf-8"?>\n<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>\n`),
    text("OEBPS/content.opf", opf),
    text("OEBPS/nav.xhtml", nav),
    text("OEBPS/book.css", BOOK_CSS),
    ...chapters.map((chapter) => text(`OEBPS/${chapter.file}`, page(chapter.title, chapter.body))),
    ...images.map((image) => ({ name: `OEBPS/${image.file}`, data: image.data })),
  ]);
}

/** Read a picked file's bytes. Older webviews (and the test environment) lack Blob.arrayBuffer. */
export function readBytes(file: Blob): Promise<Uint8Array> {
  if (typeof file.arrayBuffer === "function") return file.arrayBuffer().then((buffer) => new Uint8Array(buffer));
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(new Uint8Array(reader.result as ArrayBuffer));
    reader.onerror = () => reject(new BookImportError("This file could not be read."));
    reader.readAsArrayBuffer(file);
  });
}
