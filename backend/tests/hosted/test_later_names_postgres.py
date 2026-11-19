"""The later-name check in the database (ADR 0044 step 10), against real PostgreSQL.

``score_recap`` removes a sentence that names someone the book reveals only after the reader's
place. A hosted reader's web process is handed nothing past their place (the door), so the check
asks the database (``story_memory_later_names``, migration 0035), which holds every name of the
edition and answers with the sentence's own words only.

First, parity: the same names in a local ``MemoryDB`` and in an edition, the same sentence and
read text, and local mode's ``score_recap`` and the hosted one must decide alike (the chat drops
a sentence exactly when local mode's chat would), case by case: a later name, a later other name
of someone already met, a name met, rule (a) and rule (b) of reader parity, role words, accents,
taken-back names, the reader's place itself and one chapter after, hyphens and possessives, a
reader with no read page, a book whose file has no edition, and the one place hosted mode is
stricter. Then: what the check can be made to say. The tenant role gets back nothing but words
it sent, cannot read the words table, cannot move the place forward, gets nothing for a book that
is not theirs, and PUBLIC cannot call it; a name without its words, or whose text changed, stops
the reply. Nothing is called and nothing paid.
"""

from __future__ import annotations

import hashlib
import uuid

import psycopg
import pytest
from psycopg import sql

from app.eval.spoiler_gate import score_recap, unread_future_names
from app.eval.spoiler_gate.synthesis import later_name_question
from app.hosted.companion.later_names import rebuild_name_words
from app.hosted.companion.memory import LATER_NAMES, HostedBookStore, StoryMemoryUnavailable
from app.hosted.tenant.models import OwnerId
from app.memory.dal import _now
from app.memory.store import Store
from app.unicode_text import word_tokens
from tests.hosted import test_tenant_postgres as tenant_gate
from tests.parity._adapters import set_place
from tests.parity.test_companion_memory_parity import _verdict

pytestmark = pytest.mark.postgres

admin_dsn = tenant_gate.admin_dsn
database = tenant_gate.database
tenant_dsn = tenant_gate.tenant_dsn

CHAPTERS = 6


