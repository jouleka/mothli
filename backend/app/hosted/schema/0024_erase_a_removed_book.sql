-- Removing a book in hosted mode erases what Mothli learned from its pages (ADR 0041 part 8).
-- Every row of a book's story memory hangs from the chapter it was read from: a chapter's receipt
-- (ingested_chapters) already goes with the chapter, and now everything taken from that chapter
-- goes with its receipt. So deleting a removed book's chapters, the one DELETE the tenant role
-- holds on the story memory, erases all of it in the same statement, and a table added later
-- under a chapter is erased the same way once it cascades too. The checks stay deferred.
ALTER TABLE chunks
  DROP CONSTRAINT chunks_owner_id_book_id_book_incarnation_chapter_id_fkey,
  ADD CONSTRAINT chunks_owner_id_book_id_book_incarnation_chapter_id_fkey
    FOREIGN KEY (owner_id, book_id, book_incarnation, chapter_id)
    REFERENCES ingested_chapters(owner_id, book_id, book_incarnation, chapter_id)
    ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE entities
  DROP CONSTRAINT entities_owner_id_book_id_book_incarnation_source_chapter__fkey,
  ADD CONSTRAINT entities_owner_id_book_id_book_incarnation_source_chapter__fkey
    FOREIGN KEY (owner_id, book_id, book_incarnation, source_chapter_id)
    REFERENCES ingested_chapters(owner_id, book_id, book_incarnation, chapter_id)
    ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE aliases
  DROP CONSTRAINT aliases_owner_id_book_id_book_incarnation_source_chapter_i_fkey,
  ADD CONSTRAINT aliases_owner_id_book_id_book_incarnation_source_chapter_i_fkey
    FOREIGN KEY (owner_id, book_id, book_incarnation, source_chapter_id)
    REFERENCES ingested_chapters(owner_id, book_id, book_incarnation, chapter_id)
    ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE edges
  DROP CONSTRAINT edges_owner_id_book_id_book_incarnation_source_chapter_id_fkey,
  ADD CONSTRAINT edges_owner_id_book_id_book_incarnation_source_chapter_id_fkey
    FOREIGN KEY (owner_id, book_id, book_incarnation, source_chapter_id)
    REFERENCES ingested_chapters(owner_id, book_id, book_incarnation, chapter_id)
    ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE events
  DROP CONSTRAINT events_owner_id_book_id_book_incarnation_source_chapter_id_fkey,
  ADD CONSTRAINT events_owner_id_book_id_book_incarnation_source_chapter_id_fkey
    FOREIGN KEY (owner_id, book_id, book_incarnation, source_chapter_id)
    REFERENCES ingested_chapters(owner_id, book_id, book_incarnation, chapter_id)
    ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE event_participants
  DROP CONSTRAINT event_participants_owner_id_book_id_book_incarnation_sourc_fkey,
  ADD CONSTRAINT event_participants_owner_id_book_id_book_incarnation_sourc_fkey
    FOREIGN KEY (owner_id, book_id, book_incarnation, source_chapter_id)
    REFERENCES ingested_chapters(owner_id, book_id, book_incarnation, chapter_id)
    ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE themes
  DROP CONSTRAINT themes_owner_id_book_id_book_incarnation_source_chapter_id_fkey,
  ADD CONSTRAINT themes_owner_id_book_id_book_incarnation_source_chapter_id_fkey
    FOREIGN KEY (owner_id, book_id, book_incarnation, source_chapter_id)
    REFERENCES ingested_chapters(owner_id, book_id, book_incarnation, chapter_id)
    ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE entity_state
  DROP CONSTRAINT entity_state_owner_id_book_id_book_incarnation_source_chap_fkey,
  ADD CONSTRAINT entity_state_owner_id_book_id_book_incarnation_source_chap_fkey
    FOREIGN KEY (owner_id, book_id, book_incarnation, source_chapter_id)
    REFERENCES ingested_chapters(owner_id, book_id, book_incarnation, chapter_id)
    ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE entity_corrections
  DROP CONSTRAINT entity_corrections_owner_id_book_id_book_incarnation_sourc_fkey,
  ADD CONSTRAINT entity_corrections_owner_id_book_id_book_incarnation_sourc_fkey
    FOREIGN KEY (owner_id, book_id, book_incarnation, source_chapter_id)
    REFERENCES ingested_chapters(owner_id, book_id, book_incarnation, chapter_id)
    ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE chapter_summaries
  DROP CONSTRAINT chapter_summaries_owner_id_book_id_book_incarnation_source_fkey,
  ADD CONSTRAINT chapter_summaries_owner_id_book_id_book_incarnation_source_fkey
    FOREIGN KEY (owner_id, book_id, book_incarnation, source_chapter_id)
    REFERENCES ingested_chapters(owner_id, book_id, book_incarnation, chapter_id)
    ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED;
