/** Exact body-text anchors; page fractions never authorize conversation source text. */
import type { Atom, ReadAnchor, ReadFrontier } from "../api";
import { normalizeHref } from "./offset";
import { compare } from "../vendor/foliate-js/epubcfi.js";

const normalize = (text: string) => text.replace(/\s+/gu, " ").trim();

export function atomAtRange(atoms: Atom[], sectionHref: string, range: Range, edge: "start" | "end"): number | null {
  const candidates = atoms.map((atom, index) => ({atom, index})).filter(
    ({atom}) => normalizeHref(atom.href) === normalizeHref(sectionHref));
  if (candidates.length === 1) return candidates[0].index;
  const boundary = range.cloneRange();
  boundary.collapse(edge === "start");
  const doc = boundary.startContainer.ownerDocument;
  if (!doc?.body || !doc.body.contains(boundary.startContainer)) return null;
  let selected: number | null = null;
  for (const {atom, index} of candidates) {
    const fragment = atom.href.includes("#") ? atom.href.slice(atom.href.indexOf("#") + 1) : "";
    let element: Element = doc.body;
    if (fragment) {
      let id: string;
      try { id = decodeURIComponent(fragment); } catch { return null; }
      const matches = [...doc.querySelectorAll("[id],a[name]")].filter(
        (node) => node.id === id || node.getAttribute("name") === id);
      if (matches.length !== 1) return null;
      element = matches[0];
    }
    if (boundary.comparePoint(element, 0) <= 0) selected = index;
  }
  return selected;
}

export function pageAnchor(range: Range, atom: number, cfi: string, epoch: number,
                           edge: "start" | "end" = "start"): ReadAnchor | null {
  const boundary = range.cloneRange();
  boundary.collapse(edge === "start");
  const doc = boundary.startContainer.ownerDocument;
  if (!doc?.body || !doc.body.contains(boundary.startContainer)) return null;
  let node: Node | null = boundary.startContainer;
  let offset = boundary.startOffset;
  const eligible = (value: Node) => value.nodeType === 3 && !!value.textContent?.trim()
    && !value.parentElement?.closest("script,style,noscript");
  if (!eligible(node)) {
    const walker = doc.createTreeWalker(doc.body, 4);
    let previous: Node | null = null;
    node = null;
    for (let next = walker.nextNode(); next; next = walker.nextNode()) {
      if (!eligible(next)) continue;
      if (boundary.comparePoint(next, 0) >= 0) { node = next; offset = 0; break; }
      previous = next;
    }
    if (!node && previous) { node = previous; offset = previous.textContent?.length ?? 0; }
  }
  if (!node) return null;
  const text = node.textContent ?? "";
  // DOM offsets are UTF-16; normalize only the quote and count its prefix on the server.
  if (offset > 0 && /[\uD800-\uDBFF]/u.test(text[offset - 1]) && /[\uDC00-\uDFFF]/u.test(text[offset] ?? "")) offset--;
  const before = Array.from(text.slice(0, offset)).slice(-144).join("");
  const after = Array.from(text.slice(offset)).slice(0, 144).join("");
  const quote = normalize(before + after);
  const prefix = normalize(before);
  if (!quote || !quote.startsWith(prefix)) return null;
  return {atom:atom + 1, quote, prefix, cfi, position_epoch:epoch};
}

/** Serialize writes and retain the furthest queued CFI across fast forward/backward turns. */
export class ReadFrontierReporter {
  private pending: ReadAnchor | null = null;
  private active: Promise<ReadFrontier | null> | null = null;
  private timer: ReturnType<typeof setTimeout> | undefined;
  private closed = false;
  constructor(private put: (anchor: ReadAnchor) => Promise<ReadFrontier | null>) {}
  schedule(anchor: ReadAnchor) {
    if (this.closed) return;
    if (!this.pending || this.pending.position_epoch !== anchor.position_epoch
        || compare(anchor.cfi, this.pending.cfi) >= 0) this.pending = anchor;
    clearTimeout(this.timer);
    this.timer = setTimeout(() => { void this.flush().catch(() => {}); }, 350);
  }
  async flush(): Promise<ReadFrontier | null> {
    clearTimeout(this.timer);
    if (this.active) await this.active;
    if (!this.pending || this.closed) return null;
    const candidate = this.pending;
    this.pending = null;
    this.active = this.put(candidate);
    try { return await this.active; }
    catch (error) { if (!this.pending) this.pending = candidate; throw error; }
    finally { this.active = null; }
  }
  dispose() { this.closed = true; clearTimeout(this.timer); this.pending = null; }
}
