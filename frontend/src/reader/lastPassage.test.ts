import { beforeEach, describe, expect, test } from "vitest";
import { forgetLastPassage, minutesLeftLabel, readLastPassage, saveLastPassage, sinceLabel, trimPassage } from "./lastPassage";

beforeEach(() => localStorage.clear());

describe("where you stopped", () => {
  test("keeps whole sentences, or whole words when a sentence runs long", () => {
    const sentences = "He really was late. They had waited for him. ".repeat(20);
    const kept = trimPassage(sentences, 100);
    expect(kept.length).toBeLessThanOrEqual(100);
    expect(kept.endsWith(".")).toBe(true);
    const run = trimPassage("word ".repeat(200), 52);
    expect(run.endsWith("…")).toBe(true);
    expect(run).not.toMatch(/wor…$/);
  });

  test("is remembered per reader and per book, and can be forgotten", () => {
    const text = "He really was late. They had waited for him and had already decided to bear the coffin.";
    saveLastPassage("ownerA", "kar", `  ${text}\n\n`, 1000);
    expect(readLastPassage("ownerA", "kar")).toEqual({ text, at: 1000 });
    expect(readLastPassage("ownerB", "kar")).toBeNull();
    expect(readLastPassage("ownerA", "other")).toBeNull();
    forgetLastPassage("ownerA", "kar");
    expect(readLastPassage("ownerA", "kar")).toBeNull();
  });

  test("a near-empty page (a title or an illustration) does not replace the passage", () => {
    saveLastPassage("o", "b", "A real paragraph of the story that the reader was in the middle of.", 1);
    saveLastPassage("o", "b", "PART II", 2);
    expect(readLastPassage("o", "b")?.at).toBe(1);
  });

  test("damaged storage reads as nothing rather than breaking the library", () => {
    localStorage.setItem("mothli.lastPassage:o:b", "{not json");
    expect(readLastPassage("o", "b")).toBeNull();
    localStorage.setItem("mothli.lastPassage:o:b", JSON.stringify({ text: 5 }));
    expect(readLastPassage("o", "b")).toBeNull();
  });
});

describe("plain-language time", () => {
  const day = 86_400_000;
  test("says how long ago the reader stopped", () => {
    expect(sinceLabel(0, day / 2)).toBe("today");
    expect(sinceLabel(0, day * 1.5)).toBe("yesterday");
    expect(sinceLabel(0, day * 3)).toBe("3 days ago");
    expect(sinceLabel(0, day * 21)).toBe("3 weeks ago");
    expect(sinceLabel(0, day * 90)).toBe("3 months ago");
  });

  test("says how much of the chapter is left", () => {
    expect(minutesLeftLabel(6.2)).toBe("About 6 minutes left in this chapter");
    expect(minutesLeftLabel(1)).toBe("About 1 minute left in this chapter");
    expect(minutesLeftLabel(0.3, "section")).toBe("Under a minute left in this section");
    expect(minutesLeftLabel(120)).toBe("About 2 hours left in this chapter");
    expect(minutesLeftLabel(null)).toBeNull();
    expect(minutesLeftLabel(Number.NaN)).toBeNull();
  });
});
