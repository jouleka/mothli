"""Real-PostgreSQL acceptance tests for the hosted persistence foundation (LIT-38)."""

from __future__ import annotations

import os
import uuid

import psycopg
import pytest
from psycopg import conninfo

from app.hosted.migrations import apply_migrations, discover_migrations


pytestmark = pytest.mark.postgres


@pytest.fixture(scope="module")
def admin_dsn() -> str:
    dsn = os.environ.get("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN is required for the real PostgreSQL suite")
    return dsn


@pytest.fixture()
def database(admin_dsn: str):
    database_name = f"lit38_{uuid.uuid4().hex}"
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{database_name}"')
    dsn = conninfo.make_conninfo(admin_dsn, dbname=database_name)
    try:
        yield dsn
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (database_name,),
            )
            admin.execute(f'DROP DATABASE "{database_name}"')


def test_migrations_are_contiguous_forward_only_files() -> None:
    migrations = discover_migrations()
    assert [migration.version for migration in migrations] == list(range(1, len(migrations) + 1))
    assert all("BEGIN" not in migration.sql.upper() for migration in migrations)
    assert all("COMMIT" not in migration.sql.upper() for migration in migrations)


def test_empty_database_is_reproducible_and_pgvector_enabled(database: str) -> None:
    apply_migrations(database)
    apply_migrations(database)

    with psycopg.connect(database) as conn:
        assert conn.execute(
            "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
        ).fetchone()
        rows = conn.execute(
            "SELECT version, name, checksum FROM app_schema_migrations ORDER BY version"
        ).fetchall()

    migrations = discover_migrations()
    assert [(row[0], row[1], row[2]) for row in rows] == [
        (migration.version, migration.name, migration.checksum) for migration in migrations
    ]


def test_applied_migration_checksum_and_history_are_immutable(database: str) -> None:
    apply_migrations(database)
    with psycopg.connect(database) as conn:
        conn.execute("UPDATE app_schema_migrations SET checksum = repeat('0', 64) WHERE version = 1")
    with pytest.raises(RuntimeError, match="differs from committed file"):
        apply_migrations(database)

    with psycopg.connect(database) as conn:
        migration = discover_migrations()[0]
        conn.execute(
            "UPDATE app_schema_migrations SET checksum = %s WHERE version = 1",
            (migration.checksum,),
        )
        conn.execute("DELETE FROM app_schema_migrations WHERE version = 2")
    with pytest.raises(RuntimeError, match="not a contiguous prefix"):
        apply_migrations(database)


def test_every_tenant_table_has_non_null_owner_and_rls(database: str) -> None:
    apply_migrations(database)
    service_tables = {"app_schema_migrations", "oidc_login_attempts",
                      "platform_spend_days", "provider_pauses", "notes_account"}
    expected_tables = {
        "users",
        "external_identities",
        "sessions",
        "books",
        "source_objects",
        "reading_state",
        "reader_preferences",
        "book_search_documents",
        "chapters",
        "ingested_chapters",
        "chapter_summaries",
        "entities",
        "aliases",
        "edges",
        "events",
        "event_participants",
        "themes",
        "entity_state",
        "entity_corrections",
        "chunks",
        "chunk_embeddings",
        "jobs",
        "job_attempts",
        "cost_ledger",
        "cost_reservations",
        "highlights",
        "annotations",
        "bookmarks",
        "audit_events",
        "chapter_notes",
        "recollections",
        "recollection_cursors",
        "name_words",
    }

    with psycopg.connect(database) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
            )
        }
        tenant_tables = tables - service_tables - {"users"}
        nullable_owner_columns = conn.execute(
            """
            SELECT table_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND column_name = 'owner_id'
              AND is_nullable <> 'NO'
            """
        ).fetchall()
        owner_tables = {
            row[0]
            for row in conn.execute(
                """
                SELECT table_name
                FROM information_schema.columns
                WHERE table_schema = 'public' AND column_name = 'owner_id'
                """
            )
        }
        rls_tables = {
            row[0]
            for row in conn.execute(
                "SELECT relname FROM pg_class WHERE relnamespace = 'public'::regnamespace "
                "AND relkind = 'r' AND relrowsecurity"
            )
        }

    assert expected_tables <= tables
    assert tenant_tables <= owner_tables
    assert nullable_owner_columns == []
    assert tenant_tables <= rls_tables


def test_reading_state_has_monotonic_cross_device_clock(database: str) -> None:
    apply_migrations(database)
    with psycopg.connect(database) as conn:
        columns = {
            row[0]: (row[1], row[2])
            for row in conn.execute(
                """SELECT column_name,is_nullable,column_default
                   FROM information_schema.columns
                   WHERE table_schema='public' AND table_name='reading_state'"""
            )
        }
    for name in (
        "current_offset",
        "high_water_offset",
        "position_version",
        "last_opened_at",
        "last_client_id",
        "last_client_sequence",
    ):
        assert name in columns
    assert all(columns[name][0] == "NO" for name in (
        "current_offset", "high_water_offset", "position_version", "last_opened_at"
    ))


def test_reader_preferences_are_constrained_and_owner_scoped(database: str) -> None:
    apply_migrations(database)
    with psycopg.connect(database) as conn:
        columns = {
            row[0]
            for row in conn.execute(
                """SELECT column_name FROM information_schema.columns
                   WHERE table_schema='public' AND table_name='reader_preferences'"""
            )
        }
        rls = conn.execute(
            "SELECT relrowsecurity,relforcerowsecurity FROM pg_class "
            "WHERE oid='reader_preferences'::regclass"
        ).fetchone()
    assert {
        "owner_id",
        "font_size",
        "line_height",
        "measure",
        "theme",
        "margins",
        "typeface",
        "preference_version",
        "updated_at",
    } <= columns
    assert rls == (True, True)


def test_book_search_documents_have_generated_index_and_forced_rls(database: str) -> None:
    apply_migrations(database)
    with psycopg.connect(database) as conn:
        columns = {
            row[0]: (row[1], row[2])
            for row in conn.execute(
                """SELECT column_name,is_generated,generation_expression
                   FROM information_schema.columns
                   WHERE table_schema='public' AND table_name='book_search_documents'"""
            )
        }
        rls = conn.execute(
            "SELECT relrowsecurity,relforcerowsecurity FROM pg_class "
            "WHERE oid='book_search_documents'::regclass"
        ).fetchone()
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT indexname FROM pg_indexes WHERE tablename='book_search_documents'"
            )
        }
    assert columns["search_vector"][0] == "ALWAYS"
    assert "to_tsvector" in columns["search_vector"][1]
    assert "ix_book_search_documents_owner_vector" in indexes
    assert rls == (True, True)


def test_reader_marks_require_portable_bounded_anchors_and_versions(database: str) -> None:
    apply_migrations(database)
    with psycopg.connect(database) as conn:
        columns = {
            (row[0], row[1])
            for row in conn.execute(
                """SELECT table_name,column_name FROM information_schema.columns
                   WHERE table_schema='public'
                     AND table_name IN ('highlights','annotations','bookmarks')"""
            )
        }
        constraints = {
            row[0]
            for row in conn.execute(
                """SELECT conname FROM pg_constraint
                   WHERE conrelid IN ('highlights'::regclass,'annotations'::regclass,
                                      'bookmarks'::regclass)"""
            )
        }
    assert all((table, "version") in columns for table in (
        "highlights", "annotations", "bookmarks"
    ))
    assert {
        "highlights_anchor_portable",
        "annotations_anchor_portable",
        "bookmarks_anchor_portable",
        "highlights_text_bounded",
        "annotations_body_bounded",
        "bookmarks_label_bounded",
    } <= constraints


