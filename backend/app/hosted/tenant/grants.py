"""The exact table privileges of the hosted tenant and worker roles (ADR 0021, ADR 0024, ADR 0041
section 3).

One list per role, read by that role's startup check, the test fixtures and the deployment's role
script, so a table added for the companion is granted and checked in one place. Each role must
hold exactly its privileges: one more or one fewer refuses startup.
"""

from __future__ import annotations

TENANT_TABLE_PRIVILEGES: dict[str, frozenset[str]] = {
    "books": frozenset({"SELECT", "INSERT", "UPDATE"}),
    # DELETE on source_objects, reading_state, reader_preferences, the three marks and chapters:
    # removing a book erases what was about it (ADR 0041 part 8). Deleting a book's chapters
    # takes its whole story memory with them (migration 0024).
    "source_objects": frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"}),
    "reading_state": frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"}),
    "reader_preferences": frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"}),
    "book_search_documents": frozenset({"SELECT", "INSERT", "DELETE"}),
    "highlights": frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"}),
    "annotations": frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"}),
    "bookmarks": frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"}),
    "chapters": frozenset({"SELECT", "DELETE"}),
    "ingested_chapters": frozenset({"SELECT"}),
    "chapter_summaries": frozenset({"SELECT"}),
    "entities": frozenset({"SELECT", "INSERT", "UPDATE"}),
    "aliases": frozenset({"SELECT", "INSERT"}),
    "edges": frozenset({"SELECT", "INSERT"}),
    "events": frozenset({"SELECT"}),
    "event_participants": frozenset({"SELECT", "INSERT"}),
    "entity_state": frozenset({"SELECT", "INSERT"}),
    "entity_corrections": frozenset({"SELECT", "INSERT"}),
    "themes": frozenset({"SELECT"}),
    "cost_ledger": frozenset({"SELECT", "INSERT"}),
    "cost_reservations": frozenset({"SELECT", "INSERT", "UPDATE"}),
    "jobs": frozenset({"SELECT", "INSERT", "UPDATE"}),
    "provider_credentials": frozenset({"SELECT", "INSERT", "UPDATE"}),
    "provider_model_settings": frozenset({"SELECT", "INSERT", "UPDATE"}),
    "owner_limits": frozenset({"SELECT"}),
    "owner_request_windows": frozenset({"SELECT", "INSERT", "UPDATE"}),
    "audit_events": frozenset({"INSERT"}),
    # ADR 0041 part 1: the read map (migration 0018).
    "read_ranges": frozenset({"SELECT", "INSERT", "DELETE"}),
    "skimmed_ranges": frozenset({"SELECT", "INSERT", "DELETE"}),
    "read_position": frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"}),
    # ADR 0041 part 2: conversations (migration 0019).
    "conversation_turns": frozenset({"SELECT", "INSERT", "DELETE"}),
    # ADR 0041 part 6: chapter notes (migration 0021). Companion jobs are inserted through the
    # existing grant on ``jobs``; the tenant role holds nothing on ``provider_pauses``.
    "chapter_notes": frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"}),
    # ADR 0041 part 7: what Mothli remembers about the reader (migration 0022). Recollect jobs
    # are inserted through the existing grant on ``jobs``.
    "recollections": frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"}),
    "recollection_cursors": frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"}),
}

# The job worker (ADR 0024): BYPASSRLS, because it schedules across owners. Every owner's
# companion work after a claim goes through the tenant role instead (ADR 0041 section 5); the
# worker itself only claims, leases, finishes and pauses jobs, commits extraction chapters, and
# publishes a chapter's story memory under the job's lease (ADR 0042 part 2).
WORKER_TABLE_PRIVILEGES: dict[str, frozenset[str]] = {
    "jobs": frozenset({"SELECT", "UPDATE"}),
    "job_attempts": frozenset({"SELECT", "INSERT", "UPDATE"}),
    "chapters": frozenset({"SELECT", "INSERT"}),
    "chapter_summaries": frozenset({"INSERT"}),
    "cost_ledger": frozenset({"SELECT", "INSERT"}),
    "ingested_chapters": frozenset({"SELECT", "INSERT"}),
    "provider_credentials": frozenset({"SELECT"}),
    "provider_model_settings": frozenset({"SELECT"}),
    "owner_limits": frozenset({"SELECT"}),
    "cost_reservations": frozenset({"SELECT", "INSERT", "UPDATE"}),
    "audit_events": frozenset({"INSERT"}),
    # ADR 0041 part 6: the platform-wide pause after the provider refused the account.
    "provider_pauses": frozenset({"SELECT", "INSERT", "UPDATE"}),
    # ADR 0042 part 2: publishing one chapter's story memory. INSERT for what a chapter adds;
    # UPDATE on entity_state to close a state the chapter replaces; SELECT on what the working
    # copy was built from, which the publish reads again to refuse a copy that moved meanwhile.
    "entities": frozenset({"SELECT", "INSERT"}),
    "aliases": frozenset({"SELECT", "INSERT"}),
    "entity_state": frozenset({"SELECT", "INSERT", "UPDATE"}),
    "edges": frozenset({"INSERT"}),
    "events": frozenset({"INSERT"}),
    "event_participants": frozenset({"INSERT"}),
    "themes": frozenset({"INSERT"}),
    # ADR 0042 part 5: whether a claimed job is the library account's, whose editions are built
    # whole rather than up to a read map.
    "library_account": frozenset({"SELECT"}),
}

