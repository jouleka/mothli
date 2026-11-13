/** Kindle books (MOBI and AZW3/KF8) to EPUB, in the browser. The bundled reader engine already
 * knows how to unpack these files; this takes what it unpacks and rewrites it as a plain EPUB for
 * the server's ordinary import.
 *
 * Like the FB2 converter, nothing is copied through as markup. Each page is rebuilt from an
 * allowlist of ordinary text elements, so scripts, styles and embedded objects in a hostile file
 * never reach the book. Copy-protected (DRM) files are refused before anything is unpacked: this
 * never attempts to open them. */
import {
  BookImportError,
  buildEpub,
  esc,
  readBytes,
  safeId,
  safeLanguage,
  sniffImage,
  squash,
  type EpubChapter,
  type EpubImage,
  type EpubTocNode,
} from "./epubWriter";
import { crc32 } from "./zipStore";

export const PROTECTED_MESSAGE =
  "This book is copy-protected (DRM), so Mothli can’t open it. Books bought from Kindle usually are. Add a book without copy protection.";
export const UNREADABLE_KINDLE_MESSAGE =
  "This Kindle file is in a format Mothli can’t read. Add an EPUB, FB2, MOBI or AZW3 book.";

/** What kind of Kindle file this is, from its first bytes. `protected` means DRM. */
export function kindleKind(bytes: Uint8Array): "mobi" | "protected" | "unknown" {
  if (bytes.length < 96) return "unknown";
  const magic = String.fromCharCode(...bytes.subarray(60, 68));
  if (magic !== "BOOKMOBI") return "unknown";
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const record0 = view.getUint32(78, false);
  if (record0 + 14 > bytes.length) return "unknown";
  // PalmDOC header, bytes 12-13: 0 = none, 1 and 2 = Mobipocket/Kindle DRM.
  return view.getUint16(record0 + 12, false) === 0 ? "mobi" : "protected";
}

type TocItem = { label?: string; href?: string; subitems?: TocItem[] };
type Resolved = { index: number; anchor?: (doc: Document) => Element | null } | undefined;
/** The part of the reader engine's book object that conversion relies on. */
export type KindleBook = {
  sections: { createDocument: () => Promise<Document> | Document }[];
  toc?: TocItem[];
  metadata?: { title?: string; author?: string[] | string; language?: string[] | string };
  resolveHref?: (href: string) => Promise<Resolved> | Resolved;
  getCover?: () => Promise<Blob | null | undefined> | Blob | null | undefined;
};