def test_source_object_rows_require_opaque_encrypted_verified_epub_metadata(database: str) -> None:
    apply_migrations(database)
    owner = uuid.uuid4()
    book = uuid.uuid4()
    incarnation = uuid.uuid4()
    base = {
        "provider": "filesystem",
        "media_type": "application/epub+zip",
        "byte_size": 4,
        "encryption": "AES-256-GCM",
        "verified": True,
    }
    statement = """
        INSERT INTO source_objects
          (owner_id,id,book_id,book_incarnation,storage_provider,storage_key,media_type,
           byte_size,sha256,encryption_key_id,verified_at)
        VALUES (%(owner)s,%(id)s,%(book)s,%(incarnation)s,%(provider)s,%(storage_key)s,
                %(media_type)s,%(byte_size)s,%(sha256)s,%(encryption)s,
                CASE WHEN %(verified)s THEN now() ELSE NULL END)
    """
    with psycopg.connect(database, autocommit=True) as conn:
        conn.execute("INSERT INTO users (id,display_name) VALUES (%s,'Owner')", (owner,))
        conn.execute(
            """
            INSERT INTO books (owner_id,id,incarnation,title,schema_version)
            VALUES (%s,%s,%s,'Book',1)
            """,
            (owner, book, incarnation),
        )
        for override in (
            {"provider": "client-path"},
            {"storage_key": "owners/client/supplied.epub"},
            {"media_type": "application/octet-stream"},
            {"byte_size": 0},
            {"encryption": None},
            {"verified": False},
        ):
            object_id = uuid.uuid4()
            values = {
                **base,
                "storage_key": object_id.hex,
                **override,
                "owner": owner,
                "id": object_id,
                "book": book,
                "incarnation": incarnation,
                "sha256": "a" * 64,
            }
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(statement, values)

        valid_id = uuid.uuid4()
        valid = {
            **base,
            "storage_key": valid_id.hex,
            "owner": owner,
            "id": valid_id,
            "book": book,
            "incarnation": incarnation,
            "sha256": "b" * 64,
        }
        conn.execute(statement, valid)
        duplicate_id = uuid.uuid4()
        duplicate = {**valid, "id": duplicate_id, "storage_key": duplicate_id.hex}
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(statement, duplicate)


def test_tenant_foreign_keys_and_indexes_carry_owner_scope(database: str) -> None:
    apply_migrations(database)
    reviewed_non_owner_first_indexes = {
        "external_identities_issuer_subject_key",
        "sessions_session_digest_key",
        # Privileged worker scheduler scans all tenants; every mutation remains owner-qualified.
        "ix_jobs_global_claim",
        # PostgreSQL's built-in GIN tsvector operator class cannot prefix UUID owner_id. Queries
        # still require owner/book/incarnation predicates and forced RLS before heap rows are visible.
        "ix_book_search_documents_owner_vector",
        # ADR 0044 step 3: the build slot is one chapter of one edition across every reader, so
        # it cannot be per owner. Only the worker reads or writes it (the claim and the run's
        # own job); a reader's session sees only their own jobs (forced RLS).
        "ux_jobs_one_build_per_chapter",
        # Migration 0036: the budget of every reader's background work together is one sum per
        # UTC day across owners, read only by its security-definer function, which answers yes
        # or no; a reader's own background spend is read through the owner-first indexes.
        "ix_cost_ledger_background_time",
        "ix_cost_reservations_background_open",
    }
    with psycopg.connect(database) as conn:
        foreign_keys = conn.execute(
            """
            SELECT child.relname, constraint_row.conname,
                   array_agg(child_column.attname ORDER BY key_column.ordinality)
            FROM pg_constraint AS constraint_row
            JOIN pg_class AS child ON child.oid = constraint_row.conrelid
            JOIN unnest(constraint_row.conkey) WITH ORDINALITY AS key_column(attnum, ordinality)
              ON true
            JOIN pg_attribute AS child_column
              ON child_column.attrelid = child.oid AND child_column.attnum = key_column.attnum
            WHERE constraint_row.contype = 'f'
              AND EXISTS (
                SELECT 1 FROM pg_attribute AS parent_owner
                WHERE parent_owner.attrelid = constraint_row.confrelid
                  AND parent_owner.attname = 'owner_id'
                  AND NOT parent_owner.attisdropped
              )
            GROUP BY child.relname, constraint_row.conname
            """
        ).fetchall()
        indexes = conn.execute(
            """
            SELECT table_row.relname, index_row.relname,
                   pg_get_indexdef(index_row.oid, 1, true)
            FROM pg_index AS index_meta
            JOIN pg_class AS index_row ON index_row.oid = index_meta.indexrelid
            JOIN pg_class AS table_row ON table_row.oid = index_meta.indrelid
            WHERE table_row.relnamespace = 'public'::regnamespace
              AND EXISTS (
                SELECT 1 FROM pg_attribute AS owner_column
                WHERE owner_column.attrelid = table_row.oid
                  AND owner_column.attname = 'owner_id'
                  AND NOT owner_column.attisdropped
              )
            """
        ).fetchall()

    assert foreign_keys
    assert all("owner_id" in columns for _, _, columns in foreign_keys)
    assert all(
        first_column == "owner_id" or index_name in reviewed_non_owner_first_indexes
        for _, index_name, first_column in indexes
    )


def test_vector_foundation_has_no_cross_tenant_ann_index(database: str) -> None:
    apply_migrations(database)
    with psycopg.connect(database) as conn:
        access_methods = {
            row[0]
            for row in conn.execute(
                """
                SELECT access_method.amname
                FROM pg_index AS index_meta
                JOIN pg_class AS table_row ON table_row.oid = index_meta.indrelid
                JOIN pg_class AS index_row ON index_row.oid = index_meta.indexrelid
                JOIN pg_am AS access_method ON access_method.oid = index_row.relam
                WHERE table_row.oid = 'chunk_embeddings'::regclass
                """
            )
        }
        function_sql = conn.execute(
            "SELECT pg_get_functiondef('search_chunks_prefiltered'::regproc)"
        ).fetchone()[0]

    assert access_methods == {"btree"}
    assert "WITH eligible AS MATERIALIZED" in function_sql


def _seed_owner_and_book(conn, owner_id: uuid.UUID, book_id: uuid.UUID, incarnation: uuid.UUID) -> None:
    conn.execute("INSERT INTO users (id, display_name) VALUES (%s, %s)", (owner_id, "Reader"))
    conn.execute(
        """
        INSERT INTO books (owner_id, id, incarnation, title, schema_version)
        VALUES (%s, %s, %s, %s, 1)
        """,
        (owner_id, book_id, incarnation, "Test book"),
    )


def test_composite_foreign_keys_reject_cross_owner_references(database: str) -> None:
    apply_migrations(database)
    owner_a, owner_b = uuid.uuid4(), uuid.uuid4()
    book_id, incarnation = uuid.uuid4(), uuid.uuid4()

    with psycopg.connect(database) as conn:
        _seed_owner_and_book(conn, owner_a, book_id, incarnation)
        conn.execute("INSERT INTO users (id, display_name) VALUES (%s, %s)", (owner_b, "Intruder"))
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute(
                """
                INSERT INTO reading_state (owner_id, book_id, book_incarnation)
                VALUES (%s, %s, %s)
                """,
                (owner_b, book_id, incarnation),
            )


def test_derived_rows_require_matching_completed_receipt(database: str) -> None:
    apply_migrations(database)
    owner_id, book_id, incarnation = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    chapter_id = uuid.uuid4()

    with psycopg.connect(database) as conn:
        _seed_owner_and_book(conn, owner_id, book_id, incarnation)
        conn.execute(
            """
            INSERT INTO chapters
              (owner_id, book_id, book_incarnation, id, chapter_key, revealed_at, content_hash)
            VALUES (%s, %s, %s, %s, 'chapter-1', 1, repeat('a', 64))
            """,
            (owner_id, book_id, incarnation, chapter_id),
        )
        conn.execute(
            """
            INSERT INTO chunks
              (owner_id, book_id, book_incarnation, id, chapter_id, revealed_at, text)
            VALUES (%s, %s, %s, %s, %s, 1, 'visible text')
            """,
            (owner_id, book_id, incarnation, uuid.uuid4(), chapter_id),
        )
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.commit()


