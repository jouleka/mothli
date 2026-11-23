"""Taking a copy of one's data in hosted mode (ADR 0041 part 8), against real PostgreSQL, through the
hosted HTTP app on the RLS-enforced tenant role: what the copy holds, that it holds nothing the app
would not show the reader now (no spoilers, even here), that it changes nothing, that one reader's
copy holds nothing of another's (on the tenant role, and through a superuser connection where only
the copy's own owner predicates keep readers apart), how often it may be asked for, and that every
table with an owner is in it or left out for a stated reason. Stand-in models only; made-up books.
"""

from __future__ import annotations

import asyncio
import io
import json
import tempfile
import uuid
import zipfile

import psycopg
import pytest

from app.chapter_notes import VERSION, text_hash
from app.hosted.companion.store import HostedCompanionCatalog
from app.hosted.tenant.export import IN_THE_COPY, LEFT_OUT, ExportGate, build_copy
from app.hosted.tenant.models import OwnerId
from app.hosted.tenant.repository import PostgresTenantRepository
from app.read_frontier import source_state
from app.recollections import STORE_CAP
from tests.hosted import test_companion_read_map_postgres as read_map_gate
from tests.hosted.test_companion_recollections_postgres import kept, reader, said
from tests.hosted.test_companion_stream_postgres import adapters, restore_caps  # noqa: F401
from tests.hosted.test_removing_a_book_postgres import everything_of
from tests.hosted.test_removing_a_book_postgres import notes_app as notes_app  # noqa: F401

pytestmark = pytest.mark.postgres

admin_dsn = read_map_gate.admin_dsn
database = read_map_gate.database
tenant_dsn = read_map_gate.tenant_dsn
readers = read_map_gate.readers
lamp_book = read_map_gate.lamp_book
upload = read_map_gate.upload

ONE = "Mara kept the cobalt lamp in a red tin."            # Mothli's note on chapter 1
TWO = "Corvus broke the green lamp at the ferry."          # Mothli's note on chapter 2
EARLY = "I like Mara already, she's funny."                # said with chapter 1 read
LATER = "Corvus at the ferry, wow."                        # said with chapters 1 and 2 read
TIED_TWO = "The reader was shocked by the ferry."           # remembered from chapter 2
ABOUT = "The reader reads at night, on the train home."    # about the reader
HOME_EARLY = "Hello from home, one chapter in."            # home chat, chapter 1 read
HOME_LATER = "Home again, the ferry!"                      # home chat, chapter 2 read too


def copy_of(response) -> dict:
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["content-disposition"].startswith(
        'attachment; filename="mothli-my-data-')
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    return {name: archive.read(name).decode("utf-8") for name in archive.namelist()}


def a_reader_with_two_chapters_read(client, tokens, app, readers, label: str, words: str):
    """A reader who read chapters 1 and 2, talked about each, got Mothli's notes on both and a
    note remembered from chapter 2, highlighted a line, then marked chapter 2 not read."""
    who = reader((client, tokens, app), readers, label)
    book = upload(client, tokens, label, lamp_book())
    who.mark(book, [1])
    said(app, who.owner, book, f"{EARLY} {words}")
    said(app, who.owner, None, f"{HOME_EARLY} {words}")
    who.mark(book, [2])
    said(app, who.owner, book, f"{LATER} {words}")
    said(app, who.owner, None, f"{HOME_LATER} {words}")
    catalog, source = adapters(app, who.owner)
    _state, record, _manifest, chapters = source_state(book, catalog, app.state.settings, source)
    for atom, note in ((1, ONE), (2, TWO)):
        catalog.set_chapter_notes(book, atom, source_hash=text_hash(chapters[atom - 1]["text"]),
                                  version=VERSION, sentences=[f"{note} {words}"], dropped=0)
    read_both = {book: [record["incarnation"], {
        1: [[0, len(chapters[0]["text"])]], 2: [[0, len(chapters[1]["text"])]]}]}
    tied = kept([("feeling", f"{TIED_TWO} {words}")])
    tied[0].update(tied=True, book_id=book, drew_on=read_both)
    HostedCompanionCatalog(app.state.companion_dsn, OwnerId(who.owner)).keep_recollections(
        tied + kept([("about", f"{ABOUT} {words}")]), cap=STORE_CAP)
    highlight = who.call("POST", f"/api/books/{book}/highlights", {
        "anchor": {"cfi": "epubcfi(/6/2!/4/2,/1:0,/1:4)", "atom": 1}, "color": "yellow",
        "selected_text": f"Mara {words}"})
    assert highlight.status_code == 201, highlight.text
    assert who.call("POST", f"/api/books/{book}/annotations", {
        "anchor": {"cfi": "epubcfi(/6/2!/4/2,/1:0,/1:4)", "atom": 1},
        "body": f"Funny line. {words}", "highlight_id": highlight.json()["id"]}).status_code == 201
    who.mark(book, [2], read=False)
    return who, book


