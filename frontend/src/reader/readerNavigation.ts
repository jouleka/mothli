import type { Atom } from "../api";
import { normalizeHref } from "./offset";

export type EpubTocItem = {
  label?: string | null;
  href?: string | null;
  subitems?: EpubTocItem[] | null;
};

export type ReaderTocItem = {
  id: string;
  href: string | null;
  atom: number | null;
  children: ReaderTocItem[];
};

export type ReaderSearchExcerpt = { pre: string; match: string; post: string };
export type ReaderSearchResult = {
  cfi: string;
  excerpt: ReaderSearchExcerpt;
  atom: number;
};

/** Preserve the EPUB's hierarchy while replacing its potentially spoiler-bearing labels later. */
export function mapReaderToc(
  items: EpubTocItem[],
  sectionIds: string[],
  sectionAtoms: number[],
  atoms?: Atom[],
): ReaderTocItem[] {
  const candidates = new Map<string, number[]>();
  // Atoms that carry their own anchor ("whole.xhtml#c5") are matched by it exactly. Matching by
  // file alone handed a file's atoms out in order, so a title-page entry in the same file took
  // chapter one's place and every later entry opened the chapter before its own.
  const anchored = new Map<string, number>();
  const anchoredFiles = new Set<string>();
  const withAnchor = (href: string) => {
    const fragment = href.split("#")[1];
    return fragment ? `${normalizeHref(href)}#${fragment}` : null;
  };
  atoms?.forEach((atom, index) => {
    const href = normalizeHref(atom.href);
    candidates.set(href, [...(candidates.get(href) ?? []), index]);
    const exact = withAnchor(atom.href);
    if (exact) {
      anchored.set(exact, index);
      anchoredFiles.add(href);
    }
  });
  const used = new Map<string, number>();
  const walk = (branch: EpubTocItem[], path: string): ReaderTocItem[] => branch.map((item, index) => {
    const id = `${path}-${index}`;
    const href = item.href ?? null;
    const clean = href ? normalizeHref(href) : "";
    const children = item.subitems ?? [];
    const section = clean
      ? sectionIds.findIndex((sectionId) => normalizeHref(sectionId) === clean)
      : -1;
    let atom = section < 0 ? null : (sectionAtoms[section] ?? null);
    const matches = candidates.get(clean) ?? [];
    const exact = href ? withAnchor(href) : null;
    if (exact && anchoredFiles.has(clean)) {
      // An anchor in a file whose chapters are found by anchors: that chapter, or none at all
      // (a title page, a caption) rather than a guess at a neighbour.
      atom = anchored.get(exact) ?? null;
    } else if (matches.length) {
      const cursor = Math.min(used.get(clean) ?? 0, matches.length - 1);
      atom = matches[cursor];
      if (children.length === 0) used.set(clean, cursor + 1);
    }
    return { id, href, atom, children: walk(children, id) };
  });
  return walk(items, "toc");
}

function firstDescendantAtom(item: ReaderTocItem): number | null {
  if (item.atom != null && item.atom >= 0) return item.atom;
  for (const child of item.children) {
    const atom = firstDescendantAtom(child);
    if (atom != null) return atom;
  }
  return null;
}

/** Use only server-released labels; future EPUB labels are intentionally never copied into the DOM. */
export function safeTocLabel(item: ReaderTocItem, atoms: Atom[]): string {
  const target = firstDescendantAtom(item);
  if (target == null) return "Section";
  const atom = atoms[target];
  if (!atom) return "Section";
  if (item.atom == null && atom.part_label) return atom.part_label;
  return atom.title || `Chapter ${atom.ordinal}`;
}

export function tocContainsAtom(item: ReaderTocItem, atom: number): boolean {
  return item.atom === atom || item.children.some((child) => tocContainsAtom(child, atom));
}

export function tocAtomForHref(items: ReaderTocItem[], href: string | null | undefined): number | null {
  if (!href) return null;
  for (const item of items) {
    if (item.href === href && item.atom != null) return item.atom;
    const child = tocAtomForHref(item.children, href);
    if (child != null) return child;
  }
  return null;
}