def test_completed_receipt_is_bound_to_exact_chapter_content(database: str) -> None:
    apply_migrations(database)
    owner_id, book_id, incarnation = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    chapter_id = uuid.uuid4()

    with psycopg.connect(database) as conn:
        _seed_owner_and_book(conn, owner_id, book_id, incarnation)
        conn.execute(
            """
            INSERT INTO chapters
              (owner_id, book_id, book_incarnation, id, chapter_key, revealed_at, content_hash)
            VALUES (%s, %s, %s, %s, 'chapter-1', 1, repeat('a', 64))
            """,
            (owner_id, book_id, incarnation, chapter_id),
        )
        conn.execute(
            """
            INSERT INTO ingested_chapters
              (owner_id, book_id, book_incarnation, chapter_id, content_hash, completed_at)
            VALUES (%s, %s, %s, %s, repeat('b', 64), now())
            """,
            (owner_id, book_id, incarnation, chapter_id),
        )
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.commit()


def test_idempotency_keys_are_tenant_scoped(database: str) -> None:
    apply_migrations(database)
    owner_a, owner_b = uuid.uuid4(), uuid.uuid4()
    job_a, job_b = uuid.uuid4(), uuid.uuid4()

    with psycopg.connect(database) as conn:
        conn.execute(
            "INSERT INTO users (id, display_name) VALUES (%s, 'A'), (%s, 'B')",
            (owner_a, owner_b),
        )
        # Recollection jobs (a living kind that needs no book), running rather than waiting: at
        # most one companion job of an owner, kind and book waits, and that index must not be
        # what refuses the third.
        conn.execute(
            "INSERT INTO jobs (owner_id, id, kind, state, idempotency_key) "
            "VALUES (%s, %s, 'recollect', 'running', 'same')",
            (owner_a, job_a),
        )
        conn.execute(
            "INSERT INTO jobs (owner_id, id, kind, state, idempotency_key) "
            "VALUES (%s, %s, 'recollect', 'running', 'same')",
            (owner_b, job_b),
        )
        with pytest.raises(psycopg.errors.UniqueViolation, match="idempotency_key"):
            conn.execute(
                "INSERT INTO jobs (owner_id, id, kind, state, idempotency_key) "
                "VALUES (%s, %s, 'recollect', 'running', 'same')",
                (owner_a, uuid.uuid4()),
            )


# ---- 0028: the retired book job (ADR 0043 step 7) ---------------------------------------------

RETIRE_BOOK_JOB = 28


def _apply_before(database: str, version: int, monkeypatch) -> None:
    """Apply every migration older than ``version``, as a database made before it would be."""
    from app.hosted import migrations

    older = tuple(item for item in discover_migrations() if item.version < version)
    with monkeypatch.context() as patch:
        patch.setattr(migrations, "discover_migrations", lambda: older)
        apply_migrations(database)


def _book_job_world(conn) -> dict:
    """One reader with a book and a job of every kind, the retired one waiting for a key and with
    an attempt behind it, as a database made before 0028 could hold them."""
    owner, book, incarnation = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    conn.execute("INSERT INTO users (id, display_name) VALUES (%s, 'Reader')", (owner,))
    conn.execute("INSERT INTO books (owner_id, id, incarnation, title, schema_version) "
                 "VALUES (%s, %s, %s, 'A book', 1)", (owner, book, incarnation))
    jobs = {}
    for kind, state in (("ingest_book", "waiting_configuration"), ("chapter_notes", "pending"),
                        ("story_memory", "running"), ("recollect", "pending")):
        jobs[kind] = conn.execute(
            "INSERT INTO jobs (owner_id, book_id, book_incarnation, kind, state, idempotency_key, "
            "payload_metadata) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb) RETURNING id",
            (owner, None if kind == "recollect" else book,
             None if kind == "recollect" else incarnation, kind, state,
             f"{kind.replace('_', '-')}:{uuid.uuid4().hex}",
             '{"chapter_count": 3}' if kind == "ingest_book" else "{}")).fetchone()[0]
    conn.execute("INSERT INTO job_attempts (owner_id, job_id, attempt_no, worker_id, "
                 "lease_token_digest, leased_at, lease_expires_at, finished_at, outcome) "
                 "VALUES (%s, %s, 1, 'worker', %s, now(), now() + interval '1 minute', now(), "
                 "'expired')",
                 (owner, jobs["ingest_book"], b"0" * 32))
    return {"owner": owner, "book": book, "incarnation": incarnation, "jobs": jobs}


def test_retiring_the_book_job_deletes_its_jobs_and_leaves_every_other(
    database: str, monkeypatch
) -> None:
    """The break it catches: the migration failing on a database that holds the retired kind's
    rows, touching a job of a living kind, or leaving the kind, its state or the job limit
    possible afterwards. The rule that a living job names its book and no credential stays."""
    _apply_before(database, RETIRE_BOOK_JOB, monkeypatch)
    with psycopg.connect(database) as conn:
        world = _book_job_world(conn)
        living = conn.execute("SELECT id, kind, state, book_id FROM jobs WHERE kind<>'ingest_book' "
                              "ORDER BY id").fetchall()
    # Through 0028 only: 0029 drops the column the last refusal below names.
    _apply_before(database, RETIRE_BOOK_JOB + 1, monkeypatch)

    owner, book, incarnation = world["owner"], world["book"], world["incarnation"]
    with psycopg.connect(database) as conn:
        assert conn.execute("SELECT id, kind, state, book_id FROM jobs ORDER BY id"
                            ).fetchall() == living
        assert conn.execute("SELECT count(*) FROM job_attempts WHERE job_id=%s",
                            (world["jobs"]["ingest_book"],)).fetchone()[0] == 0
        assert not conn.execute("SELECT 1 FROM information_schema.columns WHERE "
                                "table_name='owner_limits' AND column_name='max_active_jobs'"
                                ).fetchall()
        assert not conn.execute("SELECT 1 FROM pg_indexes WHERE indexname="
                                "'ix_jobs_owner_active_limit'").fetchall()
    refused = (
        ("ingest_book", "pending", book, None, '{"chapter_count": 3}'),
        ("chapter_notes", "waiting_configuration", book, None, "{}"),
        ("story_memory", "pending", None, None, "{}"),
        ("recollect", "running", None, uuid.uuid4(), "{}"),
    )
    for kind, state, book_id, credential_id, payload in refused:
        with pytest.raises(psycopg.errors.CheckViolation):
            with psycopg.connect(database) as conn:
                conn.execute(
                    "INSERT INTO jobs (owner_id, book_id, book_incarnation, kind, state, "
                    "idempotency_key, payload_metadata, credential_id) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s)",
                    (owner, book_id, incarnation if book_id else None, kind, state,
                     f"refused:{uuid.uuid4().hex}", payload, credential_id))


def test_retiring_the_book_job_stops_rather_than_lose_a_cost_or_misplace_a_job(
    database: str, monkeypatch
) -> None:
    """The break it catches: what a retired job cost deleted with it, or a job of a living kind
    left in the retired state. Either stops the migration whole: nothing of it is applied."""
    _apply_before(database, RETIRE_BOOK_JOB, monkeypatch)
    with psycopg.connect(database) as conn:
        world = _book_job_world(conn)
        conn.execute("INSERT INTO cost_ledger (owner_id, id, book_id, book_incarnation, job_id, "
                     "phase, provider, model, input_tokens, output_tokens, usd, idempotency_key) "
                     "VALUES (%s, %s, %s, %s, %s, 'extraction', 'made-up', 'model', 10, 2, 0.01, "
                     "'cost-of-a-retired-job')",
                     (world["owner"], uuid.uuid4(), world["book"], world["incarnation"],
                      world["jobs"]["ingest_book"]))
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        apply_migrations(database)

    with psycopg.connect(database) as conn:
        assert max(row[0] for row in conn.execute(
            "SELECT version FROM app_schema_migrations").fetchall()) == RETIRE_BOOK_JOB - 1
        assert conn.execute("SELECT count(*) FROM jobs WHERE kind='ingest_book'").fetchone()[0] == 1
        conn.execute("DELETE FROM cost_ledger WHERE job_id=%s", (world["jobs"]["ingest_book"],))
        conn.execute("UPDATE jobs SET state='waiting_configuration' WHERE id=%s",
                     (world["jobs"]["chapter_notes"],))
    with pytest.raises(psycopg.errors.CheckViolation, match="jobs_state_check"):
        apply_migrations(database)
    with psycopg.connect(database) as conn:
        assert conn.execute("SELECT count(*) FROM jobs WHERE kind='ingest_book'").fetchone()[0] == 1


