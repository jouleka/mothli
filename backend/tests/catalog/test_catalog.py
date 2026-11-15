"""Catalog (global catalog.db) tests — the shelf + reading_state + cost_ledger (ADR 0002 D14, ADR 0007
D-A2/Inv 12). The catalog is the single shared writer: one connection under a global lock, busy_timeout,
and a MONOTONIC high-water bookmark / ingest_progress (a backward update never lowers them — the spoiler
frontier must not regress; backward re-reading is LIT-17).

Written test-first (TDD): targets app.catalog.catalog.Catalog (does not exist yet) -> RED, then GREEN.
"""
import sqlite3
import threading

import pytest

from app.catalog.catalog import Catalog


def _cat(tmp_path):
    return Catalog(db_path=str(tmp_path / "catalog.db"))


# --- shelf CRUD -----------------------------------------------------------------------------------
def test_existing_catalog_books_gain_unique_incarnations(tmp_path):
    path = tmp_path / "catalog.db"
    legacy = sqlite3.connect(path)
    legacy.execute(
        "CREATE TABLE books (book_id TEXT PRIMARY KEY, title TEXT NOT NULL, author TEXT, source TEXT, "
        "source_id TEXT, file_hash TEXT, cover_path TEXT, db_path TEXT NOT NULL, "
        "schema_version INTEGER NOT NULL, added_at TEXT NOT NULL, last_opened_at TEXT)"
    )
    legacy.execute(
        "INSERT INTO books(book_id,title,db_path,schema_version,added_at) VALUES ('b','B','b.db',1,'old')"
    )
    legacy.commit()
    legacy.close()

    cat = Catalog(db_path=str(path))
    book = cat.get_book("b")
    assert book is not None
    first = book["incarnation"]
    assert first
    cat.remove_book("b")
    cat.add_book("b", title="B")
    reimported = cat.get_book("b")
    assert reimported is not None and reimported["incarnation"] != first


def test_add_get_list_books(tmp_path):
    cat = _cat(tmp_path)
    cat.add_book("karamazov", title="The Brothers Karamazov", author="Dostoevsky",
                 source="gutenberg", source_id="28054", db_path="books/karamazov/memory.db")
    b = cat.get_book("karamazov")
    assert b["title"] == "The Brothers Karamazov" and b["source"] == "gutenberg"
    assert [x["book_id"] for x in cat.list_books()] == ["karamazov"]
    # a fresh book starts at bookmark 0 / ingest_progress 0
    st = cat.get_state("karamazov")
    assert st["bookmark"] == 0 and st["ingest_progress"] == 0 and st["cfi"] is None


def test_update_book_metadata_repairs_a_restored_catalog_without_touching_reading_state(tmp_path):
    cat = _cat(tmp_path)
    cat.add_book("b", title="source")
    cat.set_position("b", cfi="epubcfi(/6/4)", bookmark=12)

    cat.update_book_metadata("b", title="The Brothers Karamazov", author="Fyodor Dostoyevsky")

    assert cat.get_book("b")["title"] == "The Brothers Karamazov"
    assert cat.get_book("b")["author"] == "Fyodor Dostoyevsky"
    assert cat.get_state("b")["bookmark"] == 12
    with pytest.raises(ValueError, match="unknown book"):
        cat.update_book_metadata("missing", title="Missing", author=None)


def test_schema_version_can_follow_a_successful_per_book_migration(tmp_path):
    cat = _cat(tmp_path)
    cat.add_book("b", title="B", schema_version=3)
    cat.set_schema_version("b", 4)
    assert cat.get_book("b")["schema_version"] == 4
    cat.set_schema_version("not-shelved-yet", 4)  # import creates memory before the shelf row

    with pytest.raises(ValueError, match="positive"):
        cat.set_schema_version("b", 0)
    cat.set_schema_version("b", 5)
    with pytest.raises(RuntimeError, match="newer"):
        cat.set_schema_version("b", 4)


