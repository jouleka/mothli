"""The exact table privileges of the hosted tenant role (ADR 0021, ADR 0041 section 3).

One list, read by the repository's startup check, the test fixtures and the deployment's role
script, so a table added for the companion is granted and checked in one place. The role must hold
exactly these privileges: one more or one fewer refuses startup.
"""

from __future__ import annotations

TENANT_TABLE_PRIVILEGES: dict[str, frozenset[str]] = {
    "books": frozenset({"SELECT", "INSERT", "UPDATE"}),
    "source_objects": frozenset({"SELECT", "INSERT", "UPDATE"}),
    "reading_state": frozenset({"SELECT", "INSERT", "UPDATE"}),
    "reader_preferences": frozenset({"SELECT", "INSERT", "UPDATE"}),
    "book_search_documents": frozenset({"SELECT", "INSERT", "DELETE"}),
    "highlights": frozenset({"SELECT", "INSERT", "UPDATE"}),
    "annotations": frozenset({"SELECT", "INSERT", "UPDATE"}),
    "bookmarks": frozenset({"SELECT", "INSERT", "UPDATE"}),
    "chapters": frozenset({"SELECT"}),
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
}

# ADR 0041 part 3 (migration 0020): the platform's day total is reached only through these two
# functions, which run as their owner and answer "fits" or nothing, never a sum. The tenant role
# holds no privilege on ``platform_spend_days`` itself, and PUBLIC holds none on the functions.
TENANT_FUNCTION_PRIVILEGES: dict[str, frozenset[str]] = {
    "companion_platform_reserve(numeric,numeric)": frozenset({"EXECUTE"}),
    "companion_platform_adjust(date,numeric,numeric)": frozenset({"EXECUTE"}),
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