# ---- 0029: the reader-key tables (ADR 0043 step 8) --------------------------------------------

DROP_KEY_TABLES = 29
KEY_TABLES = ("provider_credentials", "provider_model_settings")


def _stored_key_world(conn) -> dict:
    """One reader with a book, a stored key, a model choice naming it, a living job and what it
    cost, as a database made before 0029 could hold them."""
    owner, book, incarnation = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    key, job = uuid.uuid4(), uuid.uuid4()
    _seed_owner_and_book(conn, owner, book, incarnation)
    conn.execute(
        "INSERT INTO provider_credentials (owner_id, id, provider, masked_label, ciphertext, "
        "encrypted_data_key, encryption_algorithm, key_version, nonce) "
        "VALUES (%s, %s, 'openai', 'sk-...7Rq2', %s, %s, 'AES-256-GCM/AES-256-GCM', 'v1', %s)",
        (owner, key, b"stored-key-canary-" + b"c" * 16, b"k" * 60, b"n" * 12))
    conn.execute(
        "INSERT INTO provider_model_settings (owner_id, provider, capability, credential_id, "
        "model, base_url) VALUES (%s, 'openai-compatible', 'synthesis', %s, 'gpt-x', "
        "'https://api.openai.com/v1')", (owner, key))
    conn.execute(
        "INSERT INTO jobs (owner_id, id, book_id, book_incarnation, kind, idempotency_key) "
        "VALUES (%s, %s, %s, %s, 'chapter_notes', %s)",
        (owner, job, book, incarnation, f"chapter-notes:{uuid.uuid4().hex}"))
    conn.execute(
        "INSERT INTO cost_ledger (owner_id, id, book_id, book_incarnation, job_id, phase, "
        "provider, model, input_tokens, output_tokens, usd, idempotency_key) "
        "VALUES (%s, %s, %s, %s, %s, 'synthesis', 'openai-compatible', 'gpt-x', 10, 2, 0.01, "
        "'cost-of-a-living-job')", (owner, uuid.uuid4(), book, incarnation, job))
    return {"owner": owner, "book": book, "incarnation": incarnation}


def _kept_rows(conn) -> dict:
    # A column a later migration adds is no change to a book: 0031's split fingerprint comes
    # empty on rows already there, which its own test checks, and so does 0033's edition mark on
    # a reader's book (its own test checks it), and 0034's "no holder since" mark of an edition
    # (its own test checks it). Nor is the notes account 0032 makes on a database without one a
    # change to anyone's account (its own test checks it).
    account = "notes_account" if conn.execute(
        "SELECT to_regclass('public.notes_account')").fetchone()[0] else "library_account"
    return {"users": conn.execute(
        f"SELECT * FROM users WHERE id NOT IN (SELECT account_id FROM {account}) "
        "ORDER BY 1, 2").fetchall(),
        # (0036's background mark is checked by its own test.)
        "cost_ledger": conn.execute("SELECT to_jsonb(entry) - 'background' FROM cost_ledger"
                                    " AS entry ORDER BY owner_id, id").fetchall()} | {
        "books": conn.execute("SELECT to_jsonb(book) - 'split_hash' - 'edition_of' "
                              "- 'no_holder_since' "
                              "FROM books AS book ORDER BY owner_id, id").fetchall()} | {
        "jobs": conn.execute("SELECT owner_id, id, book_id, kind, state, idempotency_key "
                             "FROM jobs ORDER BY id").fetchall()}


