import { describe, expect, test } from "vitest";
import { parseCsv, parseHistoryFile, summarize, UnknownHistoryFile } from "./parsers";

const GOODREADS = [
  "Book Id,Title,Author,Author l-f,Additional Authors,ISBN,ISBN13,My Rating,Average Rating,Publisher,Binding,Number of Pages,Year Published,Original Publication Year,Date Read,Date Added,Bookshelves,Bookshelves with positions,Exclusive Shelf,My Review,Spoiler,Private Notes,Read Count,Owned Copies",
  '1885,"Pride and Prejudice","Jane Austen","Austen, Jane",,"=""0141439513""","=""9780141439518""",5,4.28,Penguin,Paperback,480,2002,1813,2021/05/03,2021/04/01,,,read,"Still the best.<br/>Truly, ""the"" best.",,,2,0',
  '7144,"Crime and Punishment","Fyodor Dostoevsky","Dostoevsky, Fyodor",,"=""""","=""""",0,4.27,Penguin,Paperback,671,2002,1866,,2022/01/09,to-read,to-read (#3),to-read,,,,0,0',
  '99,"A Novel, With\nA Line Break","Some Author","Author, Some",,,,3,3.5,,,,,,,2023/02/02,,,currently-reading,,,,1,0',
  ',"","Nobody",,,,,,,,,,,,,,,,read,,,,,',
].join("\r\n");

describe("Goodreads library export", () => {
  test("reads shelves, ratings, dates, ISBNs and reviews", () => {
    const parsed = parseHistoryFile("goodreads_library_export.csv", `﻿${GOODREADS}`);
    expect(parsed.source).toBe("goodreads");
    expect(parsed.highlights).toEqual([]);
    expect(parsed.books).toEqual([
      {
        title: "Pride and Prejudice",
        author: "Jane Austen",
        isbn: "9780141439518",
        status: "read",
        rating: 5,
        finished_on: "2021-05-03",
        added_on: "2021-04-01",
        review: 'Still the best.\nTruly, "the" best.',
      },
      {
        title: "Crime and Punishment",
        author: "Fyodor Dostoevsky",
        isbn: null,
        status: "want",
        rating: null,
        finished_on: null,
        added_on: "2022-01-09",
        review: null,
      },
      {
        title: "A Novel, With\nA Line Break",
        author: "Some Author",
        isbn: null,
        status: "reading",
        rating: 3,
        finished_on: null,
        added_on: "2023-02-02",
        review: null,
      },
    ]);
  });
});

describe("StoryGraph export", () => {
  test("reads statuses, quarter-star ratings and the last day read", () => {
    const csv = [
      "Title,Authors,Contributors,ISBN/UID,Format,Read Status,Date Added,Last Date Read,Dates Read,Read Count,Moods,Pace,Star Rating,Review,Owned?",
      '"Piranesi","Susanna Clarke",,9781635575637,hardcover,read,2021/01/02,2021/01/20,2021/01/10-2021/01/20,1,,,4.75,"Strange and lovely",No',
      '"Babel","R.F. Kuang",,B09MD95S6M,digital,did-not-finish,2022/09/01,,,0,,,,,No',
      '"Middlemarch","George Eliot",,,paperback,to-read,2023/03/03,,,0,,,,,Yes',
    ].join("\n");
    const parsed = parseHistoryFile("export.csv", csv);
    expect(parsed.source).toBe("storygraph");
    expect(parsed.books.map((book) => [book.title, book.status, book.rating, book.finished_on, book.isbn])).toEqual([
      ["Piranesi", "read", 4.75, "2021-01-20", "9781635575637"],
      ["Babel", "abandoned", null, null, null],
      ["Middlemarch", "want", null, null, null],
    ]);
    expect(parsed.books[0].review).toBe("Strange and lovely");
  });
});

const CLIPPINGS = [
  "﻿Pride and Prejudice (Penguin Classics) (Austen, Jane)",
  "- Your Highlight on page 3 | Location 62-63 | Added on Friday, March 1, 2024 9:12:45 PM",
  "",
  "It is a truth universally",
  "==========",
  "Pride and Prejudice (Penguin Classics) (Austen, Jane)",
  "- Your Highlight on page 3 | Location 62-64 | Added on Friday, March 1, 2024 9:13:02 PM",
  "",
  "It is a truth universally acknowledged, that a single man",
  "==========",
  "Pride and Prejudice (Penguin Classics) (Austen, Jane)",
  "- Your Note on page 3 | Location 64 | Added on Friday, March 1, 2024 9:14:00 PM",
  "",
  "The most famous opening there is",
  "==========",
  "Pride and Prejudice (Penguin Classics) (Austen, Jane)",
  "- Your Bookmark on page 40 | Location 610 | Added on Saturday, March 2, 2024 8:00:00 AM",
  "",
  "",
  "==========",
  "my-own-notes.pdf",
  "- Your Highlight on Location 12-14 | Added on Sunday, 3 March 2024 10:00:00",
  "",
  "A line from a document I sent to my Kindle",
  "==========",
  "Deep Work (Newport, Cal)",
  "- Your Note on Location 900 | Added on Monday, March 4, 2024 7:30:00 AM",
  "",
  "Try this on Monday",
  "==========",
  "",
].join("\r\n");

