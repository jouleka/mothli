import { beforeEach, expect, test } from "vitest";
import { keptPasses, keptReplyShown, readSavedReplies, writeSavedReplies, type SavedCompanionReply } from "./companionNotes";
beforeEach(() => localStorage.clear());
test("saved replies survive reload only within their owner, book and reading pass", () => {
  const replies: SavedCompanionReply[] = [{id:"one",body:"Remember this.",question:"Why?",citations:[],asOfChapter:2}];
  writeSavedReplies("book", "alice", 1, replies);
  expect(readSavedReplies("book", "alice", 1)).toEqual(replies);
  expect(readSavedReplies("book", "bob", 1)).toEqual([]);
  expect(readSavedReplies("other", "alice", 1)).toEqual([]);
  expect(readSavedReplies("book", "alice", 2)).toEqual([]);
});
// Catches: a reply from an earlier reading pass being unreachable (it is filed under that pass),
// or another reader's or another book's replies being picked up.
test("the reading passes with kept replies are found for one owner and book", () => {
  writeSavedReplies("book", "alice", 10, []);
  writeSavedReplies("book", "alice", 2, []);
  writeSavedReplies("book", "bob", 3, []);
  writeSavedReplies("other", "alice", 4, []);
  localStorage.setItem("rc:pip-keeps:not json", "[]");
  expect(keptPasses("book", "alice")).toEqual(["2", "10"]);
});
// Catches: a reply that quotes a chapter no longer read staying in view because the chapter it was
// asked at is still read; and the opening pages (not in the map) hiding a reply for good.
test("a kept reply is shown only while its chapter and every chapter it cites count as read", () => {
  const cite = (ordinal: number) => ({id:ordinal,ordinal,chapter_key:"k",href:"h",title:"t",excerpt:"e"});
  const reply = (asOfChapter: number, ...cited: number[]): SavedCompanionReply =>
    ({id:"r",body:"b",question:"q",citations:cited.map(cite),asOfChapter});
  const read = {0: 1, 1: 0.2, 2: 0};
  expect(keptReplyShown(reply(2), read)).toBe(true);           // part read counts
  expect(keptReplyShown(reply(3), read)).toBe(false);
  expect(keptReplyShown(reply(2, 3), read)).toBe(false);       // cites chapter 3, not read
  expect(keptReplyShown(reply(0), read)).toBe(true);           // the opening pages
  expect(keptReplyShown(reply(1), {1: 1})).toBe(true);         // a place not in the map decides nothing
});
