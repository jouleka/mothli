import { beforeEach, expect, test } from "vitest";
import { readSavedReplies, writeSavedReplies, type SavedCompanionReply } from "./companionNotes";
beforeEach(() => localStorage.clear());
test("saved replies survive reload only within their owner, book and reading pass", () => {
  const replies: SavedCompanionReply[] = [{id:"one",body:"Remember this.",question:"Why?",citations:[],asOfChapter:2}];
  writeSavedReplies("book", "alice", 1, replies);
  expect(readSavedReplies("book", "alice", 1)).toEqual(replies);
  expect(readSavedReplies("book", "bob", 1)).toEqual([]);
  expect(readSavedReplies("other", "alice", 1)).toEqual([]);
  expect(readSavedReplies("book", "alice", 2)).toEqual([]);
});
