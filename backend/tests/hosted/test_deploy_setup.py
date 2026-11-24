"""deploy/setup.sh, the one command that prepares the server's settings (runbook step 2).

Each test runs the script with bash in a temporary copy of the three files it reads, never the
checkout's own deploy/ folder (which may hold real settings) and never a real crontab.
"""

from __future__ import annotations

import base64
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from tests.hosted.test_deploy import DEPLOY, load_roles

PRICE_MONTHLY = "pri_" + "a" * 26
PRICE_YEARLY = "pri_" + "b" * 26
ARGS = ["--no-prompt", "--domain", "reader.example.org",
        "--oidc-issuer", "https://issuer.example.org/", "--oidc-client-id", "mothli-test",
        "--operator-name", "Test Operator", "--operator-country", "Testland",
        "--operator-email", "contact@example.org", "--paddle-environment", "sandbox",
        "--paddle-price-monthly", PRICE_MONTHLY, "--paddle-price-yearly", PRICE_YEARLY,
        "--r2-endpoint", "https://r2.example.org"]
PUBLIC_PREFIX = re.compile(r"^(pdl_[a-z]+_(apikey_)?|test_|live_|AGE-SECRET-KEY-1|eyJ)")
# Shaped like a Cloudflare tunnel token (base64 of JSON, so it starts eyJ); not a real one.
TUNNEL = "eyJhIjoi" + "Q7xT2mPz9" * 14 + "fQ=="
# What the owner pasted twice on 2026-11-24 instead of the secret.
THE_COMMAND = 'ssh -t server "cd ~/mothli && deploy/setup.sh set OIDC_CLIENT_SECRET"'
SECRET_NAMES = ("PASSWORD", "HOSTED_STORAGE_FILESYSTEM_KEY", "TOKEN", "SECRET", "API_KEY",
                "ACCESS_KEY_ID", "ENCRYPTION_KEY")


def _bash() -> str | None:
    """A real bash: on Windows Git's own, never the WSL launcher that ``bash`` resolves to."""
    if sys.platform == "win32":
        git = shutil.which("git")
        if not git:
            return None
        for folder in Path(git).resolve().parents:
            if (folder / "bin" / "bash.exe").is_file() and (folder / "usr" / "bin").is_dir():
                return str(folder / "bin" / "bash.exe")
        return None
    return shutil.which("bash")


