-- ADR 0044 step 10: the check on later names, in the database. Every sentence Mothli shows is
-- scored by score_recap (app/eval/spoiler_gate/synthesis.py); one of its hard signals is a word
-- that names a person, place or other name the book reveals only after the reader's place. Since
-- step 5 the web process reads a book's notes only through the door, which hands nothing past the
-- reader's place, so that check had nothing to compare against in hosted mode. Here it runs where
-- the later names are, and only the sentence's own words come back.

-- The words of each name, worked out once when the name is published, by the very functions the
-- check uses locally (app/hosted/companion/later_names.py): one row for each person, other name
-- and theme of an edition, holding its match words (the capitalised parts of the name, lowercased
-- and split at hyphens and apostrophes), the same words with their accents folded, the ones that
-- are ordinary role words ("monk", "father"), and every word of the name. The digest is of the
-- exact text the words were made from: the check refuses to answer for a book where a name no
-- longer has words made from its present text. Nothing here says when a name is revealed, whether
-- it was taken back, or whose other name it is: those are read from the name's own row each time,
-- so they can never disagree with the notes.
--
-- Like every row of a book's notes it hangs from the chapter it was read from and goes with that
-- chapter (the cascade of migration 0024), so erasing an edition erases it. Row security is forced
-- as on every other table with an owner. Only the notes account owns rows here; the tenant role
-- holds no privilege on the table (app/hosted/tenant/grants.py, STORY_MEMORY_TABLES); the worker
-- inserts a chapter's rows when it publishes the chapter.
CREATE TABLE name_words (
  owner_id          UUID NOT NULL,
  book_id           UUID NOT NULL,
  book_incarnation  UUID NOT NULL,
  source_chapter_id UUID NOT NULL,
  source_kind       TEXT NOT NULL CHECK (source_kind IN ('entity', 'alias', 'theme')),
  source_id         UUID NOT NULL,
  source_digest     TEXT NOT NULL CHECK (source_digest ~ '^[0-9a-f]{64}$'),
  tokens            TEXT[] NOT NULL,
  folded            TEXT[] NOT NULL,
  roles             TEXT[] NOT NULL,
  words             TEXT[] NOT NULL,
  recorded_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (owner_id, book_id, book_incarnation, source_kind, source_id),
  FOREIGN KEY (owner_id, book_id, book_incarnation, source_chapter_id)
    REFERENCES ingested_chapters(owner_id, book_id, book_incarnation, chapter_id)
    ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED,
  CHECK (cardinality(tokens) = cardinality(folded)),
  CHECK (roles <@ tokens)
);

ALTER TABLE name_words ENABLE ROW LEVEL SECURITY;
ALTER TABLE name_words FORCE ROW LEVEL SECURITY;
CREATE POLICY name_words_owner_isolation ON name_words
  USING (owner_id = app_current_owner_id())
  WITH CHECK (owner_id = app_current_owner_id());
REVOKE ALL ON name_words FROM PUBLIC;