def test_dropping_the_key_tables_erases_a_stored_key_and_keeps_every_other_row(
    database: str, monkeypatch
) -> None:
    """The break it catches: the migration failing on a database that holds a reader's stored
    key or a model choice naming it, leaving either table, its row security policy or its index
    behind, leaving the jobs column or its link, or touching an account, a book, a job or a cost.
    A stored key is a secret a reader gave for a feature that is gone: it is erased, not kept."""
    _apply_before(database, DROP_KEY_TABLES, monkeypatch)
    with psycopg.connect(database) as conn:
        world = _stored_key_world(conn)
        kept = _kept_rows(conn)
    apply_migrations(database)

    with psycopg.connect(database) as conn:
        assert _kept_rows(conn) == kept
        tables = {row[0] for row in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='public'")}
        assert not set(KEY_TABLES) & tables
        assert not conn.execute("SELECT 1 FROM pg_policies WHERE tablename = ANY(%s)",
                                (list(KEY_TABLES),)).fetchall()
        assert not conn.execute(
            "SELECT 1 FROM pg_indexes WHERE indexname IN "
            "('ix_provider_credentials_owner_live', 'ix_provider_settings_owner_credential')"
        ).fetchall()
        assert not conn.execute("SELECT table_name FROM information_schema.columns WHERE "
                                "table_schema='public' AND column_name='credential_id'").fetchall()
        # Of the checks that name a credential, only the one that keeps keys out of a job's
        # payload (0008) is left, and it stays.
        assert [row[0] for row in conn.execute(
            "SELECT conname FROM pg_constraint WHERE conname LIKE '%credential%'")] == [
            "jobs_payload_has_no_credentials_ck"]
    # The first half of 0028's rule stays: a book's companion job names its book.
    with pytest.raises(psycopg.errors.CheckViolation):
        with psycopg.connect(database) as conn:
            conn.execute("INSERT INTO jobs (owner_id, kind, idempotency_key) "
                         "VALUES (%s, 'chapter_notes', 'no-book')", (world["owner"],))


def test_the_key_tables_are_gone_from_a_new_database_and_named_nowhere(database: str) -> None:
    """The break it catches: a grant, the export's list or the code still naming a dropped table,
    which would stop the deployment's role script or the export at its first run."""
    from app.hosted.tenant.export import IN_THE_COPY, LEFT_OUT
    from app.hosted.tenant.grants import TENANT_TABLE_PRIVILEGES, WORKER_TABLE_PRIVILEGES

    apply_migrations(database)
    with psycopg.connect(database) as conn:
        assert not conn.execute("SELECT 1 FROM pg_tables WHERE tablename = ANY(%s)",
                                (list(KEY_TABLES),)).fetchall()
    for listed in (TENANT_TABLE_PRIVILEGES, WORKER_TABLE_PRIVILEGES, IN_THE_COPY, LEFT_OUT):
        assert not set(KEY_TABLES) & set(listed)


# ---- 0030: the unused service table provider_capabilities ------------------------------------

DROP_PROVIDER_CAPABILITIES = 30


def test_dropping_provider_capabilities_keeps_every_other_row_and_is_named_nowhere(
    database: str, monkeypatch
) -> None:
    """The break it catches: the migration failing on a database where the table holds a row (it
    holds no reader content and no secret, so a row goes with it), leaving the table behind,
    touching an account, a book, a job or a cost, or a grant or list still naming it."""
    from app.hosted.tenant.export import IN_THE_COPY, LEFT_OUT
    from app.hosted.tenant.grants import TENANT_TABLE_PRIVILEGES, WORKER_TABLE_PRIVILEGES

    _apply_before(database, DROP_PROVIDER_CAPABILITIES, monkeypatch)
    with psycopg.connect(database) as conn:
        _seed_owner_and_book(conn, uuid.uuid4(), uuid.uuid4(), uuid.uuid4())
        conn.execute("INSERT INTO provider_capabilities (provider, capability_version, "
                     "capabilities) VALUES ('openai', 'v1', '{\"stream\": true}'::jsonb)")
        kept = _kept_rows(conn)
    apply_migrations(database)
    with psycopg.connect(database) as conn:
        assert _kept_rows(conn) == kept
        assert not conn.execute("SELECT 1 FROM pg_tables WHERE schemaname='public' AND "
                                "tablename='provider_capabilities'").fetchall()
    for listed in (TENANT_TABLE_PRIVILEGES, WORKER_TABLE_PRIVILEGES, IN_THE_COPY, LEFT_OUT):
        assert "provider_capabilities" not in listed


# ---- 0031: the split fingerprint (ADR 0044 step 1) ---------------------------------------------

BOOK_SPLIT_HASH = 31
_DOOR_FUNCTION = "library_story_memory(uuid)"


def _door_record(conn) -> tuple:
    """Everything about the door but its body: owner, privileges, security, settings, arguments
    and result."""
    return conn.execute(
        "SELECT proowner::regrole::text, proacl::text, prosecdef, provolatile, proconfig, "
        "pg_get_function_identity_arguments(oid), pg_get_function_result(oid) "
        "FROM pg_proc WHERE oid = %s::regprocedure", (_DOOR_FUNCTION,)).fetchone()


def _door_body(conn) -> str:
    return conn.execute("SELECT prosrc FROM pg_proc WHERE oid = %s::regprocedure",
                        (_DOOR_FUNCTION,)).fetchone()[0]


def test_the_split_fingerprint_starts_unknown_and_the_door_changes_only_its_match(
    database: str, monkeypatch
) -> None:
    """The break it catches: the migration failing on a database that has books, filling in a
    fingerprint it cannot know, or touching any other value of a book; a fingerprint of any shape
    but a SHA-256; or the door's replacement changing anything but its match (the place, the
    predicates, the hidden later ends and the key rewrite stay byte for byte), or its owner,
    privileges (EXECUTE revoked from PUBLIC), security, settings, arguments or result."""
    _apply_before(database, BOOK_SPLIT_HASH, monkeypatch)
    owner, book, incarnation = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    columns = ("owner_id, id, incarnation, title, author, source_kind, source_id, file_hash, "
               "schema_version, content_language, book_type, created_at, updated_at, deleted_at")
    with psycopg.connect(database) as conn:
        _seed_owner_and_book(conn, owner, book, incarnation)
        conn.execute("UPDATE books SET source_kind='gutenberg:1342', file_hash=repeat('a', 64)")
        books = conn.execute(f"SELECT {columns} FROM books ORDER BY id").fetchall()
        # As the role script grants it to the tenant role.
        if conn.execute("SELECT 1 FROM pg_roles WHERE rolname='lit38_door_reader'").fetchone() \
                is None:
            conn.execute("CREATE ROLE lit38_door_reader NOLOGIN")
        conn.execute(f"GRANT EXECUTE ON FUNCTION {_DOOR_FUNCTION} TO lit38_door_reader")
        record, body = _door_record(conn), _door_body(conn)
    # Up to 0031 only: 0032 replaces this door with another (its own test below).
    _apply_before(database, NOTES_ACCOUNT, monkeypatch)

    with psycopg.connect(database) as conn:
        assert conn.execute(f"SELECT {columns} FROM books ORDER BY id").fetchall() == books
        assert conn.execute("SELECT split_hash FROM books").fetchall() == [(None,)]
        assert _door_record(conn) == record
        assert [conn.execute("SELECT has_function_privilege(%s, %s, 'EXECUTE')",
                             (role, _DOOR_FUNCTION)).fetchone()[0]
                for role in ("lit38_door_reader", "public")] == [True, False]
        changed = [
            ("           mine.file_hash\n", "           mine.file_hash, mine.split_hash\n"),
            ("      AND mine.file_hash IS NOT NULL\n",
             "      AND mine.file_hash IS NOT NULL\n      AND mine.split_hash IS NOT NULL\n"),
            ("     AND built.file_hash = reader.file_hash\n",
             "     AND built.file_hash = reader.file_hash\n"
             "     AND built.split_hash = reader.split_hash\n"),
        ]
        expected = body
        for before, after in changed:
            assert expected.count(before) == 1, before
            expected = expected.replace(before, after)
        assert _door_body(conn) == expected
        conn.execute("UPDATE books SET split_hash=repeat('b', 64)")
    for wrong in ("B" * 64, "b" * 63, "b" * 65, "not a hash"):
        with pytest.raises(psycopg.errors.CheckViolation):
            with psycopg.connect(database) as conn:
                conn.execute("UPDATE books SET split_hash=%s", (wrong,))


# ---- 0032: the notes account and the door for every book (ADR 0044 step 2) ---------------------

NOTES_ACCOUNT = 32
_NEW_DOOR = "story_memory_door(uuid)"

# Every line of the door's body 0032 changes, as (0031's text, 0032's text): the book needs both
# fingerprints, not a free-library tag; the edition is the notes account's live book with both
# fingerprints equal; a book with no edition gets the one row with empty lists. Nothing else: the
# place, every predicate, the hidden later ends and the key rewrite stay byte for byte.
_DOOR_CHANGES = [
    ("    SELECT mine.owner_id, mine.id, mine.incarnation, mine.content_language, mine.source_kind,\n"
     "           mine.file_hash, mine.split_hash\n",
     "    SELECT mine.owner_id, mine.id, mine.incarnation, mine.content_language, mine.file_hash,\n"
     "           mine.split_hash\n"),
    ("      AND mine.id = library_story_memory.book\n",
     "      AND mine.id = story_memory_door.book\n"),
    ("      AND mine.source_kind LIKE 'gutenberg:%'\n", ""),
    ("      AND NOT EXISTS (SELECT 1 FROM public.library_account AS library\n"
     "                      WHERE library.account_id = mine.owner_id)\n",
     "      AND NOT EXISTS (SELECT 1 FROM public.notes_account AS notes\n"
     "                      WHERE notes.account_id = mine.owner_id)\n"),
    ("    JOIN public.library_account AS library ON true\n"
     "    JOIN public.books AS built\n"
     "      ON built.owner_id = library.account_id\n"
     "     AND built.source_kind = reader.source_kind\n",
     "    JOIN public.notes_account AS notes ON true\n"
     "    JOIN public.books AS built\n"
     "      ON built.owner_id = notes.account_id\n"),
    ("  FROM reader, edition, place\n",
     "  FROM reader CROSS JOIN place LEFT JOIN edition ON true\n"),
]

# The database's SECURITY DEFINER functions: the platform day's two (migration 0020) and the
# one door. A second way into story memory would show here.
_DEFINERS = {"companion_platform_adjust(date,numeric,numeric)",
             "companion_platform_reserve(numeric,numeric)", _NEW_DOOR}


def _definers(conn) -> set[str]:
    return {row[0] for row in conn.execute(
        "SELECT p.oid::regprocedure::text FROM pg_proc AS p "
        "JOIN pg_namespace AS n ON n.oid = p.pronamespace "
        "WHERE n.nspname = 'public' AND p.prosecdef").fetchall()}


def _edition(conn, owner) -> uuid.UUID:
    book = uuid.uuid4()
    conn.execute(
        "INSERT INTO books (owner_id, id, incarnation, title, schema_version, source_kind, "
        "file_hash, split_hash) VALUES (%s, %s, %s, 'A book', 1, 'upload', "
        "repeat('a', 64), repeat('b', 64))", (owner, book, uuid.uuid4()))
    return book


def test_the_notes_account_is_the_library_account_renamed_and_the_door_changes_only_its_match(
    database: str, monkeypatch
) -> None:
    """The break it catches, on a database made before 0032 with a library account and a built
    edition: the account or its edition lost or changed by the rename, a second account made
    beside it, a reader's two copies of one file refused, the old door left beside the new one,
    or the new door changing anything but how
    the edition is found and the empty row: its body beyond the listed lines, its security, its
    settings, its arguments or result, or EXECUTE for PUBLIC."""
    _apply_before(database, NOTES_ACCOUNT, monkeypatch)
    account, reader = uuid.uuid4(), uuid.uuid4()
    with psycopg.connect(database) as conn:
        conn.execute("INSERT INTO users (id, display_name, status) "
                     "VALUES (%s, 'Mothli library', 'suspended')", (account,))
        conn.execute("INSERT INTO library_account (account_id) VALUES (%s)", (account,))
        conn.execute("INSERT INTO users (id, display_name) VALUES (%s, 'Reader')", (reader,))
        edition = _edition(conn, account)
        conn.execute("UPDATE books SET source_kind = 'gutenberg:1342' WHERE id = %s", (edition,))
        _edition(conn, reader)
        _edition(conn, reader)                   # a reader may hold two copies of one file
        users = conn.execute("SELECT * FROM users ORDER BY id").fetchall()
        books = conn.execute("SELECT * FROM books ORDER BY id").fetchall()
        limits = conn.execute("SELECT * FROM owner_limits ORDER BY owner_id").fetchall()
        record = conn.execute(
            "SELECT proowner::regrole::text, prosecdef, provolatile, proconfig, "
            "pg_get_function_identity_arguments(oid), pg_get_function_result(oid) "
            "FROM pg_proc WHERE oid = %s::regprocedure", (_DOOR_FUNCTION,)).fetchone()
        body = _door_body(conn)
    # Through 0032 only: 0033 adds the edition mark to books and a line to the door (its own
    # test below).
    _apply_before(database, ONE_BUILD, monkeypatch)

    with psycopg.connect(database) as conn:
        assert conn.execute("SELECT to_regclass('public.library_account')").fetchone()[0] is None
        assert conn.execute("SELECT account_id FROM notes_account").fetchall() == [(account,)]
        assert conn.execute("SELECT * FROM users ORDER BY id").fetchall() == users
        assert conn.execute("SELECT * FROM books ORDER BY id").fetchall() == books
        assert conn.execute("SELECT * FROM owner_limits ORDER BY owner_id").fetchall() == limits
        assert conn.execute("SELECT count(*) FROM pg_proc WHERE proname = "
                            "'library_story_memory'").fetchone()[0] == 0
        assert conn.execute(
            "SELECT proowner::regrole::text, prosecdef, provolatile, proconfig, "
            "pg_get_function_identity_arguments(oid), pg_get_function_result(oid) "
            "FROM pg_proc WHERE oid = %s::regprocedure", (_NEW_DOOR,)).fetchone() == record
        assert record[1:4] == (True, "s", ["search_path=pg_catalog, public, pg_temp"])
        assert not conn.execute("SELECT has_function_privilege('public', %s, 'EXECUTE')",
                                (_NEW_DOOR,)).fetchone()[0]
        assert _definers(conn) == _DEFINERS
        expected = body
        for before, after in _DOOR_CHANGES:
            assert expected.count(before) == 1, before
            expected = expected.replace(before, after)
        assert conn.execute("SELECT prosrc FROM pg_proc WHERE oid = %s::regprocedure",
                            (_NEW_DOOR,)).fetchone()[0] == expected


def test_a_new_database_has_one_notes_account_that_never_signs_in(database: str) -> None:
    """The break it catches: a fresh database with no notes account (the door would serve no
    one and the builds of step 3 would have nowhere to write), a second one made when the
    migrations run again, or one that could sign in, holds a sign-in identity or an address, or
    has no limits of its own."""
    apply_migrations(database)
    with psycopg.connect(database) as conn:
        [(account,)] = conn.execute("SELECT account_id FROM notes_account").fetchall()
        assert conn.execute("SELECT status, email, display_name, erase_after, deleted_at "
                            "FROM users WHERE id = %s", (account,)).fetchone() == (
            "suspended", None, "Mothli library", None, None)
        assert conn.execute("SELECT count(*) FROM external_identities WHERE owner_id = %s",
                            (account,)).fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM owner_limits WHERE owner_id = %s",
                            (account,)).fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM books WHERE owner_id = %s",
                            (account,)).fetchone()[0] == 0
        # 0035 adds the later-name check, 0036 the background budget (their own tests below).
        assert _definers(conn) == _DEFINERS | {_LATER_NAMES, _BACKGROUND_OPEN}
    apply_migrations(database)
    with psycopg.connect(database) as conn:
        assert conn.execute("SELECT account_id FROM notes_account").fetchall() == [(account,)]


