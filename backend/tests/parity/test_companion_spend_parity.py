"""The companion's spend is recorded the same way locally and hosted (ADR 0041 part 3, section 9.3).

One sequence of model calls through the unchanged ``budgeted_completion`` and ``budgeted_jev``
(stand-in writer and Jev: a call that succeeds, one the provider refuses, one that times out, one
that fails) and then the cost methods called directly (an actual noted with another model, a
settlement under another name, a reservation left open, the errors for ids that are not open) runs
through the local ``Catalog`` (temporary data directory) and through the hosted adapter, for a
book and for the home conversation. ``get_costs`` and ``get_cost_reservations`` must then answer
the same, but for the ids and the times; dollars agree to the hosted column's ten places.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import psycopg
import pytest

from app.catalog.catalog import HOME_SPEND
from app.config import Settings
from app.cost import budgeted_completion
from app.hosted.companion.spend import set_spend_limits, spend_limits
from app.hosted.companion.store import HostedCompanionCatalog
from app.hosted.tenant.models import OwnerId
from app.llm.jev import JevUnavailable
from app.llm.jev_budget import budgeted_jev
from tests.hosted.test_companion_read_map_postgres import lamp_book
from tests.parity import test_companion_parity as read_map_parity
from tests.parity.test_companion_parity import LocalReader

pytestmark = pytest.mark.postgres

postgres_database = read_map_parity.postgres_database

SETTINGS = Settings(_env_file=None)


@pytest.fixture(autouse=True)
def caps():
    before = spend_limits()
    set_spend_limits(reader_usd_per_day=10, reader_usd_per_month=10, platform_usd_per_day=10)
    yield
    set_spend_limits(reader_usd_per_day=before.reader_usd_per_day,
                     reader_usd_per_month=before.reader_usd_per_month,
                     platform_usd_per_day=before.platform_usd_per_day)


class Writer:
    provider = "openai-compatible"
    _is_native_openai = True

    def __init__(self, fail=None):
        self.fail = fail

    def billing_model(self, tier):
        return "gpt-4o-mini"

    _model_for = billing_model

    def complete(self, system, user, *, tier, schema=None, max_output_tokens=None):
        if self.fail is not None:
            raise self.fail
        return "Mara kept lamp 2.", {"in": 40, "out": 12}


def checker(fail=None):
    def evaluate(state, questions, **_options):
        if fail is not None:
            raise fail
        return {"q": {"noul": .9}}, {"in": 50, "out": 5}
    return SimpleNamespace(model="jev-1.14.0", evaluate=evaluate)


def run(catalog, book):
    """The same calls, in the same order, on one mode's catalog; returns what each raised."""
    outcomes = []

    def attempt(fn, *args, **kwargs):
        try:
            fn(*args, **kwargs)
            outcomes.append("ok")
        except Exception as exc:  # noqa: BLE001 - the kind of failure is what is compared
            outcomes.append(type(exc).__name__)

    write = dict(phase="stream-reply", system="Reply.", user="What did Mara do?", tier="large")
    check = dict(phase="stream-sentence-check", state={"sentence": "Mara kept lamp 2."},
                 questions={"q": {"type": "noul"}})
    attempt(budgeted_completion, catalog, SETTINGS, Writer(), book, **write)
    attempt(budgeted_jev, catalog, SETTINGS, checker(), book, **check)
    attempt(budgeted_jev, catalog, SETTINGS, checker(JevUnavailable("x", reason="no_credits")),
            book, **check)
    attempt(budgeted_jev, catalog, SETTINGS, checker(JevUnavailable("timed out")), book, **check)
    attempt(budgeted_completion, catalog, SETTINGS, Writer(TimeoutError("t")), book, **write)

    def reserve(phase, model, usd, **extra):
        return catalog.reserve_cost(book, phase=phase, model=model, input_tokens=300,
                                    output_tokens=4096, usd=usd, max_input_tokens=10**7,
                                    max_output_tokens=10**7, max_usd=5.0, **extra)

    noted = reserve("notes-write", "gpt-4o-mini", 0.0025, chapter_ordinal=2)
    catalog.note_reservation_actual(book, noted, input_tokens=200, output_tokens=30, usd=0.0004,
                                    model="gpt-4o")
    catalog.settle_cost(book, noted, phase="notes-write-provider-limit-violation")
    released = reserve("stream-intent", "jev-1.14.0", 0.00002)
    catalog.discard_cost_reservation(book, released)
    left_open = reserve("stream-second-look", "gpt-4o-mini", 0.003, chapter_ordinal=1)
    catalog.note_reservation_actual(book, left_open, input_tokens=10, output_tokens=2, usd=0.0001)
    for gone in (noted, released, "not-a-reservation", uuid.uuid4().hex):
        attempt(catalog.settle_cost, book, gone)
        attempt(catalog.discard_cost_reservation, book, gone)
        attempt(catalog.note_reservation_actual, book, gone, input_tokens=1, output_tokens=1,
                usd=0.0)
    return outcomes


