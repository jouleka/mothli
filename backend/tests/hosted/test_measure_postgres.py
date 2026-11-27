"""Measuring before cutting costs, on real PostgreSQL: the cache split of every call's input is
recorded in the ledger (migration 0043), and the operator's command (``python -m
app.hosted.measure``) prints paying readers' replies and unique uploads and the calls' totals,
as numbers only.

The spend runs through the unchanged ``budgeted_completion`` / ``budgeted_jev`` with stand-in
clients over ``HostedCompanionCatalog`` on the RLS-enforced tenant role; the command runs on a
role granted exactly what the deployment's role script grants the operator. No model is called
and nothing is paid.
"""

from __future__ import annotations

import json
import uuid
from decimal import Decimal

import psycopg
import pytest

from app.cost import CostCeilingExceeded, budgeted_completion, usd_of
from app.hosted import measure
from app.hosted.companion.spend import _money
from tests.hosted import test_companion_read_map_postgres as read_map_gate
from tests.hosted.test_companion_spend_postgres import (
    SETTINGS,
    caps,  # noqa: F401  (autouse: generous process caps, restored afterwards)
    catalog,
    check,
    jev,
    shelve,
)
from tests.hosted.test_plans_postgres import operator_dsn  # noqa: F401  (fixture)

pytestmark = pytest.mark.postgres

admin_dsn = read_map_gate.admin_dsn
database = read_map_gate.database
tenant_dsn = read_map_gate.tenant_dsn
readers = read_map_gate.readers

MODEL = "gpt-6.1-terra"         # priced with its cache rates
PROMPT = "What did Mara do? " * 200   # reserves more input than any usage below


class CachingWriter:
    """A stand-in native writer that reports ``usage`` as the client parses it."""

    provider = "openai-compatible"
    _is_native_openai = True

    def __init__(self, usage: dict | None = None, fail: Exception | None = None) -> None:
        self.usage, self.fail = usage, fail

    def billing_model(self, tier):
        return MODEL

    _model_for = billing_model

    def complete(self, system, user, *, tier, schema=None, max_output_tokens=None):
        if self.fail is not None:
            raise self.fail
        return "Mara kept lamp 2.", dict(self.usage)


def write(adapter, book, writer, phase="stream-reply"):
    return budgeted_completion(adapter, SETTINGS, writer, book, phase=phase, system="Reply.",
                               user=PROMPT, tier="large")


def split_rows(dsn: str, owner: uuid.UUID) -> list[tuple]:
    with psycopg.connect(dsn) as conn:
        return conn.execute(
            "SELECT purpose,input_tokens,cache_read_tokens,cache_write_tokens,output_tokens,usd"
            " FROM cost_ledger WHERE owner_id=%s ORDER BY recorded_at,id", (owner,)).fetchall()


def test_every_settled_call_records_its_cache_split_and_its_money_is_unchanged(
    tenant_dsn: str, database: str, readers: dict
) -> None:
    """The break it catches: a call's cache split lost on its way to the ledger, recorded for a
    call whose split nobody knows (it would read as a call that used no cache), or the money of a
    call changed by recording it."""
    a = readers["a"]
    book = shelve(database, a)
    adapter = catalog(tenant_dsn, a)
    cached = {"in": 1000, "out": 50, "cache_read": 600, "cache_write": 100}
    result = write(adapter, book, CachingWriter(cached))
    assert result.usd == usd_of(MODEL, cached)
    write(adapter, "home", CachingWriter({"in": 800, "out": 40}), phase="home-reply")
    check(adapter, book, jev()[0])
    with pytest.raises(RuntimeError):
        write(adapter, book, CachingWriter(fail=RuntimeError("timed out")))
    # No usage from a real provider: charged at its reservation, and its split is unknown.
    write(adapter, book, CachingWriter({"in": 0, "out": 0}))
    with pytest.raises(CostCeilingExceeded):
        write(adapter, book, CachingWriter({"in": 10**6, "out": 1, "cache_read": 10**5}))

    rows = split_rows(database, a)
    assert [row[:5] for row in rows] == [
        ("stream-reply", 1000, 600, 100, 50),
        ("home-reply", 800, 0, 0, 40),
        ("stream-sentence-check", 50, None, None, 5),
        ("stream-reply-failed-reserved", rows[3][1], None, None, rows[3][4]),
        ("stream-reply", rows[4][1], None, None, rows[4][4]),
        ("stream-reply-provider-limit-violation", 10**6, 10**5, 0, 1),
    ]
    assert rows[0][5] == _money(usd_of(MODEL, cached))
    assert rows[1][5] == _money(usd_of(MODEL, {"in": 800, "out": 40}))
    with psycopg.connect(database) as conn:
        assert conn.execute(
            "SELECT count(*) FROM cost_reservations WHERE owner_id=%s AND state='reserved'",
            (a,)).fetchone()[0] == 0


HASH = {name: (name * 64)[:64] for name in "abcdef"}


