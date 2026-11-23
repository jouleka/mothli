"""The daily backups' rules, against a stand-in bucket (runbook step 4). No Cloudflare, no network:
the R2 client is replaced by an object that keeps files in memory. Backup keys are made fresh for
each run; none is written down anywhere."""

from __future__ import annotations

import hashlib
import io
import logging
import re
import tracemalloc
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.hosted import backups
from app.hosted.backups import BackupError, backup_name, count_rows, is_backup_name, to_remove

DEPLOY = Path(__file__).resolve().parents[3] / "deploy"
SETTINGS = {
    "MOTHLI_BACKUP_S3_ENDPOINT": "https://made-up-account.r2.example",
    "MOTHLI_BACKUP_BUCKET": "made-up-bucket",
    "MOTHLI_BACKUP_ACCESS_KEY_ID": "made-up-id",
    "MOTHLI_BACKUP_SECRET_ACCESS_KEY": "made-up-secret-value",
}
KEY = backups.new_key()
OTHER_KEY = backups.new_key()
WITH_KEY = {**SETTINGS, backups.KEY_SETTING: KEY}


def nightly(count: int, start: datetime = datetime(2026, 11, 1, 3, 0, 7, tzinfo=UTC)) -> list[str]:
    return [backup_name(start + timedelta(days=day)) for day in range(count)]