class Book:
    """One book's names, written alike into a local ``MemoryDB`` and into the edition of a hosted
    reader's file (the notes account's rows, with their words as a publish writes them)."""

    def __init__(self, tmp_path, database: str, tenant_dsn: str, *, edition: bool = True):
        self.database, self.tenant_dsn = database, tenant_dsn
        self.store = Store(str(tmp_path / f"local-{uuid.uuid4().hex}"),
                           vector_backend="bruteforce")
        self.local_ids, self.hosted_ids = {}, {}
        self.owner, self.book = uuid.uuid4(), uuid.uuid4()
        fingerprint = hashlib.sha256(f"a later-name book {self.book}".encode()).hexdigest()
        with psycopg.connect(database) as conn, conn.transaction():
            conn.execute("INSERT INTO users (id,display_name) VALUES (%s,'Reader')", (self.owner,))
            self.incarnation = conn.execute(
                "INSERT INTO books (owner_id,id,title,schema_version,file_hash,split_hash)"
                " VALUES (%s,%s,'B',1,%s,%s) RETURNING incarnation",
                (self.owner, self.book, fingerprint, fingerprint)).fetchone()[0]
            self.edition = None
            if edition:
                [(account,)] = conn.execute("SELECT account_id FROM notes_account").fetchall()
                self.edition = conn.execute(
                    "INSERT INTO books (owner_id,title,schema_version,file_hash,split_hash,"
                    "edition_of) VALUES (%s,'edition',1,%s,%s,%s)"
                    " RETURNING owner_id,id,incarnation",
                    (account, fingerprint, fingerprint, account)).fetchone()
                self.chapter = {}
                for ordinal in range(1, CHAPTERS + 1):
                    chapter = self.chapter[ordinal] = uuid.uuid4()
                    digest = hashlib.sha256(f"chapter {ordinal}".encode()).hexdigest()
                    conn.execute("INSERT INTO chapters (owner_id,book_id,book_incarnation,id,"
                                 "chapter_key,revealed_at,content_hash)"
                                 " VALUES (%s,%s,%s,%s,%s,%s,%s)",
                                 (*self.edition, chapter, f"{self.edition[1]}:c{ordinal}",
                                  ordinal, digest))
                    conn.execute("INSERT INTO ingested_chapters (owner_id,book_id,"
                                 "book_incarnation,chapter_id,content_hash,completed_at)"
                                 " VALUES (%s,%s,%s,%s,%s,now())",
                                 (*self.edition, chapter, digest))

    @property
    def scope(self) -> tuple:
        return (self.owner, self.book, self.incarnation)

    def _hosted(self, statement: str, values: tuple) -> None:
        with psycopg.connect(self.database) as conn:
            conn.execute(statement, values)

    def person(self, name: str, at: int, *, retracted: bool = False) -> str:
        with self.store.book("local") as mem:
            self.local_ids[name] = mem.add_entity(name, "character", at)
            if retracted:
                with mem._writer():
                    mem._conn.execute("UPDATE entities SET retracted_at=? WHERE entity_id=?",
                                      (_now(), self.local_ids[name]))
        if self.edition:
            self.hosted_ids[name] = uuid.uuid4()
            self._hosted("INSERT INTO entities (owner_id,book_id,book_incarnation,id,"
                         "source_chapter_id,canonical_name,entity_type,revealed_at,retracted_at)"
                         " VALUES (%s,%s,%s,%s,%s,%s,'character',%s,"
                         f"{'now()' if retracted else 'NULL'})",
                         (*self.edition, self.hosted_ids[name], self.chapter[1], name, at))
        return name

    def alias(self, person: str, surface: str, at: int, *, retracted: bool = False) -> None:
        with self.store.book("local") as mem:
            alias_id = mem.add_alias(self.local_ids[person], surface, at)
            if retracted:
                with mem._writer():
                    mem._conn.execute("UPDATE aliases SET retracted_at=? WHERE alias_id=?",
                                      (_now(), alias_id))
        if self.edition:
            self._hosted("INSERT INTO aliases (owner_id,book_id,book_incarnation,entity_id,"
                         "source_chapter_id,surface_form,revealed_at,retracted_at)"
                         " VALUES (%s,%s,%s,%s,%s,%s,%s,"
                         f"{'now()' if retracted else 'NULL'})",
                         (*self.edition, self.hosted_ids[person], self.chapter[1], surface, at))

    def theme(self, name: str, at: int) -> None:
        with self.store.book("local") as mem:
            mem.add_theme(name, f"{name}, as a theme.", at)
        if self.edition:
            self._hosted("INSERT INTO themes (owner_id,book_id,book_incarnation,source_chapter_id,"
                         "name,revealed_at) VALUES (%s,%s,%s,%s,%s,%s)",
                         (*self.edition, self.chapter[1], name, at))

    def ready(self, place: int) -> None:
        """The words of every name, as publishes write them, and the reader's place."""
        with psycopg.connect(self.database) as conn:
            if self.edition:
                rebuild_name_words(conn, tuple(self.edition))
            set_place(conn, self.scope, place)

    def local(self, sentence: str, bookmark: int, read_text: str | None) -> dict:
        with self.store.book("local") as mem:
            return score_recap(mem, bookmark, sentence, read_text=read_text)

    def hosted(self, sentence: str, bookmark: int, read_text: str | None) -> dict:
        with HostedBookStore(self.tenant_dsn, OwnerId(self.owner)).book(str(self.book)) as mem:
            return score_recap(mem, bookmark, sentence, read_text=read_text)

    def close(self) -> None:
        self.store.close()


@pytest.fixture()
def book(tmp_path, database, tenant_dsn):
    made = Book(tmp_path, database, tenant_dsn)
    yield made
    made.close()


def dropped(result: dict, read_text: str | None) -> bool:
    """Whether the chat's fixed check drops the sentence for a later name
    (``sentence_checks.safe``)."""
    words = set(word_tokens(read_text)) if read_text is not None else set()
    return bool(unread_future_names(result, words))


def same(book: Book, sentence: str, *, at: int, read: str | None, place: int | None = None):
    """Local mode at ``at`` and hosted mode for a reader at ``place`` (``at`` by default) asked
    as of ``at``: the same decision on every field. Returns whether the chat drops it."""
    book.ready(at if place is None else place)
    local = book.local(sentence, at, read)
    hosted = book.hosted(sentence, at, read)
    assert _verdict(hosted, sentence, read) == _verdict(local, sentence, read), sentence
    assert dropped(hosted, read) == dropped(local, read), sentence
    return dropped(hosted, read)


READ = "Mara walked to the quay, and Mara slept."      # what the reader has read, by default


# ---- parity, case by case ------------------------------------------------------------------

