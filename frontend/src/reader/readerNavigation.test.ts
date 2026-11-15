import { describe, expect, test } from "vitest";
import { mapReaderToc, safeTocLabel, tocContainsAtom } from "./readerNavigation";

const atoms = [
  { ordinal: 1, href: "part/ch1.xhtml", title: "The Arrival", part_label: "Part I", char_len: 10 },
  { ordinal: 2, href: "part/ch2.xhtml", title: "", part_label: "", char_len: 10 },
];

describe("reader navigation model", () => {
  test("preserves nested EPUB hierarchy and maps percent/query variants to spine atoms", () => {
    const toc = mapReaderToc([
      { label: "raw part title", subitems: [
        { label: "raw chapter", href: "part/ch1.xhtml?edition=1" },
        { label: "future spoiler", href: "part/ch2.xhtml" },
      ] },
    ], ["part/ch1.xhtml", "part/ch2.xhtml"], [0, 1], atoms);
    expect(toc[0].children.map((item) => item.atom)).toEqual([0, 1]);
    expect(tocContainsAtom(toc[0], 1)).toBe(true);
  });

  test("never exposes raw future labels and uses released manifest labels", () => {
    const toc = mapReaderToc([
      { label: "raw part title", subitems: [
        { label: "raw chapter", href: "part/ch1.xhtml" },
        { label: "the murderer revealed", href: "part/ch2.xhtml" },
      ] },
    ], ["part/ch1.xhtml", "part/ch2.xhtml"], [0, 1], atoms);
    expect(safeTocLabel(toc[0], atoms)).toBe("Part I");
    expect(safeTocLabel(toc[0].children[0], atoms)).toBe("The Arrival");
    expect(safeTocLabel(toc[0].children[1], atoms)).toBe("Chapter 2");
    expect(JSON.stringify(toc)).not.toContain("murderer");
  });

  test("maps multiple anchor TOC leaves in one spine file to distinct atoms", () => {
    const anchoredAtoms = [
      { ...atoms[0], href: "whole.xhtml" },
      { ...atoms[1], href: "whole.xhtml" },
    ];
    const toc = mapReaderToc([
      { href: "whole.xhtml#one" },
      { href: "whole.xhtml#two" },
    ], ["whole.xhtml"], [0], anchoredAtoms);
    expect(toc.map((item) => item.atom)).toEqual([0, 1]);
  });

  test("an entry for front matter in the chapters' file does not shift the chapters after it", () => {
    // The illustrated Pride and Prejudice: a title-page entry, then every chapter, all in one file,
    // each atom carrying its own anchor. "Chapter 5" must open chapter 5, not chapter 3.
    const anchored = [1, 2, 3].map((n) => ({ ...atoms[0], ordinal: n, title: "", href: `whole.xhtml#c${n}` }));
    const toc = mapReaderToc([
      { label: "PRIDE. and PREJUDICE", href: "whole.xhtml#title" },
      { label: "Chapter I.", href: "whole.xhtml#c1" },
      { label: "A caption. CHAPTER II.", href: "whole.xhtml#c2" },
      { label: "CHAPTER III.", href: "whole.xhtml#c3" },
    ], ["whole.xhtml"], [0], anchored);
    expect(toc.map((item) => item.atom)).toEqual([null, 0, 1, 2]);
    expect(toc.map((item) => safeTocLabel(item, anchored))).toEqual(["Section", "Chapter 1", "Chapter 2", "Chapter 3"]);
  });
});
