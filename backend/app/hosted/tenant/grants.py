"""The exact table privileges of the hosted tenant and worker roles (ADR 0021, ADR 0024, ADR 0041
section 3).

One list per role, read by that role's startup check, the test fixtures and the deployment's role
script, so a table added for the companion is granted and checked in one place. Each role must
hold exactly its privileges: one more or one fewer refuses startup.
"""

from __future__ import annotations

TENANT_TABLE_PRIVILEGES: dict[str, frozenset[str]] = {
    "books": frozenset({"SELECT", "INSERT", "UPDATE"}),
    # DELETE on source_objects, reading_state, reader_preferences and the three marks: removing a
    # book erases what was about it (ADR 0041 part 8).
    #
    # Nothing on the story memory tables (``STORY_MEMORY_TABLES``, ADR 0044 step 7): a book's
    # notes are an edition's, the notes account's rows, and a reader's session reaches them only
    # by executing the door (``story_memory_door`` below), which hands out the rows up to the
    # reader's own place. No reader owns a memory row or corrects one.
    "source_objects": frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"}),
    "reading_state": frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"}),
    "reader_preferences": frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"}),
    "book_search_documents": frozenset({"SELECT", "INSERT", "DELETE"}),
    "highlights": frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"}),
    "annotations": frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"}),
    "bookmarks": frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"}),
    "cost_ledger": frozenset({"SELECT", "INSERT"}),
    "cost_reservations": frozenset({"SELECT", "INSERT", "UPDATE"}),
    "jobs": frozenset({"SELECT", "INSERT", "UPDATE"}),
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
    # ADR 0045 (migration 0037), ADR 0046: the reader's plan. Read only: a reader's session
    # never writes it.
    "reader_plans": frozenset({"SELECT"}),
}

# A book's story memory (ADR 0042, ADR 0044): the tables the door reads (the first nine), what a
# chapter's events name, the old reader corrections, and a book's text cut up for search (no
# hosted code writes those two). The tenant role holds no privilege on any of them (ADR 0044 step 7); its startup check
# refuses one held on any table outside its list. ADR 0044 step 10: and the words of every name,
# which only the later-name check reads (``story_memory_later_names``).
STORY_MEMORY_TABLES = (
    "chapters", "ingested_chapters", "chapter_summaries", "entities", "aliases", "entity_state",
    "edges", "events", "themes", "event_participants", "entity_corrections", "chunks",
    "chunk_embeddings", "name_words",
)

# The job worker (ADR 0024): BYPASSRLS, because it schedules across owners. Every owner's
# companion work after a claim goes through the tenant role instead (ADR 0041 section 5); the
# worker itself only claims, leases, finishes and pauses jobs (reading an owner's spend to hold
# back a capped owner's companion job), and publishes a chapter's story memory under the job's
# lease (ADR 0042 part 2).
WORKER_TABLE_PRIVILEGES: dict[str, frozenset[str]] = {
    "jobs": frozenset({"SELECT", "UPDATE"}),
    "job_attempts": frozenset({"SELECT", "INSERT", "UPDATE"}),
    "chapters": frozenset({"SELECT", "INSERT"}),
    "chapter_summaries": frozenset({"INSERT"}),
    # Read only: the claim holds back a reader past their background guard
    # (``spend.background_guard_reached``). Every living
    # job's money is reserved, noted and settled through the tenant role (``companion/spend.py``,
    # ADR 0041 section 5); the worker's own writes went with the book job (ADR 0043).
    "cost_ledger": frozenset({"SELECT"}),
    "ingested_chapters": frozenset({"SELECT", "INSERT"}),
    "owner_limits": frozenset({"SELECT"}),
    "cost_reservations": frozenset({"SELECT"}),
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
    # ADR 0044 step 10: each published name's words, for the later-name check (migration 0035).
    "name_words": frozenset({"INSERT"}),
    # ADR 0044 steps 2 and 3: which account owns the editions, to find a file's edition and to
    # make it on its first chapter.
    "notes_account": frozenset({"SELECT"}),
    # ADR 0044 step 3: a run builds into the edition of its reader's file. SELECT to read the
    # claimed book's two fingerprints and find the edition with the same; INSERT to make the
    # edition (a books row of the notes account, no file) on its first chapter. The edition's
    # notes are read with the SELECT already held on the memory tables above, below the chapter
    # being built. Nothing on source_objects: an edition never has a file, and a chapter's text
    # is read from the reader's own copy through the tenant role.
    "books": frozenset({"SELECT", "INSERT"}),
    # ADR 0045 section 5, step 6: background work follows where a reader stands with their plan.
    # The claim reads it with ``reader_plan_state`` (below), which runs as its caller and reads
    # the reader's row here and their spend in the ledger and reservations above. Read only.
    "reader_plans": frozenset({"SELECT"}),
}