def test_a_later_name_and_a_name_already_met(book):
    """The break it catches: a person first met after the reader's place let through in hosted
    mode (what steps 5 to 9 did), or a person already met removed."""
    book.person("Mara", 1)
    book.person("Vex", 3)
    assert same(book, "Vex moved the lamp.", at=1, read=READ)
    assert not same(book, "Mara moved the lamp.", at=1, read=READ)
    assert not same(book, "Vex moved the lamp.", at=3, read=READ + " Vex came.")


def test_a_later_other_name_of_someone_already_met(book):
    """A late other name is itself later knowledge; the chat lets it through only when every
    word of the person's own name has been read, in both modes alike."""
    book.person("Mara", 1)
    book.alias("Mara", "the Lamplighter", 3)
    book.person("Corvus Quell", 1)
    book.alias("Corvus Quell", "Old Smoke", 3)
    assert not same(book, "The Lamplighter moved the lamp.", at=1, read=READ)
    # "Quell" was never read: the later other name is removed.
    assert same(book, "Old Smoke moved the lamp.", at=1, read=READ + " Corvus came.")
    book.ready(1)
    assert book.hosted("The Lamplighter moved the lamp.", 1, READ)["future_entity_leaks"] == [
        "lamplighter"]


def test_reader_parity_rule_a_a_later_word_read_mid_sentence(book):
    """Rule (a): a later name word the reader saw capitalised in the middle of a sentence is
    known to them; seen only at a sentence's start it is not (it stays a later name word, which
    the chat then lets through only because every word of the name was read)."""
    book.person("Russia", 3)
    assert not same(book, "They spoke of Russia.", at=1, read="They went to Russia often.")
    book.ready(1)
    assert book.hosted("They spoke of Russia.", 1, "They went to Russia often.")[
        "future_entity_leaks"] == []
    assert book.hosted("They spoke of Russia.", 1, "Russia was far. They slept.")[
        "future_entity_leaks"] == ["russia"]
    assert not same(book, "They spoke of Russia.", at=1, read="Russia was far. They slept.")
    assert same(book, "They spoke of Russia.", at=1, read=READ)


def test_reader_parity_rule_b_both_ways(book):
    """Rule (b): a later word read only as an ordinary word is dropped only while the same name
    keeps another later word the reader has not read ("Optin Monastery" keeps "Optin")."""
    book.person("Optin Monastery", 3)
    book.person("Town", 3)
    read = "The monastery was quiet and the town slept."
    assert not same(book, "The monastery bell rang.", at=1, read=read)
    assert same(book, "Optin was far away.", at=1, read=read)
    # The sole word of a later name, read only as an ordinary word, is kept by rule (b) (and
    # the chat then lets it through, every word of the name having been read): alike.
    book.ready(1)
    assert book.hosted("Town was far away.", 1, read)["future_entity_leaks"] == ["town"]
    assert not same(book, "Town was far away.", at=1, read=read)
    # Both words read only as ordinary words: neither is dropped by rule (b).
    both = read + " The optin road."
    assert book.hosted("Optin was far away.", 1, both)["future_entity_leaks"] == ["optin"]
    same(book, "Optin was far away.", at=1, read=both)


def test_role_words_are_never_later_names(book):
    book.person("Father Zossima", 3)
    book.person("the Superior", 3)
    assert not same(book, "Her father spoke to the superior.", at=1, read=READ)
    assert same(book, "Zossima spoke.", at=1, read=READ)


def test_accents_are_folded_only_where_they_can_add_a_hit(book):
    book.person("Païssy", 3)
    book.person("Adelaida", 3)
    assert same(book, "Paissy prayed.", at=1, read=READ)
    assert same(book, "Païssy prayed.", at=1, read=READ)
    # A read "Adelaïda" (mid-sentence) never clears a different, later "Adelaida".
    assert same(book, "Adelaida laughed.", at=1, read="He saw Adelaïda there.")


def test_a_taken_back_name_is_no_later_name(book):
    book.person("Grushenka", 3, retracted=True)
    book.person("Mitya", 1)
    book.alias("Mitya", "Dmitri", 3, retracted=True)
    assert not same(book, "Grushenka laughed.", at=1, read=READ)
    assert not same(book, "Dmitri laughed.", at=1, read=READ)


def test_a_name_revealed_at_the_place_and_one_chapter_after(book):
    book.person("Corvus", 2)
    book.person("Vex", 3)
    assert not same(book, "Corvus and the lamp.", at=2, read="He met Corvus there.")
    assert same(book, "Vex and the lamp.", at=2, read="He met Corvus there.")


def test_hyphens_and_possessives(book):
    book.person("Eye-Witness", 3)
    book.person("Smerdyakov", 3)
    assert same(book, "The eye-witness saw it.", at=1, read=READ)
    assert same(book, "Smerdyakov's lie was told.", at=1, read=READ)
    assert not same(book, "The eye was dry.", at=1, read="The eye was dry, and a witness slept.")