# ---- 0033: one build per chapter (ADR 0044 step 3) ---------------------------------------------

ONE_BUILD = 33
# The one line 0033 adds to the door: the edition it finds is a marked one.
_EDITION_MARK = ("     AND built.deleted_at IS NULL\n",
                 "     AND built.deleted_at IS NULL\n     AND built.edition_of IS NOT NULL\n")


def _notes_book(conn, account, title, file_hash, split_hash, *, deleted=False, age=0):
    book = uuid.uuid4()
    conn.execute(
        "INSERT INTO books (owner_id, id, incarnation, title, schema_version, file_hash, "
        "split_hash, created_at, deleted_at) VALUES (%s, %s, %s, %s, 1, %s, %s, "
        "now() - %s * interval '1 hour', CASE WHEN %s THEN now() END)",
        (account, book, uuid.uuid4(), title, file_hash, split_hash, age, deleted))
    return book


def test_one_build_per_chapter_marks_the_editions_keeps_one_a_file_and_adds_one_door_line(
    database: str, monkeypatch
) -> None:
    """The break it catches, on a database made before 0033 with the operator's editions and a
    reader's copy of the same file: an edition left unmarked (the door would no longer find it),
    a second copy of a file, a removed book or a reader's book marked, any other value of a book
    changed; a second live edition of one file, a mark on a book that is not the notes account's
    or without both fingerprints, accepted afterwards; two leased or running story-memory jobs
    holding one chapter of one edition (or the slot taken by a waiting or finished job, or one
    of another kind); or the door changed beyond its one line, or its owner, privileges,
    security, settings, arguments or result changed."""
    _apply_before(database, ONE_BUILD, monkeypatch)
    reader = uuid.uuid4()
    a, b, c = "a" * 64, "b" * 64, "c" * 64
    with psycopg.connect(database) as conn:
        [(account,)] = conn.execute("SELECT account_id FROM notes_account").fetchall()
        first = _notes_book(conn, account, "Edition", a, b, age=2)
        second = _notes_book(conn, account, "A second copy", a, b, age=1)
        gone = _notes_book(conn, account, "Removed", c, b, deleted=True)
        unsplit = _notes_book(conn, account, "No split", c, None)
        conn.execute("INSERT INTO users (id, display_name) VALUES (%s, 'Reader')", (reader,))
        mine = _notes_book(conn, reader, "Mine", a, b)
        books = conn.execute("SELECT to_jsonb(book) FROM books AS book ORDER BY id").fetchall()
        if conn.execute("SELECT 1 FROM pg_roles WHERE rolname='lit38_door_reader'").fetchone() \
                is None:
            conn.execute("CREATE ROLE lit38_door_reader NOLOGIN")
        conn.execute(f"GRANT EXECUTE ON FUNCTION {_NEW_DOOR} TO lit38_door_reader")
        record = conn.execute(
            "SELECT proowner::regrole::text, proacl::text, prosecdef, provolatile, proconfig, "
            "pg_get_function_identity_arguments(oid), pg_get_function_result(oid) "
            "FROM pg_proc WHERE oid = %s::regprocedure", (_NEW_DOOR,)).fetchone()
        body = conn.execute("SELECT prosrc FROM pg_proc WHERE oid = %s::regprocedure",
                            (_NEW_DOOR,)).fetchone()[0]
    apply_migrations(database)

    with psycopg.connect(database) as conn:
        marked = dict(conn.execute("SELECT id, edition_of FROM books").fetchall())
        assert marked == {first: account, second: None, gone: None, unsplit: None, mine: None}
        # (0034's "no holder since" comes empty on every row; its own test checks it.)
        assert conn.execute("SELECT to_jsonb(book) - 'edition_of' - 'no_holder_since' "
                            "FROM books AS book ORDER BY id").fetchall() == books
        assert conn.execute(
            "SELECT proowner::regrole::text, proacl::text, prosecdef, provolatile, proconfig, "
            "pg_get_function_identity_arguments(oid), pg_get_function_result(oid) "
            "FROM pg_proc WHERE oid = %s::regprocedure", (_NEW_DOOR,)).fetchone() == record
        assert _definers(conn) == _DEFINERS | {_LATER_NAMES, _BACKGROUND_OPEN}
        before, after = _EDITION_MARK
        assert body.count(before) == 1
        assert conn.execute("SELECT prosrc FROM pg_proc WHERE oid = %s::regprocedure",
                            (_NEW_DOOR,)).fetchone()[0] == body.replace(before, after)
    refused = (
        ("UPDATE books SET edition_of = owner_id WHERE id = %s", (second,),
         psycopg.errors.UniqueViolation),
        ("UPDATE books SET edition_of = %s WHERE id = %s", (account, mine),
         psycopg.errors.CheckViolation),
        ("UPDATE books SET edition_of = owner_id WHERE id = %s", (mine,),
         psycopg.errors.ForeignKeyViolation),
        ("UPDATE books SET edition_of = owner_id WHERE id = %s", (unsplit,),
         psycopg.errors.CheckViolation),
    )
    for statement, params, error in refused:
        with pytest.raises(error):
            with psycopg.connect(database) as conn:
                conn.execute(statement, params)
    with psycopg.connect(database) as conn:
        # A removed edition frees its file: a new live one may be made.
        conn.execute("UPDATE books SET deleted_at = now() WHERE id = %s", (first,))
        conn.execute("UPDATE books SET edition_of = owner_id WHERE id = %s", (second,))

    # The build slot.
    with psycopg.connect(database) as conn:
        incarnation = conn.execute("SELECT incarnation FROM books WHERE id = %s",
                                   (mine,)).fetchone()[0]

    def job(state, chapter, kind="story_memory", owner=reader, book=mine):
        conn.execute(
            "INSERT INTO jobs (owner_id, book_id, book_incarnation, kind, state, "
            "idempotency_key, payload_metadata, completed_at) VALUES (%s, %s, %s, %s, %s, %s, "
            "jsonb_build_object('chapter', %s::integer, 'edition', %s::text), "
            "CASE WHEN %s IN ('succeeded', 'failed', 'cancelled') THEN now() END)",
            (owner, book, incarnation, kind, state, f"slot:{uuid.uuid4().hex}", chapter,
             str(second), state))

    with psycopg.connect(database, autocommit=True) as conn:
        job("running", 1)
        for state in ("pending", "succeeded", "failed", "cancelled"):
            job(state, 1)
        job("leased", 2)
        job("running", 1, kind="chapter_notes")
        for state in ("leased", "running"):
            with pytest.raises(psycopg.errors.UniqueViolation):
                job(state, 1)
            with pytest.raises(psycopg.errors.UniqueViolation):
                job(state, 2)


