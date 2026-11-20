"""Background work is Mothli's, not the reader's allowance (ADR 0041 section 6, amended 20
November 2026), end to end through the hosted app and the worker, on real PostgreSQL.

What was measured on 20 November 2026: a new reader imported a book, marked chapters 1 to 30 read
and asked two questions; the chapter notes those marks started in the background used $0.076 of
the reader's $0.10 day, and the chat answered "We've talked a lot today". Proven here: notes for
thirty chapters leave the reader's whole allowance; background spend is in the ledger, marked,
and on the platform's day; the budget of every reader's background work, once used, holds the
work for the next UTC day without a word to the reader and without touching the chat; and the
"talked a lot today" line comes only from the reader's own conversation. The guard per reader is
proven beside the notes (``test_companion_notes_postgres.py``) and at the adapter
(``test_companion_spend_postgres.py``). Stand-in writer and Jev only: nothing is paid.
"""

from __future__ import annotations

import re
import uuid
from decimal import Decimal

import pytest

from app.hosted.companion.spend import _DAY, set_spend_limits, spend_limits
from app.hosted.limits import update_limits
from tests.api._epub import epub_ncx
from tests.hosted import test_companion_notes_postgres as notes_gate
from tests.hosted.test_companion_stream_postgres import (
    StandIns,
    restore_caps,  # noqa: F401  (autouse: the hosted app sets this process's caps)
    sentences,
)

pytestmark = pytest.mark.postgres

admin_dsn = notes_gate.admin_dsn
database = notes_gate.database
tenant_dsn = notes_gate.tenant_dsn
worker_dsn = notes_gate.worker_dsn
readers = notes_gate.readers
notes_app = notes_gate.notes_app
drain, worker, jobs_of, notes_of, rows = (notes_gate.drain, notes_gate.worker, notes_gate.jobs_of,
                                         notes_gate.notes_of, notes_gate.rows)
CHAPTERS = 30


def said(chapter: int, number: int) -> str:
    return f"The keeper lit lamp {number} in part {chapter}."


def thirty_chapters() -> bytes:
    """An imported novel of thirty short chapters, every sentence distinct."""
    return epub_ncx(
        [(f"c{n}.xhtml", f"Part {n}", f"Part {n}", " ".join(said(n, i) for i in range(1, 13)))
         for n in range(1, CHAPTERS + 1)],
        title="The Long Lamp Book", author="Ada Writer")


class Models(StandIns):
    """The stream tests' stand-ins, also writing chapter notes: two true sentences of the
    chapter the prompt holds."""

    def __init__(self, client, monkeypatch, **options):
        super().__init__(client, monkeypatch, **options)
        self.notes = 0

    def complete(self, system, user, tier="cheap", schema=None):
        if schema is None and "CHAPTER TEXT" in user:
            ordinal = int(re.search(r"CHAPTER TEXT \(Part (\d+)\)", user)[1])
            with self._lock:
                self.notes += 1
            return f"{said(ordinal, 2)} {said(ordinal, 3)}", {"in": 100, "out": 40}
        return super().complete(system, user, tier=tier, schema=schema)


def spent(dsn: str, owner: uuid.UUID, *, background: bool) -> Decimal:
    return rows(dsn, "SELECT COALESCE(sum(usd),0) AS usd FROM cost_ledger"
                     " WHERE owner_id=%s AND background=%s", owner, background)[0]["usd"]


def platform_settled(dsn: str) -> Decimal:
    found = rows(dsn, "SELECT settled_usd FROM platform_spend_days"
                      " WHERE day=(now() AT TIME ZONE 'UTC')::date")
    return found[0]["settled_usd"] if found else Decimal(0)


def tomorrow(dsn: str):
    return rows(dsn, "SELECT (date_trunc('day', now() AT TIME ZONE 'UTC') + interval '1 day')"
                     " AT TIME ZONE 'UTC' AS at")[0]["at"]


def thirty_read(notes_app, readers, monkeypatch, label="a"):
    """A reader who imports the novel and marks chapters 1 to 30 read, as on 20 November."""
    _client, _tokens, app = notes_app
    models = Models(app.state.platform_client, monkeypatch, reply=said(1, 2))
    someone = notes_gate.reader(notes_app, readers, label)
    book = notes_gate.upload(someone.client, someone.tokens, label, thirty_chapters())
    someone.mark(book, list(range(1, CHAPTERS + 1)))
    return someone, book, models


