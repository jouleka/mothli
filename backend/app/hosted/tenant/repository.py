"""PostgreSQL repositories whose API makes tenant scope impossible to omit accidentally.

Every operation opens one transaction, sets the RLS owner locally, and still includes the same owner
in every predicate. The two barriers are independently testable by calling this repository through a
non-BYPASSRLS runtime role or a superuser connection that bypasses RLS.
"""

from __future__ import annotations

import hashlib
import math
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import AsyncIterator

import psycopg
from psycopg.rows import dict_row

from app.content_scope import chapter_numbers, is_content
from app.hosted.audit import record_event_async
from app.hosted.auth.repository import AuthConfigurationError
from app.hosted.limits import LimitExceededError
from app.hosted.tenant.grants import ALL_TABLE_PRIVILEGES, TENANT_TABLE_PRIVILEGES
from app.reader_place import content_numbers
from app.hosted.tenant.models import (
    AlreadyOnShelfError,
    FuturePositionVersionError,
    InvalidPositionError,
    MissingTenantResourceError,
    OwnerId,
    SourceObjectRecord,
    StalePositionEpochError,
)

# What removing a book erases, keyed by the book (ADR 0041 part 8), children before parents.
# A reader owns no story memory (ADR 0044): a book's notes are its edition's, which stays for
# the other readers of the file, and the reader's link to it (the two fingerprints on their
# ``books`` row) is cleared with the row. The test in
# tests/hosted/test_removing_a_book_postgres.py fails for a table with an owner and a book that
# is neither here nor kept there with its reason.
ERASED_WITH_THE_BOOK = (
    "book_search_documents",
    "annotations",
    "highlights",
    "bookmarks",
    "reading_state",
    "reader_preferences",
    "read_ranges",
    "skimmed_ranges",
    "read_position",
    "conversation_turns",
    "chapter_notes",
    "recollections",
    "source_objects",
)


