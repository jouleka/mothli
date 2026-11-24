"""deploy/report.sh: runs a scheduled job unchanged and tells the watcher ok or fail."""

from __future__ import annotations

import shutil
import socket
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[3] / "deploy"
BASH = shutil.which("bash")
CURL = shutil.which("curl")
TOKEN = "test-token-" + "0" * 40  # a stand-in, never a real one

pytestmark = pytest.mark.skipif(not BASH or not CURL, reason="needs bash and curl")


class Watcher:
    """A stand-in for the Worker: records each report and answers 204."""

    def __init__(self) -> None:
        self.reports: list[dict] = []
        reports = self.reports

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - the http.server name
                length = int(self.headers.get("Content-Length") or 0)
                reports.append({"path": self.path,
                                "auth": self.headers.get("Authorization"),
                                "body": self.rfile.read(length).decode()})
                self.send_response(204)
                self.end_headers()

            def log_message(self, *args) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def watcher():
    w = Watcher()
    yield w
    w.close()


def setup(tmp_path: Path, url: str, token: str = TOKEN) -> Path:
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    script = deploy / "report.sh"
    script.write_bytes((DEPLOY / "report.sh").read_bytes())
    (deploy / ".env").write_text(
        "OTHER_SETTING_NOT_FOR_JOBS=kept-out\n"
        f"MOTHLI_WATCH_URL={url}\n"
        f'MOTHLI_WATCH_TOKEN="{token}"\n', encoding="utf-8")
    return script


def run(script: Path, job: str, command: str) -> subprocess.CompletedProcess:
    return subprocess.run([BASH, script.as_posix(), job, "--", BASH, "-c", command],
                          capture_output=True, text=True, timeout=60, check=False)


def test_ok_passes_output_and_status_through_and_reports_ok(tmp_path, watcher):
    script = setup(tmp_path, watcher.url)
    done = run(script, "paddle-check",
               "echo 'checked 2, changed 0, failed 0'; echo note >&2; env")
    assert done.returncode == 0
    assert done.stdout.startswith("checked 2, changed 0, failed 0\n")
    assert done.stderr == "note\n"
    assert "kept-out" not in done.stdout  # deploy/.env is not loaded into the job
    assert watcher.reports == [
        {"path": "/report/paddle-check/ok", "auth": f"Bearer {TOKEN}", "body": ""}]


def test_a_failed_backup_reports_its_own_words(tmp_path, watcher):
    script = setup(tmp_path, watcher.url)
    done = run(script, "backup",
               "echo '2026-11-24T03:00:00Z backup started'; "
               "echo '2026-11-24T03:00:09Z BACKUP FAILED while uploading to R2: kept' >&2; exit 1")
    assert done.returncode == 1
    assert "BACKUP FAILED while uploading to R2" in done.stderr
    assert watcher.reports[0]["path"] == "/report/backup/fail"
    assert watcher.reports[0]["body"] == "BACKUP FAILED while uploading to R2: kept"


def test_a_held_account_is_a_failure_that_names_paddle(tmp_path, watcher):
    script = setup(tmp_path, watcher.url)
    done = run(script, "purge",
               "echo 'erased 0, unfinished 0, held 1, waiting 3; editions erased 0, "
               "unfinished 0, waiting 0'; exit 1")
    assert done.returncode == 1
    assert watcher.reports[0]["path"] == "/report/purge/fail"
    assert "waiting on Paddle" in watcher.reports[0]["body"]


def test_other_failures_give_the_exit_status(tmp_path, watcher):
    script = setup(tmp_path, watcher.url)
    done = run(script, "purge", "printf 'odd\\001line\\n'; exit 7")
    assert done.returncode == 7
    assert done.stdout == "odd\x01line\n"  # output passes through untouched
    assert watcher.reports[0]["body"] == "it stopped with exit status 7"


def test_an_unreachable_watcher_never_fails_the_job_or_shows_the_token(tmp_path):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        closed = s.getsockname()[1]
    script = setup(tmp_path, f"http://127.0.0.1:{closed}")
    for command, status in (("echo fine", 0), ("echo bad; exit 4", 4)):
        done = run(script, "backup", command)
        assert done.returncode == status
        assert "could not tell the watcher" in done.stderr
        assert TOKEN not in done.stdout + done.stderr


def test_no_settings_means_no_report(tmp_path, watcher):
    script = setup(tmp_path, "", token="")
    done = run(script, "backup", "echo fine")
    assert (done.returncode, done.stdout, done.stderr) == (0, "fine\n", "")
    assert watcher.reports == []


def test_a_wrong_call_is_refused(tmp_path, watcher):
    script = setup(tmp_path, watcher.url)
    done = run(script, "nightly", "echo never")
    assert done.returncode == 2
    assert "never" not in done.stdout
    assert watcher.reports == []