const INLINE = new Map(
  Object.entries({ em: "em", i: "em", strong: "strong", b: "strong", u: "u", s: "s", strike: "s", del: "s", sub: "sub", sup: "sup", code: "code", small: "small", span: "span", font: "span", big: "span", cite: "em" }),
);
const BLOCK = new Set(["p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "ul", "ol", "li", "table", "thead", "tbody", "tr", "td", "th", "pre", "section", "article", "aside", "header", "footer", "figure", "figcaption", "dl", "dt", "dd"]);
const DROPPED = new Set(["script", "style", "head", "title", "meta", "link", "svg", "math", "object", "embed", "iframe", "form", "input", "button", "select", "textarea", "video", "audio", "source", "guide", "reference", "template", "noscript"]);
const fileOf = (index: number) => `sec${String(index + 1).padStart(4, "0")}.xhtml`;

export async function convertKindleBook(
  book: KindleBook,
  loadImage: (resourceIndex: number) => Promise<Uint8Array | null>,
  { identifier, fallbackTitle = "Untitled" }: { identifier: string; fallbackTitle?: string },
): Promise<{ epub: Uint8Array; title: string; author: string }> {
  const docs: Document[] = [];
  for (const section of book.sections) docs.push(await section.createDocument());
  if (!docs.length) throw new BookImportError("This book has no text in it.");

  const title = squash(book.metadata?.title) || fallbackTitle;
  const authors = book.metadata?.author;
  const author = squash(Array.isArray(authors) ? authors.join(", ") : authors);
  const languages = book.metadata?.language;
  const language = safeLanguage(Array.isArray(languages) ? languages[0] : languages);

  // Where a link or a contents entry lands: a page file, and an id on that page when one exists.
  let minted = 0;
  const resolve = async (href: string): Promise<string | null> => {
    let resolved: Resolved;
    try {
      resolved = await book.resolveHref?.(href);
    } catch {
      return null;
    }
    if (!resolved || resolved.index < 0 || resolved.index >= docs.length) return null;
    const target = resolved.anchor?.(docs[resolved.index]) ?? null;
    if (!target) return fileOf(resolved.index);
    if (!target.getAttribute("id")) target.setAttribute("id", `mothli-a${(minted += 1)}`);
    return `${fileOf(resolved.index)}#${safeId(target.getAttribute("id")!)}`;
  };

  // First pass: resolve every internal link and collect every picture, before any page is rendered.
  const linkTargets = new Map<Element, string>();
  const images = new Map<number, EpubImage | null>();
  const imageOf = new Map<Element, EpubImage>();
  const picture = async (resourceIndex: number) => {
    if (!Number.isInteger(resourceIndex) || resourceIndex < 0) return null;
    if (images.has(resourceIndex)) return images.get(resourceIndex) ?? null;
    let found: EpubImage | null = null;
    try {
      const data = await loadImage(resourceIndex);
      const kind = data && sniffImage(data);
      if (data && kind) found = { file: `images/img${String(images.size + 1).padStart(4, "0")}.${kind.ext}`, type: kind.type, data };
    } catch {
      found = null; // a broken picture is dropped; the text still imports
    }
    images.set(resourceIndex, found);
    return found;
  };
  for (const doc of docs) {
    for (const link of Array.from(doc.querySelectorAll("a"))) {
      const filepos = link.getAttribute("filepos");
      const href = link.getAttribute("href") ?? "";
      if (filepos) {
        const target = await resolve(`filepos:${filepos}`);
        if (target) linkTargets.set(link, target);
      } else if (/^kindle:pos:/i.test(href)) {
        const target = await resolve(href);
        if (target) linkTargets.set(link, target);
      } else if (/^https?:\/\//i.test(href)) linkTargets.set(link, href);
    }
    for (const img of Array.from(doc.querySelectorAll("img"))) {
      const recindex = img.getAttribute("recindex");
      const embed = /^kindle:embed:(\w+)/i.exec(img.getAttribute("src") ?? "")?.[1];
      const index = recindex ? Number(recindex) - 1 : embed ? parseInt(embed, 32) - 1 : -1;
      const found = await picture(index);
      if (found) imageOf.set(img, found);
    }
  }

  // The contents, resolved the same way.
  const tocNodes = async (items: TocItem[]): Promise<EpubTocNode[]> => {
    const nodes: EpubTocNode[] = [];
    for (const item of items) {
      const children = await tocNodes(item.subitems ?? []);
      const href = item.href ? await resolve(item.href) : null;
      const label = squash(item.label);
      if (href && label) nodes.push({ label, href, children });
      else nodes.push(...children);
    }
    return nodes;
  };
  let toc = await tocNodes(book.toc ?? []);

  const render = (node: Node, file: string): string => {
    if (node.nodeType === 3) return esc(node.nodeValue ?? "");
    if (node.nodeType !== 1) return "";
    const el = node as Element;
    const name = el.localName.toLowerCase();
    if (DROPPED.has(name)) return "";
    const inner = () => Array.from(el.childNodes).map((child) => render(child, file)).join("");
    const rawId = el.getAttribute("id");
    const id = rawId ? ` id="${esc(safeId(rawId))}"` : "";
    if (name === "br") return "<br/>";
    if (name === "hr") return "<hr/>";
    if (name === "img") {
      const image = imageOf.get(el);
      return image ? `<img${id} src="${esc(image.file)}" alt="${esc(squash(el.getAttribute("alt")))}"/>` : "";
    }
    if (name === "a") {
      const target = linkTargets.get(el);
      const local = target?.startsWith(`${file}#`) ? target.slice(file.length) : target;
      if (local) return `<a${id} href="${esc(local)}">${inner()}</a>`;
      return id ? `<span${id}>${inner()}</span>` : inner();
    }
    if (name === "center") return `<div${id} class="center">${inner()}</div>`;
    const inline = INLINE.get(name);
    if (inline) return `<${inline}${id}>${inner()}</${inline}>`;
    if (BLOCK.has(name)) {
      const align = (el.getAttribute("align") ?? "").toLowerCase();
      const cls = align === "center" || align === "right" ? ` class="${align}"` : "";
      return `<${name}${id}${cls}>${inner()}</${name}>`;
    }
    return id ? `<span${id}>${inner()}</span>` : inner(); // unknown elements keep their text only
  };

  const chapters: EpubChapter[] = [];
  const kept = new Set<string>();
  docs.forEach((doc, index) => {
    const file = fileOf(index);
    const root = doc.body ?? doc.documentElement;
    const body = root ? Array.from(root.childNodes).map((child) => render(child, file)).join("") : "";
    if (!squash(root?.textContent) && !body.includes("<img")) return; // an empty page between sections
    // Older MOBI files style a paragraph as the heading; a short opening line serves as the name.
    const firstLine = squash(Array.from(root?.querySelectorAll("p, div") ?? []).map((el) => squash(el.textContent)).find(Boolean));
    const heading = squash(root?.querySelector("h1, h2, h3")?.textContent) || (firstLine.length <= 60 ? firstLine : "");
    chapters.push({ file, title: heading || `Section ${chapters.length + 1}`, body });
    kept.add(file);
  });
  if (!chapters.length) throw new BookImportError("This book has no text in it.");

  // Contents entries that point at pages which turned out empty are dropped; with no usable
  // contents at all, every page becomes an entry so the server still sees chapters.
  const prune = (nodes: EpubTocNode[]): EpubTocNode[] =>
    nodes.flatMap((node) => (kept.has(node.href.split("#")[0]) ? [{ ...node, children: prune(node.children) }] : prune(node.children)));
  toc = prune(toc);
  if (!toc.length) toc = chapters.map((chapter) => ({ label: chapter.title, href: chapter.file, children: [] }));

  let cover: EpubImage | undefined;
  try {
    const blob = await book.getCover?.();
    const data = blob ? await readBytes(blob) : null;
    const kind = data && sniffImage(data);
    if (data && kind) cover = { file: `images/cover.${kind.ext}`, type: kind.type, data };
  } catch {
    cover = undefined;
  }
  const allImages = [...images.values()].filter((image): image is EpubImage => image !== null);
  if (cover) allImages.push(cover);

  const epub = buildEpub({ identifier, title, author, language, chapters, toc, images: allImages, cover });
  return { epub, title, author };
}

/** Convert a picked MOBI or AZW3 file into an EPUB file ready for the ordinary import. */
export async function mobiToEpub(file: File): Promise<File> {
  const bytes = await readBytes(file);
  const kind = kindleKind(bytes);
  if (kind === "protected") throw new BookImportError(PROTECTED_MESSAGE);
  if (kind !== "mobi") throw new BookImportError(UNREADABLE_KINDLE_MESSAGE);
  const [{ MOBI }, fflate] = await Promise.all([
    import("../vendor/foliate-js/mobi.js"),
    import("../vendor/foliate-js/vendor/fflate.js"),
  ]);
  // The engine reads through Blob.slice().arrayBuffer(); hand it the bytes we already hold.
  const source = {
    slice: (start?: number, end?: number) => ({
      arrayBuffer: async () => bytes.slice(start, end).buffer as ArrayBuffer,
    }),
  };
  let book: KindleBook & { mobi?: { loadResource: (index: number) => Promise<ArrayBuffer> } };
  let mobi: { loadResource: (index: number) => Promise<ArrayBuffer> };
  try {
    const opened = new MOBI({ unzlib: fflate.unzlibSync });
    mobi = opened as unknown as typeof mobi;
    book = (await opened.open(source as unknown as File)) as unknown as typeof book;
  } catch {
    throw new BookImportError("This book couldn’t be opened. The file may be damaged.");
  }
  const baseName = file.name.replace(/\.(mobi|azw3?|prc)$/i, "");
  const { epub } = await convertKindleBook(
    book,
    async (index) => new Uint8Array(await mobi.loadResource(index)),
    {
      identifier: `urn:mothli:kindle:${crc32(bytes).toString(16).padStart(8, "0")}-${bytes.length.toString(16)}`,
      fallbackTitle: baseName || "Untitled",
    },
  );
  return new File([epub as BlobPart], `${baseName || "book"}.epub`, { type: "application/epub+zip" });
}