@dataclass(slots=True)
class PostgresTenantRepository:
    _dsn: str = field(repr=False)

    async def _connect(self) -> psycopg.AsyncConnection:
        return await psycopg.AsyncConnection.connect(self._dsn, row_factory=dict_row)

    @asynccontextmanager
    async def _transaction(self, owner_id: OwnerId) -> AsyncIterator[psycopg.AsyncConnection]:
        async with await self._connect() as conn, conn.transaction():
            await conn.execute(
                "SELECT set_config('app.owner_id', %s, true)", (str(owner_id.value),)
            )
            yield conn

    async def check_runtime_role(self) -> None:
        required = {
            f"public.{table}": set(privileges)
            for table, privileges in TENANT_TABLE_PRIVILEGES.items()
        }
        all_privileges = set(ALL_TABLE_PRIVILEGES)
        async with await self._connect() as conn:
            role = await (
                await conn.execute(
                    """
                    SELECT rolsuper, rolbypassrls, rolcreatedb, rolcreaterole,
                           rolreplication, rolinherit,
                           EXISTS (SELECT 1 FROM pg_auth_members WHERE member = role_row.oid)
                             AS has_membership
                    FROM pg_roles AS role_row WHERE rolname = current_user
                    """
                )
            ).fetchone()
            if role is None or role["rolsuper"] or role["rolbypassrls"] or any(
                role[name]
                for name in (
                    "rolcreatedb", "rolcreaterole", "rolreplication", "rolinherit", "has_membership"
                )
            ):
                raise AuthConfigurationError(
                    "HOSTED_TENANT_DSN must use an isolated non-superuser RLS-enforced role"
                )

            incorrect = []
            for table, expected in sorted(required.items()):
                for privilege in sorted(all_privileges):
                    row = await (
                        await conn.execute(
                            "SELECT has_table_privilege(current_user, %s, %s)",
                            (table, privilege),
                        )
                    ).fetchone()
                    held = bool(row and row["has_table_privilege"])
                    if held != (privilege in expected):
                        incorrect.append(f"{table}:{privilege}")

            unexpected = await (
                await conn.execute(
                    """
                    SELECT table_name
                    FROM information_schema.tables
                    WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
                      AND table_name <> ALL(%s)
                      AND has_table_privilege(
                            current_user, quote_ident(table_schema) || '.' || quote_ident(table_name),
                            'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
                          )
                    ORDER BY table_name
                    """,
                    ([name.removeprefix("public.") for name in required],),
                )
            ).fetchall()
        if incorrect or unexpected:
            raise AuthConfigurationError(
                "tenant runtime role privileges are not the reviewed repository allow-list"
            )

    async def consume_request_limit(self, owner_id: OwnerId) -> dict:
        async with self._transaction(owner_id) as conn:
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s::text,47))",
                (owner_id.value,),
            )
            await conn.execute(
                """
                INSERT INTO public.owner_request_windows (owner_id,request_count)
                VALUES (%s,0) ON CONFLICT (owner_id) DO NOTHING
                """,
                (owner_id.value,),
            )
            row = await (
                await conn.execute(
                    """
                    SELECT limits.requests_per_window,limits.request_window_seconds,
                           rate_window.window_started_at,rate_window.request_count,
                           clock_timestamp() AS checked_at
                    FROM public.owner_limits AS limits
                    JOIN public.owner_request_windows AS rate_window USING (owner_id)
                    WHERE limits.owner_id=%s
                    FOR UPDATE OF rate_window
                    """,
                    (owner_id.value,),
                )
            ).fetchone()
            if row is None:
                raise RuntimeError("owner limit policy is unavailable")
            elapsed = (row["checked_at"] - row["window_started_at"]).total_seconds()
            if elapsed >= row["request_window_seconds"]:
                used = 1
                elapsed = 0
                await conn.execute(
                    """
                    UPDATE public.owner_request_windows
                    SET window_started_at=%s,request_count=1,updated_at=now()
                    WHERE owner_id=%s
                    """,
                    (row["checked_at"], owner_id.value),
                )
            elif row["request_count"] >= row["requests_per_window"]:
                retry = max(1, math.ceil(row["request_window_seconds"] - elapsed))
                raise LimitExceededError(
                    "request_rate_exceeded",
                    row["requests_per_window"],
                    retry,
                    "Retry after the current request window resets.",
                )
            else:
                used = row["request_count"] + 1
                await conn.execute(
                    """
                    UPDATE public.owner_request_windows
                    SET request_count=%s,updated_at=now() WHERE owner_id=%s
                    """,
                    (used, owner_id.value),
                )
        return {
            "limit": row["requests_per_window"],
            "remaining": row["requests_per_window"] - used,
            "reset_after_seconds": max(1, math.ceil(row["request_window_seconds"] - elapsed)),
        }

    async def limit_status(self, owner_id: OwnerId) -> dict:
        async with self._transaction(owner_id) as conn:
            row = await (
                await conn.execute(
                    """
                    SELECT limits.max_upload_bytes,limits.max_library_bytes,limits.max_books,
                           limits.requests_per_window,
                           limits.request_window_seconds,limits.max_provider_concurrency,
                           limits.max_spend_usd,limits.updated_at,
                           (SELECT count(*) FROM public.books AS book
                            WHERE book.owner_id=limits.owner_id AND book.deleted_at IS NULL) AS books,
                           COALESCE((SELECT sum(object.byte_size)
                            FROM public.source_objects AS object
                            WHERE object.owner_id=limits.owner_id AND object.deleted_at IS NULL),0)
                              AS library_bytes,
                           COALESCE((SELECT sum(entry.usd) FROM public.cost_ledger AS entry
                            WHERE entry.owner_id=limits.owner_id),0) AS spend_usd,
                           COALESCE((SELECT sum(reservation.reserved_usd)
                            FROM public.cost_reservations AS reservation
                            WHERE reservation.owner_id=limits.owner_id
                              AND reservation.state='reserved'),0) AS reserved_usd
                    FROM public.owner_limits AS limits WHERE limits.owner_id=%s
                    """,
                    (owner_id.value,),
                )
            ).fetchone()
        if row is None:
            raise RuntimeError("owner limit policy is unavailable")
        return self._json_row(row)

    async def list_books(self, owner_id: OwnerId) -> list[dict]:
        async with self._transaction(owner_id) as conn:
            rows = await (
                await conn.execute(
                    """
                    SELECT b.id,b.title,b.author,b.source_kind AS source,b.content_language,b.book_type,b.created_at,
                           s.last_opened_at,
                           COALESCE(s.bookmark,0) AS bookmark,
                           COALESCE(s.position_epoch,0) AS position_epoch
                    FROM public.books AS b
                    LEFT JOIN public.reading_state AS s
                      ON (s.owner_id,s.book_id,s.book_incarnation)=(b.owner_id,b.id,b.incarnation)
                    WHERE b.owner_id=%s AND b.deleted_at IS NULL
                    ORDER BY s.last_opened_at DESC NULLS LAST,b.created_at,b.id
                    """,
                    (owner_id.value,),
                )
            ).fetchall()
        return [self._book_payload(row) for row in rows]

    async def get_book(self, owner_id: OwnerId, book_id: uuid.UUID) -> dict | None:
        async with self._transaction(owner_id) as conn:
            row = await (
                await conn.execute(
                    """
                    SELECT b.id,b.title,b.author,b.source_kind AS source,b.content_language,b.book_type,b.created_at,
                           s.last_opened_at,
                           COALESCE(s.bookmark,0) AS bookmark,
                           COALESCE(s.position_epoch,0) AS position_epoch
                    FROM public.books AS b
                    LEFT JOIN public.reading_state AS s
                      ON (s.owner_id,s.book_id,s.book_incarnation)=(b.owner_id,b.id,b.incarnation)
                    WHERE b.owner_id=%s AND b.id=%s AND b.deleted_at IS NULL
                    """,
                    (owner_id.value, book_id),
                )
            ).fetchone()
        return self._book_payload(row) if row else None

    async def get_book_manifest(self, owner_id: OwnerId, book_id: uuid.UUID) -> dict | None:
        async with self._transaction(owner_id) as conn:
            book = await (
                await conn.execute(
                    """SELECT book.incarnation,book.content_language,book.book_type,
                              COALESCE(state.bookmark,0) AS bookmark
                       FROM public.books AS book
                       LEFT JOIN public.reading_state AS state
                         ON (state.owner_id,state.book_id,state.book_incarnation)
                          =(book.owner_id,book.id,book.incarnation)
                       WHERE book.owner_id=%s AND book.id=%s AND book.deleted_at IS NULL""",
                    (owner_id.value, book_id),
                )
            ).fetchone()
            if book is None:
                return None
            rows = await (
                await conn.execute(
                    """SELECT ordinal,href,title,part_label,char_end-char_start AS char_len
                       FROM public.book_search_documents
                       WHERE owner_id=%s AND book_id=%s AND book_incarnation=%s
                       ORDER BY ordinal""",
                    (owner_id.value, book_id, book["incarnation"]),
                )
            ).fetchall()
            begun = {
                row["atom"] for row in await (
                    await conn.execute(
                        """SELECT DISTINCT atom FROM public.read_ranges
                           WHERE owner_id=%s AND book_id=%s AND book_incarnation=%s""",
                        (owner_id.value, book_id, book["incarnation"]),
                    )
                ).fetchall()
            }
        # As the local manifest (app/api/books.py): titles up to the chapter being read, and of
        # any chapter the reader has read some of; the chapter numbers are structural.
        released = book["bookmark"] + 1
        full = {"atoms": [dict(row) for row in rows]}
        numbers, places = chapter_numbers(full), content_numbers(full)
        atoms = []
        for row in full["atoms"]:
            shown = row["ordinal"] <= released or row["ordinal"] in begun
            atoms.append({
                "ordinal": row["ordinal"],
                "href": row["href"],
                "title": row["title"] if shown else "",
                "part_label": row["part_label"] if shown else "",
                "char_len": row["char_len"],
                "chapter_number": numbers.get(row["ordinal"]),
                "content_number": places.get(row["ordinal"]),
                "content_role": ("content" if is_content(row) else "supplementary")
                if shown else None,
            })
        identity = "|".join(
            f"{atom['ordinal']}:{atom['href']}:{atom['char_len']}" for atom in atoms
        )
        return {
            "book_id": str(book_id),
            "atom_set_version": hashlib.sha256(identity.encode()).hexdigest()[:16],
            "mode": "indexed",
            "content_language": book["content_language"],
            "book_profile": {
                "book_type": book["book_type"],
                "confidence": 1,
                "detector_version": "hosted-import-v1",
                "signals": [],
            },
            "content_chapters": max(numbers.values(), default=0),
            "content_count": len(places),
            "atoms": atoms,
        }

    async def search_book(
        self, owner_id: OwnerId, book_id: uuid.UUID, query: str, *, limit: int
    ) -> dict | None:
        """Search only completed chapters of one live owned book.

        Reading-state ``bookmark`` is the spoiler frontier: the current chapter is deliberately not
        included because a context excerpt could otherwise reveal prose below the visible viewport.
        """
        async with self._transaction(owner_id) as conn:
            book = await (
                await conn.execute(
                    """SELECT book.incarnation,COALESCE(state.bookmark,0) AS bookmark
                       FROM public.books AS book
                       LEFT JOIN public.reading_state AS state
                         ON (state.owner_id,state.book_id,state.book_incarnation)
                          =(book.owner_id,book.id,book.incarnation)
                       WHERE book.owner_id=%s AND book.id=%s AND book.deleted_at IS NULL""",
                    (owner_id.value, book_id),
                )
            ).fetchone()
            if book is None:
                return None
            rows = await (
                await conn.execute(
                    """WITH search_query AS (
                         SELECT websearch_to_tsquery('simple',%s) AS value
                       )
                       SELECT document.ordinal,document.href,document.title,document.part_label,
                              ts_headline(
                                'simple',document.content,search_query.value,
                                'MaxFragments=1,MinWords=12,MaxWords=36,StartSel=[,StopSel=]'
                              ) AS snippet,
                              ts_rank_cd(document.search_vector,search_query.value) AS score
                       FROM public.book_search_documents AS document
                       CROSS JOIN search_query
                       WHERE document.owner_id=%s AND document.book_id=%s
                         AND document.book_incarnation=%s AND document.ordinal<=%s
                         AND document.search_vector @@ search_query.value
                       ORDER BY score DESC,document.ordinal,document.href
                       LIMIT %s""",
                    (
                        query,
                        owner_id.value,
                        book_id,
                        book["incarnation"],
                        book["bookmark"],
                        limit,
                    ),
                )
            ).fetchall()
        return {
            "as_of_chapter": book["bookmark"],
            "hits": [self._json_row(row) for row in rows],
        }

    @staticmethod
    async def _select_position(conn, owner_id: OwnerId, book_id: uuid.UUID):
        return await (
            await conn.execute(
                """SELECT state.bookmark,state.current_cfi AS cfi,state.position_epoch,
                          state.current_offset,state.high_water_offset,state.position_version,
                          state.last_opened_at,state.updated_at
                   FROM public.books AS book
                   JOIN public.reading_state AS state
                     ON (state.owner_id,state.book_id,state.book_incarnation)
                      =(book.owner_id,book.id,book.incarnation)
                   WHERE book.owner_id=%s AND book.id=%s AND book.deleted_at IS NULL""",
                (owner_id.value, book_id),
            )
        ).fetchone()

    async def get_position(self, owner_id: OwnerId, book_id: uuid.UUID) -> dict | None:
        async with self._transaction(owner_id) as conn:
            touched = await (
                await conn.execute(
                    """UPDATE public.reading_state AS state SET last_opened_at=now()
                       FROM public.books AS book
                       WHERE (book.owner_id,book.id,book.incarnation)
                           =(state.owner_id,state.book_id,state.book_incarnation)
                         AND state.owner_id=%s AND state.book_id=%s
                         AND book.deleted_at IS NULL
                       RETURNING state.book_id""",
                    (owner_id.value, book_id),
                )
            ).fetchone()
            row = await self._select_position(conn, owner_id, book_id) if touched else None
        return self._position_payload(row) if row else None

    async def get_reader_preferences(
        self, owner_id: OwnerId, book_id: uuid.UUID
    ) -> dict | None:
        async with self._transaction(owner_id) as conn:
            row = await (
                await conn.execute(
                    """SELECT COALESCE(preference.font_size,'book') AS font_size,
                              COALESCE(preference.line_height,'comfortable') AS line_height,
                              COALESCE(preference.measure,'balanced') AS measure,
                              COALESCE(preference.theme,'paper') AS theme,
                              COALESCE(preference.margins,'balanced') AS margins,
                              COALESCE(preference.typeface,'publisher') AS typeface,
                              COALESCE(preference.preference_version,0) AS preference_version
                       FROM public.books AS book
                       LEFT JOIN public.reader_preferences AS preference
                         ON (preference.owner_id,preference.book_id,preference.book_incarnation)
                          =(book.owner_id,book.id,book.incarnation)
                       WHERE book.owner_id=%s AND book.id=%s AND book.deleted_at IS NULL""",
                    (owner_id.value, book_id),
                )
            ).fetchone()
        return None if row is None else dict(row)

    async def upsert_reader_preferences(
        self,
        owner_id: OwnerId,
        book_id: uuid.UUID,
        *,
        font_size: str,
        line_height: str,
        measure: str,
        theme: str,
        margins: str,
        typeface: str,
    ) -> dict:
        async with self._transaction(owner_id) as conn:
            book = await (
                await conn.execute(
                    """SELECT incarnation FROM public.books
                       WHERE owner_id=%s AND id=%s AND deleted_at IS NULL
                       FOR SHARE""",
                    (owner_id.value, book_id),
                )
            ).fetchone()
            if book is None:
                raise MissingTenantResourceError
            row = await (
                await conn.execute(
                    """INSERT INTO public.reader_preferences
                         (owner_id,book_id,book_incarnation,font_size,line_height,measure,theme,
                          margins,typeface,preferences,preference_version,updated_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'{}'::jsonb,1,now())
                       ON CONFLICT (owner_id,book_id,book_incarnation) DO UPDATE SET
                         font_size=EXCLUDED.font_size,line_height=EXCLUDED.line_height,
                         measure=EXCLUDED.measure,theme=EXCLUDED.theme,margins=EXCLUDED.margins,
                         typeface=EXCLUDED.typeface,preferences='{}'::jsonb,
                         preference_version=reader_preferences.preference_version+1,
                         updated_at=now()
                       RETURNING font_size,line_height,measure,theme,margins,typeface,
                                 preference_version""",
                    (
                        owner_id.value,
                        book_id,
                        book["incarnation"],
                        font_size,
                        line_height,
                        measure,
                        theme,
                        margins,
                        typeface,
                    ),
                )
            ).fetchone()
        return dict(row)

    @staticmethod
    async def _reader_mark_book(conn, owner_id: OwnerId, book_id: uuid.UUID):
        return await (
            await conn.execute(
                """SELECT book.incarnation,COALESCE(state.bookmark,0) AS bookmark
                   FROM public.books AS book
                   LEFT JOIN public.reading_state AS state
                     ON (state.owner_id,state.book_id,state.book_incarnation)
                      =(book.owner_id,book.id,book.incarnation)
                   WHERE book.owner_id=%s AND book.id=%s AND book.deleted_at IS NULL""",
                (owner_id.value, book_id),
            )
        ).fetchone()

    async def list_reader_marks(
        self, owner_id: OwnerId, book_id: uuid.UUID
    ) -> dict | None:
        """Return only marks at the current spoiler frontier.

        Starting a new reading pass lowers ``bookmark``. Older marks remain durable, but the query
        hides marks from later chapters until that frontier is reached again.
        """
        async with self._transaction(owner_id) as conn:
            book = await self._reader_mark_book(conn, owner_id, book_id)
            if book is None:
                return None
            rows = await (
                await conn.execute(
                    """SELECT * FROM (
                         SELECT 'highlight'::text AS kind,id,anchor,color,selected_text,
                                NULL::text AS body,NULL::text AS label,NULL::uuid AS highlight_id,
                                version,created_at,updated_at
                         FROM public.highlights
                         WHERE owner_id=%s AND book_id=%s AND book_incarnation=%s
                           AND deleted_at IS NULL AND (anchor->>'atom')::integer<=%s
                         UNION ALL
                         SELECT 'annotation',id,anchor,NULL,NULL,body,NULL,highlight_id,
                                version,created_at,updated_at
                         FROM public.annotations
                         WHERE owner_id=%s AND book_id=%s AND book_incarnation=%s
                           AND deleted_at IS NULL AND (anchor->>'atom')::integer<=%s
                         UNION ALL
                         SELECT 'bookmark',id,anchor,NULL,NULL,NULL,label,NULL::uuid,
                                version,created_at,updated_at
                         FROM public.bookmarks
                         WHERE owner_id=%s AND book_id=%s AND book_incarnation=%s
                           AND deleted_at IS NULL AND (anchor->>'atom')::integer<=%s
                       ) AS mark ORDER BY created_at,id""",
                    (
                        owner_id.value, book_id, book["incarnation"], book["bookmark"] + 1,
                        owner_id.value, book_id, book["incarnation"], book["bookmark"] + 1,
                        owner_id.value, book_id, book["incarnation"], book["bookmark"] + 1,
                    ),
                )
            ).fetchall()
        return {
            "as_of_chapter": book["bookmark"],
            "marks": [self._json_row(row) for row in rows],
        }

    async def create_reader_mark(
        self,
        owner_id: OwnerId,
        book_id: uuid.UUID,
        *,
        kind: str,
        anchor: dict,
        color: str | None = None,
        selected_text: str | None = None,
        body: str | None = None,
        label: str | None = None,
        highlight_id: uuid.UUID | None = None,
    ) -> dict:
        async with self._transaction(owner_id) as conn:
            book = await self._reader_mark_book(conn, owner_id, book_id)
            if book is None:
                raise MissingTenantResourceError
            if anchor["atom"] > book["bookmark"] + 1:
                raise InvalidPositionError("mark anchor is beyond the revealed frontier")
            if kind == "highlight":
                row = await (
                    await conn.execute(
                        """INSERT INTO public.highlights
                             (owner_id,book_id,book_incarnation,anchor,color,selected_text)
                           VALUES (%s,%s,%s,%s,%s,%s)
                           RETURNING 'highlight'::text AS kind,id,anchor,color,selected_text,
                                     NULL::text AS body,NULL::text AS label,
                                     NULL::uuid AS highlight_id,version,
                                     created_at,updated_at""",
                        (
                            owner_id.value, book_id, book["incarnation"],
                            psycopg.types.json.Jsonb(anchor), color, selected_text,
                        ),
                    )
                ).fetchone()
            elif kind == "annotation":
                if highlight_id is not None:
                    linked = await (
                        await conn.execute(
                            """SELECT id FROM public.highlights
                               WHERE owner_id=%s AND book_id=%s AND book_incarnation=%s
                                 AND id=%s AND deleted_at IS NULL""",
                            (owner_id.value, book_id, book["incarnation"], highlight_id),
                        )
                    ).fetchone()
                    if linked is None:
                        raise MissingTenantResourceError
                row = await (
                    await conn.execute(
                        """INSERT INTO public.annotations
                             (owner_id,book_id,book_incarnation,highlight_id,anchor,body)
                           VALUES (%s,%s,%s,%s,%s,%s)
                           RETURNING 'annotation'::text AS kind,id,anchor,NULL::text AS color,
                                     NULL::text AS selected_text,body,NULL::text AS label,
                                     highlight_id,version,
                                     created_at,updated_at""",
                        (
                            owner_id.value, book_id, book["incarnation"], highlight_id,
                            psycopg.types.json.Jsonb(anchor), body,
                        ),
                    )
                ).fetchone()
            elif kind == "bookmark":
                row = await (
                    await conn.execute(
                        """INSERT INTO public.bookmarks
                             (owner_id,book_id,book_incarnation,anchor,label)
                           VALUES (%s,%s,%s,%s,%s)
                           RETURNING 'bookmark'::text AS kind,id,anchor,NULL::text AS color,
                                     NULL::text AS selected_text,NULL::text AS body,label,
                                     NULL::uuid AS highlight_id,version,
                                     created_at,updated_at""",
                        (
                            owner_id.value, book_id, book["incarnation"],
                            psycopg.types.json.Jsonb(anchor), label,
                        ),
                    )
                ).fetchone()
            else:
                raise ValueError("unknown reader mark kind")
        return self._json_row(row)

    async def update_reader_mark(
        self,
        owner_id: OwnerId,
        book_id: uuid.UUID,
        mark_id: uuid.UUID,
        *,
        kind: str,
        value: str,
    ) -> dict | None:
        columns = {"highlight": "color", "annotation": "body", "bookmark": "label"}
        tables = {
            "highlight": "highlights",
            "annotation": "annotations",
            "bookmark": "bookmarks",
        }
        returning = {
            "highlight": (
                "color,selected_text,NULL::text AS body,NULL::text AS label,"
                "NULL::uuid AS highlight_id"
            ),
            "annotation": (
                "NULL::text AS color,NULL::text AS selected_text,body,"
                "NULL::text AS label,highlight_id"
            ),
            "bookmark": (
                "NULL::text AS color,NULL::text AS selected_text,NULL::text AS body,"
                "label,NULL::uuid AS highlight_id"
            ),
        }
        column = columns[kind]
        table = tables[kind]
        async with self._transaction(owner_id) as conn:
            book = await self._reader_mark_book(conn, owner_id, book_id)
            if book is None:
                return None
            row = await (
                await conn.execute(
                    f"""UPDATE public.{table} SET {column}=%s,version=version+1,updated_at=now()
                        WHERE owner_id=%s AND book_id=%s AND book_incarnation=%s
                          AND id=%s AND deleted_at IS NULL
                        RETURNING id,anchor,{returning[kind]},version,created_at,updated_at""",
                    (value, owner_id.value, book_id, book["incarnation"], mark_id),
                )
            ).fetchone()
        if row is None:
            return None
        payload = self._json_row(row)
        payload["kind"] = kind
        return payload

    async def delete_reader_mark(
        self,
        owner_id: OwnerId,
        book_id: uuid.UUID,
        mark_id: uuid.UUID,
        *,
        kind: str,
    ) -> bool:
        tables = {"highlight": "highlights", "annotation": "annotations", "bookmark": "bookmarks"}
        async with self._transaction(owner_id) as conn:
            book = await self._reader_mark_book(conn, owner_id, book_id)
            if book is None:
                return False
            row = await (
                await conn.execute(
                    f"""UPDATE public.{tables[kind]} SET deleted_at=now(),updated_at=now(),
                               version=version+1
                        WHERE owner_id=%s AND book_id=%s AND book_incarnation=%s
                          AND id=%s AND deleted_at IS NULL RETURNING id""",
                    (owner_id.value, book_id, book["incarnation"], mark_id),
                )
            ).fetchone()
        return row is not None

    async def update_position(
        self,
        owner_id: OwnerId,
        book_id: uuid.UUID,
        *,
        cfi: str,
        offset: int,
        completed_chapter: int,
        expected_epoch: int,
        base_version: int,
        client_id: uuid.UUID,
        client_sequence: int,
        move_counter: bool = True,
    ) -> dict:
        """Keep the reader's place. ``completed_chapter`` raises the chapter counter only with
        ``move_counter``; without it (the read map, which moves the counter itself) it is checked
        against the book and otherwise leaves the counter as it is."""
        async with self._transaction(owner_id) as conn:
            state = await (
                await conn.execute(
                    """SELECT state.book_incarnation,state.bookmark,state.current_offset,
                              state.high_water_offset,state.position_epoch,state.position_version,
                              state.last_client_id,state.last_client_sequence
                       FROM public.reading_state AS state
                       JOIN public.books AS book
                         ON (book.owner_id,book.id,book.incarnation)
                          =(state.owner_id,state.book_id,state.book_incarnation)
                       WHERE state.owner_id=%s AND state.book_id=%s
                         AND book.deleted_at IS NULL
                       FOR UPDATE OF state""",
                    (owner_id.value, book_id),
                )
            ).fetchone()
            if state is None:
                raise MissingTenantResourceError
            if state["position_epoch"] != expected_epoch:
                raise StalePositionEpochError
            if base_version > state["position_version"]:
                raise FuturePositionVersionError
            # The book's chapters as its manifest lists them (written at import). Its story
            # memory is an edition's, which a reader's session cannot read (ADR 0044 step 7).
            maximum = await (
                await conn.execute(
                    """SELECT COALESCE(max(ordinal),0) AS maximum
                       FROM public.book_search_documents
                       WHERE owner_id=%s AND book_id=%s AND book_incarnation=%s""",
                    (owner_id.value, book_id, state["book_incarnation"]),
                )
            ).fetchone()
            if completed_chapter > maximum["maximum"]:
                raise InvalidPositionError("completed chapter is outside the current book")
            if not move_counter:
                completed_chapter = min(completed_chapter, state["bookmark"])

            stale_base = base_version < state["position_version"]
            incoming_writer = (client_id.hex, client_sequence)
            current_writer = (
                state["last_client_id"].hex if state["last_client_id"] else "",
                state["last_client_sequence"] or 0,
            )
            advances = (
                completed_chapter > state["bookmark"]
                or offset > state["high_water_offset"]
            )
            wins_tie = (
                completed_chapter == state["bookmark"]
                and offset == state["high_water_offset"]
                and incoming_writer > current_writer
            )
            applied = not stale_base or advances or wins_tie
            if not stale_base:
                conflict = None
            elif advances:
                conflict = "merged_advance"
            elif wins_tie:
                conflict = "tie_won"
            elif completed_chapter < state["bookmark"] or offset < state["high_water_offset"]:
                conflict = "stale_behind"
            else:
                conflict = "tie_lost"

            if applied:
                await conn.execute(
                    """UPDATE public.reading_state
                       SET bookmark=GREATEST(bookmark,%s),
                           current_cfi=CASE WHEN NOT %s OR %s>=current_offset THEN %s
                                            ELSE current_cfi END,
                           current_offset=CASE WHEN %s THEN GREATEST(current_offset,%s)
                                               ELSE %s END,
                           high_water_cfi=CASE WHEN %s>=high_water_offset THEN %s
                                               ELSE high_water_cfi END,
                           high_water_offset=GREATEST(high_water_offset,%s),
                           position_version=position_version+1,
                           last_client_id=%s,last_client_sequence=%s,
                           updated_at=now(),last_opened_at=now()
                       WHERE owner_id=%s AND book_id=%s""",
                    (
                        completed_chapter,
                        stale_base,
                        offset,
                        cfi,
                        stale_base,
                        offset,
                        offset,
                        offset,
                        cfi,
                        offset,
                        client_id,
                        client_sequence,
                        owner_id.value,
                        book_id,
                    ),
                )
            else:
                await conn.execute(
                    """UPDATE public.reading_state SET last_opened_at=now()
                       WHERE owner_id=%s AND book_id=%s""",
                    (owner_id.value, book_id),
                )
            row = await self._select_position(conn, owner_id, book_id)
        return self._position_payload(row, applied=applied, conflict=conflict)

    async def reset_position(
        self, owner_id: OwnerId, book_id: uuid.UUID, expected_epoch: int
    ) -> dict:
        async with self._transaction(owner_id) as conn:
            current = await (
                await conn.execute(
                    """
                    SELECT state.position_epoch
                    FROM public.reading_state AS state
                    JOIN public.books AS book
                      ON (book.owner_id,book.id,book.incarnation)
                       =(state.owner_id,state.book_id,state.book_incarnation)
                    WHERE state.owner_id=%s AND state.book_id=%s AND book.deleted_at IS NULL
                    FOR UPDATE OF state
                    """,
                    (owner_id.value, book_id),
                )
            ).fetchone()
            if current is None:
                raise MissingTenantResourceError
            if current["position_epoch"] != expected_epoch:
                raise StalePositionEpochError
            row = await (
                await conn.execute(
                    """
                    UPDATE public.reading_state
                    SET bookmark=0,high_water_cfi=NULL,current_cfi=NULL,
                        current_offset=0,high_water_offset=0,
                        position_epoch=position_epoch+1,position_version=position_version+1,
                        last_client_id=NULL,last_client_sequence=NULL,
                        updated_at=now(),last_opened_at=now()
                    WHERE owner_id=%s AND book_id=%s AND position_epoch=%s
                      AND position_epoch<9223372036854775807
                    RETURNING book_id
                    """,
                    (owner_id.value, book_id, expected_epoch),
                )
            ).fetchone()
            if row is None:
                raise StalePositionEpochError
            position = await self._select_position(conn, owner_id, book_id)
        return self._position_payload(position, applied=True, conflict=None)

    async def list_costs(self, owner_id: OwnerId, book_id: uuid.UUID | None = None) -> dict | None:
        async with self._transaction(owner_id) as conn:
            if book_id is not None:
                exists = await (
                    await conn.execute(
                        "SELECT 1 FROM public.books WHERE owner_id=%s AND id=%s "
                        "AND deleted_at IS NULL",
                        (owner_id.value, book_id),
                    )
                ).fetchone()
                if exists is None:
                    return None
            rows = await (
                await conn.execute(
                    """
                    SELECT id,book_id,phase,provider,model,input_tokens,output_tokens,usd,recorded_at
                    FROM public.cost_ledger
                    WHERE owner_id=%s AND (%s::uuid IS NULL OR book_id=%s::uuid)
                    ORDER BY recorded_at,id
                    """,
                    (owner_id.value, book_id, book_id),
                )
            ).fetchall()
        payload = [self._json_row(row) for row in rows]
        return {
            "items": payload,
            "total_input_tokens": sum(item["input_tokens"] or 0 for item in payload),
            "total_output_tokens": sum(item["output_tokens"] or 0 for item in payload),
            "total_usd": str(sum((row["usd"] or 0 for row in rows), start=0)),
        }

    @staticmethod
    async def _admit_book(
        conn: psycopg.AsyncConnection, owner_id: OwnerId, byte_size: int
    ) -> None:
        """Raise ``LimitExceededError`` when one more book of ``byte_size`` bytes would exceed this
        owner's limits: the one check an upload and a free-library add both meet."""
        limits = await (
            await conn.execute(
                """
                SELECT max_upload_bytes,max_library_bytes,max_books
                FROM public.owner_limits WHERE owner_id=%s
                """,
                (owner_id.value,),
            )
        ).fetchone()
        if limits is None:
            raise RuntimeError("owner limit policy is unavailable")
        usage = await (
            await conn.execute(
                """
                SELECT
                  (SELECT count(*) FROM public.books
                   WHERE owner_id=%s AND deleted_at IS NULL) AS books,
                  COALESCE((SELECT sum(byte_size) FROM public.source_objects
                   WHERE owner_id=%s AND deleted_at IS NULL),0) AS library_bytes
                """,
                (owner_id.value, owner_id.value),
            )
        ).fetchone()
        assert usage is not None
        if byte_size > limits["max_upload_bytes"]:
            raise LimitExceededError(
                "upload_size_exceeded",
                limits["max_upload_bytes"],
                None,
                "Choose a smaller EPUB or ask an operator to raise the upload limit.",
            )
        if usage["books"] >= limits["max_books"]:
            raise LimitExceededError(
                "book_quota_exceeded",
                limits["max_books"],
                None,
                "Delete an existing book or ask an operator to raise the book limit.",
            )
        if usage["library_bytes"] + byte_size > limits["max_library_bytes"]:
            raise LimitExceededError(
                "library_storage_exceeded",
                limits["max_library_bytes"],
                None,
                "Delete stored books or ask an operator to raise the storage limit.",
            )

    async def admit_book(self, owner_id: OwnerId) -> None:
        """The upload's limit check for a book of no size, before anything is fetched: a reader
        whose shelf is already full is refused without a request to the free library's source.
        ``create_uploaded_book`` checks again, with the real size, under the owner lock."""
        async with self._transaction(owner_id) as conn:
            await self._admit_book(conn, owner_id, 0)

    async def shelved_classics(self, owner_id: OwnerId) -> dict[str, str]:
        """{free-library tag: book id} for this owner's live books added from the library (and
        local books migrated with their library tag), oldest first wins."""
        async with self._transaction(owner_id) as conn:
            rows = await (
                await conn.execute(
                    """
                    SELECT source_kind,id FROM public.books
                    WHERE owner_id=%s AND deleted_at IS NULL AND source_kind LIKE 'gutenberg:%%'
                    ORDER BY created_at DESC,id DESC
                    """,
                    (owner_id.value,),
                )
            ).fetchall()
        return {row["source_kind"]: str(row["id"]) for row in rows}

    async def create_uploaded_book(
        self,
        owner_id: OwnerId,
        *,
        book_id: uuid.UUID,
        incarnation: uuid.UUID,
        title: str,
        author: str | None,
        file_hash: str,
        split_hash: str,
        content_language: str,
        book_type: str,
        object_id: uuid.UUID,
        storage_provider: str,
        media_type: str,
        byte_size: int,
        encryption: str,
        search_documents: list[dict] | None = None,
        source_kind: str = "upload",
    ) -> dict:
        """Store one book's metadata, search text and source object row. The book is readable and
        chattable at once, on an empty story memory; no job is made for it here (story memory
        jobs follow the reader's reading, ADR 0042).

        ``source_kind`` is ``upload`` for a reader's own file, or the free-library entry's tag
        (``gutenberg:<number>``, the local catalog's ``source``) for a book added from the library;
        a second live book from the same entry is refused with ``AlreadyOnShelfError``, as the
        local library refuses it, under the same owner lock as the limits.

        ``split_hash`` is how the file was split into chapters (``split_hash_of``, ADR 0044
        section 1); with ``file_hash`` it is what the door matches an edition on."""
        async with self._transaction(owner_id) as conn:
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s::text,47))",
                (owner_id.value,),
            )
            await self._admit_book(conn, owner_id, byte_size)
            if source_kind != "upload":
                existing = await (
                    await conn.execute(
                        """
                        SELECT id FROM public.books
                        WHERE owner_id=%s AND source_kind=%s AND deleted_at IS NULL
                        ORDER BY created_at,id LIMIT 1
                        """,
                        (owner_id.value, source_kind),
                    )
                ).fetchone()
                if existing is not None:
                    raise AlreadyOnShelfError(existing["id"])
            row = await (
                await conn.execute(
                    """
                    INSERT INTO public.books
                      (owner_id,id,incarnation,title,author,source_kind,file_hash,split_hash,
                       schema_version,content_language,book_type)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,1,%s,%s)
                    RETURNING id,title,author,content_language,book_type,created_at
                    """,
                    (
                        owner_id.value,
                        book_id,
                        incarnation,
                        title,
                        author,
                        source_kind,
                        file_hash,
                        split_hash,
                        content_language,
                        book_type,
                    ),
                )
            ).fetchone()
            await conn.execute(
                """
                INSERT INTO public.reading_state
                  (owner_id,book_id,book_incarnation,bookmark,position_epoch)
                VALUES (%s,%s,%s,0,0)
                """,
                (owner_id.value, book_id, incarnation),
            )
            await conn.execute(
                """
                INSERT INTO public.source_objects
                  (owner_id,id,book_id,book_incarnation,storage_provider,storage_key,media_type,
                   byte_size,sha256,encryption_key_id,verified_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now())
                """,
                (
                    owner_id.value,
                    object_id,
                    book_id,
                    incarnation,
                    storage_provider,
                    object_id.hex,
                    media_type,
                    byte_size,
                    file_hash,
                    encryption,
                ),
            )
            if search_documents:
                await conn.execute(
                    """INSERT INTO public.book_search_documents
                         (owner_id,book_id,book_incarnation,ordinal,href,title,part_label,content,
                          char_start,char_end)
                       SELECT %s,%s,%s,document.ordinal,document.href,document.title,
                              document.part_label,document.content,document.char_start,
                              document.char_end
                       FROM jsonb_to_recordset(%s::jsonb) AS document(
                         ordinal integer,href text,title text,part_label text,content text,
                         char_start bigint,char_end bigint
                       )""",
                    (
                        owner_id.value,
                        book_id,
                        incarnation,
                        psycopg.types.json.Jsonb(search_documents),
                    ),
                )
            await record_event_async(
                conn,
                owner_id=owner_id.value,
                actor_kind="owner",
                action="book.import",
                target_kind="book",
                target_id=book_id,
                result="succeeded",
            )
        payload = self._book_payload(row)
        payload.update(bookmark=0, position_epoch=0)
        return payload

    async def cancel_owner_jobs(self, owner_id: OwnerId) -> int:
        """Every job of this reader that waits or runs, of every kind, as removing a book does
        for one book: waiting ones end now, running ones are asked to stop and the worker records
        them cancelled (ADR 0041 part 8, asking to delete the account)."""
        async with self._transaction(owner_id) as conn:
            cursor = await conn.execute(
                """
                UPDATE public.jobs
                SET cancellation_requested_at=COALESCE(cancellation_requested_at,now()),
                    state=CASE WHEN state='pending' THEN 'cancelled' ELSE state END,
                    completed_at=CASE WHEN state='pending' THEN now() ELSE completed_at END,
                    updated_at=now()
                WHERE owner_id=%s
                  AND state IN ('pending','leased','running')
                """,
                (owner_id.value,),
            )
            return cursor.rowcount

    async def record_account_export(self, owner_id: OwnerId) -> None:
        """A content-free audit line: this reader took a copy of their data."""
        async with self._transaction(owner_id) as conn:
            await record_event_async(
                conn,
                owner_id=owner_id.value,
                actor_kind="owner",
                action="account.export",
                target_kind="account",
                target_id=None,
                result="succeeded",
            )

    async def source_object(
        self, owner_id: OwnerId, book_id: uuid.UUID
    ) -> SourceObjectRecord | None:
        async with self._transaction(owner_id) as conn:
            row = await (
                await conn.execute(
                    """
                    SELECT source.book_id,source.book_incarnation,source.storage_key,
                           source.storage_provider,source.media_type,source.byte_size,source.sha256,
                           source.encryption_key_id
                    FROM public.source_objects AS source
                    JOIN public.books AS book
                      ON (book.owner_id,book.id,book.incarnation)
                       =(source.owner_id,source.book_id,source.book_incarnation)
                    WHERE source.owner_id=%s AND source.book_id=%s
                      AND source.deleted_at IS NULL AND book.deleted_at IS NULL
                    """,
                    (owner_id.value, book_id),
                )
            ).fetchone()
        if row is None:
            return None
        try:
            object_id = uuid.UUID(hex=row["storage_key"])
        except (ValueError, TypeError) as exc:
            raise RuntimeError("stored source object identity is invalid") from exc
        return SourceObjectRecord(
            book_id=row["book_id"],
            book_incarnation=row["book_incarnation"],
            object_id=object_id,
            provider=row["storage_provider"],
            media_type=row["media_type"],
            byte_size=row["byte_size"],
            sha256=row["sha256"],
            encryption=row["encryption_key_id"],
        )

    async def remove_book(self, owner_id: OwnerId, book_id: uuid.UUID) -> bool:
        """Take a book off the reader's shelf and erase what was about it, in one transaction:
        their place and read map, highlights, notes and bookmarks, their conversation in it and
        how far it was looked at for notes, Mothli's chapter notes, what Mothli remembers from it,
        the home chat's messages that leaned on it, its search text, and the record of its stored
        file (the route deletes the file itself first). What stays is the book's row as a bare
        id with its fingerprints cleared, its cancelled jobs, what its replies cost and the audit
        trail (ADR 0041 part 8). The book's notes are its edition's, not the reader's, and stay
        for whoever else has the file (ADR 0044 section 6). False when it is not this reader's
        live book."""
        async with self._transaction(owner_id) as conn:
            # The companion's locks for this book and for the reader's notes, before the book's
            # row: a read-map write or a batch of notes under way finishes before this erases,
            # never after, and neither waits on the row while holding its lock.
            for key in (str(book_id), "recollections"):
                await conn.execute(
                    "SELECT pg_advisory_xact_lock("
                    "hashtextextended(%s::text || ':' || %s::text, 48))",
                    (owner_id.value, key),
                )
            row = await (
                await conn.execute(
                    """
                    SELECT incarnation FROM public.books
                    WHERE owner_id=%s AND id=%s AND deleted_at IS NULL
                    FOR UPDATE
                    """,
                    (owner_id.value, book_id),
                )
            ).fetchone()
            if row is None:
                await record_event_async(
                    conn,
                    owner_id=owner_id.value,
                    actor_kind="owner",
                    action="book.delete",
                    target_kind="book",
                    target_id=book_id,
                    result="denied",
                    reason_code="not_found",
                )
                return False
            await conn.execute(
                """
                UPDATE public.jobs
                SET cancellation_requested_at=COALESCE(cancellation_requested_at,now()),
                    state=CASE WHEN state='pending' THEN 'cancelled' ELSE state END,
                    completed_at=CASE WHEN state='pending' THEN now() ELSE completed_at END,
                    updated_at=now()
                WHERE owner_id=%s AND book_id=%s AND book_incarnation=%s
                  AND state IN ('pending','leased','running')
                """,
                (owner_id.value, book_id, row["incarnation"]),
            )
            await record_event_async(
                conn,
                owner_id=owner_id.value,
                actor_kind="owner",
                action="book.delete",
                target_kind="book",
                target_id=book_id,
                result="succeeded",
            )
            # Every shelf lifetime of this book id: a book id is never reused.
            scope = (owner_id.value, book_id)
            for table in ERASED_WITH_THE_BOOK:
                await conn.execute(
                    f"DELETE FROM public.{table} WHERE owner_id=%s AND book_id=%s", scope
                )
            # What Mothli remembers from this book's chat goes (above); a note about the reader
            # stays, but not the message it came from when that message leaned on this book: it
            # can never be shown again (``written_from_read``), like the conversation it was in.
            # The home chat's messages that leaned on this book cannot be shown again either.
            book_key = str(book_id)
            await conn.execute(
                """UPDATE public.recollections SET said='',drew_on=NULL
                   WHERE owner_id=%s AND book_id IS NULL AND drew_on ? %s""",
                (owner_id.value, book_key),
            )
            await conn.execute(
                """DELETE FROM public.conversation_turns
                   WHERE owner_id=%s AND book_id IS NULL AND drew_on ? %s""",
                (owner_id.value, book_key),
            )
            await conn.execute(
                "DELETE FROM public.recollection_cursors WHERE owner_id=%s AND conversation=%s",
                (owner_id.value, book_key),
            )
            # The book's row stays, as a bare id: what its replies cost (cost_ledger,
            # cost_reservations) and its cancelled jobs still refer to it.
            await conn.execute(
                """
                UPDATE public.books
                SET deleted_at=now(),updated_at=now(),title='',author=NULL,source_kind=NULL,
                    source_id=NULL,file_hash=NULL,split_hash=NULL
                WHERE owner_id=%s AND id=%s AND incarnation=%s AND deleted_at IS NULL
                """,
                (owner_id.value, book_id, row["incarnation"]),
            )
        return True

    @staticmethod
    def _book_payload(row: dict) -> dict:
        """A book as the shelf reads it: ``book_id``, the name local mode and the app use for it,
        beside ``id``, which hosted callers already read."""
        payload = dict(row)
        payload["id"] = payload["book_id"] = str(payload["id"])
        payload["created_at"] = payload["created_at"].isoformat()
        if payload.get("last_opened_at") is not None:
            payload["last_opened_at"] = payload["last_opened_at"].isoformat()
        return payload

    @classmethod
    def _position_payload(
        cls, row: dict, *, applied: bool | None = None, conflict: str | None = None
    ) -> dict:
        payload = cls._json_row(row)
        payload["completed_chapter"] = payload["bookmark"]
        if applied is not None:
            payload["applied"] = applied
            payload["conflict"] = conflict
        return payload

    @staticmethod
    def _json_row(row: dict) -> dict:
        payload = dict(row)
        for key, value in tuple(payload.items()):
            if isinstance(value, uuid.UUID):
                payload[key] = str(value)
            elif hasattr(value, "isoformat"):
                payload[key] = value.isoformat()
            elif (key == "usd" or key.endswith("_usd")) and value is not None:
                payload[key] = str(value)
        return payload

