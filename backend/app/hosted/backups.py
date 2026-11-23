"""The daily backups' half that talks to Cloudflare R2 (runbook: "Backups").

``deploy/backup.sh`` makes one file a day on the server (the database dump, the book files as they
are on disk, the row counts and checksums, in one tar), has this module encrypt it, and hands the
encrypted file back to it to upload. It runs in the ``backup`` container, which holds the four R2
settings and the backup key and nothing else:

    python -m app.hosted.backups check-key                         # before anything is dumped
    python -m app.hosted.backups encrypt < file.tar > file.tar.age
    python -m app.hosted.backups decrypt < file.tar.age > file.tar # fails loudly if damaged
    python -m app.hosted.backups new-key                           # once, for deploy/.env
    python -m app.hosted.backups upload NAME --size BYTES < file   # upload, check, keep newest 14
    python -m app.hosted.backups list                              # what is in the bucket
    python -m app.hosted.backups download NAME|latest > file       # for a restore (still encrypted)
    python -m app.hosted.backups count-rows < dump.sql             # rows per table, from a dump

The encrypted file is a standard age file (https://age-encryption.org/v1), made with rage through
pyrage: a random file key wrapped for the X25519 recipient of the backup key, then the tar in 64 KiB
chunks, each sealed with ChaCha20-Poly1305, the last one marked as last. A changed byte, a cut-short
file or the wrong key fails to decrypt; nothing is held in memory but a chunk. The stock ``age``
tool decrypts it by hand (runbook, step 5.3).

Older backups are removed only after the new one is in the bucket at its full size, and only the
files this module names (``mothli-backup-<UTC time>.tar.age``): anything else in the bucket is left
alone. Nothing here reads the database, logs anything a reader wrote, or prints the backup key.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, BinaryIO, TextIO

KEEP = 14
_NAME_RE = re.compile(r"^mothli-backup-(?P<stamp>[0-9]{8}T[0-9]{6}Z)\.tar\.age$")
KEY_SETTING = "MOTHLI_BACKUP_ENCRYPTION_KEY"
_KEY_PREFIX = "AGE-SECRET-KEY-1"
_SETTINGS = {
    "endpoint": "MOTHLI_BACKUP_S3_ENDPOINT",
    "bucket": "MOTHLI_BACKUP_BUCKET",
    "access_key_id": "MOTHLI_BACKUP_ACCESS_KEY_ID",
    "secret_access_key": "MOTHLI_BACKUP_SECRET_ACCESS_KEY",
}
_PLACEHOLDER = "OWNER_MUST_FILL_IN"


class BackupError(RuntimeError):
    """A refusal or failure whose message names a step or a setting, never a key."""


def backup_name(now: datetime) -> str:
    """The object name for a backup started at ``now`` (UTC to the second)."""
    if now.tzinfo is None:
        raise ValueError("the backup time must carry its time zone")
    return f"mothli-backup-{now.astimezone(UTC):%Y%m%dT%H%M%SZ}.tar.age"


def is_backup_name(name: str) -> bool:
    if not _NAME_RE.fullmatch(name):
        return False
    try:
        datetime.strptime(_NAME_RE.fullmatch(name)["stamp"], "%Y%m%dT%H%M%SZ")
    except ValueError:
        return False
    return True


def to_remove(names: Iterable[str], new: str, keep: int = KEEP) -> list[str]:
    """The backups to delete once ``new`` is safely uploaded: all of ours but the newest ``keep``.

    Refuses (and so deletes nothing) when ``new`` is not among the names listed, or when it would
    not itself be kept (the server's clock went back): a failed or odd run never thins the bucket.
    Names that are not this module's are never returned."""
    if keep < 1:
        raise ValueError("at least one backup must be kept")
    ours = sorted({name for name in names if is_backup_name(name)}, reverse=True)
    if new not in ours:
        raise BackupError("the new backup is not in the bucket; no older backup was removed")
    kept, removed = ours[:keep], ours[keep:]
    if new not in kept:
        raise BackupError("the new backup is older than the ones kept (is the server's clock "
                          "right?); no older backup was removed")
    return removed


@dataclass(frozen=True)
class Settings:
    endpoint: str
    bucket: str
    access_key_id: str
    secret_access_key: str


def settings_from(env: dict[str, str]) -> Settings:
    values = {}
    for field, variable in _SETTINGS.items():
        value = env.get(variable, "").strip()
        if not value or _PLACEHOLDER in value:
            raise BackupError(f"{variable} is not set in deploy/.env")
        values[field] = value
    if not values["endpoint"].startswith("https://"):
        raise BackupError("MOTHLI_BACKUP_S3_ENDPOINT must start with https://")
    return Settings(**values)


def key_from(env: dict[str, str]) -> Any:
    """The backup key (an age X25519 identity) from ``MOTHLI_BACKUP_ENCRYPTION_KEY``. The value
    never appears in a message: not even pyrage's own words about it are passed on."""
    from pyrage import x25519

    value = env.get(KEY_SETTING, "").strip()
    if not value.startswith(_KEY_PREFIX):
        raise BackupError(f"{KEY_SETTING} is not set in deploy/.env (runbook step 4.1 makes it)")
    try:
        return x25519.Identity.from_str(value)
    except Exception:  # noqa: BLE001
        raise BackupError(f"{KEY_SETTING} in deploy/.env is damaged (a character missing or "
                          "changed): copy that line again from your offline copy of deploy/.env"
                          ) from None


def new_key() -> str:
    from pyrage import x25519

    return str(x25519.Identity.generate())


def encrypt(identity: Any, source: BinaryIO, out: BinaryIO) -> None:
    """Encrypt ``source`` to ``out`` as an age file for the key's own recipient, a chunk at a time."""
    import pyrage

    try:
        pyrage.encrypt_io(source, out, [identity.to_public()])
    except Exception as exc:  # noqa: BLE001
        raise BackupError(f"encrypting the backup failed ({type(exc).__name__})") from None


def decrypt(identity: Any, source: BinaryIO, out: BinaryIO) -> None:
    """Decrypt an age file from ``source`` to ``out``, a chunk at a time. Each chunk is checked
    before it is written; a damaged or cut-short file raises, after the chunks before the damage
    were written, so a caller must treat what it read as worthless unless this returns."""
    import pyrage

    try:
        pyrage.decrypt_io(source, out, [identity])
    except pyrage.DecryptError as exc:
        if "no matching keys" in str(exc).lower():
            raise BackupError(f"{KEY_SETTING} in deploy/.env does not open this backup: it is "
                              "not the key the backup was made with (or the file's first lines "
                              "are damaged)") from None
        raise BackupError("this is not a whole encrypted Mothli backup (damaged, cut short, or "
                          "not one at all)") from None
    except BrokenPipeError:
        raise BackupError("the step reading the decrypted backup stopped early") from None
    except Exception:  # noqa: BLE001 - rage reports a failed chunk as an OSError
        raise BackupError("the backup is damaged or cut short: part of it failed its check, so "
                          "nothing read from it may be used") from None


def client_for(settings: Settings) -> Any:
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=settings.endpoint,
        aws_access_key_id=settings.access_key_id,
        aws_secret_access_key=settings.secret_access_key,
        region_name="auto",
        config=Config(
            signature_version="s3v4",
            retries={"max_attempts": 5, "mode": "standard"},
            connect_timeout=30,
            read_timeout=300,
            # Cloudflare's advice for R2: checksums only where S3 requires them.
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    )


@dataclass(frozen=True)
class Stored:
    name: str
    size: int
    modified: datetime


def listed(client: Any, bucket: str) -> list[Stored]:
    """Every object in the bucket, ours or not (the caller filters)."""
    found: list[Stored] = []
    token: str | None = None
    while True:
        page = client.list_objects_v2(Bucket=bucket, **({"ContinuationToken": token} if token else {}))
        for item in page.get("Contents", []):
            found.append(Stored(item["Key"], int(item["Size"]), item["LastModified"]))
        if not page.get("IsTruncated"):
            return found
        token = page["NextContinuationToken"]


class _Counted:
    """A read-only stream that counts what passes through it."""

    def __init__(self, stream: BinaryIO) -> None:
        self._stream = stream
        self.size = 0

    def read(self, amount: int = -1) -> bytes:
        chunk = self._stream.read(amount)
        self.size += len(chunk)
        return chunk


@dataclass(frozen=True)
class Uploaded:
    name: str
    size: int
    kept: int
    removed: list[str]


def upload(client: Any, bucket: str, name: str, stream: BinaryIO, expected_size: int,
           keep: int = KEEP) -> Uploaded:
    """Upload one backup, check it arrived whole, then remove all of ours but the newest ``keep``.

    Any failure before the check raises before anything is deleted."""
    if not is_backup_name(name):
        raise BackupError("the backup's name is not one this tool makes")
    if expected_size < 1:
        raise BackupError("the backup file is empty; nothing was uploaded and no older backup was removed")
    counted = _Counted(stream)
    try:
        client.upload_fileobj(counted, bucket, name)
    except Exception as exc:  # noqa: BLE001 - name the kind of failure, never a key
        raise BackupError(f"the upload failed ({type(exc).__name__}); no older backup was "
                          "removed") from exc
    if counted.size != expected_size:
        raise BackupError(f"read {counted.size} bytes of a {expected_size}-byte backup; no "
                          "older backup was removed")
    try:
        stored = int(client.head_object(Bucket=bucket, Key=name)["ContentLength"])
    except Exception as exc:  # noqa: BLE001
        raise BackupError(f"the uploaded backup could not be checked ({type(exc).__name__}); "
                          "no older backup was removed") from exc
    if stored != expected_size:
        raise BackupError(f"the bucket holds {stored} of {expected_size} bytes; no older backup "
                          "was removed")
    try:
        names = [item.name for item in listed(client, bucket)]
    except Exception as exc:  # noqa: BLE001
        raise BackupError(f"{name} is uploaded, but the bucket could not be listed "
                          f"({type(exc).__name__}); no older backup was removed") from exc
    removed = to_remove(names, name, keep)
    for old in removed:
        try:
            client.delete_object(Bucket=bucket, Key=old)
        except Exception as exc:  # noqa: BLE001
            raise BackupError(f"{name} is uploaded, but removing {old} failed "
                              f"({type(exc).__name__}); the next backup tries again") from exc
    kept = len([item for item in names if is_backup_name(item)]) - len(removed)
    return Uploaded(name, stored, kept, removed)


def ours(client: Any, bucket: str) -> list[Stored]:
    """This module's backups in the bucket, newest first."""
    return sorted((item for item in listed(client, bucket) if is_backup_name(item.name)),
                  key=lambda item: item.name, reverse=True)


def download(client: Any, bucket: str, which: str, out: BinaryIO) -> Stored:
    backups = ours(client, bucket)
    if not backups:
        raise BackupError("the bucket holds no backup")
    chosen = backups[0] if which == "latest" else next(
        (item for item in backups if item.name == which), None)
    if chosen is None:
        raise BackupError("no backup of that name in the bucket (see: list)")
    client.download_fileobj(bucket, chosen.name, out)
    return chosen


def count_rows(lines: Iterable[str]) -> dict[str, int]:
    """Rows per table in a plain-text dump (``pg_restore --data-only -f -``): every ``COPY`` block
    is one table and each line inside it one row (a row's own line breaks are written as ``\\n``)."""
    counts: dict[str, int] = {}
    table: str | None = None
    for line in lines:
        line = line.rstrip("\n")
        if table is None:
            match = re.match(r"^COPY (\S+) .*FROM stdin;$", line)
            if match:
                schema, _, name = match[1].partition(".")
                table = (name or schema).strip('"')
                counts[table] = counts.get(table, 0)
        elif line == "\\.":
            table = None
        else:
            counts[table] += 1
    if table is not None:
        raise BackupError("the dump ends inside a table's rows: it is cut short")
    return counts


def format_counts(counts: dict[str, int]) -> Iterator[str]:
    """One ``table rows`` line per table, in the order deploy/counts.sql prints them."""
    for table in sorted(counts):
        yield f"{table} {counts[table]}"


def _megabytes(size: int) -> str:
    return f"{size / 1_000_000:.1f} MB"


def main(argv: list[str] | None = None, *, env: dict[str, str] | None = None,
         stdin: BinaryIO | None = None, stdout: BinaryIO | None = None,
         text_out: TextIO | None = None, client: Any = None) -> int:
    parser = argparse.ArgumentParser(description="Mothli's backups in Cloudflare R2")
    commands = parser.add_subparsers(dest="command", required=True)
    up = commands.add_parser("upload", help="upload a backup from standard input")
    up.add_argument("name")
    up.add_argument("--size", type=int, required=True)
    commands.add_parser("list", help="the backups in the bucket, newest first")
    down = commands.add_parser("download", help="write a backup to standard output")
    down.add_argument("which", help="a backup's name, or latest")
    commands.add_parser("count-rows", help="rows per table in a plain-text dump on standard input")
    commands.add_parser("check-key", help="refuse unless the backup key in deploy/.env is usable")
    commands.add_parser("encrypt", help="encrypt standard input to standard output")
    commands.add_parser("decrypt", help="decrypt standard input to standard output")
    commands.add_parser("new-key", help="print a new backup key for deploy/.env")
    args = parser.parse_args(argv)
    env = dict(os.environ) if env is None else env
    stdin = sys.stdin.buffer if stdin is None else stdin
    stdout = sys.stdout.buffer if stdout is None else stdout
    say = sys.stderr if text_out is None else text_out
    try:
        if args.command == "count-rows":
            import io

            text = io.TextIOWrapper(stdin, encoding="utf-8", errors="replace", newline="\n")
            for line in format_counts(count_rows(text)):
                stdout.write(line.encode("utf-8") + b"\n")
            stdout.flush()
            return 0
        if args.command == "new-key":
            if env.get(KEY_SETTING, "").strip().startswith(_KEY_PREFIX):
                raise BackupError(f"{KEY_SETTING} is already set in deploy/.env: keep it. A new "
                                  "key cannot open the backups made with the old one")
            stdout.write(new_key().encode("ascii") + b"\n")
            stdout.flush()
            return 0
        if args.command in ("check-key", "encrypt", "decrypt"):
            identity = key_from(env)
            if args.command == "encrypt":
                encrypt(identity, stdin, stdout)
            elif args.command == "decrypt":
                decrypt(identity, stdin, stdout)
            stdout.flush()
            if args.command == "check-key":
                print("the backup key in deploy/.env is set", file=say, flush=True)
            return 0
        settings = settings_from(env)
        client = client_for(settings) if client is None else client
        if args.command == "upload":
            done = upload(client, settings.bucket, args.name, stdin, args.size)
            print(f"uploaded {done.name} ({_megabytes(done.size)}); {done.kept} backups kept, "
                  f"{len(done.removed)} older removed", file=say, flush=True)
        elif args.command == "list":
            backups = ours(client, settings.bucket)
            for item in backups:
                print(f"{item.name}  {_megabytes(item.size):>10}  uploaded "
                      f"{item.modified.astimezone(UTC):%Y-%m-%d %H:%M} UTC", file=say)
            print(f"{len(backups)} backups in {settings.bucket}", file=say, flush=True)
        else:
            chosen = download(client, settings.bucket, args.which, stdout)
            stdout.flush()
            print(f"downloaded {chosen.name} ({_megabytes(chosen.size)})", file=say, flush=True)
    except BackupError as exc:
        print(f"backup failed: {exc}", file=say, flush=True)
        return 1
    except Exception as exc:  # noqa: BLE001 - name the failure, never print a key
        print(f"backup failed: {type(exc).__name__} talking to the bucket", file=say, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
