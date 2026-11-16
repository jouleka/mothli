import { describe, expect, it } from "vitest";
import type { Atom } from "../api";
import { chapterCount, chapterLine, chapterNumber, chaptersFinished, finishedNoteFor, shelfJourney } from "./chapterPlace";

const atom = (ordinal: number, title: string, content_number: number | null | undefined): Atom => ({
  ordinal, href: `f${ordinal}.xhtml`, title, part_label: "", char_len: 1000,
  ...(content_number === undefined ? {} : { content_number }),
});

// Alice's Adventures in Wonderland as the server describes it (audit 6am): the cover, Project
// Gutenberg's own page, then twelve chapters numbered 1 to 12.
const alice: Atom[] = [
  atom(1, "(untitled)", null),
  atom(2, "The Project Gutenberg eBook of Alice's Adventures in Wonderland", null),
  ...Array.from({ length: 12 }, (_, i) => atom(i + 3, i ? "" : "CHAPTER I. Down the Rabbit-Hole", i + 1)),
];
// Pride and Prejudice: every piece is a chapter, so a piece's number is its chapter number.
const pride: Atom[] = Array.from({ length: 61 }, (_, i) => atom(i + 1, `Chapter ${i + 1}`, i + 1));
// A manifest from a server that did not number chapters yet.
const older: Atom[] = alice.map((item) => atom(item.ordinal, item.title, undefined));

describe("one chapter number, the book's own", () => {
  // The break: the footer and the shelf counted every piece, "Chapter 3 of 14" on Chapter I.
  it("calls Chapter I of Alice chapter 1 of 12", () => {
    expect(chapterNumber(alice, 2)).toBe(1);
    expect(chapterCount(alice)).toBe(12);
    expect(chapterLine(alice, 2, 2, "chapter")).toBe("Chapter 1 of 12");
  });

  // The break: a chapter number shown for the cover and the Gutenberg page, which are in none.
  it("shows no chapter number on the cover or the Gutenberg page", () => {
    expect(chapterNumber(alice, 0)).toBeNull();
    expect(chapterNumber(alice, 1)).toBeNull();
    expect(chapterLine(alice, 0, 0, "chapter")).toBe("Ready to begin");
    expect(chapterLine(alice, 1, 2, "chapter")).toBe("Ready to begin");
    // Back on the title page after reading: no number, and not "Ready to begin" either.
    expect(chapterLine(alice, 1, 5, "chapter")).toBe("");
  });

  // The break: the progress bar and the shelf counting the cover and Gutenberg's page as finished.
  it("counts only chapters as finished", () => {
    expect(chaptersFinished(alice, 2)).toBe(0);
    expect(chaptersFinished(alice, 3)).toBe(1);
    expect(chaptersFinished(alice, 14)).toBe(12);
  });

  it("says what it said before for Pride and Prejudice and for an older server", () => {
    expect(chapterLine(pride, 2, 2, "chapter")).toBe("Chapter 3 of 61");
    expect(chapterCount(pride)).toBe(61);
    expect(chaptersFinished(pride, 7)).toBe(7);
    expect(chapterLine(older, 2, 2, "chapter")).toBe("Chapter 3 of 14");
    expect(chapterLine(older, 40, 2, "chapter")).toBe("Chapter 14 of 14");
    expect(chapterCount(older)).toBe(14);
  });

  // The break: the shelf card saying "Chapter 3 of 14" (or "In chapter 3 of 14") on Chapter I.
  it("gives the shelf card the book's own numbers", () => {
    expect(shelfJourney(alice, 2, 3)).toEqual({ done: 0, total: 12, place: 1 });
    expect(shelfJourney(alice, 2, 0)).toEqual({ done: 0, total: 12, place: 0 });
    expect(shelfJourney(alice, 2, 2)).toEqual({ done: 0, total: 12, place: 0 });
    expect(shelfJourney(alice, 4, 5)).toEqual({ done: 2, total: 12, place: 3 });
    expect(shelfJourney(pride, 2, 3)).toEqual({ done: 2, total: 61, place: 3 });
    expect(shelfJourney(older, 2, 3)).toEqual({ done: 2, total: 14, place: 3 });
  });
});

describe("the chapter finished note", () => {
  // The break: "That's The Project Gutenberg eBook of Alice's Adventures in Wonderland done!"
  it("is never offered for a piece that is not a chapter", () => {
    expect(finishedNoteFor(alice, 0, 2)).toBeNull();
    expect(finishedNoteFor(alice, 1, 2)).toBeNull();
  });

  it("is offered for a chapter, by its own title or its chapter number", () => {
    expect(finishedNoteFor(alice, 2, 3)).toEqual({ chapter: 3, title: "CHAPTER I. Down the Rabbit-Hole" });
    expect(finishedNoteFor(alice, 3, 4)?.title).toBe("chapter 2");     // its title not yet given
    expect(finishedNoteFor(pride, 2, 3)).toEqual({ chapter: 3, title: "Chapter 3" });
  });

  it("is offered by an older server's manifest as before", () => {
    expect(finishedNoteFor(older, 1, 2)?.chapter).toBe(2);
  });
});