def test_add_duplicate_book_raises(tmp_path):
    cat = _cat(tmp_path)
    cat.add_book("b", title="B")
    with pytest.raises(ValueError):
        cat.add_book("b", title="B again")     # re-import yields a NEW book_id (ADR D-A10), never a dup


def test_unknown_book_reads_none(tmp_path):
    cat = _cat(tmp_path)
    assert cat.get_book("nope") is None
    assert cat.get_state("nope") is None


def test_remove_book_deletes_children(tmp_path):
    cat = _cat(tmp_path)
    cat.add_book("b", title="B")
    cat.set_position("b", cfi="x", bookmark=2)
    cat.record_cost("b", phase="extraction", model="m", input_tokens=10, output_tokens=2, usd=0.001)
    cat.remove_book("b")                              # children deleted first (FK-safe; foreign_keys=ON)
    assert cat.get_book("b") is None
    assert cat.get_state("b") is None
    assert cat.total_cost("b") == 0.0


def test_finalize_ingest_atomically_reconciles_chunk_reservations(tmp_path):
    cat = _cat(tmp_path)
    cat.add_book("b", title="B")
    reservation_ids = [
        cat.reserve_cost(
            "b", phase="extraction", model="m", input_tokens=10, output_tokens=2, usd=0.01,
            max_input_tokens=100, max_output_tokens=100, max_usd=1, chapter_ordinal=1,
        )
        for _ in range(2)
    ]
    for reservation_id in reservation_ids:
        cat.note_reservation_actual(
            "b", reservation_id, input_tokens=7, output_tokens=1, usd=0.005
        )
    cat.finalize_ingest(
        "b", 1, cost={"model": "m", "input_tokens": 14, "output_tokens": 2, "usd": 0.01}
    )
    assert cat.get_cost_reservations("b") == []
    [row] = cat.get_costs("b")
    assert row["phase"] == "extraction" and row["input_tokens"] == 14


def test_nonfinite_or_negative_cost_values_cannot_reduce_the_ceiling(tmp_path):
    cat = _cat(tmp_path)
    cat.add_book("b", title="B")
    with pytest.raises(ValueError, match="finite non-negative"):
        cat.record_cost("b", phase="bad", model="m", usd=float("nan"))
    with pytest.raises(ValueError, match="non-negative SQLite int"):
        cat.record_cost("b", phase="bad", model="m", input_tokens=-1)


def test_add_book_is_atomic_no_orphan(tmp_path, monkeypatch):
    """A failed reading_state INSERT must roll back the books INSERT too — never leave an orphaned
    shelved book with no reading_state (catalog-review HIGH)."""
    cat = _cat(tmp_path)

    class _Proxy:                                    # sqlite3.Connection.execute is read-only; wrap it
        def __init__(self, real):
            self._real = real

        def execute(self, sql, *a, **k):
            if sql.strip().upper().startswith("INSERT INTO READING_STATE"):
                raise sqlite3.OperationalError("simulated reading_state failure")
            return self._real.execute(sql, *a, **k)

        def __getattr__(self, name):
            return getattr(self._real, name)

    monkeypatch.setattr(cat, "_conn", _Proxy(cat._conn))
    with pytest.raises(sqlite3.OperationalError):
        cat.add_book("z", title="Z")
    monkeypatch.undo()                               # restore the real connection for the asserts
    assert cat.get_book("z") is None                 # books INSERT rolled back -> no orphan
    assert cat.get_state("z") is None


# --- monotonic high-water reading state (Inv 12 / ADR D-A10) --------------------------------------
def test_bookmark_is_monotonic_high_water(tmp_path):
    cat = _cat(tmp_path)
    cat.add_book("b", title="B")
    cat.set_position("b", cfi="cfi5", bookmark=5)
    assert cat.get_state("b")["bookmark"] == 5
    cat.set_position("b", cfi="cfi3", bookmark=3)        # backward (re-read) must NOT lower the frontier
    assert cat.get_state("b")["bookmark"] == 5
    assert cat.high_water("b") == 5
    cat.set_position("b", cfi="cfi9", bookmark=9)        # forward advances it
    assert cat.get_state("b")["bookmark"] == 9


