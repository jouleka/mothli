-- ADR 0044 step 1: the split fingerprint. Story memory is stamped by chapter number, so two copies
-- of one file whose chapters were numbered differently (imported on either side of a change to
-- Mothli's chapter splitter) must never count as one book: one copy's "chapter 7" would be handed
-- out as the other's chapter 7, a spoiler. The import already keeps the SHA-256 of the file's
-- bytes (file_hash); it now also keeps split_hash, the SHA-256 over the ordered list of (chapter
-- number, the file part of the chapter key, SHA-256 of the chapter's text), worked out from the
-- chapters the import has in hand (app/hosted/tenant/api.py, store_epub). The book id is not in it,
-- so two readers' copies of one file split by one version of the code have the same fingerprint.
--
-- Rows already there get no fingerprint: NULL means "not known". It is never worked out here
-- (that needs the file and the splitter), and nothing is deployed, so no book needs it filled in
-- afterwards. A book with no fingerprint is matched to nothing.
ALTER TABLE books ADD COLUMN split_hash TEXT;
ALTER TABLE books
  ADD CONSTRAINT books_split_hash_ck CHECK (split_hash IS NULL OR split_hash ~ '^[0-9a-f]{64}$');

-- The door (0026) now also requires the reader's split fingerprint to be known and equal to the
-- library edition's. Three lines change: the reader's fingerprint is read with the rest of their
-- book, a reader's book with no fingerprint is refused, and the edition must have the same one (an
-- edition with none matches nothing, since NULL equals nothing). Everything else is as 0026 left
-- it: the place, every predicate, the hidden later ends, the key rewrite, the arguments and the
-- result. Replacing the function keeps its owner and its privileges: EXECUTE stays revoked from
-- PUBLIC and granted to the tenant role alone by the role script (app/hosted/tenant/grants.py).
-- The door can only refuse more than before; it never hands anyone anything new.
CREATE OR REPLACE FUNCTION library_story_memory(book UUID)
RETURNS TABLE (content_language TEXT, chapters JSON, completed JSON, entities JSON,
               aliases JSON, themes JSON, events JSON, summaries JSON, edges JSON, states JSON)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
  WITH reader AS (
    SELECT mine.owner_id, mine.id, mine.incarnation, mine.content_language, mine.source_kind,
           mine.file_hash, mine.split_hash
    FROM public.books AS mine
    WHERE mine.owner_id = public.app_current_owner_id()
      AND mine.id = library_story_memory.book
      AND mine.deleted_at IS NULL
      AND mine.source_kind LIKE 'gutenberg:%'
      AND mine.file_hash IS NOT NULL
      AND mine.split_hash IS NOT NULL
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
  FROM reader, edition, place
$$;

REVOKE ALL ON FUNCTION library_story_memory(UUID) FROM PUBLIC;