class Bucket:
    """Just enough of an S3 client: keys to bytes, listed a few to a page."""

    def __init__(self, names=(), *, fail_upload=False, cut_upload=False, fail_delete=False):
        self.objects = {name: b"old" for name in names}
        self.fail_upload, self.cut_upload, self.fail_delete = fail_upload, cut_upload, fail_delete
        self.deleted: list[str] = []

    def upload_fileobj(self, stream, bucket, key):
        data = stream.read()
        if self.fail_upload:
            raise ConnectionError("made-up network failure")
        self.objects[key] = data[: len(data) // 2] if self.cut_upload else data

    def head_object(self, Bucket, Key):  # noqa: N803 - boto3's own argument names
        return {"ContentLength": len(self.objects[Key])}

    def list_objects_v2(self, Bucket, ContinuationToken=None):  # noqa: N803
        names = sorted(self.objects)
        start = int(ContinuationToken or 0)
        page = names[start:start + 4]
        more = start + 4 < len(names)
        return {"Contents": [{"Key": name, "Size": len(self.objects[name]),
                              "LastModified": datetime(2026, 11, 20, tzinfo=UTC)} for name in page],
                "IsTruncated": more, **({"NextContinuationToken": str(start + 4)} if more else {})}

    def delete_object(self, Bucket, Key):  # noqa: N803
        if self.fail_delete:
            raise ConnectionError("made-up network failure")
        self.deleted.append(Key)
        del self.objects[Key]

    def download_fileobj(self, bucket, key, out):
        out.write(self.objects[key])


def run(argv, bucket=None, stdin=b"", env=None):
    out, said = io.BytesIO(), io.StringIO()
    code = backups.main(argv, env=dict(WITH_KEY if env is None else env),
                        stdin=io.BytesIO(stdin), stdout=out, text_out=said, client=bucket)
    return code, out.getvalue(), said.getvalue()


def sealed(body: bytes, key: str = KEY) -> bytes:
    code, out, said = run(["encrypt"], stdin=body, env={backups.KEY_SETTING: key})
    assert code == 0, said
    return out


def test_a_backup_is_named_by_its_utc_time_to_the_second() -> None:
    assert backup_name(datetime(2026, 11, 23, 3, 0, 12, tzinfo=UTC)) == \
        "mothli-backup-20261123T030012Z.tar.age"
    # Any time zone in, UTC out.
    tirana = timezone(timedelta(hours=1))
    assert backup_name(datetime(2026, 11, 23, 4, 0, 12, tzinfo=tirana)) == \
        "mothli-backup-20261123T030012Z.tar.age"
    with pytest.raises(ValueError):
        backup_name(datetime(2026, 11, 23, 3, 0, 12))
    # The shell script makes the same name.
    script = (DEPLOY / "backup.sh").read_text(encoding="utf-8")
    assert 'name="mothli-backup-$(date -u +%Y%m%dT%H%M%SZ).tar.age"' in script
    assert is_backup_name("mothli-backup-20261123T030012Z.tar.age")
    for other in ("mothli-backup-20261123T030012Z.tar.age.part", "notes.txt", "buzzfetti-x.tar",
                  "mothli-backup-20261123T030012Z.tar",  # an unencrypted backup is never ours
                  "mothli-backup-20261323T030012Z.tar.age",
                  "old/mothli-backup-20261123T030012Z.tar.age", "mothli-backup-2026-11-23.tar.age"):
        assert not is_backup_name(other), other


def test_the_newest_fourteen_are_kept_and_only_ours_are_ever_removed() -> None:
    names = nightly(20)
    strangers = ["README.txt", "buzzfetti-protected.tar", "mothli-backup-latest.tar",
                 "manual/mothli-backup-20200101T000000Z.tar"]
    removed = to_remove([*reversed(names), *strangers], new=names[-1])
    assert removed == list(reversed(names[:6]))
    assert not set(removed) & set(strangers)
    # Fewer than fourteen: nothing goes.
    assert to_remove(nightly(14), new=nightly(14)[-1]) == []
    assert to_remove(nightly(3), new=nightly(3)[-1]) == []


def test_nothing_is_removed_when_the_new_backup_is_missing_or_older_than_the_kept_ones() -> None:
    names = nightly(20)
    with pytest.raises(BackupError, match="not in the bucket"):
        to_remove(names, new=backup_name(datetime(2026, 12, 25, tzinfo=UTC)))
    # A clock that went back: the new file would be the one pruned.
    with pytest.raises(BackupError, match="clock"):
        to_remove(names, new=names[0])


BODY = sealed(b"backup body")  # what deploy/backup.sh uploads: the encrypted file
SIZE = len(BODY)


def test_an_upload_that_arrives_whole_keeps_fourteen_and_says_so() -> None:
    old = nightly(15)
    bucket = Bucket([*old, "someone-elses-file.zip"])
    new = backup_name(datetime(2026, 11, 23, 3, 0, 9, tzinfo=UTC))
    code, _, said = run(["upload", new, "--size", str(SIZE)], bucket, BODY)
    assert code == 0, said
    assert bucket.objects[new] == BODY
    assert sorted(bucket.deleted) == old[:2]
    assert "someone-elses-file.zip" in bucket.objects
    assert len([name for name in bucket.objects if is_backup_name(name)]) == 14
    assert said == f"uploaded {new} (0.0 MB); 14 backups kept, 2 older removed\n"
    # What a restore downloads opens with the key, and with no other.
    code, out, _ = run(["download", "latest"], bucket)
    assert (code, out) == (0, BODY)
    assert run(["decrypt"], stdin=out) == (0, b"backup body", "")
    assert run(["decrypt"], stdin=out, env={backups.KEY_SETTING: OTHER_KEY})[0] == 1


@pytest.mark.parametrize("bucket, size, reason", [
    (Bucket(nightly(20), fail_upload=True), SIZE, "the upload failed (ConnectionError)"),
    (Bucket(nightly(20), cut_upload=True), SIZE, f"the bucket holds {SIZE // 2} of {SIZE} bytes"),
    (Bucket(nightly(20)), SIZE + 1, f"read {SIZE} bytes of a {SIZE + 1}-byte backup"),
    (Bucket(nightly(20)), 0, "the backup file is empty"),
])
def test_a_failed_upload_never_removes_an_older_backup(bucket, size, reason) -> None:
    before = set(bucket.objects)
    new = backup_name(datetime(2026, 11, 23, 3, 0, 9, tzinfo=UTC))
    code, _, said = run(["upload", new, "--size", str(size)], bucket, BODY)
    assert code == 1
    assert bucket.deleted == []
    assert before <= set(bucket.objects)
    assert said.startswith("backup failed: ") and reason in said, said
    assert "no older backup was removed" in said
    assert "made-up-secret-value" not in said and "made-up-id" not in said and KEY not in said


def test_a_failed_removal_says_the_new_backup_is_safe_and_exits_non_zero() -> None:
    bucket = Bucket(nightly(15), fail_delete=True)
    new = backup_name(datetime(2026, 11, 23, 3, 0, 9, tzinfo=UTC))
    code, _, said = run(["upload", new, "--size", str(SIZE)], bucket, BODY)
    assert code == 1
    assert new in bucket.objects
    assert f"{new} is uploaded, but removing" in said


def test_a_name_the_tool_does_not_make_is_refused_before_anything_is_sent() -> None:
    bucket = Bucket(nightly(3))
    for name in ("../mothli.tar.age", "mothli-backup-20261123T030012Z.tar"):
        code, _, said = run(["upload", name, "--size", str(SIZE)], bucket, BODY)
        assert code == 1 and name not in bucket.objects and bucket.deleted == []


def test_list_shows_ours_newest_first_and_download_fetches_latest_or_by_name() -> None:
    names = nightly(3)
    bucket = Bucket([*names, "notes.txt"])
    code, _, said = run(["list"], bucket)
    assert code == 0
    lines = said.splitlines()
    assert [line.split()[0] for line in lines[:-1]] == list(reversed(names))
    assert lines[-1] == "3 backups in made-up-bucket"
    bucket.objects[names[-1]] = b"newest"
    code, out, said = run(["download", "latest"], bucket)
    assert (code, out) == (0, b"newest") and said.startswith(f"downloaded {names[-1]}")
    code, out, _ = run(["download", names[0]], bucket)
    assert (code, out) == (0, b"old")
    code, out, said = run(["download", "notes.txt"], bucket)
    assert code == 1 and out == b"" and "no backup of that name" in said
    assert run(["download", "latest"], Bucket(["notes.txt"]))[2] == \
        "backup failed: the bucket holds no backup\n"


@pytest.mark.parametrize("variable", sorted(SETTINGS))
def test_a_missing_or_placeholder_setting_is_named_and_nothing_is_sent(variable) -> None:
    for value in ("", "OWNER_MUST_FILL_IN", "https://OWNER_MUST_FILL_IN.r2.cloudflarestorage.com"):
        env = {**SETTINGS, variable: value}
        with pytest.raises(BackupError, match=variable):
            backups.settings_from(env)
    with pytest.raises(BackupError, match="https"):
        backups.settings_from({**SETTINGS, "MOTHLI_BACKUP_S3_ENDPOINT": "http://plain.example"})


def test_rows_are_counted_from_a_plain_text_dump() -> None:
    dump = "\n".join([
        "SET statement_timeout = 0;",
        "COPY public.users (id, email) FROM stdin;",
        "1\tone@example.test",
        "2\ttwo\\nlines@example.test",
        "\\.",
        "",
        "COPY public.audit_events (id) FROM stdin;",
        "\\.",
        'COPY public."chapter_notes" (id, sentences) FROM stdin;',
        "7\t[\"COPY public.users (x) FROM stdin;\"]",
        "\\.",
        "SELECT pg_catalog.setval('public.users_id_seq', 2, true);",
    ]) + "\n"
    assert count_rows(io.StringIO(dump)) == {"users": 2, "audit_events": 0, "chapter_notes": 1}
    out = io.BytesIO()
    assert backups.main(["count-rows"], env={}, stdin=io.BytesIO(dump.encode()), stdout=out,
                        text_out=io.StringIO()) == 0
    assert out.getvalue() == b"audit_events 0\nchapter_notes 1\nusers 2\n"
    with pytest.raises(BackupError, match="cut short"):
        count_rows(io.StringIO("COPY public.users (id) FROM stdin;\n1\n"))


def test_the_backup_container_alone_holds_the_r2_settings_and_the_backup_key() -> None:
    """The break it catches: the uploader given a database password, the reader content key, or
    a way onto the data network, or the R2 key or the backup key handed to any other service."""
    compose = (DEPLOY / "compose.yaml").read_text(encoding="utf-8")
    service = compose.split("\n  backup:\n", 1)[1].split("\n\n", 1)[0]
    assert set(re.findall(r"^      ([A-Z0-9_]+): ", service, flags=re.M)) == \
        {*SETTINGS, backups.KEY_SETTING}
    assert "networks: [egress]" in service
    assert "books:/var/lib/mothli/books:ro" in service
    assert "env_file" not in service and "PASSWORD" not in service and "DSN" not in service
    others = compose.replace(service, "")
    assert "MOTHLI_BACKUP_" not in others
    template = (DEPLOY / ".env.example").read_text(encoding="utf-8")
    for variable in ("MOTHLI_BACKUP_S3_ENDPOINT", "MOTHLI_BACKUP_ACCESS_KEY_ID",
                     "MOTHLI_BACKUP_SECRET_ACCESS_KEY", backups.KEY_SETTING):
        assert re.search(rf"^{variable}=\S*OWNER_MUST_FILL_IN", template, flags=re.M), variable
    # The template's placeholder is refused as "not set", not taken for a key.
    placeholder = re.search(rf"^{backups.KEY_SETTING}=(\S+)", template, flags=re.M)[1]
    with pytest.raises(BackupError, match="is not set"):
        backups.key_from({backups.KEY_SETTING: placeholder})
    assert "MOTHLI_BACKUP_" not in (DEPLOY / "app.env.example").read_text(encoding="utf-8")


def test_the_scripts_stop_on_any_failure_and_dump_as_the_superuser_inside_db() -> None:
    """The break it catches: a pipe that hides a failed step (sh has no pipefail), a dump taken
    over the network with a password, a restore that keeps going after an error, an unencrypted
    file uploaded, or a restore that trusts a backup before it decrypted whole."""
    backup = (DEPLOY / "backup.sh").read_text(encoding="utf-8")
    restore = (DEPLOY / "restore.sh").read_text(encoding="utf-8")
    for script in (backup, restore):
        assert script.startswith("#!/usr/bin/env bash\n")
        assert "\nset -Eeuo pipefail\n" in script
    assert "exec -T db sh -c 'exec pg_dump -U postgres -d \"$POSTGRES_DB\" --format=custom'" in backup
    # The key is checked before anything is dumped; the tar goes straight into the encryption,
    # and the file measured and uploaded is the encrypted one.
    assert backup.index("backups check-key") < backup.index("pg_dump")
    assert "-cf - database.dump counts.txt books.tar SHA256SUMS \\\n" \
           "  | $COMPOSE run --rm --no-deps -T backup python -m app.hosted.backups encrypt " \
           '> "$DIR/$name"' in backup
    assert backup.index("backups encrypt") < backup.index("stat -c %s") < \
        backup.index("upload \"$name\"")
    # The upload comes after every other step, and removing old files happens only inside it.
    assert backup.index("upload \"$name\"") > backup.index("sha256sum")
    assert "delete" not in backup.lower()
    # A restore checks the key first, decrypts (dropping what it unpacked if that fails), then
    # checks the checksums; a plain tar is never unpacked as it is.
    assert restore.index("backups check-key") < restore.index("backups download")
    assert restore.index("backups decrypt") < restore.index("sha256sum -c")
    assert '| tar -C "$src" -xf - || { rm -rf "$src"; false; }' in restore
    assert '-xf "$file"' not in restore
    assert restore.count("--exit-on-error --single-transaction") == 2


class Source:
    """A long made-up backup, produced a piece at a time and never held whole."""

    def __init__(self, size: int) -> None:
        self.size, self.given = size, 0
        self.digest = hashlib.sha256()
        self.block = hashlib.sha256(b"made-up").digest() * 2048  # 64 KiB, not all one byte

    def read(self, amount: int = -1) -> bytes:
        assert amount is not None and amount > 0, "asked to read everything at once"
        amount = min(amount, self.size - self.given)
        start = self.given % len(self.block)
        chunk = self.block[start:start + amount]
        while len(chunk) < amount:
            chunk += self.block[:amount - len(chunk)]
        self.given += len(chunk)
        self.digest.update(chunk)
        return chunk


class Sink:
    """Takes what is written, a piece at a time, noting how far the source was at the first."""

    def __init__(self, source=None, keep_to=None) -> None:
        self.source, self.keep_to = source, keep_to
        self.digest, self.size, self.largest = hashlib.sha256(), 0, 0
        self.source_at_first_write: int | None = None

    def write(self, data) -> int:
        data = bytes(data)
        if self.source_at_first_write is None and self.source is not None:
            self.source_at_first_write = self.source.given
        self.digest.update(data)
        self.size += len(data)
        self.largest = max(self.largest, len(data))
        if self.keep_to is not None:
            self.keep_to.write(data)
        return len(data)

    def flush(self) -> None:
        pass


def test_a_large_backup_is_encrypted_and_decrypted_a_piece_at_a_time(tmp_path) -> None:
    """The break it catches: a backup of gigabytes read into memory to encrypt or decrypt it, or
    one that comes back different."""
    size = 96 * 1024 * 1024 + 12345  # not a whole number of chunks
    identity = backups.key_from({backups.KEY_SETTING: KEY})
    tracemalloc.start()
    try:
        source = Source(size)
        with (tmp_path / "backup.tar.age").open("wb") as file:
            sealed_sink = Sink(source, keep_to=file)
            backups.encrypt(identity, source, sealed_sink)
        with (tmp_path / "backup.tar.age").open("rb") as file:
            reader = _Tracked(file)
            opened = Sink(reader)
            backups.decrypt(identity, reader, opened)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert source.given == size and opened.size == size
    assert opened.digest.digest() == source.digest.digest()
    assert sealed_sink.size > size  # each 64 KiB chunk carries its own check
    # Output began long before the input ended, in small pieces, and memory stayed small.
    for sink in (sealed_sink, opened):
        assert sink.source_at_first_write is not None and sink.source_at_first_write < 1024 * 1024
        assert sink.largest <= 1024 * 1024
    assert peak < 16 * 1024 * 1024, peak


class _Tracked:
    def __init__(self, file) -> None:
        self.file, self.given = file, 0

    def read(self, amount: int = -1) -> bytes:
        assert amount is not None and amount > 0, "asked to read everything at once"
        chunk = self.file.read(amount)
        self.given += len(chunk)
        return chunk


def _damaged() -> dict[str, bytes]:
    whole = sealed(Source(300 * 1024).read(300 * 1024))
    # age: the header ends with its "--- " line, then a 16-byte nonce, then the chunks, each
    # 64 KiB of the tar and a 16-byte check.
    first_chunk = whole.index(b"\n", whole.index(b"\n--- ") + 1) + 1 + 16
    assert len(whole) > first_chunk + 4 * (65536 + 16)
    flipped_middle, flipped_last = bytearray(whole), bytearray(whole)
    flipped_middle[len(whole) // 2] ^= 0x01
    flipped_last[-5] ^= 0x80
    return {
        "a byte changed in the middle": bytes(flipped_middle),
        "a byte changed in the last chunk": bytes(flipped_last),
        "cut short inside a chunk": whole[:-1000],
        "cut short at a chunk's end": whole[:first_chunk + 2 * (65536 + 16)],
        "cut to its first lines": whole[:first_chunk - 30],
        "empty": b"",
        "a plain unencrypted tar": b"database.dump" + bytes(500),
    }


@pytest.mark.parametrize("label", sorted(_damaged()))
def test_a_damaged_or_cut_short_backup_fails_loudly(label) -> None:
    """The break it catches: a tampered, truncated or unencrypted file restored quietly."""
    code, _, said = run(["decrypt"], stdin=_damaged()[label])
    assert code == 1, label
    assert said.startswith("backup failed: ") and ("damaged" in said or "not a whole" in said), said
    assert said.endswith("\n") and said.count("\n") == 1
    assert KEY not in said


def test_the_wrong_key_says_so_and_a_missing_or_broken_key_refuses_before_reading() -> None:
    code, out, said = run(["decrypt"], stdin=BODY, env={backups.KEY_SETTING: OTHER_KEY})
    assert (code, out) == (1, b"")
    assert "does not open this backup: it is not the key the backup was made with" in said
    assert OTHER_KEY not in said and KEY not in said
    broken = KEY[:-3] + ("QQQ" if not KEY.endswith("QQQ") else "PPP")
    for env, reason in (({}, "is not set"),
                        ({backups.KEY_SETTING: ""}, "is not set"),
                        ({backups.KEY_SETTING: "OWNER_MUST_FILL_IN"}, "is not set"),
                        ({backups.KEY_SETTING: broken}, "is damaged"),
                        ({backups.KEY_SETTING: KEY[:-1]}, "is damaged")):
        for command in ("check-key", "encrypt", "decrypt"):
            source = _Tracked(io.BytesIO(b"never read"))
            said = io.StringIO()
            code = backups.main([command], env=env, stdin=source, stdout=io.BytesIO(),
                                text_out=said)
            assert code == 1 and source.given == 0, (command, reason)
            assert said.getvalue().startswith(f"backup failed: {backups.KEY_SETTING} ")
            assert reason in said.getvalue()
            assert KEY not in said.getvalue() and broken not in said.getvalue()
    assert run(["check-key"]) == (0, b"", "the backup key in deploy/.env is set\n")


def test_a_new_key_is_a_usable_age_key_and_is_never_made_over_an_existing_one() -> None:
    code, out, said = run(["new-key"], env={})
    assert code == 0 and said == ""
    made = out.decode("ascii").strip()
    assert made.startswith("AGE-SECRET-KEY-1") and made != KEY
    assert sealed(b"x", key=made) != sealed(b"x")
    code, out, said = run(["new-key"])
    assert (code, out) == (1, b"") and "already set" in said and KEY not in said


def test_no_command_logs_or_prints_the_backup_key(caplog, capsys) -> None:
    """The break it catches: the key in a log line, an error, or anything a command prints."""
    caplog.set_level(logging.DEBUG)
    bucket = Bucket(nightly(15))
    new = backup_name(datetime(2026, 11, 23, 3, 0, 9, tzinfo=UTC))
    outputs = [
        run(["check-key"]), run(["encrypt"], stdin=b"made-up"), run(["decrypt"], stdin=BODY),
        run(["decrypt"], stdin=BODY[:-9]), run(["decrypt"], stdin=b"plain"),
        run(["upload", new, "--size", str(SIZE)], bucket, BODY), run(["list"], bucket),
        run(["download", "latest"], bucket), run(["new-key"]),
        run(["decrypt"], stdin=BODY, env={backups.KEY_SETTING: OTHER_KEY}),
    ]
    printed = capsys.readouterr()
    seen = "".join(said for _, _, said in outputs) + printed.out + printed.err + caplog.text
    assert KEY not in seen and OTHER_KEY not in seen
    assert all(KEY.encode() not in out for _, out, _ in outputs)
