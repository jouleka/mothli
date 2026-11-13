import { describe, expect, test } from "vitest";
import type { GraphNode } from "../api";
import { buildNameMatcher, findNames, markNames, nameAt } from "./nameTaps";

const person = (entity_id: number, canonical_name: string, aliases: string[] = [], type = "character"): GraphNode => ({
  entity_id,
  canonical_name,
  aliases,
  type,
  revealed_at: 1,
});

const cast = [
  person(1, "Alexey Karamazov", ["Alyosha", "Karamazov"]),
  person(2, "Dmitri Karamazov", ["Mitya", "Karamazov"]),
  person(3, "Katerina Ivanovna", ["Katerina", "Katya"]),
  person(4, "Captain Snegiryov", ["the captain", "Snegiryov"]),
  person(5, "Skotoprigonyevsk", [], "place"),
];

const found = (text: string) =>
  findNames(text, buildNameMatcher(cast)).map((span) => [text.slice(span.start, span.end), span.entityId]);

describe("finding known names in book text", () => {
  test("matches canonical names and aliases as whole words", () => {
    expect(found("“What is it, Kolya?” said Alyosha. Mitya was not there.")).toEqual([
      ["Alyosha", 1],
      ["Mitya", 2],
    ]);
    expect(found("Alyoshas and Mityaesque are not names.")).toEqual([]);
  });

  test("prefers the longest name so a full name is one tap target", () => {
    expect(found("Katerina Ivanovna sent flowers; Katerina wept.")).toEqual([
      ["Katerina Ivanovna", 3],
      ["Katerina", 3],
    ]);
  });

  test("a name shared by two people is left alone rather than guessed", () => {
    expect(found("“How glad I am you’ve come, Karamazov!”")).toEqual([]);
    expect(found("Dmitri Karamazov spoke.")).toEqual([["Dmitri Karamazov", 2]]);
  });

  test("ignores lowercase aliases, very short names and non-people", () => {
    expect(found("the captain lived in Skotoprigonyevsk")).toEqual([]);
    expect(findNames("Al and Bo", buildNameMatcher([person(9, "Al", ["Bo"])]))).toEqual([]);
  });

  test("a name broken across a line still matches", () => {
    expect(found("Katerina\n   Ivanovna")).toEqual([["Katerina\n   Ivanovna", 3]]);
  });

  test("nobody known means nothing is searched", () => {
    expect(buildNameMatcher([]).pattern).toBeNull();
    expect(findNames("Alyosha", buildNameMatcher([]))).toEqual([]);
  });
});

describe("marking a loaded page", () => {
  const page = () => {
    const doc = document.implementation.createHTMLDocument("page");
    doc.body.innerHTML =
      '<p>He met <em>Alyosha</em> and Mitya.</p><p><a href="#n1">Snegiryov</a></p><style>Alyosha{}</style>';
    return doc;
  };

  test("indexes names without changing the book's markup", () => {
    const doc = page();
    const before = doc.body.innerHTML;
    const index = markNames(doc, buildNameMatcher(cast));
    expect(doc.body.innerHTML).toBe(before);
    const names = [...index.entries()].flatMap(([node, spans]) =>
      spans.map((span) => node.data.slice(span.start, span.end)),
    );
    // links keep their own behaviour, and style text is never a name
    expect(names).toEqual(["Alyosha", "Mitya"]);
  });

  test("a tap inside a name resolves to that person; a tap beside it does not", () => {
    const doc = page();
    const index = markNames(doc, buildNameMatcher(cast));
    const tail = doc.querySelector("p")!.lastChild as Text; // " and Mitya."
    const start = tail.data.indexOf("Mitya");
    const hit = nameAt(index, tail, start + 2);
    expect(hit?.entityId).toBe(2);
    expect(hit?.range.toString()).toBe("Mitya");
    expect(nameAt(index, tail, 1)).toBeNull();
    expect(nameAt(index, doc.body, 0)).toBeNull();
  });
});