def test_a_theme_is_a_soft_signal_in_both_modes(book):
    """Themes go to the soft bucket in both modes (the chat does not drop on them); a visible
    theme's words still whitelist, as locally."""
    book.theme("Family Tensions", 3)
    book.ready(1)
    hosted = book.hosted("Family tensions grew.", 1, READ)
    assert hosted["future_theme_hits"] == ["tensions"]          # "family" is a role word
    assert not same(book, "Family tensions grew.", at=1, read=READ)


def test_the_eval_only_mode_without_read_text(book):
    book.person("Mara", 1)
    book.person("Vex", 3)
    same(book, "Vex and Mara.", at=1, read=None)
    book.ready(1)
    assert book.hosted("Vex and Mara.", 1, None)["future_entity_leaks"] == ["vex"]


def test_a_reader_with_no_read_page_meets_every_name_later(book):
    """Place 0: the reader has read nothing, so every name of the book is later, whatever
    ceiling the caller asks for."""
    book.person("Mara", 1)
    book.ready(0)
    for asked in (0, 1, 5):
        assert book.hosted("Mara smiled.", asked, None)["future_entity_leaks"] == ["mara"]
    assert same(book, "Mara smiled.", at=0, read=None)


def test_a_book_whose_file_has_no_edition(tmp_path, database, tenant_dsn):
    """No one has built notes for this file: no later name, as a local book with none."""
    book = Book(tmp_path, database, tenant_dsn, edition=False)
    try:
        assert not same(book, "Vex moved the lamp.", at=1, read=READ)
        book.ready(1)
        assert book.hosted("Vex moved the lamp.", 1, READ)["future_entity_leaks"] == []
    finally:
        book.close()


def test_where_hosted_mode_is_stricter_one_word_in_two_later_names(book):
    """The one place the modes differ, on the safe side. "Ivan" is a later other name of Mara
    (whose name the reader has read) and a word of a later person, Ivan Petrov. Local mode reports
    one of the two names for the word, the last it listed (Mara), and the chat excuses it; hosted
    mode removes the sentence because one of the names it belongs to is not wholly read."""
    book.person("Mara", 1)
    book.person("Ivan Petrov", 3)
    book.alias("Mara", "Ivan", 3)
    book.ready(1)
    local = book.local("Ivan came.", 1, READ)
    hosted = book.hosted("Ivan came.", 1, READ)
    assert local["future_entity_leaks"] == ["Mara"] and not dropped(local, READ)
    assert hosted["future_entity_leaks"] == ["ivan"] and dropped(hosted, READ)


# ---- what the check can be made to say -------------------------------------------------------

def call(dsn: str, owner, book, words, *, at_most=None, read_any=(), read_proper=()):
    """The function as the tenant role calls it: with ``owner`` as the transaction's owner."""
    with psycopg.connect(dsn) as conn, conn.transaction():
        if owner is not None:
            conn.execute("SELECT set_config('app.owner_id', %s, true)", (str(owner),))
        return conn.execute(LATER_NAMES, {
            "book": book, "at_most": at_most, "words": list(words), "folded": list(words),
            "read_any": list(read_any), "read_proper": list(read_proper), "read_also": [],
            "read_not": []}).fetchone()


def test_the_tenant_role_gets_back_only_words_it_sent(book):
    """The break it catches: any word of a later name the caller did not send leaving the
    database, whether the sentence names a later person or not."""
    book.person("Mara", 1)
    book.person("Vex Quillon", 3)
    book.alias("Mara", "the Lamplighter", 4)
    book.theme("Harbour Wars", 5)
    book.ready(1)
    sent = ["vex", "lamp", "the", "wars", "zzz"]
    row = call(book.tenant_dsn, book.owner, book.book, sent)
    assert row[0] is True
    returned = {word for part in row[1:] for word in part}
    assert returned <= set(sent)
    assert row[1:] == (["vex"], ["vex"], ["vex"], ["wars"])
    assert call(book.tenant_dsn, book.owner, book.book, [])[1:] == ([], [], [], [])
    # "Quillon", "Lamplighter" and "Harbour" were never sent and never came back.
    everything = repr(call(book.tenant_dsn, book.owner, book.book, sent))
    for later in ("quillon", "lamplighter", "harbour"):
        assert later not in everything