describe("Kindle My Clippings.txt", () => {
  test("reads highlights and notes, joins a note to its passage, and keeps only the final version of an edited highlight", () => {
    const parsed = parseHistoryFile("My Clippings.txt", CLIPPINGS);
    expect(parsed.source).toBe("kindle");
    expect(parsed.books).toEqual([]);
    expect(parsed.highlights).toEqual([
      {
        title: "Pride and Prejudice (Penguin Classics)",
        author: "Jane Austen",
        text: "It is a truth universally acknowledged, that a single man",
        note: "The most famous opening there is",
        location: "Location 62-64",
        position: 62,
        made_at: "2024-03-01T21:13:02",
      },
      {
        title: "my-own-notes.pdf",
        author: "",
        text: "A line from a document I sent to my Kindle",
        note: null,
        location: "Location 12-14",
        position: 12,
        made_at: "2024-03-03T10:00:00",
      },
      {
        title: "Deep Work",
        author: "Cal Newport",
        text: "",
        note: "Try this on Monday",
        location: "Location 900",
        position: 900,
        made_at: "2024-03-04T07:30:00",
      },
    ]);
  });
});

describe("Libby exports", () => {
  test("reads a book's highlights and notes from its Reading Journey", () => {
    const journey = JSON.stringify({
      readingJourney: { title: { text: "The Overstory" }, author: "Richard Powers", isbn: "9780393356687", percent: 1 },
      highlights: [
        { timestamp: 1709300000000, chapter: "Roots", percent: 0.12, color: "#FFB", quote: "A tree is a passage" },
        { timestamp: 1709400000000, chapter: "Trunk", percent: 0.4, quote: "", note: "Remember this" },
        { timestamp: 1709500000000, quote: "", note: "" },
      ],
      bookmarks: [],
    });
    const parsed = parseHistoryFile("libbyjourney-overstory.json", journey);
    expect(parsed.source).toBe("libby");
    expect(parsed.books).toEqual([{ title: "The Overstory", author: "Richard Powers", isbn: "9780393356687" }]);
    expect(parsed.highlights.map((mark) => [mark.text, mark.note, mark.location, mark.position])).toEqual([
      ["A tree is a passage", null, "Roots", 0.12],
      ["", "Remember this", "Trunk", 0.4],
    ]);
    expect(parsed.highlights[0].made_at).toMatch(/^2024-03-01T/);
  });

  test("lists the books of a Timeline once each, without claiming they were finished", () => {
    const timeline = JSON.stringify({
      version: 1,
      timeline: [
        { title: { text: "Circe" }, author: "Madeline Miller", isbn: "9780316556347", timestamp: 1700000000000, activity: "Borrowed" },
        { title: { text: "Circe" }, author: "Madeline Miller", timestamp: 1701000000000, activity: "Returned" },
        { title: { text: "" }, author: "Nobody", timestamp: 1 },
      ],
    });
    const parsed = parseHistoryFile("libbytimeline-all.json", timeline);
    expect(parsed.books).toEqual([{ title: "Circe", author: "Madeline Miller", isbn: "9780316556347", added_on: "2023-11-14" }]);
  });
});

describe("files we do not read", () => {
  test.each([
    ["book.epub", "PK\u0003\u0004binary", /That is a book/],
    ["notes.txt", "just some notes of mine", /couldn’t tell which app/],
    ["data.json", '{"something": "else"}', /isn’t a Libby export/],
    ["cut-off.json", '{"readingJourney": {', /isn’t a complete export/],
    ["random.csv", "a,b,c\n1,2,3", /couldn’t tell which app/],
  ])("%s is refused with a plain explanation", (name, content, message) => {
    expect(() => parseHistoryFile(name, content)).toThrow(UnknownHistoryFile);
    expect(() => parseHistoryFile(name, content)).toThrow(message);
  });
});

test("CSV fields may hold commas, doubled quotes and line breaks", () => {
  expect(parseCsv('a,"b, c","d ""e""\nf"\r\n1,,3\n')).toEqual([
    ["a", "b, c", 'd "e"\nf'],
    ["1", "", "3"],
  ]);
});

test("a set of files is summed up for the reader before anything is imported", () => {
  const files = [parseHistoryFile("g.csv", GOODREADS), parseHistoryFile("My Clippings.txt", CLIPPINGS)];
  expect(summarize(files)).toEqual({ books: 3, highlights: 3, sources: ["goodreads", "kindle"] });
});
