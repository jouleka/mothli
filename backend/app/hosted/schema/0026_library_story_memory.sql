-- ADR 0042 part 5: story memory of free-library books, built once by the operator as the library
-- account and handed out only up to each reader's place, by one function that decides that place
-- in the database.

-- The reader's place for the door: the read map's finished_through, written only by the read
-- map's sync_counter through the hosted adapter (set_bookmark). No route a device calls writes it.
ALTER TABLE reading_state
  ADD COLUMN memory_through INTEGER NOT NULL DEFAULT 0 CHECK (memory_through >= 0);

-- The one account that builds library editions. It never signs in (no sign-in identity, and its
-- user row is suspended, which every session check refuses). Named in a column deliberately not
-- called owner_id, so the account purge neither lists this table nor gains a privilege on it.
CREATE TABLE library_account (
  account_id UUID PRIMARY KEY REFERENCES users(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX ux_library_account_one ON library_account ((true));
REVOKE ALL ON library_account FROM PUBLIC;

-- A story-memory run that stopped its book: three chapters in a row could not be remembered, so
-- the model is likely failing every chapter and each would be paid for twice and kept empty. Not
-- retried; the book's memory stays where it is until the operator looks.
ALTER TABLE jobs DROP CONSTRAINT jobs_failure_shape_ck;
ALTER TABLE jobs
  ADD CONSTRAINT jobs_failure_shape_ck
    CHECK (
      sanitized_error IS NULL OR (
        sanitized_error ? 'code'
        AND sanitized_error ? 'retryable'
        AND jsonb_typeof(sanitized_error->'code') = 'string'
        AND sanitized_error->>'code' IN (
          'attempts_exhausted','budget_exceeded','cancelled','grounding_rejected',
          'internal_error','invalid_model_output','provider_rejected','provider_unavailable',
          'source_integrity','source_missing','too_many_unremembered'
        )
        AND jsonb_typeof(sanitized_error->'retryable') = 'boolean'
        AND sanitized_error - ARRAY['code', 'retryable'] = '{}'::jsonb
      )
    );

ALTER TABLE job_attempts DROP CONSTRAINT job_attempts_failure_shape_ck;
ALTER TABLE job_attempts
  ADD CONSTRAINT job_attempts_failure_shape_ck
    CHECK (
      sanitized_error IS NULL OR (
        sanitized_error ? 'code'
        AND sanitized_error ? 'retryable'
        AND jsonb_typeof(sanitized_error->'code') = 'string'
        AND sanitized_error->>'code' IN (
          'attempts_exhausted','budget_exceeded','cancelled','grounding_rejected',
          'internal_error','invalid_model_output','provider_rejected','provider_unavailable',
          'source_integrity','source_missing','too_many_unremembered'
        )
        AND jsonb_typeof(sanitized_error->'retryable') = 'boolean'
        AND sanitized_error - ARRAY['code', 'retryable'] = '{}'::jsonb
      )
    );

-- The door. It takes the book and nothing else. The reader is the transaction's owner setting
-- (the one row-level security uses); their place is worked out here from their own rows; and only
-- rows of the library edition stamped at or before that place leave the database, in the shape
-- HostedBookStore reads for a reader's own rows. "Ends at chapter N" past the place is handed out
-- as no end. No row at all for a caller with no owner setting, the library account itself, a book
-- that is not the caller's live book, an upload, or a library book whose file is not exactly a
-- built edition's.
CREATE FUNCTION library_story_memory(book UUID)
RETURNS TABLE (content_language TEXT, chapters JSON, completed JSON, entities JSON,
               aliases JSON, themes JSON, events JSON, summaries JSON, edges JSON, states JSON)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
  WITH reader AS (
    SELECT mine.owner_id, mine.id, mine.incarnation, mine.content_language, mine.source_kind,
           mine.file_hash
    FROM public.books AS mine
    WHERE mine.owner_id = public.app_current_owner_id()
      AND mine.id = library_story_memory.book
      AND mine.deleted_at IS NULL
      AND mine.source_kind LIKE 'gutenberg:%'
      AND mine.file_hash IS NOT NULL
      AND NOT EXISTS (SELECT 1 FROM public.library_account AS library
                      WHERE library.account_id = mine.owner_id)
  ),
  edition AS (
    SELECT built.owner_id, built.id, built.incarnation, reader.id AS reader_book
    FROM reader
    JOIN public.library_account AS library ON true
    JOIN public.books AS built
      ON built.owner_id = library.account_id
     AND built.source_kind = reader.source_kind
     AND built.file_hash = reader.file_hash
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
  FROM reader, edition, place
$$;

REVOKE ALL ON FUNCTION library_story_memory(UUID) FROM PUBLIC;