def test_ingest_progress_is_monotonic(tmp_path):
    cat = _cat(tmp_path)
    cat.add_book("b", title="B")
    cat.set_ingest_progress("b", 4)
    cat.set_ingest_progress("b", 2)                      # never moves backward
    assert cat.get_state("b")["ingest_progress"] == 4


def test_set_position_rejects_non_int_bookmark(tmp_path):
    """Defense in depth with the DAL's guard: a non-int bookmark must never be persisted (it would be
    fed to view() later and leak via SQLite type affinity)."""
    cat = _cat(tmp_path)
    cat.add_book("b", title="B")
    for bad in ("", "abc", 1.5, True, None):
        with pytest.raises((ValueError, TypeError)):
            cat.set_position("b", cfi="x", bookmark=bad)


def test_set_position_unknown_book_raises(tmp_path):
    cat = _cat(tmp_path)
    with pytest.raises(ValueError):
        cat.set_position("nope", cfi="x", bookmark=1)


# --- cost ledger ----------------------------------------------------------------------------------
def test_cost_ledger_accrues_per_book(tmp_path):
    cat = _cat(tmp_path)
    cat.add_book("a", title="A")
    cat.add_book("b", title="B")
    cat.record_cost("a", phase="extraction", model="m", input_tokens=100, output_tokens=20, usd=0.001)
    cat.record_cost("a", phase="synthesis", model="m2", input_tokens=200, output_tokens=50, usd=0.002)
    cat.record_cost("b", phase="extraction", model="m", input_tokens=10, output_tokens=2, usd=0.0005)
    assert cat.total_cost("a") == pytest.approx(0.003)
    assert cat.total_cost("b") == pytest.approx(0.0005)
    assert cat.total_cost("unknown") == 0.0


# --- concurrency (global lock + monotonic MAX) ----------------------------------------------------
def test_concurrent_position_updates_stay_monotonic(tmp_path):
    cat = _cat(tmp_path)
    cat.add_book("b", title="B")
    errors = []

    def w(bm):
        try:
            cat.set_position("b", cfi=f"cfi{bm}", bookmark=bm)
        except Exception as e:                           # pragma: no cover
            errors.append(repr(e))

    bms = [3, 7, 2, 9, 5, 1, 8, 4, 6, 10]
    threads = [threading.Thread(target=w, args=(bm,)) for bm in bms]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    assert cat.get_state("b")["bookmark"] == 10          # MAX wins; no lost update under contention


