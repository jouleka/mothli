-- ADR 0044 step 3: builds into the edition. A reader's story-memory run builds the next missing
-- chapter of the edition of the reader's file, and makes the edition when its first chapter is
-- built; one run, of whichever reader, builds a chapter. The worker's new grants (it reads and adds
-- rows of books) are in app/hosted/tenant/grants.py, as every role's are.

-- The mark of an edition: edition_of names the notes account. The foreign key to notes_account
-- and the check that it is the row's own owner let only the notes account's books carry it; an
-- edition always has both fingerprints. A reader's book can never carry it.
ALTER TABLE books ADD COLUMN edition_of UUID REFERENCES notes_account (account_id);
ALTER TABLE books
  ADD CONSTRAINT books_edition_shape_ck
    CHECK (edition_of IS NULL OR (edition_of = owner_id AND file_hash IS NOT NULL
                                  AND split_hash IS NOT NULL));

-- One live edition per file. There is one notes account (ux_notes_account_one) and only its rows
-- carry the mark, so (owner, file_hash, split_hash) over the live marked rows is one edition for
-- each file, whoever makes it and however many make it at once.
CREATE UNIQUE INDEX ux_books_one_live_edition ON books (owner_id, file_hash, split_hash)
  WHERE edition_of IS NOT NULL AND deleted_at IS NULL;

-- The editions the operator's command already holds are marked: for each file, the first live
-- book of the notes account (a second copy of one file, if any, stays an unmarked book).
UPDATE books AS book SET edition_of = book.owner_id
WHERE book.owner_id IN (SELECT notes.account_id FROM notes_account AS notes)
  AND book.deleted_at IS NULL AND book.file_hash IS NOT NULL AND book.split_hash IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM books AS earlier
                  WHERE earlier.owner_id = book.owner_id AND earlier.deleted_at IS NULL
                    AND earlier.file_hash = book.file_hash
                    AND earlier.split_hash = book.split_hash
                    AND (earlier.created_at, earlier.id) < (book.created_at, book.id));

-- The build slot: at most one story-memory job leased or running on one chapter of one edition,
-- across every reader. A run names its edition and chapter in payload_metadata under its lease;
-- the claim takes the edition out again when it leases a job, so a job put back to wait never
-- carries an old slot into its next attempt.
CREATE UNIQUE INDEX ux_jobs_one_build_per_chapter
  ON jobs ((payload_metadata->>'edition'), (payload_metadata->>'chapter'))
  WHERE kind = 'story_memory' AND state IN ('leased', 'running') AND payload_metadata ? 'edition';