# ADR 0041 part 3 (migration 0020): the platform's day total is reached only through these two
# functions, which run as their owner and answer "fits" or nothing, never a sum. The tenant role
# holds no privilege on ``platform_spend_days`` itself, and PUBLIC holds none on the functions.
# ADR 0042 part 5 (migration 0026): a library edition's story memory is reached by a reader only
# through the door, which hands out the rows up to the reader's own place and nothing else.
TENANT_FUNCTION_PRIVILEGES: dict[str, frozenset[str]] = {
    "companion_platform_reserve(numeric,numeric)": frozenset({"EXECUTE"}),
    "companion_platform_adjust(date,numeric,numeric)": frozenset({"EXECUTE"}),
    "library_story_memory(uuid)": frozenset({"EXECUTE"}),
}

# ADR 0042 part 5: the operator's library build (python -m app.hosted.library_memory). Its own
# role only makes the library account once and finds it again, with row security on; adding,
# queueing and reporting the account's books go through the tenant role bound to that account,
# as a reader's add does. Never in web or worker.
LIBRARY_TABLE_PRIVILEGES: dict[str, frozenset[str]] = {
    "users": frozenset({"INSERT"}),
    "library_account": frozenset({"SELECT", "INSERT"}),
}

# ADR 0041 part 8: the account purge, run once a day from the command line, never by web or
# worker. It erases a reader 30 days after they asked, so it may read and delete every row that
# carries an ``owner_id``: those tables are found in the database's own catalogue when the grants
# are made and when the role is checked, so a table a later migration adds is covered without
# anyone listing it. Beyond them it reads and updates ``users`` (to find who is due and leave a
# bare id behind) and writes one content-free audit event.
PURGE_OWNER_TABLE_PRIVILEGES = frozenset({"SELECT", "DELETE"})
PURGE_OTHER_TABLE_PRIVILEGES: dict[str, frozenset[str]] = {
    "users": frozenset({"SELECT", "UPDATE"}),
    "audit_events": frozenset({"INSERT"}),
}

ALL_TABLE_PRIVILEGES = frozenset(
    {"SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"}
)


def tenant_grants() -> list[tuple[str, tuple[str, ...]]]:
    """(table, privileges) pairs in a stable order, for a script that grants them."""
    return [
        (table, tuple(sorted(privileges)))
        for table, privileges in sorted(TENANT_TABLE_PRIVILEGES.items())
    ]


def tenant_function_grants() -> list[tuple[str, tuple[str, ...]]]:
    """(function signature, privileges) pairs in a stable order, for a script that grants them."""
    return [
        (function, tuple(sorted(privileges)))
        for function, privileges in sorted(TENANT_FUNCTION_PRIVILEGES.items())
    ]


def worker_grants() -> list[tuple[str, tuple[str, ...]]]:
    """(table, privileges) pairs of the worker role in a stable order."""
    return [
        (table, tuple(sorted(privileges)))
        for table, privileges in sorted(WORKER_TABLE_PRIVILEGES.items())
    ]


def purge_table_privileges(owner_tables) -> dict[str, frozenset[str]]:
    """The purge role's exact privileges, given the tables that have an ``owner_id`` column."""
    privileges = {table: PURGE_OWNER_TABLE_PRIVILEGES for table in owner_tables}
    for table, extra in PURGE_OTHER_TABLE_PRIVILEGES.items():
        privileges[table] = privileges.get(table, frozenset()) | extra
    return privileges


def purge_grants(owner_tables) -> list[tuple[str, tuple[str, ...]]]:
    """(table, privileges) pairs of the purge role in a stable order."""
    return [
        (table, tuple(sorted(privileges)))
        for table, privileges in sorted(purge_table_privileges(owner_tables).items())
    ]