# ---- 0034: the "no holder since" mark of an edition (ADR 0044 step 8) ----------------------------

EDITION_HOLDERS = 34


def test_the_edition_holders_mark_starts_empty_changes_nothing_else_and_fits_only_an_edition(
    database: str, monkeypatch
) -> None:
    """The break it catches, on a database made before 0034 with an edition and a reader's copy
    of its file: a mark set by the migration (an edition would start its 30 days at the
    deployment), any other value of a book or the door changed, or the mark accepted on a book
    that is not a live edition (a reader's book, an unmarked copy of the notes account)."""
    _apply_before(database, EDITION_HOLDERS, monkeypatch)
    reader = uuid.uuid4()
    a, b = "a" * 64, "b" * 64
    with psycopg.connect(database) as conn:
        [(account,)] = conn.execute("SELECT account_id FROM notes_account").fetchall()
        edition = _notes_book(conn, account, "edition aaaaaaaaaaaa", a, b)
        conn.execute("UPDATE books SET edition_of = owner_id WHERE id = %s", (edition,))
        copy = _notes_book(conn, account, "A copy", a, b, age=1)
        conn.execute("INSERT INTO users (id, display_name) VALUES (%s, 'Reader')", (reader,))
        mine = _notes_book(conn, reader, "Mine", a, b)
        books = conn.execute("SELECT to_jsonb(book) FROM books AS book ORDER BY id").fetchall()
        door = conn.execute("SELECT prosrc FROM pg_proc WHERE oid = %s::regprocedure",
                            (_NEW_DOOR,)).fetchone()[0]
    apply_migrations(database)

    with psycopg.connect(database) as conn:
        assert conn.execute("SELECT to_jsonb(book) - 'no_holder_since' FROM books AS book "
                            "ORDER BY id").fetchall() == books
        assert conn.execute("SELECT count(*) FROM books WHERE no_holder_since IS NOT NULL"
                            ).fetchone()[0] == 0
        assert conn.execute("SELECT prosrc FROM pg_proc WHERE oid = %s::regprocedure",
                            (_NEW_DOOR,)).fetchone()[0] == door
        conn.execute("UPDATE books SET no_holder_since = now() WHERE id = %s", (edition,))
    for book in (copy, mine):
        with pytest.raises(psycopg.errors.CheckViolation):
            with psycopg.connect(database) as conn:
                conn.execute("UPDATE books SET no_holder_since = now() WHERE id = %s", (book,))


# ---- 0035: the later-name check in the database (ADR 0044 step 10) -----------------------------

LATER_NAME_WORDS = 35
_LATER_NAMES = "story_memory_later_names(uuid,integer,text[],text[],text[],text[],text[],text[])"


