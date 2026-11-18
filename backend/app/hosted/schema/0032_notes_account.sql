-- ADR 0044 step 2: the door for every book. Story memory is kept once for each exact book file
-- (an edition), whoever brought the file, and each reader is handed it only up to their own
-- place. Nothing builds an edition for an upload yet (step 3); this step only changes how the
-- door finds one.

-- The library account becomes the notes account: the one reserved account that holds every
-- edition. It never signs in (no sign-in identity, and its user row is suspended, which every
-- session check refuses). Its rows, and the editions it already holds, stay as they are.
ALTER TABLE library_account RENAME TO notes_account;
ALTER TABLE notes_account RENAME CONSTRAINT library_account_pkey TO notes_account_pkey;
ALTER TABLE notes_account
  RENAME CONSTRAINT library_account_account_id_fkey TO notes_account_account_id_fkey;
ALTER INDEX ux_library_account_one RENAME TO ux_notes_account_one;

-- A database without one gets it here, rather than from the operator's command. Its user row is
-- written with the transaction's owner set to it, as row-level security asks of every insert;
-- its limits row comes with it (the users_ensure_owner_limits rule). The name is the one the
-- operator's command gives it; no reader ever sees it.
SELECT set_config('app.owner_id', gen_random_uuid()::text, true)
WHERE NOT EXISTS (SELECT 1 FROM notes_account);
INSERT INTO users (id, display_name, status)
SELECT app_current_owner_id(), 'Mothli library', 'suspended'
WHERE NOT EXISTS (SELECT 1 FROM notes_account) AND app_current_owner_id() IS NOT NULL;
INSERT INTO notes_account (account_id)
SELECT app_current_owner_id()
WHERE NOT EXISTS (SELECT 1 FROM notes_account) AND app_current_owner_id() IS NOT NULL;
SELECT set_config('app.owner_id', '', true);

-- The door, for every book. As 0031 left it, except how the edition is found and one row for a
-- book with no edition:
--   * the reader's book needs both fingerprints, not a free-library tag (any live book of the
--     reader's that was imported, whichever way it came);
--   * the edition is the notes account's live book with the same file_hash and split_hash;
--   * a live book of the reader's with no edition gets one row with empty lists: the same answer,
--     byte for byte, as an edition read at place 0 (every row is stamped at chapter 1 or later),
--     so "nobody has built this" and "you have read none of it" are one answer.
-- The place, every predicate, the hidden later ends and the key rewrite are unchanged. It takes
-- the book and nothing else; the reader is the transaction's owner setting. No row at all for a
-- caller with no owner setting, the notes account itself, a book that is not the caller's live
-- book, or a book of theirs with no fingerprint.
--
-- A new name, so the old door is dropped, not replaced: there is one door. EXECUTE is revoked
-- from PUBLIC here and granted to the tenant role alone by the role script
-- (app/hosted/tenant/grants.py, scripts/hosted_roles.py), as the old door's was.
DROP FUNCTION library_story_memory(UUID);

CREATE FUNCTION story_memory_door(book UUID)
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

REVOKE ALL ON FUNCTION story_memory_door(UUID) FROM PUBLIC;
