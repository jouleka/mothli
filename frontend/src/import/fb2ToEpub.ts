/** FictionBook (FB2) to EPUB, in the browser. The server only ever takes in EPUB (its import path
 * is hardened for that one format), so other formats are converted here first and uploaded as an
 * ordinary EPUB, which the server still validates as untrusted.
 *
 * The output is built from escaped text only: nothing from the source file is copied through as
 * markup, so a hostile FB2 cannot smuggle scripts or styles into the book. Conversion is
 * deterministic, so the same FB2 always becomes the same EPUB and is recognised as the same book. */
import { BookImportError, buildEpub, esc, readBytes, safeId, safeLanguage, squash, type EpubImage } from "./epubWriter";
import { crc32 } from "./zipStore";

export class Fb2Error extends BookImportError {}

export function isFb2(file: { name: string; type: string }) {
  return /\.fb2$/i.test(file.name) || file.type === "application/x-fictionbook+xml";
}

type Chapter = { file: string; title: string; body: string };
type TocNode = { label: string; file: string; children: TocNode[] };
type Image = EpubImage;

const kids = (el: Element, name?: string) =>
  Array.from(el.children).filter((child) => !name || child.localName === name);
const kid = (el: Element | null | undefined, name: string) => (el ? kids(el, name)[0] : undefined);
const hrefOf = (el: Element) =>
  Array.from(el.attributes).find((attr) => attr.localName === "href")?.value ?? "";