def _book(conn, owner, file_hash, split_hash, *, source="upload", deleted=False,
          edition=False) -> None:
    conn.execute(
        "INSERT INTO books (owner_id,id,incarnation,title,author,source_kind,file_hash,"
        " split_hash,schema_version,deleted_at,edition_of)"
        " VALUES (%s,%s,%s,'A secret title','A secret author',%s,%s,%s,1,"
        " CASE WHEN %s THEN now() END,CASE WHEN %s THEN %s::uuid END)",
        (owner, uuid.uuid4(), uuid.uuid4(), source, file_hash, split_hash, deleted, edition,
         owner))


def _call(conn, owner, purpose, *, ago="0 days", background=False, input_tokens=100,
          read=None, write=None, output=10, usd="0.01", phase="synthesis") -> None:
    conn.execute(
        "INSERT INTO cost_ledger (owner_id,phase,model,input_tokens,output_tokens,usd,"
        " idempotency_key,purpose,background,cache_read_tokens,cache_write_tokens,recorded_at)"
        " VALUES (%s,%s,'m',%s,%s,%s,%s,%s,%s,%s,%s,now() - %s::interval)",
        (owner, phase, input_tokens, output, Decimal(usd), f"companion:{uuid.uuid4().hex}",
         purpose, background, read, write, ago))


def test_the_command_prints_paying_readers_and_the_calls_as_numbers_only(
    database: str, readers: dict, operator_dsn: str, monkeypatch, capsys  # noqa: F811
) -> None:
    """The break it catches: a reader who does not pay listed; a sentence check, a second look or
    background work counted as a reply; a book shared with another reader (or a free-library
    book, or a removed one) counted as unique, or one whose only other copy is the notes
    account's edition counted as shared; totals that lose the cache split; or a title, an author,
    a name or a fingerprint printed. And it runs on the operator's role as granted."""
    a, b = readers["a"], readers["b"]
    never = uuid.uuid4()
    with psycopg.connect(database) as conn:
        conn.execute("INSERT INTO users (id,display_name) VALUES (%s,'Reader C')", (never,))
        notes = conn.execute("SELECT account_id FROM notes_account").fetchone()[0]
        # A's uploads: a alone (and the notes account's edition of it), b also held by B, c a
        # file without its split fingerprint, d removed; e from the free library.
        _book(conn, a, HASH["a"], HASH["a"])
        _book(conn, notes, HASH["a"], HASH["a"], source=None, edition=True)
        _book(conn, a, HASH["b"], HASH["b"])
        _book(conn, b, HASH["b"], HASH["b"])
        _book(conn, a, HASH["c"], None)
        _book(conn, a, HASH["d"], HASH["d"], deleted=True)
        _book(conn, a, HASH["e"], HASH["e"], source="gutenberg:1")
        # B's only other upload is the same file as A's removed one: unique for B.
        _book(conn, b, HASH["d"], HASH["d"])
        _book(conn, never, HASH["f"], HASH["f"])
        # A's replies: two today, one failed yesterday on the home page, one 40 days ago; and
        # calls that are not replies.
        _call(conn, a, "stream-reply", read=60, write=10)
        _call(conn, a, "stream-reply", read=0, write=0)
        _call(conn, a, "home-reply-failed-reserved", ago="1 day")
        _call(conn, a, "stream-reply", ago="40 days")
        _call(conn, a, "stream-sentence-check", phase="judge")
        _call(conn, a, "stream-second-look")
        _call(conn, a, "notes-write", background=True)
        _call(conn, never, "stream-reply")

    monkeypatch.setenv("DATABASE_URL", operator_dsn)
    assert measure.main(["--days", "3"]) == 0
    printed = capsys.readouterr().out
    assert printed.count("\n") == 1
    for secret in ("secret", "Reader", *HASH.values()):
        assert secret not in printed
    shown = json.loads(printed)
    assert shown["days"] == 3

    by_owner = {reader["owner_id"]: reader for reader in shown["readers"]}
    assert set(by_owner) == {str(a), str(b)}
    days = sorted(by_owner[str(a)]["replies_per_day"].items())
    assert len(days) == 3 and days[0][0] == shown["from"] and days[-1][0] == shown["to"]
    assert [count for _day, count in days] == [0, 1, 2]
    assert sum(by_owner[str(a)]["replies_per_month"].values()) == 4
    assert by_owner[str(a)]["unique_uploads"] == 2
    assert sum(by_owner[str(b)]["replies_per_day"].values()) == 0
    assert by_owner[str(b)]["replies_per_month"] == {}
    assert by_owner[str(b)]["unique_uploads"] == 1

    calls = {(row["purpose"], row["phase"]): row for row in shown["calls"]}
    assert ("stream-reply", "synthesis") in calls
    replies = calls[("stream-reply", "synthesis")]      # A's two today and C's, not the old one
    assert replies == {
        "purpose": "stream-reply", "phase": "synthesis", "calls": 3,
        "full_price_input_tokens": 230, "cache_write_input_tokens": 10,
        "cache_read_input_tokens": 60, "output_tokens": 30, "usd": "0.03",
        "calls_without_split": 1,
    }
    assert calls[("notes-write", "synthesis")]["calls"] == 1
    assert calls[("stream-sentence-check", "judge")]["full_price_input_tokens"] == 100
