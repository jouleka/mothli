/** Tap a name in the text. The cast Mothli already knows (bookmark-clamped, so nobody past the
 * reader's frontier is in it) is matched against the visible book text. Matches are painted with
 * the CSS Custom Highlight API and hit-tested from the tap point, so the EPUB's DOM is never
 * mutated: CFIs, highlights and reading positions keep working exactly as before. */
import type { GraphNode } from "../api";

export const NAME_HIGHLIGHT = "mothli-names";

export type NameMatcher = {
  /** Longest names first, so "Katerina Ivanovna" wins over "Katerina". Null when nobody is known. */
  pattern: RegExp | null;
  /** Exact matched text to the one person it can mean. Ambiguous names are left out. */
  owner: Map<string, number>;
};

export type NameSpan = { start: number; end: number; entityId: number };
/** Per text node, the names found in it. Kept outside the DOM. */
export type NameIndex = Map<Text, NameSpan[]>;

const escapePattern = (value: string) => value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
const clean = (value: string) => value.replace(/\s+/g, " ").trim();

/** Names worth tapping: at least three characters and starting with a capital, which keeps
 * ordinary words that happen to be someone's alias ("the captain") from lighting up. */
function tappable(name: string) {
  return name.length >= 3 && /^\p{Lu}/u.test(name);
}

export function buildNameMatcher(people: GraphNode[]): NameMatcher {
  const owner = new Map<string, number>();
  const ambiguous = new Set<string>();
  for (const person of people) {
    if (person.type && person.type !== "character") continue;
    for (const raw of [person.canonical_name, ...(person.aliases ?? [])]) {
      const name = clean(raw ?? "");
      if (!tappable(name) || ambiguous.has(name)) continue;
      const known = owner.get(name);
      if (known != null && known !== person.entity_id) {
        owner.delete(name);
        ambiguous.add(name);
      } else owner.set(name, person.entity_id);
    }
  }
  if (!owner.size) return { pattern: null, owner };
  const names = [...owner.keys()].sort((a, b) => b.length - a.length);
  const body = names.map((name) => escapePattern(name).replace(/ /g, "\\s+")).join("|");
  return {
    pattern: new RegExp(`(?<![\\p{L}\\p{N}])(?:${body})(?![\\p{L}\\p{N}])`, "gu"),
    owner,
  };
}

export function findNames(text: string, matcher: NameMatcher): NameSpan[] {
  if (!matcher.pattern) return [];
  const spans: NameSpan[] = [];
  matcher.pattern.lastIndex = 0;
  for (const match of text.matchAll(matcher.pattern)) {
    const entityId = matcher.owner.get(clean(match[0]));
    if (entityId != null && match.index != null)
      spans.push({ start: match.index, end: match.index + match[0].length, entityId });
  }
  return spans;
}

type HighlightWindow = Window & {
  Highlight?: new (...ranges: Range[]) => unknown;
  CSS?: { highlights?: { set: (name: string, value: unknown) => void; delete: (name: string) => void } };
};

/** Find every known name in a loaded section and underline it. Returns the index used for taps.
 * Where the Highlight API is missing the names are simply not underlined; taps still work. */
export function markNames(doc: Document, matcher: NameMatcher): NameIndex {
  const index: NameIndex = new Map();
  const view = doc.defaultView as HighlightWindow | null;
  const registry = view?.CSS?.highlights;
  registry?.delete(NAME_HIGHLIGHT);
  if (!matcher.pattern || !doc.body) return index;
  const ranges: Range[] = [];
  const walker = doc.createTreeWalker(doc.body, 4 /* NodeFilter.SHOW_TEXT */);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const text = node as Text;
    const parent = text.parentElement;
    if (!parent || parent.closest("script, style, a[href], rt")) continue;
    const spans = findNames(text.data, matcher);
    if (!spans.length) continue;
    index.set(text, spans);
    for (const span of spans) {
      const range = doc.createRange();
      range.setStart(text, span.start);
      range.setEnd(text, span.end);
      ranges.push(range);
    }
  }
  if (registry && view?.Highlight && ranges.length)
    registry.set(NAME_HIGHLIGHT, new view.Highlight(...ranges));
  return index;
}

/** The name under a caret position, with the range that covers it. */
export function nameAt(index: NameIndex, node: Node | null, offset: number): { entityId: number; range: Range } | null {
  if (!node || node.nodeType !== 3) return null;
  const text = node as Text;
  const span = index.get(text)?.find((item) => offset >= item.start && offset <= item.end);
  if (!span) return null;
  const range = text.ownerDocument.createRange();
  range.setStart(text, span.start);
  range.setEnd(text, span.end);
  return { entityId: span.entityId, range };
}

type CaretDocument = Document & {
  caretPositionFromPoint?: (x: number, y: number) => { offsetNode: Node; offset: number } | null;
  caretRangeFromPoint?: (x: number, y: number) => Range | null;
};

/** The name under a tap, if the tap really landed on its letters (not just nearby). */
export function nameAtPoint(doc: Document, index: NameIndex, x: number, y: number) {
  const caretDoc = doc as CaretDocument;
  let node: Node | null = null;
  let offset = 0;
  const position = caretDoc.caretPositionFromPoint?.(x, y);
  if (position) {
    node = position.offsetNode;
    offset = position.offset;
  } else {
    const range = caretDoc.caretRangeFromPoint?.(x, y);
    if (range) {
      node = range.startContainer;
      offset = range.startOffset;
    }
  }
  const hit = nameAt(index, node, offset);
  if (!hit) return null;
  const inside = [...hit.range.getClientRects()].some(
    (rect) => x >= rect.left - 2 && x <= rect.right + 2 && y >= rect.top - 2 && y <= rect.bottom + 2,
  );
  return inside ? hit : null;
}

export type TapRect = { left: number; top: number; right: number; bottom: number };

/** A range's box in the top window's coordinates (the book lives in an iframe). */
export function rectInTopWindow(range: Range): TapRect {
  const rect = range.getBoundingClientRect();
  const frame = range.startContainer.ownerDocument?.defaultView?.frameElement?.getBoundingClientRect();
  const dx = frame?.left ?? 0;
  const dy = frame?.top ?? 0;
  return { left: rect.left + dx, top: rect.top + dy, right: rect.right + dx, bottom: rect.bottom + dy };
}