# ADR 0045 step 6: and when the reader signed up, for a new reader's warm-up (within so many days
# of sign-up). SELECT on these two columns of ``users`` only, never the table: the worker reads
# no name, email or status. The startup check verifies every column of the table both ways.
WORKER_COLUMN_PRIVILEGES: dict[str, frozenset[str]] = {
    "users": frozenset({"id", "created_at"}),
}
# ADR 0045 step 6: where a reader stands, the one function the session, the spend check and the
# worker all call, so they cannot disagree (ADR 0045 section 1).
WORKER_FUNCTION_PRIVILEGES: dict[str, frozenset[str]] = {
    "reader_plan_state(uuid)": frozenset({"EXECUTE"}),
}

# ADR 0041 part 3 (migration 0020): the platform's day total is reached only through these two
# functions, which run as their owner and answer "fits" or nothing, never a sum. The tenant role
# holds no privilege on ``platform_spend_days`` itself, and PUBLIC holds none on the functions.
# ADR 0042 part 5 (migration 0026), ADR 0044 step 2 (migration 0032): an edition's story memory
# is reached by a reader only through the door, which hands out the rows up to the reader's own
# place and nothing else, for any book of theirs whose file matches an edition.
# ADR 0044 step 10 (migration 0035): the later-name check, the one other way a reader's session
# touches an edition: it answers which words of a sentence it is given name something the book
# reveals after the reader's place, and nothing else; it takes no place, only a lower ceiling.
TENANT_FUNCTION_PRIVILEGES: dict[str, frozenset[str]] = {
    "companion_platform_reserve(numeric,numeric)": frozenset({"EXECUTE"}),
    "companion_platform_adjust(date,numeric,numeric)": frozenset({"EXECUTE"}),
    # Migration 0036: whether all readers' background work today is under its budget, yes or no.
    "companion_background_open(numeric)": frozenset({"EXECUTE"}),
    "story_memory_door(uuid)": frozenset({"EXECUTE"}),
    "story_memory_later_names(uuid,integer,text[],text[],text[],text[],text[],text[])":
        frozenset({"EXECUTE"}),
    # Where a reader stands (migration 0042, ADR 0046), which runs as its caller, so row-level
    # security shows the reader only their own row and spend.
    "reader_plan_state(uuid)": frozenset({"EXECUTE"}),
}

# ADR 0045 sections 1 and 7, step 3 (migration 0039): the billing role, used only by Paddle's
# message route and, from step 4, the subscribe, cancel, keep and portal routes and the regular
# check against Paddle (app/hosted/subscribe.py, app/hosted/paddle_check.py). It does not bypass row-level
# security: each message's transaction is set to the account the message is for, as the tenant
# role's is set to the session's reader, so it reads and writes that one reader's plan and their
# recorded messages only. Exactly section 1's rights on its two tables, and one function: which
# account holds a subscription (or, for a new one, whether the account a message names exists and
# is not erased), the one look across accounts it needs, answered as one id.
BILLING_TABLE_PRIVILEGES: dict[str, frozenset[str]] = {
    "reader_plans": frozenset({"SELECT", "INSERT", "UPDATE"}),
    "paddle_events": frozenset({"SELECT", "INSERT"}),
    # Step 4 (migration 0040): the checkouts the subscribe route makes, read and used up when a
    # new subscription is applied. No DELETE: an erased account's rows go with the purge.
    "paddle_checkouts": frozenset({"SELECT", "INSERT", "UPDATE"}),
}
BILLING_FUNCTION_PRIVILEGES: dict[str, frozenset[str]] = {
    "paddle_reader(text,uuid)": frozenset({"EXECUTE"}),
    # Step 4: the stored subscriptions the regular check fetches from Paddle, ids only.
    "paddle_plans_to_check(integer)": frozenset({"EXECUTE"}),
}

# ADR 0041 part 8: the account purge, run once a day from the command line, never by web or
# worker. It erases a reader 30 days after they asked, so it may read and delete every row that
# carries an ``owner_id``: those tables are found in the database's own catalogue when the grants
# are made and when the role is checked, so a table a later migration adds is covered without
# anyone listing it. Beyond them it reads and updates ``users`` (to find who is due and leave a
# bare id behind) and writes one content-free audit event.
#
# ADR 0044 step 8: it also erases an edition 30 days after the last reader holding its file is
# gone. UPDATE on books, to mark an edition with no holder ("no holder since", migration 0034),
# clear the mark when a holder is back, and leave an erased edition's row as a bare id (its
# fingerprints, title and source cleared), as removing a book leaves a reader's: the notes
# account's ledger, reservations and jobs still refer to it. Its notes go with its chapters
# (the cascade of migration 0024), with the DELETE it already holds.
PURGE_OWNER_TABLE_PRIVILEGES = frozenset({"SELECT", "DELETE"})
PURGE_OTHER_TABLE_PRIVILEGES: dict[str, frozenset[str]] = {
    "users": frozenset({"SELECT", "UPDATE"}),
    "audit_events": frozenset({"INSERT"}),
    "books": frozenset({"UPDATE"}),
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