def test_the_later_name_words_start_empty_change_nothing_else_and_only_the_owner_may_call(
    database: str, monkeypatch
) -> None:
    """The break it catches, on a database made before 0035 with an edition holding a person: a
    book or a note changed, the door changed, words made up for a name by the migration (nothing
    was deployed before it, so no edition needs them, and the check refuses an edition with a
    name that has none: ADR 0044 step 10); the new table without forced row security, or with a
    privilege for PUBLIC; the check callable by PUBLIC, not a security definer, with another
    search path, or taking anything but the book, a ceiling, the sentence and the read words."""
    _apply_before(database, LATER_NAME_WORDS, monkeypatch)
    a, b = "a" * 64, "b" * 64
    with psycopg.connect(database) as conn:
        [(account,)] = conn.execute("SELECT account_id FROM notes_account").fetchall()
        edition = _notes_book(conn, account, "edition aaaaaaaaaaaa", a, b)
        conn.execute("UPDATE books SET edition_of = owner_id WHERE id = %s", (edition,))
        [(incarnation,)] = conn.execute("SELECT incarnation FROM books WHERE id = %s",
                                        (edition,)).fetchall()
        chapter = uuid.uuid4()
        conn.execute("INSERT INTO chapters (owner_id, book_id, book_incarnation, id, chapter_key,"
                     " revealed_at, content_hash) VALUES (%s, %s, %s, %s, 'k', 1, %s)",
                     (account, edition, incarnation, chapter, a))
        conn.execute("INSERT INTO ingested_chapters (owner_id, book_id, book_incarnation,"
                     " chapter_id, content_hash, completed_at) VALUES (%s, %s, %s, %s, %s, now())",
                     (account, edition, incarnation, chapter, a))
        conn.execute("INSERT INTO entities (owner_id, book_id, book_incarnation, source_chapter_id,"
                     " canonical_name, entity_type, revealed_at) VALUES (%s, %s, %s, %s, 'Vex',"
                     " 'character', 2)", (account, edition, incarnation, chapter))
        books = conn.execute("SELECT to_jsonb(book) FROM books AS book ORDER BY id").fetchall()
        people = conn.execute("SELECT to_jsonb(person) FROM entities AS person").fetchall()
        door = conn.execute("SELECT prosrc FROM pg_proc WHERE oid = %s::regprocedure",
                            (_NEW_DOOR,)).fetchone()[0]
    apply_migrations(database)

    with psycopg.connect(database) as conn:
        assert conn.execute("SELECT to_jsonb(book) FROM books AS book ORDER BY id"
                            ).fetchall() == books
        assert conn.execute("SELECT to_jsonb(person) FROM entities AS person").fetchall() == people
        assert conn.execute("SELECT prosrc FROM pg_proc WHERE oid = %s::regprocedure",
                            (_NEW_DOOR,)).fetchone()[0] == door
        assert conn.execute("SELECT count(*) FROM name_words").fetchone()[0] == 0
        assert conn.execute("SELECT relrowsecurity AND relforcerowsecurity FROM pg_class"
                            " WHERE oid = 'public.name_words'::regclass").fetchone()[0]
        assert not any(conn.execute(
            "SELECT has_table_privilege('public', 'public.name_words', %s)", (privilege,)
        ).fetchone()[0] for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"))
        assert conn.execute(
            "SELECT prosecdef, provolatile, proconfig, pg_get_function_identity_arguments(oid)"
            " FROM pg_proc WHERE oid = %s::regprocedure", (_LATER_NAMES,)).fetchone() == (
            True, "s", ["search_path=pg_catalog, public, pg_temp"],
            "book uuid, at_most integer, sentence_words text[], sentence_folded text[], "
            "read_any text[], read_proper text[], read_also text[], read_not text[]")
        assert not conn.execute("SELECT has_function_privilege('public', %s, 'EXECUTE')",
                                (_LATER_NAMES,)).fetchone()[0]
        assert _definers(conn) == _DEFINERS | {_LATER_NAMES, _BACKGROUND_OPEN}


# ---- 0036: background work is Mothli's, not the reader's allowance (ADR 0041 section 6) ---------

BACKGROUND_SPEND = 36
_BACKGROUND_OPEN = "companion_background_open(numeric)"


def test_background_spend_is_marked_by_its_names_and_nothing_else_changes(
    database: str, monkeypatch
) -> None:
    """The break it catches, on a database made before 0036 with a reader's replies, chapter
    notes and recollections in the ledger and open: background rows left unmarked (the reader
    would wait out the month on notes they never asked for), a reply or a row of the older paths
    marked, any other value changed; or the new budget check callable by PUBLIC, not a security
    definer, with another search path, or answering anything but yes or no."""
    _apply_before(database, BACKGROUND_SPEND, monkeypatch)
    reader = uuid.uuid4()
    purposes = ("stream-reply", "stream-sentence-check", "home-reply", "notes-write",
                "notes-sentence-check", "notes-write-failed-reserved", "recollect-write",
                "recollect-check", None)
    with psycopg.connect(database) as conn:
        conn.execute("INSERT INTO users (id, display_name) VALUES (%s, 'Reader')", (reader,))
        for purpose in purposes:
            key = f"companion:{uuid.uuid4().hex}"
            conn.execute("INSERT INTO cost_ledger (owner_id, phase, model, usd, idempotency_key,"
                         " purpose) VALUES (%s, 'synthesis', 'm', 0.01, %s, %s)",
                         (reader, key, purpose))
            conn.execute("INSERT INTO cost_reservations (owner_id, id, phase, model,"
                         " reserved_input_tokens, reserved_output_tokens, reserved_usd,"
                         " idempotency_key, purpose) VALUES (%s, %s, 'synthesis', 'm', 1, 1,"
                         " 0.01, %s, %s)", (reader, uuid.uuid4(), key, purpose))
        ledger = conn.execute("SELECT to_jsonb(row) FROM cost_ledger AS row ORDER BY id"
                              ).fetchall()
        held = conn.execute("SELECT to_jsonb(row) FROM cost_reservations AS row ORDER BY id"
                            ).fetchall()
    apply_migrations(database)

    background = {"notes-write", "notes-sentence-check", "notes-write-failed-reserved",
                  "recollect-write", "recollect-check"}
    with psycopg.connect(database) as conn:
        for table, before in (("cost_ledger", ledger), ("cost_reservations", held)):
            assert conn.execute(f"SELECT to_jsonb(row) - 'background' FROM {table} AS row"
                                " ORDER BY id").fetchall() == before
            marked = dict(conn.execute(f"SELECT COALESCE(purpose, ''), background FROM {table}"
                                       ).fetchall())
            assert marked == {purpose or "": purpose in background for purpose in purposes}
        assert conn.execute(
            "SELECT prosecdef, proconfig, pg_get_function_identity_arguments(oid),"
            " pg_get_function_result(oid) FROM pg_proc WHERE oid = %s::regprocedure",
            (_BACKGROUND_OPEN,)).fetchone() == (
            True, ["search_path=pg_catalog, public, pg_temp"], "day_cap numeric", "boolean")
        assert not conn.execute("SELECT has_function_privilege('public', %s, 'EXECUTE')",
                                (_BACKGROUND_OPEN,)).fetchone()[0]
        assert _definers(conn) == _DEFINERS | {_LATER_NAMES, _BACKGROUND_OPEN}
        # Today's background spend is $0.05 in the ledger and $0.05 still open.
        assert conn.execute("SELECT companion_background_open(0.11)").fetchone()[0] is True
        assert conn.execute("SELECT companion_background_open(0.10)").fetchone()[0] is False


def test_rls_uses_transaction_local_owner_context(database: str) -> None:
    apply_migrations(database)
    owner_a, owner_b = uuid.uuid4(), uuid.uuid4()

    with psycopg.connect(database, autocommit=True) as admin:
        if admin.execute("SELECT 1 FROM pg_roles WHERE rolname = 'lit38_runtime'").fetchone() is None:
            admin.execute("CREATE ROLE lit38_runtime NOLOGIN")
        admin.execute("GRANT USAGE ON SCHEMA public TO lit38_runtime")
        admin.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO lit38_runtime")
        admin.execute(
            "INSERT INTO users (id, display_name) VALUES (%s, 'A'), (%s, 'B')",
            (owner_a, owner_b),
        )

    with psycopg.connect(database) as conn:
        conn.execute("SET LOCAL ROLE lit38_runtime")
        conn.execute("SELECT set_config('app.owner_id', %s, true)", (str(owner_a),))
        assert conn.execute("SELECT id FROM users").fetchall() == [(owner_a,)]
        conn.commit()
        conn.execute("SET LOCAL ROLE lit38_runtime")
        assert conn.execute("SELECT id FROM users").fetchall() == []


def test_prefiltered_vector_search_excludes_ineligible_candidates(database: str) -> None:
    apply_migrations(database)
    owner_id, book_id, incarnation = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    chapter_1, chapter_2 = uuid.uuid4(), uuid.uuid4()
    chunk_visible, chunk_future, chunk_retracted, chunk_wrong_space = (
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
    )

    with psycopg.connect(database) as conn:
        _seed_owner_and_book(conn, owner_id, book_id, incarnation)
        for chapter_id, key, ordinal in (
            (chapter_1, "chapter-1", 1),
            (chapter_2, "chapter-2", 2),
        ):
            conn.execute(
                """
                INSERT INTO chapters
                  (owner_id, book_id, book_incarnation, id, chapter_key, revealed_at, content_hash)
                VALUES (%s, %s, %s, %s, %s, %s, repeat(%s, 64))
                """,
                (owner_id, book_id, incarnation, chapter_id, key, ordinal, str(ordinal)),
            )
            conn.execute(
                """
                INSERT INTO ingested_chapters
                  (owner_id, book_id, book_incarnation, chapter_id, content_hash, completed_at)
                VALUES (%s, %s, %s, %s, repeat(%s, 64), now())
                """,
                (owner_id, book_id, incarnation, chapter_id, str(ordinal)),
            )
        for chunk_id, chapter_id, ordinal, text, retracted_at in (
            (chunk_visible, chapter_1, 1, "eligible", None),
            (chunk_future, chapter_2, 2, "future", None),
            (chunk_retracted, chapter_1, 1, "retracted", "now()"),
            (chunk_wrong_space, chapter_1, 1, "wrong space", None),
        ):
            conn.execute(
                """
                INSERT INTO chunks
                  (owner_id, book_id, book_incarnation, id, chapter_id, revealed_at, text, retracted_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, CASE WHEN %s::boolean THEN now() ELSE NULL END)
                """,
                (
                    owner_id,
                    book_id,
                    incarnation,
                    chunk_id,
                    chapter_id,
                    ordinal,
                    text,
                    retracted_at is not None,
                ),
            )
        for chunk_id, vector, embedding_space in (
            (chunk_visible, "[0,1,0]", "test-space"),
            (chunk_future, "[1,0,0]", "test-space"),
            (chunk_retracted, "[1,0,0]", "test-space"),
            (chunk_wrong_space, "[1,0,0]", "other-space"),
        ):
            conn.execute(
                """
                INSERT INTO chunk_embeddings
                  (owner_id, book_id, book_incarnation, chunk_id, embedding_model,
                   embedding_dimension, embedding_space, distance_metric, embedding)
                VALUES (%s, %s, %s, %s, 'test-model', 3, %s, 'cosine', %s::vector)
                """,
                (owner_id, book_id, incarnation, chunk_id, embedding_space, vector),
            )

        rows = conn.execute(
            """
            SELECT chunk_id FROM search_chunks_prefiltered(
              %s, %s, %s, 1, 'test-model', 3, 'test-space', 'cosine', '[1,0,0]'::vector, 10
            )
            """,
            (owner_id, book_id, incarnation),
        ).fetchall()

    assert rows == [(chunk_visible,)]