def test_every_owner_table_is_in_the_copy_or_left_out_for_a_reason(database) -> None:
    """The break it catches: a table a later migration adds with an owner that the copy neither
    includes nor leaves out with a reason, so the copy falls behind without anyone noticing."""
    with psycopg.connect(database) as conn:
        tables = {row[0] for row in conn.execute(
            """SELECT table_name FROM information_schema.columns
               WHERE table_schema='public' AND column_name='owner_id'""").fetchall()}
    assert not set(IN_THE_COPY) & set(LEFT_OUT)
    assert tables - {"users"} == (set(IN_THE_COPY) | set(LEFT_OUT)) - {"users"}, sorted(
        tables ^ (set(IN_THE_COPY) | set(LEFT_OUT)))
    assert all(reason.strip() for reason in (*IN_THE_COPY.values(), *LEFT_OUT.values()))


def test_a_copy_holds_the_readers_own_data_and_nothing_the_app_would_hide(
        notes_app, readers, database) -> None:
    """The break it catches: a copy missing the reader's shelf, place, highlights and notes,
    chats, chapter notes or what Mothli remembers; a chapter note, chat line or remembered note
    from a chapter the reader has since marked not read (the app hides them, so must the copy);
    secrets or internal records in it; or a copy that changes anything of the reader's."""
    client, tokens, app, _objects = notes_app
    a, book = a_reader_with_two_chapters_read(client, tokens, app, readers, "a", "(made-up A)")
    before = everything_of(database, a.owner)

    files = copy_of(a.call("GET", "/api/account/export"))

    assert sorted(files) == ["READ ME.txt", "books/01 The Lamp Book.json", "home chat.json",
                             "shelf.json", "what Mothli remembers.json", "you.json",
                             "your plan.json"]
    assert 'press Notebook, and choose "Download the book"' in " ".join(
        files["READ ME.txt"].split())
    assert json.loads(files["you.json"]) == {"name": "Reader A", "email": None,
                                             "joined": "2026-07-01T09:00:00+00:00"}
    shelf = json.loads(files["shelf.json"])["books"]
    assert [(item["title"], item["author"], item["came_from"], item["file"]) for item in shelf] == [
        ("The Lamp Book", "Ada Writer", "You added the file yourself",
         "books/01 The Lamp Book.json")]
    entry = json.loads(files["books/01 The Lamp Book.json"])
    assert entry["where_you_are"]["every_chapter_read_through"] == 1
    assert [c["share_read"] for c in entry["where_you_are"]["chapters"]] == [1.0, 0, 0]
    marks = entry["highlights_notes_and_bookmarks"]
    assert [(m["kind"], m["words"]) for m in marks] == [
        ("highlight", "Mara (made-up A)"), ("note", "Mara (made-up A)")]
    assert marks[1]["note"] == "Funny line. (made-up A)"
    assert entry["how_it_looks"]["theme"] == "paper"
    # Chapter 2 is no longer read: what was written from it waits, as in the app.
    chat = [line["said"] for line in entry["chat_with_mothli"]]
    assert f"{EARLY} (made-up A)" in chat and f"{LATER} (made-up A)" not in chat
    assert [n["notes"] for n in entry["mothli_notes_on_chapters_you_finished"]] == [
        [f"{ONE} (made-up A)"]]
    assert entry["held_back_until_you_read_again"] == {"chat_lines": 2, "chapter_notes": 1}
    remembered = json.loads(files["what Mothli remembers.json"])
    assert [note["what"] for note in remembered["remembers"]] == [f"{ABOUT} (made-up A)"]
    assert remembered["held_back_until_you_read_again"] == 1
    home = json.loads(files["home chat.json"])
    assert [line["said"] for line in home["chat"] if line["who"] == "you"] == [
        f"{HOME_EARLY} (made-up A)"]
    assert home["held_back_until_you_read_again"] == 2
    everything = "\n".join(files.values())
    for hidden in (TWO, LATER, TIED_TWO, HOME_LATER):
        assert hidden not in everything, hidden
    for internal in (str(a.owner), book, "incarnation", "drew_on", "turn_id", "storage_key",
                     "session", "csrf", "Reader B", "(made-up B)"):
        assert internal not in everything, internal

    # Nothing of the reader's changed (the request counter and the audit line aside).
    after = everything_of(database, a.owner)
    for table in ("owner_request_windows", "audit_events"):
        before.pop(table), after.pop(table)
    assert after == before
    with psycopg.connect(database) as conn:
        assert conn.execute("SELECT action, target_id, metadata FROM audit_events "
                            "WHERE owner_id=%s AND action='account.export'",
                            (a.owner,)).fetchall() == [("account.export", None, {})]