def test_a_reader_who_marks_thirty_chapters_read_keeps_their_whole_allowance(
        notes_app, readers, database, worker_dsn, monkeypatch):
    """The break it catches: the failure of 20 November 2026. The reader's own day is set to
    exactly what their thirty chapters of notes cost Mothli: under the old rule that was their
    whole day, and the chat would say "We've talked a lot today" at the first question. Now they
    are answered until their own conversation has used the day, and only then see the line."""
    _client, _tokens, app = notes_app
    platform = platform_settled(database)
    a, book, models = thirty_read(notes_app, readers, monkeypatch)
    assert drain(worker(app, worker_dsn), 20) == 10        # three chapters a run, in turn
    assert models.notes == CHAPTERS
    assert [n["atom"] for n in notes_of(database, a.owner)] == list(range(1, CHAPTERS + 1))

    # The notes are in the reader's ledger, marked as background, and on the platform's day.
    background = spent(database, a.owner, background=True)
    assert background > 0 and spent(database, a.owner, background=False) == 0
    ledger = rows(database, "SELECT purpose,background FROM cost_ledger WHERE owner_id=%s",
                  a.owner)
    assert all(row["background"] and row["purpose"].startswith("notes-") for row in ledger)
    assert platform_settled(database) == platform + background

    update_limits(database, a.owner, {"max_spend_usd_per_day": background,
                                      "max_spend_usd_per_month": background})
    asked = 0
    while spent(database, a.owner, background=False) < background:
        events = a.ask(book)
        assert events[-1]["type"] == "done", events[-1]
        assert sentences(events) == [said(1, 2)]
        asked += 1
        assert asked < 500
    assert asked >= 2                                      # the two questions of 20 November
    writes = models.writes
    assert a.ask(book) == [{"type": "error", "status": 429, "code": "allowance_reached",
                            "message": _DAY}]
    assert models.writes == writes


def test_the_background_budget_waits_for_tomorrow_in_silence_and_the_chat_goes_on(
        notes_app, readers, database, worker_dsn, monkeypatch):
    """The break it catches: the budget of every reader's background work, once used, failing the
    job (a failure that is retried all day, or one a reader could be shown), spending anything,
    or refusing the reader's chat."""
    _client, _tokens, app = notes_app
    limits = spend_limits()
    set_spend_limits(reader_usd_per_day=limits.reader_usd_per_day,
                     reader_usd_per_month=limits.reader_usd_per_month,
                     platform_usd_per_day=limits.platform_usd_per_day,
                     background_usd_per_day=0)
    a, book, models = thirty_read(notes_app, readers, monkeypatch)
    assert drain(worker(app, worker_dsn)) == 1
    assert models.notes == 0 and notes_of(database, a.owner) == []
    assert rows(database, "SELECT * FROM cost_ledger WHERE owner_id=%s", a.owner) == []
    assert rows(database, "SELECT * FROM cost_reservations WHERE owner_id=%s", a.owner) == []
    jobs = jobs_of(database, a.owner)
    assert [(job["state"], job["sanitized_error"]) for job in jobs] == [
        ("succeeded", None), ("pending", None)]
    assert jobs[1]["run_after"] == tomorrow(database)
    events = a.ask(book)
    assert sentences(events) == [said(1, 2)] and events[-1]["type"] == "done"
    assert spent(database, a.owner, background=False) > 0


def test_background_work_refused_by_the_platforms_day_waits_for_tomorrow(
        notes_app, readers, database, worker_dsn, monkeypatch):
    """The break it catches: background spend left off the platform's day, so the owner's worst
    day is passed; or, once the platform's day is used, a notes job that fails and is retried
    instead of waiting for the next UTC day."""
    _client, _tokens, app = notes_app
    a, _book, models = thirty_read(notes_app, readers, monkeypatch)
    limits = spend_limits()
    set_spend_limits(reader_usd_per_day=limits.reader_usd_per_day,
                     reader_usd_per_month=limits.reader_usd_per_month,
                     platform_usd_per_day=platform_settled(database))
    assert drain(worker(app, worker_dsn)) == 1
    assert models.notes == 0
    assert rows(database, "SELECT * FROM cost_ledger WHERE owner_id=%s", a.owner) == []
    jobs = jobs_of(database, a.owner)
    assert [job["state"] for job in jobs] == ["succeeded", "pending"]
    assert jobs[1]["run_after"] == tomorrow(database)
