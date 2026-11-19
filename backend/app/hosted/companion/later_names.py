"""The words of an edition's names, for the later-name check in the database (ADR 0044 step 10).

``score_recap`` removes a sentence that names someone the book reveals only after the reader's
place. A hosted reader's own process holds no such name (the door hands nothing past their place),
so the check runs inside the database, in ``story_memory_later_names`` (migration 0035), as set
comparisons over words kept for every name of the edition in ``name_words``. Those words are made
here, by the very functions the check uses locally (``synthesis.name_words_of``), so the two
modes split a name into the same words.

Rows are written by the worker in the transaction that publishes a chapter
(``PostgresWorkerRepository.commit_chapter_memory``), one for each person, other name and theme
the chapter adds. ``rebuild_name_words`` writes them again for a whole edition from its rows as
they stand: what a backfill of editions made before migration 0035 would run (none exists: nothing
was deployed before it), and what the tests use for notes they write by hand.
"""

from __future__ import annotations

import hashlib
import uuid

from psycopg.rows import tuple_row

from app.eval.spoiler_gate.synthesis import name_words_of

KINDS = {"entity": ("entities", "canonical_name"), "alias": ("aliases", "surface_form"),
         "theme": ("themes", "name")}


def name_words_row(kind: str, source_id: uuid.UUID, text: str) -> dict:
    """One ``name_words`` row's values, but for the edition and the chapter: the name's words
    and the digest of the exact text they were made from."""
    if kind not in KINDS:
        raise ValueError("a name is a person's name, another name or a theme")
    if not isinstance(text, str):
        raise ValueError("a name is a text")
    return {"source_kind": kind, "source_id": source_id,
            "source_digest": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            **name_words_of(text)}


def rebuild_name_words(conn, scope: tuple) -> int:
    """Write the ``name_words`` of one edition again from its people, other names and themes as
    they stand now, in one transaction; returns how many rows were written. ``scope`` is the
    edition's (owner, book, incarnation). ``conn`` must read and write those tables past row
    security (the database owner)."""
    owner, book, incarnation = scope
    written = 0
    cursor = conn.cursor(row_factory=tuple_row)     # whatever rows the connection makes
    with conn.transaction():
        cursor.execute("DELETE FROM public.name_words WHERE owner_id=%s AND book_id=%s"
                       " AND book_incarnation=%s", scope)
        for kind, (table, column) in KINDS.items():
            rows = cursor.execute(
                f"SELECT id, source_chapter_id, {column} FROM public.{table}"
                " WHERE owner_id=%s AND book_id=%s AND book_incarnation=%s ORDER BY id",
                scope).fetchall()
            for source_id, chapter_id, text in rows:
                values = name_words_row(kind, source_id, text)
                cursor.execute(
                    "INSERT INTO public.name_words (owner_id,book_id,book_incarnation,"
                    "source_chapter_id,source_kind,source_id,source_digest,tokens,folded,roles,"
                    "words) VALUES (%s,%s,%s,%s,%s,%s,%s,%s::text[],%s::text[],%s::text[],"
                    "%s::text[])",
                    (owner, book, incarnation, chapter_id, values["source_kind"],
                     values["source_id"], values["source_digest"], values["tokens"],
                     values["folded"], values["roles"], values["words"]))
                written += 1
    return written