-- The door, as 0032 left it, with one line more: the edition is a marked one, so the one live
-- edition of a file is the only one it can find. Replaced in place: its owner, privileges,
-- security, settings, arguments and result stay as they are.
CREATE OR REPLACE FUNCTION story_memory_door(book UUID)
RETURNS TABLE (content_language TEXT, chapters JSON, completed JSON, entities JSON,
               aliases JSON, themes JSON, events JSON, summaries JSON, edges JSON, states JSON)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
  WITH reader AS (
    SELECT mine.owner_id, mine.id, mine.incarnation, mine.content_language, mine.file_hash,
           mine.split_hash
    FROM public.books AS mine
    WHERE mine.owner_id = public.app_current_owner_id()
      AND mine.id = story_memory_door.book
      AND mine.deleted_at IS NULL
      AND mine.file_hash IS NOT NULL
      AND mine.split_hash IS NOT NULL
      AND NOT EXISTS (SELECT 1 FROM public.notes_account AS notes
                      WHERE notes.account_id = mine.owner_id)
  ),
  edition AS (
    SELECT built.owner_id, built.id, built.incarnation, reader.id AS reader_book
    FROM reader
    JOIN public.notes_account AS notes ON true
    JOIN public.books AS built
      ON built.owner_id = notes.account_id
     AND built.file_hash = reader.file_hash
     AND built.split_hash = reader.split_hash
     AND built.deleted_at IS NULL
     AND built.edition_of IS NOT NULL
    ORDER BY built.created_at, built.id
    LIMIT 1
  ),
  place AS MATERIALIZED (
    SELECT LEAST(
             COALESCE((SELECT state.memory_through FROM public.reading_state AS state
                       WHERE (state.owner_id, state.book_id, state.book_incarnation)
                           = (reader.owner_id, reader.id, reader.incarnation)), 0),
             COALESCE((SELECT max(marked.atom) FROM public.read_ranges AS marked
                       WHERE (marked.owner_id, marked.book_id, marked.book_incarnation)
                           = (reader.owner_id, reader.id, reader.incarnation)), 0)
           ) AS through
    FROM reader
  )
  SELECT reader.content_language,
    COALESCE((SELECT json_agg(json_build_object(
                'key', CASE WHEN starts_with(chapter.chapter_key, edition.id::text || ':')
                            THEN edition.reader_book::text
                                 || substr(chapter.chapter_key, length(edition.id::text) + 1)
                            ELSE chapter.chapter_key END,
                'ordinal', chapter.revealed_at, 'title', chapter.title,
                'part_label', chapter.part_label, 'kind', chapter.kind,
                'live', chapter.retracted_at IS NULL)
              ORDER BY chapter.revealed_at, chapter.chapter_key, chapter.id)
              FROM public.chapters AS chapter
              WHERE (chapter.owner_id, chapter.book_id, chapter.book_incarnation)
                  = (edition.owner_id, edition.id, edition.incarnation)
                AND chapter.revealed_at <= place.through), '[]'::json),
    COALESCE((SELECT json_agg(json_build_object(
                'key', CASE WHEN starts_with(chapter.chapter_key, edition.id::text || ':')
                            THEN edition.reader_book::text
                                 || substr(chapter.chapter_key, length(edition.id::text) + 1)
                            ELSE chapter.chapter_key END,
                'ordinal', chapter.revealed_at))
              FROM public.ingested_chapters AS receipt
              JOIN public.chapters AS chapter
                ON (chapter.owner_id, chapter.book_id, chapter.book_incarnation, chapter.id)
                 = (receipt.owner_id, receipt.book_id, receipt.book_incarnation,
                    receipt.chapter_id)
              WHERE (receipt.owner_id, receipt.book_id, receipt.book_incarnation)
                  = (edition.owner_id, edition.id, edition.incarnation)
                AND chapter.revealed_at <= place.through
                AND receipt.retracted_at IS NULL AND chapter.retracted_at IS NULL
                AND receipt.content_hash = chapter.content_hash), '[]'::json),
    COALESCE((SELECT json_agg(json_build_object(
                'id', entity.id, 'name', entity.canonical_name, 'type', entity.entity_type,
                'revealed_at', entity.revealed_at,
                'invalid_at', CASE WHEN entity.invalid_at <= place.through
                                   THEN entity.invalid_at END,
                'recorded_at', entity.recorded_at, 'retracted_at', entity.retracted_at)
              ORDER BY entity.recorded_at, entity.id)
              FROM public.entities AS entity
              WHERE (entity.owner_id, entity.book_id, entity.book_incarnation)
                  = (edition.owner_id, edition.id, edition.incarnation)
                AND entity.revealed_at <= place.through), '[]'::json),
    COALESCE((SELECT json_agg(json_build_object(
                'id', alias.id, 'entity_id', alias.entity_id,
                'surface_form', alias.surface_form, 'revealed_at', alias.revealed_at,
                'recorded_at', alias.recorded_at, 'retracted_at', alias.retracted_at)
              ORDER BY alias.recorded_at, alias.id)
              FROM public.aliases AS alias
              WHERE (alias.owner_id, alias.book_id, alias.book_incarnation)
                  = (edition.owner_id, edition.id, edition.incarnation)
                AND alias.revealed_at <= place.through
                AND EXISTS (SELECT 1 FROM public.entities AS person
                            WHERE (person.owner_id, person.book_id, person.book_incarnation,
                                   person.id)
                                = (edition.owner_id, edition.id, edition.incarnation,
                                   alias.entity_id)
                              AND person.revealed_at <= place.through)), '[]'::json),
    COALESCE((SELECT json_agg(json_build_object(
                'id', theme.id, 'name', theme.name, 'description', theme.description,
                'revealed_at', theme.revealed_at,
                'invalid_at', CASE WHEN theme.invalid_at <= place.through
                                   THEN theme.invalid_at END,
                'recorded_at', theme.recorded_at, 'retracted_at', theme.retracted_at)
              ORDER BY theme.recorded_at, theme.id)
              FROM public.themes AS theme
              WHERE (theme.owner_id, theme.book_id, theme.book_incarnation)
                  = (edition.owner_id, edition.id, edition.incarnation)
                AND theme.revealed_at <= place.through), '[]'::json),
    COALESCE((SELECT json_agg(json_build_object(
                'id', event.id, 'revealed_at', event.revealed_at,
                'invalid_at', CASE WHEN event.invalid_at <= place.through
                                   THEN event.invalid_at END,
                'order_idx', event.order_idx, 'summary', event.summary, 'kind', event.kind,
                'live', event.retracted_at IS NULL)
              ORDER BY event.recorded_at, event.id)
              FROM public.events AS event
              WHERE (event.owner_id, event.book_id, event.book_incarnation)
                  = (edition.owner_id, edition.id, edition.incarnation)
                AND event.revealed_at <= place.through), '[]'::json),
    COALESCE((SELECT json_agg(json_build_object(
                'kind', summary.kind, 'summary', summary.summary,
                'revealed_at', summary.revealed_at,
                'invalid_at', CASE WHEN summary.invalid_at <= place.through
                                   THEN summary.invalid_at END,
                'live', summary.retracted_at IS NULL,
                'chapter_key', CASE WHEN starts_with(chapter.chapter_key, edition.id::text || ':')
                                    THEN edition.reader_book::text
                                         || substr(chapter.chapter_key,
                                                   length(edition.id::text) + 1)
                                    ELSE chapter.chapter_key END,
                'chapter_ordinal', chapter.revealed_at,
                'chapter_live', chapter.retracted_at IS NULL)
              ORDER BY summary.recorded_at, summary.id)
              FROM public.chapter_summaries AS summary
              JOIN public.chapters AS chapter
                ON (chapter.owner_id, chapter.book_id, chapter.book_incarnation, chapter.id)
                 = (summary.owner_id, summary.book_id, summary.book_incarnation,
                    summary.source_chapter_id)
              WHERE (summary.owner_id, summary.book_id, summary.book_incarnation)
                  = (edition.owner_id, edition.id, edition.incarnation)
                AND summary.revealed_at <= place.through
                AND chapter.revealed_at <= place.through), '[]'::json),
    COALESCE((SELECT json_agg(json_build_object(
                'id', edge.id, 'src', edge.src_entity_id, 'dst', edge.dst_entity_id,
                'type', edge.relationship_type, 'label', edge.label,
                'revealed_at', edge.revealed_at,
                'invalid_at', CASE WHEN edge.invalid_at <= place.through
                                   THEN edge.invalid_at END,
                'live', edge.retracted_at IS NULL)
              ORDER BY edge.recorded_at, edge.id)
              FROM public.edges AS edge
              WHERE (edge.owner_id, edge.book_id, edge.book_incarnation)
                  = (edition.owner_id, edition.id, edition.incarnation)
                AND edge.revealed_at <= place.through
                AND EXISTS (SELECT 1 FROM public.entities AS person
                            WHERE (person.owner_id, person.book_id, person.book_incarnation,
                                   person.id)
                                = (edition.owner_id, edition.id, edition.incarnation,
                                   edge.src_entity_id)
                              AND person.revealed_at <= place.through)
                AND EXISTS (SELECT 1 FROM public.entities AS person
                            WHERE (person.owner_id, person.book_id, person.book_incarnation,
                                   person.id)
                                = (edition.owner_id, edition.id, edition.incarnation,
                                   edge.dst_entity_id)
                              AND person.revealed_at <= place.through)), '[]'::json),
    COALESCE((SELECT json_agg(json_build_object(
                'id', state.id, 'entity_id', state.entity_id, 'status', state.status,
                'revealed_at', state.revealed_at,
                'invalid_at', CASE WHEN state.invalid_at <= place.through
                                   THEN state.invalid_at END,
                'live', state.retracted_at IS NULL)
              ORDER BY state.recorded_at, state.id)
              FROM public.entity_state AS state
              WHERE (state.owner_id, state.book_id, state.book_incarnation)
                  = (edition.owner_id, edition.id, edition.incarnation)
                AND state.revealed_at <= place.through
                AND EXISTS (SELECT 1 FROM public.entities AS person
                            WHERE (person.owner_id, person.book_id, person.book_incarnation,
                                   person.id)
                                = (edition.owner_id, edition.id, edition.incarnation,
                                   state.entity_id)
                              AND person.revealed_at <= place.through)), '[]'::json)
  FROM reader CROSS JOIN place LEFT JOIN edition ON true
$$;
