"""The daily backups' rules, against a stand-in bucket (runbook step 4). No Cloudflare, no network:
the R2 client is replaced by an object that keeps files in memory."""

from __future__ import annotations

import io
import re
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


def run(argv, bucket, stdin=b""):
    out, said = io.BytesIO(), io.StringIO()
    code = backups.main(argv, env=dict(SETTINGS), stdin=io.BytesIO(stdin), stdout=out,
                        text_out=said, client=bucket)
    return code, out.getvalue(), said.getvalue()


def test_a_backup_is_named_by_its_utc_time_to_the_second() -> None:
    assert backup_name(datetime(2026, 11, 23, 3, 0, 12, tzinfo=UTC)) == \
        "mothli-backup-20261123T030012Z.tar"
    # Any time zone in, UTC out.
    tirana = timezone(timedelta(hours=1))
    assert backup_name(datetime(2026, 11, 23, 4, 0, 12, tzinfo=tirana)) == \
        "mothli-backup-20261123T030012Z.tar"
    with pytest.raises(ValueError):
        backup_name(datetime(2026, 11, 23, 3, 0, 12))
    # The shell script makes the same name.
    script = (DEPLOY / "backup.sh").read_text(encoding="utf-8")
    assert 'name="mothli-backup-$(date -u +%Y%m%dT%H%M%SZ).tar"' in script
    assert is_backup_name("mothli-backup-20261123T030012Z.tar")
    for other in ("mothli-backup-20261123T030012Z.tar.part", "notes.txt", "buzzfetti-x.tar",
                  "mothli-backup-20261323T030012Z.tar", "old/mothli-backup-20261123T030012Z.tar",
                  "mothli-backup-2026-11-23.tar"):
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


def test_an_upload_that_arrives_whole_keeps_fourteen_and_says_so() -> None:
    old = nightly(15)
    bucket = Bucket([*old, "someone-elses-file.zip"])
    new = backup_name(datetime(2026, 11, 23, 3, 0, 9, tzinfo=UTC))
    code, _, said = run(["upload", new, "--size", "11"], bucket, b"backup body")
    assert code == 0, said
    assert bucket.objects[new] == b"backup body"
    assert sorted(bucket.deleted) == old[:2]
    assert "someone-elses-file.zip" in bucket.objects
    assert len([name for name in bucket.objects if is_backup_name(name)]) == 14
    assert said == f"uploaded {new} (0.0 MB); 14 backups kept, 2 older removed\n"


@pytest.mark.parametrize("bucket, size, reason", [
    (Bucket(nightly(20), fail_upload=True), 11, "the upload failed (ConnectionError)"),
    (Bucket(nightly(20), cut_upload=True), 11, "the bucket holds 5 of 11 bytes"),
    (Bucket(nightly(20)), 12, "read 11 bytes of a 12-byte backup"),
    (Bucket(nightly(20)), 0, "the backup file is empty"),
])
def test_a_failed_upload_never_removes_an_older_backup(bucket, size, reason) -> None:
    before = set(bucket.objects)
    new = backup_name(datetime(2026, 11, 23, 3, 0, 9, tzinfo=UTC))
    code, _, said = run(["upload", new, "--size", str(size)], bucket, b"backup body")
    assert code == 1
    assert bucket.deleted == []
    assert before <= set(bucket.objects)
    assert said.startswith("backup failed: ") and reason in said, said
    assert "no older backup was removed" in said
    assert "made-up-secret-value" not in said and "made-up-id" not in said


def test_a_failed_removal_says_the_new_backup_is_safe_and_exits_non_zero() -> None:
    bucket = Bucket(nightly(15), fail_delete=True)
    new = backup_name(datetime(2026, 11, 23, 3, 0, 9, tzinfo=UTC))
    code, _, said = run(["upload", new, "--size", "11"], bucket, b"backup body")
    assert code == 1
    assert new in bucket.objects
    assert f"{new} is uploaded, but removing" in said


def test_a_name_the_tool_does_not_make_is_refused_before_anything_is_sent() -> None:
    bucket = Bucket(nightly(3))
    code, _, said = run(["upload", "../mothli.tar", "--size", "11"], bucket, b"backup body")
    assert code == 1 and "../mothli.tar" not in bucket.objects and bucket.deleted == []


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


def test_the_backup_container_holds_the_r2_settings_and_no_database_role() -> None:
    """The break it catches: the uploader given a database password, the reader content key, or
    a way onto the data network, or the R2 key handed to web or worker."""
    compose = (DEPLOY / "compose.yaml").read_text(encoding="utf-8")
    service = compose.split("\n  backup:\n", 1)[1].split("\n\n", 1)[0]
    assert set(re.findall(r"^      ([A-Z0-9_]+): ", service, flags=re.M)) == set(SETTINGS)
    assert "networks: [egress]" in service
    assert "books:/var/lib/mothli/books:ro" in service
    assert "env_file" not in service and "PASSWORD" not in service and "DSN" not in service
    others = compose.replace(service, "")
    assert "MOTHLI_BACKUP_" not in others
    template = (DEPLOY / ".env.example").read_text(encoding="utf-8")
    for variable in ("MOTHLI_BACKUP_S3_ENDPOINT", "MOTHLI_BACKUP_ACCESS_KEY_ID",
                     "MOTHLI_BACKUP_SECRET_ACCESS_KEY"):
        assert re.search(rf"^{variable}=\S*OWNER_MUST_FILL_IN", template, flags=re.M), variable
    assert "MOTHLI_BACKUP_" not in (DEPLOY / "app.env.example").read_text(encoding="utf-8")


def test_the_scripts_stop_on_any_failure_and_dump_as_the_superuser_inside_db() -> None:
    """The break it catches: a pipe that hides a failed step (sh has no pipefail), a dump taken
    over the network with a password, or a restore that keeps going after an error."""
    backup = (DEPLOY / "backup.sh").read_text(encoding="utf-8")
    restore = (DEPLOY / "restore.sh").read_text(encoding="utf-8")
    for script in (backup, restore):
        assert script.startswith("#!/usr/bin/env bash\n")
        assert "\nset -Eeuo pipefail\n" in script
    assert "exec -T db sh -c 'exec pg_dump -U postgres -d \"$POSTGRES_DB\" --format=custom'" in backup
    # The upload comes after every other step, and removing old files happens only inside it.
    assert backup.index("upload \"$name\"") > backup.index("sha256sum")
    assert "delete" not in backup.lower()
    assert restore.count("--exit-on-error --single-transaction") == 2