def test_two_instances_high_water_holds_under_concurrency(tmp_path):
    """The atomic SQL MAX must hold across SEPARATE Catalog instances on one file — the per-instance
    lock cannot protect this (catalog-review HIGH). With app-side max() this loses updates and regresses."""
    path = str(tmp_path / "catalog.db")
    setup = Catalog(db_path=path)
    setup.add_book("b", title="B")
    setup.close()
    insts = [Catalog(db_path=path) for _ in range(4)]
    errors = []

    def w(inst, bm):
        try:
            inst.set_position("b", cfi=f"c{bm}", bookmark=bm)
        except Exception as e:                           # pragma: no cover
            errors.append(repr(e))

    bms = list(range(1, 41))
    threads = [threading.Thread(target=w, args=(insts[i % 4], bm)) for i, bm in enumerate(bms)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    assert insts[0].get_state("b")["bookmark"] == 40     # high-water == max across all instances
    for inst in insts:
        inst.close()


def test_unknown_book_guard_does_not_leak_write_lock(tmp_path):
    """The rowcount==0 unknown-book guard must roll back its empty write tx (pass-2 HIGH regression):
    otherwise it leaks the WAL write lock and a SECOND instance's legitimate write blocks ~5s and fails
    with 'database is locked'. Here B's writes must succeed promptly after A hits the guard."""
    path = str(tmp_path / "catalog.db")
    a = Catalog(db_path=path)
    a.add_book("b", title="B")
    b = Catalog(db_path=path)                          # separate instance on the same file
    with pytest.raises(ValueError):
        a.set_position("ghost", cfi="x", bookmark=1)   # unknown book -> raises (must release the lock)
    b.set_position("b", cfi="y", bookmark=4)           # must NOT block on a leaked lock
    with pytest.raises(ValueError):
        a.set_ingest_progress("ghost", 2)
    b.set_ingest_progress("b", 3)
    st = b.get_state("b")
    assert st["bookmark"] == 4 and st["ingest_progress"] == 3
    a.close()
    b.close()


def test_negative_and_oversized_bookmark_rejected(tmp_path):
    cat = _cat(tmp_path)
    cat.add_book("b", title="B")
    for bad in (-1, -100, 2 ** 63):
        with pytest.raises(ValueError):
            cat.set_position("b", cfi="x", bookmark=bad)
    with pytest.raises(ValueError):
        cat.set_ingest_progress("b", -3)


def test_cfi_stored_and_backward_is_latest_reported(tmp_path):
    """cfi is the LATEST reported position (resume UX), not a high-water — a backward re-read lowers
    cfi while the integer bookmark stays at the high-water (the documented catalog contract)."""
    cat = _cat(tmp_path)
    cat.add_book("b", title="B")
    cat.set_position("b", cfi="epubcfi(/6/20)", bookmark=20)
    assert cat.get_state("b")["cfi"] == "epubcfi(/6/20)"
    cat.set_position("b", cfi="epubcfi(/6/2)", bookmark=2)
    st = cat.get_state("b")
    assert st["bookmark"] == 20 and st["cfi"] == "epubcfi(/6/2)"


def test_explicit_new_pass_resets_cursor_and_frontier_but_not_ingest_progress(tmp_path):
    cat = _cat(tmp_path)
    cat.add_book("b", title="B")
    cat.set_position("b", cfi="chapter-8", bookmark=8, expected_epoch=0)
    cat.set_ingest_progress("b", 8)

    reset = cat.reset_position("b", expected_epoch=0)

    assert reset == {
        "bookmark": 0,
        "cfi": None,
        "ingest_progress": 8,
        "position_epoch": 1,
    }


def test_position_epoch_rejects_stale_pre_reset_writes_and_resets(tmp_path):
    cat = _cat(tmp_path)
    cat.add_book("b", title="B")
    cat.set_position("b", cfi="old-pass", bookmark=6, expected_epoch=0)
    cat.reset_position("b", expected_epoch=0)

    with pytest.raises(ValueError, match="position epoch"):
        cat.set_position("b", cfi="late-old-tab", bookmark=9, expected_epoch=0)
    with pytest.raises(ValueError, match="position epoch"):
        cat.reset_position("b", expected_epoch=0)
    assert cat.get_state("b")["bookmark"] == 0

    cat.set_position("b", cfi="new-pass-chapter-2", bookmark=2, expected_epoch=1)
    state = cat.get_state("b")
    assert state["bookmark"] == 2 and state["position_epoch"] == 1


def test_cross_instance_resets_have_exactly_one_epoch_winner(tmp_path):
    path = tmp_path / "catalog.db"
    first = Catalog(str(path))
    first.add_book("b", title="B")
    second = Catalog(str(path))
    barrier = threading.Barrier(2)
    results = []

    def reset(catalog):
        barrier.wait()
        try:
            results.append(catalog.reset_position("b", expected_epoch=0))
        except ValueError as error:
            results.append(error)

    threads = [threading.Thread(target=reset, args=(catalog,)) for catalog in (first, second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(isinstance(result, ValueError) for result in results) == 1
    assert first.get_state("b")["position_epoch"] == 1
    first.close()
    second.close()


def test_existing_catalog_gains_a_zero_position_epoch_without_losing_state(tmp_path):
    path = tmp_path / "catalog.db"
    cat = Catalog(str(path))
    cat.add_book("b", title="B")
    cat.set_position("b", cfi="chapter-4", bookmark=4)
    cat.close()
    connection = sqlite3.connect(path)
    connection.execute("ALTER TABLE reading_state DROP COLUMN position_epoch")
    connection.commit()
    connection.close()

    migrated = Catalog(str(path))
    assert migrated.get_state("b") == {
        "bookmark": 4,
        "cfi": "chapter-4",
        "ingest_progress": 0,
        "position_epoch": 0,
        "updated_at": migrated.get_state("b")["updated_at"],
    }
    migrated.close()


def test_cost_fields_persisted_and_readable(tmp_path):
    cat = _cat(tmp_path)
    cat.add_book("a", title="A")
    cat.record_cost("a", phase="extraction", model="gpt-4o-mini", input_tokens=1000,
                    output_tokens=200, usd=0.001, chapter_ordinal=3)
    rows = cat.get_costs("a")
    assert len(rows) == 1
    r = rows[0]
    assert (r["phase"] == "extraction" and r["model"] == "gpt-4o-mini" and r["input_tokens"] == 1000
            and r["output_tokens"] == 200 and r["chapter_ordinal"] == 3)


def test_finalize_ingest_records_cost_once_and_advances_progress_atomically(tmp_path):
    cat = _cat(tmp_path)
    cat.add_book("a", title="A")
    cost = dict(model="gpt-4o-mini", input_tokens=100, output_tokens=20, usd=0.001)
    cat.finalize_ingest("a", 3, cost=cost)
    cat.finalize_ingest("a", 3, cost=cost)
    assert cat.get_state("a")["ingest_progress"] == 3
    rows = [row for row in cat.get_costs("a") if row["phase"] == "extraction"]
    assert len(rows) == 1
    assert rows[0]["chapter_ordinal"] == 3 and rows[0]["usd"] == pytest.approx(0.001)


def test_finalize_ingest_repairs_a_stale_wrong_extraction_cost(tmp_path):
    cat = _cat(tmp_path)
    cat.add_book("a", title="A")
    cat.record_cost(
        "a", phase="extraction", model="wrong", input_tokens=1, output_tokens=2,
        usd=9.0, chapter_ordinal=3,
    )
    authoritative = dict(model="right", input_tokens=100, output_tokens=20, usd=0.001)

    cat.finalize_ingest("a", 3, cost=authoritative)

    rows = [row for row in cat.get_costs("a") if row["phase"] == "extraction"]
    assert len(rows) == 1
    assert {key: rows[0][key] for key in authoritative} == authoritative


def test_extraction_cost_uniqueness_migration_collapses_identical_legacy_duplicates(tmp_path):
    path = str(tmp_path / "catalog.db")
    cat = Catalog(path)
    cat.add_book("a", title="A")
    cat.close()
    legacy = sqlite3.connect(path)
    legacy.execute("DROP INDEX ux_cost_extraction_book_chapter")
    for _ in range(2):
        legacy.execute(
            "INSERT INTO cost_ledger(book_id,chapter_ordinal,phase,model,input_tokens,"
            "output_tokens,usd,at) VALUES ('a',3,'extraction','m',10,2,0.01,'old')"
        )
    legacy.commit()
    legacy.close()

    migrated = Catalog(path)
    assert len(migrated.get_costs("a")) == 1
    with migrated._lock:
        with pytest.raises(sqlite3.IntegrityError):
            migrated._conn.execute(
                "INSERT INTO cost_ledger(book_id,chapter_ordinal,phase) "
                "VALUES ('a',3,'extraction')"
            )
        migrated._conn.rollback()


def test_extraction_cost_uniqueness_migration_rejects_conflicting_duplicates(tmp_path):
    path = str(tmp_path / "catalog.db")
    cat = Catalog(path)
    cat.add_book("a", title="A")
    cat.close()
    legacy = sqlite3.connect(path)
    legacy.execute("DROP INDEX ux_cost_extraction_book_chapter")
    legacy.execute(
        "INSERT INTO cost_ledger(book_id,chapter_ordinal,phase,model,input_tokens,"
        "output_tokens,usd,at) VALUES ('a',3,'extraction','first',10,2,0.01,'old')"
    )
    legacy.execute(
        "INSERT INTO cost_ledger(book_id,chapter_ordinal,phase,model,input_tokens,"
        "output_tokens,usd,at) VALUES ('a',3,'extraction','different',10,2,0.01,'old')"
    )
    legacy.commit()
    legacy.close()

    with pytest.raises(RuntimeError, match="irreconcilable duplicate extraction costs"):
        Catalog(path)


def test_record_cost_rejects_non_numeric_usd(tmp_path):
    cat = _cat(tmp_path)
    cat.add_book("a", title="A")
    with pytest.raises(ValueError):
        cat.record_cost("a", phase="x", model="m", usd="not-a-number")


def test_record_cost_unknown_book_raises_value_error(tmp_path):
    cat = _cat(tmp_path)
    cat.add_book("a", title="A")
    with pytest.raises(ValueError):                      # FK rejects -> friendly ValueError (matches add_book)
        cat.record_cost("nope", phase="x", model="m", usd=0.1)


def test_schema_idempotent_on_reopen(tmp_path):
    path = str(tmp_path / "catalog.db")
    c1 = Catalog(db_path=path)
    c1.add_book("b", title="B")
    c1.set_position("b", cfi="x", bookmark=5)
    c1.record_cost("b", phase="x", model="m", usd=0.01)
    c1.close()
    c2 = Catalog(db_path=path)                           # re-open: IF NOT EXISTS DDL must not error/wipe
    assert c2.get_book("b")["title"] == "B"
    assert c2.get_state("b")["bookmark"] == 5
    assert c2.total_cost("b") == pytest.approx(0.01)


# --- companion memory: chapter notes and conversation turns ---------------------------------------
def _shelved(tmp_path):
    cat = _cat(tmp_path)
    cat.add_book("b", title="B", db_path="books/b/memory.db")
    return cat


def test_chapter_notes_are_kept_per_chapter_and_a_rebuild_replaces_them(tmp_path):
    cat = _shelved(tmp_path)
    cat.set_chapter_notes("b", 2, source_hash="h2", version="v1", sentences=["Mara hid the key."], dropped=1)
    cat.set_chapter_notes("b", 1, source_hash="h1", version="v1", sentences=[], dropped=0)
    cat.set_chapter_notes("b", 2, source_hash="h2b", version="v1", sentences=["Jonas waited."], dropped=0)
    assert [(n["atom"], n["source_hash"], n["sentences"], n["dropped"]) for n in cat.get_chapter_notes("b")] == [
        (1, "h1", [], 0), (2, "h2b", ["Jonas waited."], 0)]


def test_turns_come_back_oldest_first_and_only_up_to_the_asked_position(tmp_path):
    cat = _shelved(tmp_path)
    incarnation = cat.get_book("b")["incarnation"]
    def add(role, content, atom, offset, **more):
        return cat.add_turn("b", incarnation=incarnation, position_epoch=0, atom=atom, offset=offset,
                            role=role, content=content, **more)
    add("user", "Poor Mara.", 2, 40, reaction=True)
    add("assistant", "It's the red tin that gets me.", 2, 40)
    add("user", "Who is Corvus?", 3, 10)
    add("user", "Later in two.", 2, 900)
    at = lambda atom, offset, **more: [  # noqa: E731
        t["content"] for t in cat.get_turns("b", incarnation=incarnation, atom=atom, offset=offset, **more)]
    assert at(2, 40) == ["Poor Mara.", "It's the red tin that gets me."]
    assert at(2, 39) == []
    assert at(3, 10) == ["Poor Mara.", "It's the red tin that gets me.", "Who is Corvus?", "Later in two."]
    assert at(3, 10, limit=2) == ["Who is Corvus?", "Later in two."]
    assert at(9, 0, reactions=True) == ["Poor Mara."]
    # Another shelf lifetime of the same book id does not inherit them.
    assert cat.get_turns("b", incarnation="other", atom=9, offset=0) == []


def test_removing_a_book_removes_its_notes_and_turns(tmp_path):
    cat = _shelved(tmp_path)
    incarnation = cat.get_book("b")["incarnation"]
    cat.set_chapter_notes("b", 1, source_hash="h", version="v1", sentences=["x"], dropped=0)
    cat.add_turn("b", incarnation=incarnation, position_epoch=0, atom=1, offset=1, role="user", content="hi")
    cat.remove_book("b")
    cat.add_book("b", title="B", db_path="books/b/memory.db")
    assert cat.get_chapter_notes("b") == []
    assert cat.get_turns("b", incarnation=incarnation, atom=9, offset=0) == []


# --- the read map ---------------------------------------------------------------------------------
def test_read_ranges_merge_when_they_touch_or_overlap_and_stay_apart_across_a_gap(tmp_path):
    cat = _shelved(tmp_path)
    inc = cat.get_book("b")["incarnation"]
    add = lambda atom, start, end, source="read": cat.add_read_range(  # noqa: E731
        "b", incarnation=inc, atom=atom, start=start, end=end, source=source)
    add(3, 100, 200)
    add(3, 200, 260)            # the next page
    add(3, 150, 180)            # a page read again
    add(3, 900, 950)            # after a jump
    add(1, 0, 40, source="told")
    add(3, 240, 300, source="told")
    add(3, 301, 310)            # the next page, past the one space between two sentences
    assert cat.get_read_map("b", incarnation=inc) == {1: [(0, 40)], 3: [(100, 310), (900, 950)]}
    assert cat.get_read_map("b", incarnation="another shelf lifetime") == {}


def test_a_chapter_can_be_taken_out_of_the_read_map_and_the_whole_map_cleared(tmp_path):
    cat = _shelved(tmp_path)
    inc = cat.get_book("b")["incarnation"]
    for atom in (1, 2):
        cat.add_read_range("b", incarnation=inc, atom=atom, start=0, end=50, source="read")
    cat.clear_read_map("b", atom=2)
    assert cat.get_read_map("b", incarnation=inc) == {1: [(0, 50)]}
    cat.clear_read_map("b")
    assert cat.get_read_map("b", incarnation=inc) == {}


def test_the_place_the_reader_is_now_can_move_backward(tmp_path):
    cat = _shelved(tmp_path)
    inc = cat.get_book("b")["incarnation"]
    assert cat.get_read_position("b", incarnation=inc) is None
    cat.set_read_position("b", incarnation=inc, atom=9, offset=400)
    cat.set_read_position("b", incarnation=inc, atom=2, offset=30)
    assert cat.get_read_position("b", incarnation=inc) == (2, 30)
    assert cat.get_read_position("b", incarnation="another shelf lifetime") is None


def test_removing_a_book_removes_its_read_map(tmp_path):
    cat = _shelved(tmp_path)
    inc = cat.get_book("b")["incarnation"]
    cat.add_read_range("b", incarnation=inc, atom=1, start=0, end=9, source="read")
    cat.set_read_position("b", incarnation=inc, atom=1, offset=9)
    cat.remove_book("b")
    cat.add_book("b", title="B", db_path="books/b/memory.db")
    assert cat.get_read_map("b", incarnation=inc) == {} and cat.get_read_position("b", incarnation=inc) is None