BASH = _bash()
pytestmark = pytest.mark.skipif(BASH is None, reason="bash is not available")


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A checkout holding only what the script reads: never the real deploy/.env or app.env."""
    deploy = tmp_path / "mothli" / "deploy"
    deploy.mkdir(parents=True)
    for name in ("setup.sh", ".env.example", "app.env.example"):
        shutil.copyfile(DEPLOY / name, deploy / name)
    return tmp_path / "mothli"


def run(checkout: Path, *args: str, stdin: str | None = None,
        path_first: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    if path_first is not None:
        env["PATH"] = str(path_first) + os.pathsep + env["PATH"]
    return subprocess.run([BASH, "deploy/setup.sh", *args], cwd=checkout, env=env, text=True,
                          input=stdin if stdin is not None else "", capture_output=True,
                          timeout=60, check=False)


def values(path: Path) -> dict[str, str]:
    return dict(line.split("=", 1) for line in path.read_text(encoding="utf-8").splitlines()
                if re.match(r"^[A-Z][A-Z0-9_]*=", line))


def secrets(checkout: Path) -> list[str]:
    found = []
    for name in (".env", "app.env"):
        for key, value in values(checkout / "deploy" / name).items():
            if any(part in key for part in SECRET_NAMES) and len(value) >= 8 \
                    and "OWNER_MUST_FILL_IN" not in value and "GENERATE_WITH" not in value:
                found.append(value)
    return found


def assert_no_secret_in(text: str, checkout: Path, *extra: str) -> None:
    """Not a value, nor any eight characters of one (past the public prefix of its kind, which
    the script may name when a value is not of the kind expected)."""
    for value in [*secrets(checkout), *extra]:
        value = PUBLIC_PREFIX.sub("", value)
        for start in range(len(value) - 7):
            assert value[start:start + 8] not in text


def test_the_files_are_made_private_and_a_second_run_changes_nothing(checkout: Path) -> None:
    first = run(checkout, *ARGS)
    assert first.returncode == 0, first.stderr
    env_file, app_file = checkout / "deploy" / ".env", checkout / "deploy" / "app.env"
    if sys.platform.startswith("linux"):
        for path in (env_file, app_file):
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
    before = (env_file.read_bytes(), app_file.read_bytes())
    again = run(checkout, *ARGS)
    assert again.returncode == 0, again.stderr
    assert "nothing to change" in again.stdout
    assert (env_file.read_bytes(), app_file.read_bytes()) == before
    assert_no_secret_in(first.stdout + first.stderr + again.stdout + again.stderr, checkout)
    # No temporary file is left beside them.
    assert sorted(p.name for p in (checkout / "deploy").iterdir()) == [
        ".env", ".env.example", "app.env", "app.env.example", "setup.sh"]


def test_generated_values_pass_the_deployments_own_rules(checkout: Path) -> None:
    assert run(checkout, *ARGS).returncode == 0
    env = values(checkout / "deploy" / ".env")
    app = values(checkout / "deploy" / "app.env")
    rule = load_roles()._PASSWORD_RE
    passwords = {name: value for name, value in env.items() if name.endswith("_PASSWORD")}
    assert set(passwords) == {"POSTGRES_PASSWORD", *(role.password_env
                                                    for role in load_roles().ALL_ROLES)}
    for name, value in passwords.items():
        assert rule.fullmatch(value), name
    assert len(set(passwords.values())) == len(passwords)
    assert len(base64.b64decode(app["HOSTED_STORAGE_FILESYSTEM_KEY"], validate=True)) == 32
    # The values that are not secret, as given; Paddle's checkout page waits for its key.
    assert env["MOTHLI_DOMAIN"] == "reader.example.org"
    assert env["MOTHLI_BACKUP_S3_ENDPOINT"] == "https://r2.example.org"
    assert app["OIDC_ISSUER"] == "https://issuer.example.org/"
    assert app["OPERATOR_CONTACT_EMAIL"] == "contact@example.org"
    assert app["PADDLE_PRICE_MONTHLY"] == PRICE_MONTHLY
    assert app["PADDLE_API_KEY"] == "" and app["PADDLE_WEBHOOK_SECRET"] == ""
    assert "PADDLE_CHECKOUT_URL" not in app
    assert "\n# PADDLE_CHECKOUT_URL=https://reader.example.org/\n" in (
        checkout / "deploy" / "app.env").read_text(encoding="utf-8")


def test_a_value_the_owner_already_set_is_kept(checkout: Path) -> None:
    env_file = checkout / "deploy" / ".env"
    text = (DEPLOY / ".env.example").read_text(encoding="utf-8")
    owner_password = "owner-chose-this-" + "x" * 20
    text = text.replace("MOTHLI_DOMAIN=OWNER_MUST_FILL_IN.example", "MOTHLI_DOMAIN=mine.example.org")
    text = text.replace("POSTGRES_PASSWORD=GENERATE_WITH_openssl_rand_hex_32",
                        f"POSTGRES_PASSWORD={owner_password}")
    # An older file, made before the billing role existed: its line is added.
    text = "\n".join(line for line in text.splitlines()
                     if "MOTHLI_DB_BILLING_PASSWORD" not in line) + "\n"
    env_file.write_text(text, encoding="utf-8", newline="\n")
    result = run(checkout, *ARGS)
    assert result.returncode == 0, result.stderr
    env = values(env_file)
    assert env["MOTHLI_DOMAIN"] == "mine.example.org"
    assert env["POSTGRES_PASSWORD"] == owner_password
    assert load_roles()._PASSWORD_RE.fullmatch(env["MOTHLI_DB_BILLING_PASSWORD"])
    assert "MOTHLI_DOMAIN" in result.stdout and "POSTGRES_PASSWORD" in result.stdout
    assert_no_secret_in(result.stdout + result.stderr, checkout)


def test_check_names_what_is_missing_and_prints_no_value(checkout: Path) -> None:
    assert run(checkout, *ARGS).returncode == 0
    result = run(checkout, "check")
    assert result.returncode == 1
    start = result.stdout.split("Needed later", 1)[0]
    assert "CLOUDFLARE_TUNNEL_TOKEN" in start and "OIDC_CLIENT_SECRET" in start
    for name in ("MOTHLI_BACKUP_ACCESS_KEY_ID", "MOTHLI_BACKUP_ENCRYPTION_KEY", "PADDLE_API_KEY",
                 "TYPESAFE_API_KEY", "OPENAI_API_KEY"):
        assert name in result.stdout.split("Needed later", 1)[1]
    assert "POSTGRES_PASSWORD" not in result.stdout
    assert_no_secret_in(result.stdout + result.stderr, checkout)

    # The owner pastes the two it named, and Mothli can start (both AI keys empty: no chat).
    assert run(checkout, "set", "CLOUDFLARE_TUNNEL_TOKEN", stdin=f"{TUNNEL}\n").returncode == 0
    assert run(checkout, "set", "OIDC_CLIENT_SECRET", stdin="client-stand-in-value\n").returncode == 0
    ready = run(checkout, "check")
    assert ready.returncode == 0, ready.stdout
    assert "Still needed before Mothli can start" not in ready.stdout
    assert_no_secret_in(ready.stdout + ready.stderr, checkout)

    # One AI key without the other stops the server, so check names the missing one.
    assert run(checkout, "set", "TYPESAFE_API_KEY", stdin="checker-stand-in-value\n").returncode == 0
    half = run(checkout, "check")
    assert half.returncode == 1
    assert "OPENAI_API_KEY" in half.stdout.split("Needed later", 1)[0]
    assert_no_secret_in(half.stdout + half.stderr, checkout)


def test_set_takes_a_piped_value_and_never_shows_it(checkout: Path) -> None:
    assert run(checkout, *ARGS).returncode == 0
    secret = "pdl_ntfset_" + "q7" * 12
    result = run(checkout, "set", "PADDLE_WEBHOOK_SECRET", stdin=f"  {secret}\r\n")
    assert result.returncode == 0, result.stderr
    assert "looks right" in result.stdout
    assert values(checkout / "deploy" / "app.env")["PADDLE_WEBHOOK_SECRET"] == secret
    assert_no_secret_in(result.stdout + result.stderr, checkout, secret)

    # Kept without --replace; replaced with it.
    other = "pdl_ntfset_" + "z9" * 12
    kept = run(checkout, "set", "PADDLE_WEBHOOK_SECRET", stdin=other)
    assert kept.returncode == 0 and "kept" in kept.stdout
    assert values(checkout / "deploy" / "app.env")["PADDLE_WEBHOOK_SECRET"] == secret
    replaced = run(checkout, "set", "PADDLE_WEBHOOK_SECRET", "--replace", stdin=other)
    assert replaced.returncode == 0
    assert values(checkout / "deploy" / "app.env")["PADDLE_WEBHOOK_SECRET"] == other
    assert_no_secret_in(kept.stdout + kept.stderr + replaced.stdout + replaced.stderr,
                        checkout, secret, other)

    # A key of the wrong kind is not written, and is named by its kind only.
    wrong = "pdl_live_apikey_" + "w4" * 10
    refused = run(checkout, "set", "PADDLE_API_KEY", stdin=wrong)
    assert refused.returncode == 1
    assert "PADDLE_API_KEY was not written" in refused.stderr and "sandbox" in refused.stderr
    assert_no_secret_in(refused.stdout + refused.stderr, checkout, wrong)
    assert values(checkout / "deploy" / "app.env")["PADDLE_API_KEY"] == ""
    assert "PADDLE_CHECKOUT_URL" not in values(checkout / "deploy" / "app.env")
    right = "pdl_sdbx_apikey_" + "r5" * 10
    assert run(checkout, "set", "PADDLE_API_KEY", stdin=right).returncode == 0
    # With the key in, Paddle's checkout page is turned on.
    assert values(checkout / "deploy" / "app.env")["PADDLE_CHECKOUT_URL"] == (
        "https://reader.example.org/")


def test_set_refuses_an_unknown_name_and_a_value_on_two_lines(checkout: Path) -> None:
    assert run(checkout, *ARGS).returncode == 0
    before = (checkout / "deploy" / "app.env").read_bytes()
    unknown = run(checkout, "set", "NOT_A_SETTING", stdin="anything-at-all")
    assert unknown.returncode == 1
    assert "NOT_A_SETTING is not a setting" in unknown.stderr
    assert "OIDC_CLIENT_SECRET" in unknown.stderr and "CLOUDFLARE_TUNNEL_TOKEN" in unknown.stderr
    two = run(checkout, "set", "OIDC_CLIENT_SECRET", stdin="first-line-value\nsecond-line-value\n")
    assert two.returncode == 1 and "more than one line" in two.stderr
    assert (checkout / "deploy" / "app.env").read_bytes() == before
    assert_no_secret_in(two.stdout + two.stderr, checkout, "first-line-value",
                        "second-line-value")


def cron_lines(checkout: Path) -> list[str]:
    return [line for line in run(checkout, "cron", "--show").stdout.splitlines() if line]


def test_cron_show_prints_the_three_lines_with_this_checkouts_path(checkout: Path) -> None:
    lines = cron_lines(checkout)
    assert len(lines) == 3
    assert lines[0].startswith("0 3 * * * cd '") and "deploy/backup.sh" in lines[0]
    assert lines[1].startswith("0 4 * * * cd '") and "run --rm purge" in lines[1]
    assert lines[2].startswith("30 * * * * cd '") and "app.hosted.paddle_check" in lines[2]
    for line, job in zip(lines, ("backup", "purge", "paddle-check"), strict=True):
        assert line.endswith(f"# mothli-setup:{job}")
        assert "~/mothli" not in line and checkout.name in line
        # Each job runs through report.sh, and its output still goes to its own log.
        assert f"&& deploy/report.sh {job} -- " in line
        assert f">> deploy/backups/{job}.log 2>&1 #" in line


@pytest.mark.skipif(sys.platform == "win32", reason="the stand-in crontab is a shell script")
def test_cron_installs_its_own_lines_once_and_leaves_the_others(checkout: Path,
                                                                tmp_path: Path) -> None:
    """A stand-in ``crontab`` keeps the table in a file: the real one is never touched."""
    bin_dir, table = tmp_path / "bin", tmp_path / "table"
    bin_dir.mkdir()
    table.write_text("15 2 * * * /usr/local/bin/other-product-job\n", encoding="utf-8")
    fake = bin_dir / "crontab"
    fake.write_text(f"""#!/usr/bin/env bash
