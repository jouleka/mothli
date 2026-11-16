/** One chapter number everywhere the reader sees one: the book's own, as Mothli counts them.
 *
 * The server numbers the chapters of the book proper on the manifest (``content_number``, from
 * reader_place.content_numbers). A piece in no chapter (a cover, Project Gutenberg's own page,
 * an index) has none, and no chapter number is shown for it. A manifest from a server that did
 * not number chapters counts every piece, as the app always did. */
import type { Atom } from "../api";

const numbered = (atoms: Atom[]) => atoms.some((atom) => atom.content_number !== undefined);

/** The chapter number of the piece at ``index`` (0-based), or null when it is in no chapter. */
export function chapterNumber(atoms: Atom[], index: number): number | null {
  if (!atoms.length || index < 0) return null;
  if (!numbered(atoms)) return Math.min(index + 1, atoms.length);
  return atoms[index]?.content_number ?? null;
}

export function chapterCount(atoms: Atom[]): number {
  if (!numbered(atoms)) return atoms.length;
  return atoms.reduce((most, atom) => Math.max(most, atom.content_number ?? 0), 0);
}

/** How many chapters are among the first ``bookmark`` pieces (the server's chapter counter). */
export function chaptersFinished(atoms: Atom[], bookmark: number): number {
  const through = Math.max(0, Math.min(bookmark, atoms.length));
  if (!numbered(atoms)) return through;
  return atoms.slice(0, through).filter((atom) => atom.content_number != null).length;
}

/** The reader's footer: "Chapter 1 of 12"; on a piece before the first chapter of a book not yet
 * begun, "Ready to begin"; on any other piece in no chapter, nothing. */
export function chapterLine(atoms: Atom[], index: number, bookmark: number, unit: string): string {
  const number = chapterNumber(atoms, index);
  const Unit = `${unit[0]?.toUpperCase() ?? ""}${unit.slice(1)}`;
  if (number != null) return `${Unit} ${number} of ${chapterCount(atoms)}`;
  const first = atoms.findIndex((atom) => atom.content_number != null);
  return chaptersFinished(atoms, bookmark) === 0 && (first < 0 || index < first) ? "Ready to begin" : "";
}

/** The shelf card's numbers: chapters finished, chapters in all, and the chapter the reader is in
 * (0 when none) from the server's counter and place (1-based piece, 0 for none). */
export function shelfJourney(atoms: Atom[], bookmark: number, placeAtom: number) {
  return {
    done: chaptersFinished(atoms, bookmark),
    total: chapterCount(atoms),
    place: placeAtom > 0 ? chapterNumber(atoms, Math.min(placeAtom, atoms.length) - 1) ?? 0 : 0,
  };
}

/** The "That's ... done!" note when the counter moves from ``previous`` to ``bookmark``: for the
 * piece at the counter, only when it is a chapter. Its title when the server has given it. */
export function finishedNoteFor(atoms: Atom[], previous: number | null, bookmark: number, unit = "chapter") {
  if (previous == null || bookmark <= previous || bookmark > atoms.length) return null;
  const piece = atoms[bookmark - 1];
  if (piece?.content_role && piece.content_role !== "content") return null;
  const number = chapterNumber(atoms, bookmark - 1);
  if (number == null) return null;
  return { chapter: bookmark, title: piece?.title?.trim() || `${unit} ${number}` };
}