def shown(catalog, book):
    costs = [{**{k: v for k, v in cost.items() if k not in ("at", "usd")},
              "usd": round(cost["usd"], 10)} for cost in catalog.get_costs(book)]
    held = [{**{k: v for k, v in row.items()
                if k not in ("at", "reservation_id", "reserved_usd", "actual_usd")},
             "reserved_usd": round(row["reserved_usd"], 10),
             "actual_usd": None if row["actual_usd"] is None else round(row["actual_usd"], 10)}
            for row in catalog.get_cost_reservations(book)]
    return costs, held


def test_the_same_calls_leave_the_same_ledger_locally_and_hosted(tmp_path, postgres_database):
    """The break it catches: a hosted adapter that records a call differently from the local
    catalog (another size, another name, a refused call charged, an unknown outcome forgotten, an
    actual not replacing the estimate, a settlement or release that works twice), so hosted spend
    and its caps would disagree with what local mode has been measured on."""
    local = LocalReader(tmp_path, lamp_book())
    owner = uuid.uuid4()
    book = uuid.uuid4()
    with psycopg.connect(postgres_database) as conn:
        conn.execute("INSERT INTO users (id,display_name) VALUES (%s,'Parity reader')", (owner,))
        conn.execute(
            "INSERT INTO books (owner_id,id,incarnation,title,author,schema_version)"
            " VALUES (%s,%s,%s,'The Lamp Book','Ada Writer',1)", (owner, book, uuid.uuid4()))
    hosted = HostedCompanionCatalog(postgres_database, OwnerId(owner))
    try:
        here, there = local.app.state.catalog, hosted
        for local_book, hosted_book in ((local.book, str(book)), (HOME_SPEND, HOME_SPEND)):
            assert run(here, local_book) == run(there, hosted_book)
            assert shown(here, local_book) == shown(there, hosted_book)
            costs, held = shown(there, hosted_book)
            assert [cost["phase"] for cost in costs] == [
                "stream-reply", "stream-sentence-check", "stream-sentence-check-failed-reserved",
                "stream-reply-failed-reserved", "notes-write-provider-limit-violation"]
            assert [(row["phase"], row["actual_input_tokens"]) for row in held] == [
                ("stream-second-look", 10)]
        # The home conversation's spend is its own on both sides, and a book unknown to the
        # reader is refused alike before anything is reserved.
        for catalog_ in (here, there):
            with pytest.raises(ValueError, match="unknown book"):
                catalog_.reserve_cost(str(uuid.uuid4()), phase="stream-reply", model="gpt-4o-mini",
                                      input_tokens=1, output_tokens=1, usd=0.0,
                                      max_input_tokens=10, max_output_tokens=10, max_usd=1.0)
    finally:
        local.close()