def test_the_tenant_role_cannot_read_the_words_or_pass_a_place(book, database):
    book.person("Vex", 3)
    book.ready(1)
    with psycopg.connect(book.tenant_dsn) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            conn.execute("SELECT set_config('app.owner_id', %s, true)", (str(book.owner),))
            conn.execute("SELECT * FROM name_words").fetchall()
    # A ceiling above the place changes nothing: the place is the reader's own.
    for asked in (None, 1, 3, 99):
        assert call(book.tenant_dsn, book.owner, book.book, ["vex"], at_most=asked)[1] == ["vex"]
    # Only reading on moves it.
    book.ready(3)
    assert call(book.tenant_dsn, book.owner, book.book, ["vex"])[1] == []
    # The function takes no place: its arguments are the book, a ceiling, the sentence and the
    # reader's read words, and nobody but the tenant role (and its owner) may call it.
    with psycopg.connect(database) as conn:
        assert conn.execute(
            "SELECT proargnames FROM pg_proc WHERE proname = 'story_memory_later_names'"
        ).fetchone()[0][:8] == ["book", "at_most", "sentence_words", "sentence_folded",
                                "read_any", "read_proper", "read_also", "read_not"]
        stranger = f"later_stranger_{uuid.uuid4().hex}"
        conn.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(stranger)))
        try:
            for role in ("public", stranger):
                assert not conn.execute(
                    "SELECT has_function_privilege(%s, 'public.story_memory_later_names("
                    "uuid,integer,text[],text[],text[],text[],text[],text[])', 'EXECUTE')",
                    (role,)).fetchone()[0]
        finally:
            conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(stranger)))


def test_nothing_for_a_book_that_is_not_the_callers(book, database):
    """The break it catches: the check answering for another reader's book, for no owner, or for
    the notes account's own edition."""
    book.person("Vex", 3)
    book.ready(1)
    other = uuid.uuid4()
    with psycopg.connect(database) as conn:
        conn.execute("INSERT INTO users (id,display_name) VALUES (%s,'Other')", (other,))
        [(account,)] = conn.execute("SELECT account_id FROM notes_account").fetchall()
    assert call(book.tenant_dsn, other, book.book, ["vex"]) is None
    assert call(book.tenant_dsn, None, book.book, ["vex"]) is None
    assert call(book.tenant_dsn, account, book.edition[1], ["vex"]) is None
    assert call(database, account, book.edition[1], ["vex"]) is None
    with pytest.raises(LookupError):
        with HostedBookStore(book.tenant_dsn, OwnerId(other)).book(str(book.book)):
            pass


def test_a_name_without_its_words_stops_the_reply(book, database):
    """The break it catches: a name with no words (a publish that forgot them) or words made from
    another text (a name changed afterwards) letting a sentence through unchecked."""
    book.person("Vex", 3)
    book.ready(1)
    assert book.hosted("Vex came.", 1, READ)["future_entity_leaks"] == ["vex"]
    with psycopg.connect(database) as conn:
        conn.execute("UPDATE entities SET canonical_name='Vexalia' WHERE owner_id=%s"
                     " AND book_id=%s", book.edition[:2])
    with pytest.raises(StoryMemoryUnavailable):
        book.hosted("Vex came.", 1, READ)
    with psycopg.connect(database) as conn:
        rebuild_name_words(conn, tuple(book.edition))
        conn.execute("DELETE FROM name_words WHERE owner_id=%s AND book_id=%s",
                     book.edition[:2])
    with pytest.raises(StoryMemoryUnavailable):
        book.hosted("Vex came.", 1, READ)
    assert call(book.tenant_dsn, book.owner, book.book, ["vex"])[0] is False


def test_one_call_per_sentence_and_the_question_holds_no_story(book):
    """What leaves the web process for one sentence: the sentence's words and the reader's own
    read words, once."""
    book.person("Vex", 3)
    book.ready(1)
    question = later_name_question("Vex moved the lamp.", READ)
    assert set(question) == {"words", "folded", "read_any", "read_proper", "read_also",
                             "read_not"}
    assert question["words"] == ["lamp", "moved", "the", "vex"]
    assert set(question["read_any"]) == set(word_tokens(READ))
    calls = []
    with HostedBookStore(book.tenant_dsn, OwnerId(book.owner)).book(str(book.book)) as mem:
        later = mem._later

        def counted(bookmark, asked):
            calls.append(asked)
            return later(bookmark, asked)

        object.__setattr__(mem, "_later", counted)   # the snapshot refuses ordinary changes
        score_recap(mem, 1, "Vex moved the lamp. Mara slept.", read_text=READ)
    assert len(calls) == 1