/** FB2 files are often Windows-1251 or KOI8-R; honour the declared encoding. */
export function decodeFb2(bytes: Uint8Array): string {
  const head = new TextDecoder("ascii").decode(bytes.subarray(0, 200));
  const declared = /encoding\s*=\s*["']([\w.-]+)["']/i.exec(head)?.[1] ?? "utf-8";
  let text: string;
  try {
    text = new TextDecoder(declared).decode(bytes);
  } catch {
    text = new TextDecoder("utf-8").decode(bytes);
  }
  return text.replace(/^﻿/, "");
}

const INLINE: Record<string, string> = {
  emphasis: "em",
  strong: "strong",
  strikethrough: "s",
  sub: "sub",
  sup: "sup",
  code: "code",
  style: "span",
};
const BLOCK: Record<string, [tag: string, cls?: string]> = {
  p: ["p"],
  v: ["p", "verse"],
  subtitle: ["h3"],
  "text-author": ["p", "author"],
  date: ["p", "date"],
  epigraph: ["blockquote", "epigraph"],
  cite: ["blockquote"],
  poem: ["blockquote", "poem"],
  stanza: ["div", "stanza"],
  annotation: ["div", "annotation"],
  table: ["table"],
  tr: ["tr"],
  td: ["td"],
  th: ["th"],
};

function base64Bytes(text: string): Uint8Array {
  const binary = atob(text.replace(/\s+/g, ""));
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

const IMAGE_EXT: Record<string, string> = { "image/jpeg": "jpg", "image/jpg": "jpg", "image/png": "png", "image/gif": "gif" };

export function convertFb2(bytes: Uint8Array, fallbackTitle = "Untitled"): { epub: Uint8Array; title: string; author: string } {
  const doc = new DOMParser().parseFromString(decodeFb2(bytes), "application/xml");
  const root = doc.documentElement;
  if (!root || root.localName !== "FictionBook" || doc.getElementsByTagName("parsererror").length)
    throw new Fb2Error("This file is not a readable FB2 book.");

  const info = kid(kid(root, "description"), "title-info");
  const title = squash(kid(info, "book-title")?.textContent) || fallbackTitle;
  const authorEl = kid(info, "author");
  const author = authorEl
    ? ["first-name", "middle-name", "last-name"].map((part) => squash(kid(authorEl, part)?.textContent)).filter(Boolean).join(" ") ||
      squash(kid(authorEl, "nickname")?.textContent)
    : "";
  const language = safeLanguage(kid(info, "lang")?.textContent);

  // Embedded pictures. Only real raster images are carried over.
  const images = new Map<string, Image>();
  for (const binary of kids(root, "binary")) {
    const id = binary.getAttribute("id");
    const type = (binary.getAttribute("content-type") ?? "").toLowerCase();
    const ext = IMAGE_EXT[type];
    if (!id || !ext || images.has(id)) continue;
    try {
      images.set(id, { file: `images/img${String(images.size + 1).padStart(4, "0")}.${ext}`, type: type === "image/jpg" ? "image/jpeg" : type, data: base64Bytes(binary.textContent ?? "") });
    } catch {
      /* a broken picture is dropped; the text still imports */
    }
  }
  const coverId = hrefOf(kid(kid(info, "coverpage"), "image") ?? root).replace(/^#/, "");
  const cover = images.get(coverId);

  const bodies = kids(root, "body");
  if (!bodies.length) throw new Fb2Error("This FB2 file has no text in it.");
  const main = bodies[0];
  const noteBodies = bodies.slice(1);

  const hasProse = (section: Element) =>
    Array.from(section.children).some((child) => !["title", "section", "epigraph", "image", "annotation"].includes(child.localName));
  const titleOf = (section: Element) =>
    kids(kid(section, "title") ?? section, "p").filter((p) => p.parentElement?.localName === "title").map((p) => squash(p.textContent)).filter(Boolean).join(" ");

  // First pass: decide which sections are chapters (their own file) and which only group chapters.
  type Planned = { section: Element; file: string; title: string };
  const planned: Planned[] = [];
  const idFile = new Map<string, string>();
  const claim = (el: Element, file: string) => {
    for (const node of [el, ...Array.from(el.querySelectorAll("[id]"))]) {
      const id = node.getAttribute("id");
      if (id && !idFile.has(id)) idFile.set(id, file);
    }
  };
  const plan = (section: Element): TocNode | null => {
    const subsections = kids(section, "section");
    const label = titleOf(section);
    if (subsections.length && !hasProse(section)) {
      const children = subsections.map(plan).filter((node): node is TocNode => node !== null);
      if (!children.length) return null;
      return { label: label || children[0].label, file: children[0].file, children };
    }
    const file = `ch${String(planned.length + 1).padStart(4, "0")}.xhtml`;
    const chapterTitle = label || `Chapter ${planned.length + 1}`;
    planned.push({ section, file, title: chapterTitle });
    claim(section, file);
    return { label: chapterTitle, file, children: [] };
  };
  const topSections = kids(main, "section");
  let toc: TocNode[];
  if (topSections.length) toc = topSections.map(plan).filter((node): node is TocNode => node !== null);
  else {
    // A body with no sections at all: the whole body is one chapter.
    planned.push({ section: main, file: "ch0001.xhtml", title });
    claim(main, "ch0001.xhtml");
    toc = [{ label: title, file: "ch0001.xhtml", children: [] }];
  }
  if (!planned.length) throw new Fb2Error("This FB2 file has no chapters to read.");
  const NOTES = "notes.xhtml";
  const hasNotes = noteBodies.some((body) => squash(body.textContent));
  if (hasNotes) for (const body of noteBodies) claim(body, NOTES);

  // Second pass: render. Everything goes through esc(); attributes are rebuilt, never copied.
  const render = (node: Node, file: string, depth: number): string => {
    if (node.nodeType === 3) return esc(node.nodeValue ?? "");
    if (node.nodeType !== 1) return "";
    const el = node as Element;
    const name = el.localName;
    const inner = () => Array.from(el.childNodes).map((child) => render(child, file, depth)).join("");
    const rawId = el.getAttribute("id");
    const id = rawId ? ` id="${esc(safeId(rawId))}"` : "";
    if (name === "image") {
      const image = images.get(hrefOf(el).replace(/^#/, ""));
      if (!image) return "";
      const img = `<img src="${esc(image.file)}" alt=""/>`;
      return el.parentElement && INLINE[el.parentElement.localName] !== undefined || ["p", "v", "subtitle", "td", "th"].includes(el.parentElement?.localName ?? "")
        ? img
        : `<p class="picture">${img}</p>`;
    }
    if (name === "empty-line") return '<div class="gap"></div>';
    if (name === "a") {
      const href = hrefOf(el);
      let target = "";
      if (href.startsWith("#")) {
        const targetFile = idFile.get(href.slice(1));
        if (targetFile) target = `${targetFile === file ? "" : targetFile}#${safeId(href.slice(1))}`;
      } else if (/^https?:\/\//i.test(href)) target = href;
      return target ? `<a href="${esc(target)}">${inner()}</a>` : inner();
    }
    if (INLINE[name]) return `<${INLINE[name]}>${inner()}</${INLINE[name]}>`;
    if (name === "title") {
      const level = Math.min(6, depth + 1);
      const text = kids(el, "p").map((p) => Array.from(p.childNodes).map((child) => render(child, file, depth)).join("")).filter(Boolean).join("<br/>");
      return text ? `<h${level}>${text}</h${level}>` : "";
    }
    if (name === "section") {
      const nested = Array.from(el.childNodes).map((child) => render(child, file, depth + 1)).join("");
      return `<section${id}>${nested}</section>`;
    }
    const block = BLOCK[name];
    if (block) {
      const [tag, cls] = block;
      return `<${tag}${id}${cls ? ` class="${cls}"` : ""}>${inner()}</${tag}>`;
    }
    return inner(); // unknown elements keep their text, lose their markup
  };


  const chapters: Chapter[] = planned.map(({ section, file, title: chapterTitle }) => {
    const hasTitle = !!kid(section, "title") && !!titleOf(section);
    const content = Array.from(section.childNodes).map((child) => render(child, file, 1)).join("");
    return { file, title: chapterTitle, body: `${hasTitle ? "" : `<h1>${esc(chapterTitle)}</h1>`}${content}` };
  });
  if (hasNotes) {
    const body = noteBodies
      .map((notes) => Array.from(notes.childNodes).map((child) => render(child, NOTES, 2)).join(""))
      .join("");
    chapters.push({ file: NOTES, title: "Notes", body: `<h1>Notes</h1>${body}` });
    toc.push({ label: "Notes", file: NOTES, children: [] });
  }

  type Toc = { label: string; href: string; children: Toc[]; heading: boolean };
  const asToc = (nodes: TocNode[]): Toc[] =>
    nodes.map((node) => ({ label: node.label, href: node.file, children: asToc(node.children), heading: node.children.length > 0 }));
  const epub = buildEpub({
    identifier: `urn:mothli:fb2:${crc32(bytes).toString(16).padStart(8, "0")}-${bytes.length.toString(16)}`,
    title,
    author,
    language,
    chapters,
    toc: asToc(toc),
    images: [...images.values()],
    cover,
  });
  return { epub, title, author };
}

/** Convert a picked FB2 file into an EPUB file ready for the ordinary import. */
export async function fb2ToEpub(file: File): Promise<File> {
  const bytes = await readBytes(file);
  const baseName = file.name.replace(/\.fb2$/i, "");
  const { epub } = convertFb2(bytes, baseName || "Untitled");
  return new File([epub as BlobPart], `${baseName || "book"}.epub`, { type: "application/epub+zip" });
}