-- The check. It takes a reader's book, a ceiling on the place, the sentence's words (as they are,
-- and with their accents folded) and the reader's own read words; it returns only words of the
-- sentence it was given. The reader is the transaction's owner setting, and their place is worked
-- out exactly as the door does it (the read map's memory_through, never past the last chapter
-- with a read page), for their live book whose file matches the live edition on both
-- fingerprints. at_most can only lower the place, never raise it: local mode checks a sentence as
-- of a chapter that can be below the reader's place (the chapters whose notes are complete), and a
-- name revealed between the two is one the door already hands this reader. NULL means the place.
--
-- Its rule is score_recap's, set for set:
--   * the visible words are the match words of every live name revealed at or before the place;
--     with read words given, only those the reader has read (any of read_any or read_proper)
--     whitelist; with none (read_any NULL, local mode's eval-only use) every visible word does;
--   * the later words of a name are the match words, role words left out, of its live rows
--     revealed after the place, less the whitelist; a person's other names count as that person's
--     (grouped by the person's name, as locally), a theme is a group of its own;
--   * with read words given: (a) a later word the reader saw as a capitalised word in the middle
--     of a sentence (read_proper) is dropped; (b) a later word the reader read at all (read_any)
--     is dropped only while the same name keeps another later word the reader has not read;
--   * a later name word matches a sentence word when their folded forms are equal; a later theme
--     word only when equal as written.
-- It also says which matched sentence words belong to a later name not every word of whose
-- person's name the reader has read (read_any with read_also added and read_not taken away: the
-- reader's words exactly as the chat's own rule reads them), and which sentence words are exactly
-- a later name word (score_recap's count of unexplained capitalised words leaves those out).
-- complete is false when some name of the edition has no words made from its present text: the
-- caller then stops the reply rather than let a name through unchecked.
--
-- No row at all for a caller with no owner setting, the notes account itself, a book that is not
-- the caller's live book, or a book of theirs with no fingerprint. A book of theirs with no
-- edition gets one row: complete, and nothing matched. EXECUTE is revoked from PUBLIC here and
-- granted to the tenant role alone by the role script (app/hosted/tenant/grants.py,
-- scripts/hosted_roles.py), as the door's is.
CREATE FUNCTION story_memory_later_names(book UUID, at_most INTEGER, sentence_words TEXT[],
                                         sentence_folded TEXT[], read_any TEXT[],
                                         read_proper TEXT[], read_also TEXT[], read_not TEXT[])
RETURNS TABLE (complete BOOLEAN, name_words TEXT[], unread_name_words TEXT[],
               exact_name_words TEXT[], theme_words TEXT[])
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
  WITH reader AS (
    SELECT mine.owner_id, mine.id, mine.incarnation, mine.file_hash, mine.split_hash
    FROM public.books AS mine
    WHERE mine.owner_id = public.app_current_owner_id()
      AND mine.id = story_memory_later_names.book
      AND mine.deleted_at IS NULL
      AND mine.file_hash IS NOT NULL
      AND mine.split_hash IS NOT NULL
      AND NOT EXISTS (SELECT 1 FROM public.notes_account AS notes
                      WHERE notes.account_id = mine.owner_id)
  ),
  edition AS (
    SELECT built.owner_id, built.id, built.incarnation
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
                           = (reader.owner_id, reader.id, reader.incarnation)), 0),
             story_memory_later_names.at_most
           ) AS through
    FROM reader
  ),
  any_word AS MATERIALIZED (
    SELECT DISTINCT given.word FROM unnest(story_memory_later_names.read_any) AS given(word)
  ),
  proper_word AS MATERIALIZED (
    SELECT DISTINCT given.word FROM unnest(story_memory_later_names.read_proper) AS given(word)
  ),
  read_word AS MATERIALIZED (
    SELECT any_word.word FROM any_word
    UNION
    SELECT given.word FROM unnest(story_memory_later_names.read_also) AS given(word)
    EXCEPT
    SELECT given.word FROM unnest(story_memory_later_names.read_not) AS given(word)
  ),
  person AS MATERIALIZED (
    SELECT entity.id, entity.canonical_name AS name, entity.revealed_at, entity.retracted_at,
           made.tokens, made.folded, made.roles, made.words,
           made.source_digest IS NOT DISTINCT FROM
             encode(sha256(convert_to(entity.canonical_name, 'UTF8')), 'hex') AS fresh
    FROM edition
    JOIN public.entities AS entity
      ON (entity.owner_id, entity.book_id, entity.book_incarnation)
       = (edition.owner_id, edition.id, edition.incarnation)
    LEFT JOIN public.name_words AS made
      ON (made.owner_id, made.book_id, made.book_incarnation, made.source_kind, made.source_id)
       = (entity.owner_id, entity.book_id, entity.book_incarnation, 'entity', entity.id)
  ),
  other_name AS MATERIALIZED (
    SELECT person.name, alias.revealed_at, alias.retracted_at, made.tokens, made.folded,
           made.roles,
           made.source_digest IS NOT DISTINCT FROM
             encode(sha256(convert_to(alias.surface_form, 'UTF8')), 'hex') AS fresh
    FROM edition
    JOIN public.aliases AS alias
      ON (alias.owner_id, alias.book_id, alias.book_incarnation)
       = (edition.owner_id, edition.id, edition.incarnation)
    JOIN person ON person.id = alias.entity_id
    LEFT JOIN public.name_words AS made
      ON (made.owner_id, made.book_id, made.book_incarnation, made.source_kind, made.source_id)
       = (alias.owner_id, alias.book_id, alias.book_incarnation, 'alias', alias.id)
  ),
  theme AS MATERIALIZED (
    SELECT topic.name, topic.revealed_at, topic.retracted_at, made.tokens, made.folded,
           made.roles,
           made.source_digest IS NOT DISTINCT FROM
             encode(sha256(convert_to(topic.name, 'UTF8')), 'hex') AS fresh
    FROM edition
    JOIN public.themes AS topic
      ON (topic.owner_id, topic.book_id, topic.book_incarnation)
       = (edition.owner_id, edition.id, edition.incarnation)
    LEFT JOIN public.name_words AS made
      ON (made.owner_id, made.book_id, made.book_incarnation, made.source_kind, made.source_id)
       = (topic.owner_id, topic.book_id, topic.book_incarnation, 'theme', topic.id)
  ),
  stale AS (
    SELECT 1 FROM person WHERE NOT person.fresh
    UNION ALL
    SELECT 1 FROM other_name WHERE NOT other_name.fresh
    UNION ALL
    SELECT 1 FROM theme WHERE NOT theme.fresh
  ),
  token AS MATERIALIZED (
    SELECT 'name' AS bucket, person.name AS grp, person.revealed_at, part.token, part.folded,
           part.token = ANY (person.roles) AS role
    FROM person CROSS JOIN LATERAL unnest(person.tokens, person.folded) AS part(token, folded)
    WHERE person.retracted_at IS NULL AND person.fresh
    UNION ALL
    SELECT 'name', other_name.name, other_name.revealed_at, part.token, part.folded,
           part.token = ANY (other_name.roles)
    FROM other_name
    CROSS JOIN LATERAL unnest(other_name.tokens, other_name.folded) AS part(token, folded)
    WHERE other_name.retracted_at IS NULL AND other_name.fresh
    UNION ALL
    SELECT 'theme', theme.name, theme.revealed_at, part.token, part.folded,
           part.token = ANY (theme.roles)
    FROM theme CROSS JOIN LATERAL unnest(theme.tokens, theme.folded) AS part(token, folded)
    WHERE theme.retracted_at IS NULL AND theme.fresh
  ),
  whitelist AS MATERIALIZED (
    SELECT DISTINCT token.token
    FROM token CROSS JOIN place
    WHERE token.revealed_at <= place.through
      AND (story_memory_later_names.read_any IS NULL
           OR token.token IN (SELECT any_word.word FROM any_word)
           OR token.token IN (SELECT proper_word.word FROM proper_word))
  ),
  later AS MATERIALIZED (
    SELECT DISTINCT token.bucket, token.grp, token.token, token.folded
    FROM token CROSS JOIN place
    WHERE token.revealed_at > place.through
      AND NOT token.role
      AND token.token NOT IN (SELECT whitelist.token FROM whitelist)
  ),
  kept AS MATERIALIZED (
    SELECT later.bucket, later.grp, later.token, later.folded
    FROM later
    WHERE story_memory_later_names.read_any IS NULL
       OR (later.token NOT IN (SELECT proper_word.word FROM proper_word)
           AND NOT (later.token IN (SELECT any_word.word FROM any_word)
                    AND EXISTS (SELECT 1 FROM later AS other
                                WHERE other.bucket = later.bucket AND other.grp = later.grp
                                  AND other.token <> later.token
                                  AND other.token NOT IN (SELECT any_word.word
                                                          FROM any_word))))
  ),
  excused AS MATERIALIZED (
    SELECT DISTINCT person.name
    FROM person
    WHERE NOT EXISTS (SELECT 1 FROM unnest(person.words) AS part(word)
                      WHERE part.word NOT IN (SELECT read_word.word FROM read_word))
  )
  SELECT
    NOT EXISTS (SELECT 1 FROM stale) AS complete,
    ARRAY(SELECT DISTINCT given.word
          FROM unnest(story_memory_later_names.sentence_folded) AS given(word)
          WHERE given.word IN (SELECT kept.folded FROM kept WHERE kept.bucket = 'name')
          ORDER BY given.word) AS name_words,
    ARRAY(SELECT DISTINCT given.word
          FROM unnest(story_memory_later_names.sentence_folded) AS given(word)
          WHERE EXISTS (SELECT 1 FROM kept
                        WHERE kept.bucket = 'name' AND kept.folded = given.word
                          AND kept.grp NOT IN (SELECT excused.name FROM excused))
          ORDER BY given.word) AS unread_name_words,
    ARRAY(SELECT DISTINCT given.word
          FROM unnest(story_memory_later_names.sentence_words) AS given(word)
          WHERE given.word IN (SELECT kept.token FROM kept WHERE kept.bucket = 'name')
          ORDER BY given.word) AS exact_name_words,
    ARRAY(SELECT DISTINCT given.word
          FROM unnest(story_memory_later_names.sentence_words) AS given(word)
          WHERE given.word IN (SELECT kept.token FROM kept WHERE kept.bucket = 'theme')
          ORDER BY given.word) AS theme_words
  FROM reader
$$;

REVOKE ALL ON FUNCTION story_memory_later_names(UUID, INTEGER, TEXT[], TEXT[], TEXT[], TEXT[],
                                                TEXT[], TEXT[]) FROM PUBLIC;