if [ "$1" = -l ]; then cat '{table}'; elif [ "$1" = - ]; then cat > '{table}'; else exit 2; fi
""", encoding="utf-8")
    fake.chmod(0o755)
    for _ in range(2):
        result = run(checkout, "cron", path_first=bin_dir)
        assert result.returncode == 0, result.stderr
    lines = table.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "15 2 * * * /usr/local/bin/other-product-job"
    assert lines[1:] == cron_lines(checkout)
    assert (checkout / "deploy" / "backups").is_dir()


def test_the_watchers_two_settings_are_set_checked_and_never_shown(checkout: Path) -> None:
    assert run(checkout, *ARGS).returncode == 0
    later = run(checkout, "check").stdout.split("Needed later", 1)[1]
    assert "MOTHLI_WATCH_URL" in later and "MOTHLI_WATCH_TOKEN" in later
    assert "step 6a" in later

    token = "watch-stand-in-" + "k3" * 16
    piped = run(checkout, "set", "MOTHLI_WATCH_TOKEN", stdin=f"{token}\n")
    assert piped.returncode == 0 and "looks right" in piped.stdout
    url = run(checkout, "set", "MOTHLI_WATCH_URL", stdin="https://mothli-watch.example.workers.dev\n")
    assert url.returncode == 0 and "looks right" in url.stdout
    env = values(checkout / "deploy" / ".env")
    assert env["MOTHLI_WATCH_TOKEN"] == token
    assert env["MOTHLI_WATCH_URL"] == "https://mothli-watch.example.workers.dev"
    after = run(checkout, "check").stdout
    assert "MOTHLI_WATCH_URL" not in after and "MOTHLI_WATCH_TOKEN" not in after
    assert_no_secret_in(piped.stdout + piped.stderr + after, checkout, token)

    # Their rules: a long token of plain characters; an https:// address with no path.
    short = run(checkout, "set", "MOTHLI_WATCH_TOKEN", "--replace", stdin="too-short\n")
    assert short.returncode == 1 and "32 or more" in short.stderr
    assert values(checkout / "deploy" / ".env")["MOTHLI_WATCH_TOKEN"] == token
    for bad in ("http://mothli-watch.example.workers.dev", "https://watch.example.org/report"):
        wrong = run(checkout, "set", "MOTHLI_WATCH_URL", "--replace", stdin=bad)
        assert wrong.returncode == 1 and "does not look right" in wrong.stdout


def env_text(checkout: Path) -> str:
    return (checkout / "deploy" / ".env").read_text(encoding="utf-8")


def test_worker_on_needs_both_ai_keys_and_changes_only_its_line(checkout: Path) -> None:
    assert run(checkout, *ARGS).returncode == 0
    before = env_text(checkout)
    assert "\n# COMPOSE_PROFILES=worker\n" in before

    refused = run(checkout, "worker", "on")
    assert refused.returncode == 1 and "needs both AI keys" in refused.stderr
    assert run(checkout, "set", "TYPESAFE_API_KEY", stdin="checker-stand-in-value\n").returncode == 0
    assert run(checkout, "worker", "on").returncode == 1
    assert env_text(checkout) == before
    assert run(checkout, "set", "OPENAI_API_KEY", stdin="writer-stand-in-value\n").returncode == 0

    on = run(checkout, "worker", "on")
    assert on.returncode == 0, on.stderr
    assert "is on" in on.stdout
    assert env_text(checkout) == before.replace("\n# COMPOSE_PROFILES=worker\n",
                                                "\nCOMPOSE_PROFILES=worker\n")
    assert "already on" in run(checkout, "worker", "on").stdout
    assert "COMPOSE_PROFILES" not in run(checkout, "check").stdout

    off = run(checkout, "worker", "off")
    assert off.returncode == 0 and "is off" in off.stdout
    assert env_text(checkout) == before
    assert "already off" in run(checkout, "worker", "off").stdout
    assert "deploy/setup.sh worker on" in run(checkout, "check").stdout
    assert run(checkout, "worker", "maybe").returncode == 1


# 2026-11-24: twice the owner pasted the command itself into the hidden prompt, `set` wrote it and
# said "looks right", and sign-in then failed at the code exchange with nothing to say why.
def test_set_refuses_the_command_pasted_in_place_of_a_secret(checkout: Path) -> None:
    assert run(checkout, *ARGS).returncode == 0
    before = [(checkout / "deploy" / name).read_bytes() for name in (".env", "app.env")]
    for name in ("OIDC_CLIENT_SECRET", "CLOUDFLARE_TUNNEL_TOKEN", "PADDLE_API_KEY"):
        pasted = THE_COMMAND.replace("OIDC_CLIENT_SECRET", name)
        result = run(checkout, "set", name, stdin=f"{pasted}\n")
        assert result.returncode == 1
        assert f"{name} was not written" in result.stderr
        assert "that looks like the command, not the value: copy the value first, then run " \
            "the command and paste" in result.stderr
        assert "looks right" not in result.stdout
        assert "mothli &&" not in result.stdout + result.stderr
    assert [(checkout / "deploy" / name).read_bytes() for name in (".env", "app.env")] == before


@pytest.mark.parametrize("name", ["OIDC_CLIENT_SECRET", "TYPESAFE_API_KEY",
                                  "MOTHLI_BACKUP_SECRET_ACCESS_KEY", "MOTHLI_BACKUP_ACCESS_KEY_ID"])
def test_a_pasted_secret_must_be_one_word(checkout: Path, name: str) -> None:
    assert run(checkout, *ARGS).returncode == 0
    file = checkout / "deploy" / ("app.env" if name in ("OIDC_CLIENT_SECRET", "TYPESAFE_API_KEY")
                                  else ".env")
    before = file.read_bytes()
    two_words = "first-half-of-it\tsecond-half-of-it"
    result = run(checkout, "set", name, stdin=f"{two_words}\n")
    assert result.returncode == 1
    assert f"{name} was not written: expected one word, with no spaces inside" in result.stderr
    assert file.read_bytes() == before
    assert_no_secret_in(result.stdout + result.stderr, checkout, "first-half-of-it",
                        "second-half-of-it")
    # Settings that are not secrets keep today's behaviour (written, then named if they look odd).
    plain = run(checkout, "set", "OPERATOR_COUNTRY", "--replace", stdin="Two Words\n")
    assert plain.returncode == 0 and "looks right" in plain.stdout


def test_the_tunnel_token_is_kept_from_cloudflares_install_command(checkout: Path) -> None:
    assert run(checkout, *ARGS).returncode == 0
    command = f"sudo cloudflared service install {TUNNEL}"
    result = run(checkout, "set", "CLOUDFLARE_TUNNEL_TOKEN", stdin=f"{command}\n")
    assert result.returncode == 0, result.stderr
    assert "kept the token from the command you pasted" in result.stdout
    assert "looks right" in result.stdout
    assert values(checkout / "deploy" / ".env")["CLOUDFLARE_TUNNEL_TOKEN"] == TUNNEL
    assert_no_secret_in(result.stdout + result.stderr, checkout, TUNNEL)

    # Two token-shaped words, or none: nothing is guessed, and nothing is written.
    for pasted in (f"cloudflared tunnel run --token {TUNNEL} {TUNNEL[:-4]}AAAA",
                   "cloudflared tunnel run --token not-a-token", "eyJshort"):
        refused = run(checkout, "set", "CLOUDFLARE_TUNNEL_TOKEN", "--replace", stdin=pasted)
        assert refused.returncode == 1
        assert "CLOUDFLARE_TUNNEL_TOKEN was not written: expected the tunnel's token" \
            in refused.stderr
        assert values(checkout / "deploy" / ".env")["CLOUDFLARE_TUNNEL_TOKEN"] == TUNNEL
        assert_no_secret_in(refused.stdout + refused.stderr, checkout, TUNNEL)


def test_check_names_a_secret_that_breaks_its_rule(checkout: Path) -> None:
    """A file written before these rules (or by hand): check says so, and starts nothing."""
    assert run(checkout, *ARGS).returncode == 0
    assert run(checkout, "set", "CLOUDFLARE_TUNNEL_TOKEN", stdin=TUNNEL).returncode == 0
    app_file = checkout / "deploy" / "app.env"
    text = app_file.read_text(encoding="utf-8")
    text = text.replace("\nOIDC_CLIENT_SECRET=OWNER_MUST_FILL_IN\n",
                        f"\nOIDC_CLIENT_SECRET={THE_COMMAND}\n", 1)
    text = text.replace("\nTYPESAFE_API_KEY=\n", "\nTYPESAFE_API_KEY=two words\n", 1)
    app_file.write_text(text, encoding="utf-8", newline="\n")
    result = run(checkout, "check")
    assert result.returncode == 1
    start = result.stdout.split("Needed later", 1)[0]
    assert "Still needed before Mothli can start" in start
    oidc = next(line for line in start.splitlines() if "OIDC_CLIENT_SECRET" in line)
    assert "that looks like the command, not the value: copy the value first" in oidc
    typesafe = next(line for line in start.splitlines() if "TYPESAFE_API_KEY" in line)
    assert "one word, with no spaces inside" in typesafe
    assert "mothli &&" not in result.stdout + result.stderr
    assert "two words" not in result.stdout + result.stderr