def test_each_readers_copy_holds_only_their_own_data(notes_app, readers, database) -> None:
    """The break it catches: anything of reader A in reader B's copy, B's own copy of the very
    same file included, through the app on the RLS-enforced role, or through a superuser
    connection where only the copy's own owner predicates keep readers apart."""
    client, tokens, app, _objects = notes_app
    a_reader_with_two_chapters_read(client, tokens, app, readers, "a", "(made-up A)")
    b, _book = a_reader_with_two_chapters_read(client, tokens, app, readers, "b", "(made-up B)")

    files = copy_of(b.call("GET", "/api/account/export"))
    everything = "\n".join(files.values())
    assert "(made-up B)" in everything and "(made-up A)" not in everything
    assert str(readers["a"]) not in everything

    # The same copy built through a superuser connection: row-level security is off, so only the
    # copy's own owner predicates (its catalog's and the repository's) stand between readers.
    repository = PostgresTenantRepository(database)
    out = tempfile.SpooledTemporaryFile()
    build_copy(out, settings=app.state.settings, dsn=database, storage=app.state.object_storage,
               owner_id=OwnerId(b.owner), who={},
               marks_of=lambda bid: asyncio.run(
                   repository.list_reader_marks(OwnerId(b.owner), uuid.UUID(bid))),
               looks_of=lambda bid: asyncio.run(
                   repository.get_reader_preferences(OwnerId(b.owner), uuid.UUID(bid))))
    out.seek(0)
    archive = zipfile.ZipFile(out)
    bypassed = "\n".join(archive.read(name).decode("utf-8") for name in archive.namelist())
    assert "(made-up B)" in bypassed and "(made-up A)" not in bypassed
    assert sorted(archive.namelist()) == sorted(files)


def test_a_reader_takes_a_copy_now_and_then_and_never_slows_anyone_else(
        notes_app, readers) -> None:
    """The break it catches: a reader able to ask for copy after copy, or one reader's copy
    standing in another reader's way."""
    client, tokens, app, _objects = notes_app
    a = reader((client, tokens, app), readers, "a")
    b = reader((client, tokens, app), readers, "b")
    upload(client, tokens, "a", lamp_book())
    copy_of(a.call("GET", "/api/account/export"))
    again = a.call("GET", "/api/account/export")
    assert again.status_code == 429
    assert again.json()["detail"]["code"] == "export_too_soon"
    assert again.json()["detail"]["message"].startswith("You just took a copy.")
    copy_of(b.call("GET", "/api/account/export"))      # B is not held up by A

    # One at a time per reader, a few at once in all, and again after a few minutes.
    now = [0.0]
    gate = ExportGate(at_once=2, apart_seconds=300, clock=lambda: now[0])
    owners = [OwnerId(uuid.uuid4()) for _ in range(3)]
    gate.admit(owners[0])
    with pytest.raises(Exception) as running:
        gate.admit(owners[0])
    assert running.value.code == "export_in_progress"
    gate.admit(owners[1])
    with pytest.raises(Exception) as busy:
        gate.admit(owners[2])
    assert (busy.value.status, busy.value.code) == (503, "export_busy")
    gate.finished(owners[0], made=True)
    gate.finished(owners[1], made=False)
    gate.admit(owners[1])                                # a failed copy is not counted
    with pytest.raises(Exception) as soon:
        gate.admit(owners[0])
    assert soon.value.code == "export_too_soon"
    now[0] = 301
    gate.finished(owners[1], made=True)
    gate.admit(owners[0])
