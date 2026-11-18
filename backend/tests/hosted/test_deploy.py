"""The deployment's files agree with the code they deploy (ADR 0041 part 9). No database needed."""

from __future__ import annotations

import ast
import importlib.util
import re
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from app.hosted.tenant.grants import (
    TENANT_FUNCTION_PRIVILEGES,
    TENANT_TABLE_PRIVILEGES,
    WORKER_TABLE_PRIVILEGES,
)
from tests.hosted.test_platform_pays import hosted_app

BACKEND = Path(__file__).resolve().parents[2]
REPO = BACKEND.parent
DEPLOY = REPO / "deploy"


def load_roles():
    """backend/scripts/hosted_roles.py, which is a script, not a package module."""
    if "hosted_roles" in sys.modules:
        return sys.modules["hosted_roles"]
    spec = importlib.util.spec_from_file_location(
        "hosted_roles", BACKEND / "scripts" / "hosted_roles.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["hosted_roles"] = module
    spec.loader.exec_module(module)
    return module


def granted(text: str) -> tuple[dict[str, frozenset[str]], dict[str, frozenset[str]]]:
    tables: dict[str, set[str]] = {}
    functions: dict[str, set[str]] = {}
    for line in text.splitlines():
        table = re.fullmatch(r'GRANT (.+) ON TABLE "public"\."(\w+)" TO "\w+";', line)
        function = re.fullmatch(r"GRANT (.+) ON FUNCTION public\.(\S+) TO \"\w+\";", line)
        if table:
            tables.setdefault(table[2], set()).update(table[1].split(", "))
        elif function:
            functions.setdefault(function[2], set()).update(function[1].split(", "))
    return ({k: frozenset(v) for k, v in tables.items()},
            {k: frozenset(v) for k, v in functions.items()})


def check_list(path: Path, class_name: str) -> dict[str, frozenset[str]]:
    """The ``required`` allow-list inside a repository's ``check_runtime_role``, as written."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in ast.walk(node):
                if (isinstance(item, ast.Assign) and isinstance(item.targets[0], ast.Name)
                        and item.targets[0].id == "required"):
                    value = ast.literal_eval(item.value)
                    return {table.removeprefix("public."): frozenset(
                        privileges.split(",") if isinstance(privileges, str) else privileges)
                        for table, privileges in value.items()}
    raise AssertionError(f"no allow-list found in {class_name}")


def test_the_role_script_grants_the_tenant_exactly_what_grants_py_lists() -> None:
    """The break it catches: the role script and the list the app checks at startup drifting
    apart, which would stop the app on the server (or grant the reader role one table more)."""
    roles = load_roles()
    tenant = next(role for role in roles.RUNTIME_ROLES if role.key == "tenant")
    text = roles.render(roles.grant_statements("mothli", tenant))
    tables, functions = granted(text)
    assert tables == TENANT_TABLE_PRIVILEGES
    assert functions == TENANT_FUNCTION_PRIVILEGES
    assert "platform_spend_days" not in tables
    # Everything else is taken away first, in the same run.
    assert text.splitlines()[0] == 'REVOKE ALL ON ALL TABLES IN SCHEMA public FROM "mothli_tenant";'
    assert not tenant.bypass_rls


def test_the_role_script_matches_every_startup_check_as_written() -> None:
    """The break it catches: an allow-list changed in a repository's startup check and not in
    the role script, or the reverse. The tenant and worker lists come from grants.py itself."""
    roles = load_roles()
    by_key = {role.key: role for role in roles.RUNTIME_ROLES}
    assert by_key["auth"].tables == check_list(
        BACKEND / "app" / "hosted" / "auth" / "repository.py", "PostgresAuthRepository")
    assert by_key["worker"].tables is WORKER_TABLE_PRIVILEGES
    assert by_key["tenant"].tables is TENANT_TABLE_PRIVILEGES
    for key in ("auth", "worker", "audit", "operator"):
        tables, functions = granted(roles.render(roles.grant_statements("mothli", by_key[key])))
        assert tables == by_key[key].tables
        assert functions == {}
    assert [role.bypass_rls for role in roles.RUNTIME_ROLES] == [
        True, False, True, True, True, True, False]
    assert [role.key for role in roles.RUNTIME_ROLES if role.owner_tables] == ["purge"]


def test_the_role_script_grants_the_purge_role_the_owner_tables_and_its_few_others() -> None:
    """The break it catches: the purge role granted a table it should not touch, or not granted
    a table that holds a reader's rows (which would leave them behind after an erasure)."""
    from app.hosted.tenant.grants import PURGE_OTHER_TABLE_PRIVILEGES

    roles = load_roles()
    purge = next(role for role in roles.RUNTIME_ROLES if role.key == "purge")
    assert purge.tables is PURGE_OTHER_TABLE_PRIVILEGES
    tables, functions = granted(roles.render(
        roles.grant_statements("mothli", purge, ["books", "audit_events", "sessions"])))
    assert tables == {
        "books": frozenset({"SELECT", "DELETE"}),
        "sessions": frozenset({"SELECT", "DELETE"}),
        "audit_events": frozenset({"SELECT", "DELETE", "INSERT"}),
        "users": frozenset({"SELECT", "UPDATE"}),
    }
    assert functions == {}


def test_the_role_script_refuses_weak_or_unsafe_passwords_by_name() -> None:
    roles = load_roles()
    for value in ("", "short", "x" * 31, "a" * 40 + "@b", "a" * 40 + "/"):
        try:
            roles._password({"MOTHLI_DB_TENANT_PASSWORD": value}, "MOTHLI_DB_TENANT_PASSWORD")
        except roles.ProvisioningError as exc:
            assert "MOTHLI_DB_TENANT_PASSWORD" in str(exc)
            assert value not in str(exc) or value == ""
        else:
            raise AssertionError("a weak password was accepted")
    assert roles._password({"P": "a1" * 32}, "P") == "a1" * 32


def test_the_proxy_sets_the_same_browser_defences_as_the_backend() -> None:
    """The break it catches: the static app served by the proxy without the CSP that keeps
    EPUB scripts from running in the app's origin, or with one that drifted from the backend's."""
    from app.main import _CONTENT_SECURITY_POLICY

    caddyfile = (DEPLOY / "Caddyfile").read_text(encoding="utf-8")
    headers = dict(re.findall(r'^\t\t([A-Z][A-Za-z-]+) "(.*)"$', caddyfile, flags=re.M))
    assert headers == {
        "Content-Security-Policy": _CONTENT_SECURITY_POLICY,
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "X-Frame-Options": "DENY",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    }
    # Streams: no periodic-flush override (it would keep a reply running after the reader left)
    # and no compression of /api.
    api = caddyfile.split("handle /api/* {", 1)[1].split("\n\t}\n", 1)[0]
    assert "flush_interval" not in api.replace("`flush_interval -1` is deliberately", "")
    assert "encode" not in api.replace("No `encode` here", "")


def test_every_setting_hosted_startup_requires_has_a_place_in_the_deployment() -> None:
    """The break it catches: a setting the app refuses to start without that neither the compose
    file nor the environment template provides."""
    tree = ast.parse((BACKEND / "app" / "config.py").read_text(encoding="utf-8"))
    required = next(
        set(ast.literal_eval(key) for key in node.value.keys)
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == "required")
    compose = (DEPLOY / "compose.yaml").read_text(encoding="utf-8")
    web = compose.split("\n  web:\n", 1)[1].split("\n  # The durable job worker", 1)[0]
    from_compose = set(re.findall(r"^      ([A-Z_]+): ", web, flags=re.M))
    template = (DEPLOY / "app.env.example").read_text(encoding="utf-8")
    from_template = set(re.findall(r"^([A-Z_]+)=", template, flags=re.M))
    assert required <= from_compose | from_template
    assert {"TRUSTED_HOSTS", "DEPLOYMENT_MODE"} <= from_compose
    # Templates hold placeholders only, beside the AI keys left empty (no chat until they are
    # filled in) and the chat's switches set to the values it works with.
    working = {"READER_PAGE_CONTEXT": "true", "READER_READ_MAP": "true", "JEV_ENABLED": "true",
               "READER_CONVERSATION_JEV": "true", "OPENAI_API_KEY": "", "TYPESAFE_API_KEY": "",
               "HOSTED_JOB_HANDLER": "app.hosted.companion.jobs:CompanionJobs"}
    for name in (".env.example", "app.env.example"):
        for line in (DEPLOY / name).read_text(encoding="utf-8").splitlines():
            if re.match(r"^[A-Z_]+=", line):
                setting, value = line.split("=", 1)
                assert (value.startswith(("GENERATE_WITH_", "OWNER_MUST_FILL_IN", "https://OWNER"))
                        or value in {"0.10", "2.00", "5.00", "local-placeholder"}
                        or value.endswith("OWNER_MUST_FILL_IN.example")
                        or (name == "app.env.example" and working.get(setting) == value)), line


def test_no_deployment_file_asks_for_the_removed_reader_key_settings() -> None:
    """The break it catches: a template, compose file or runbook still telling the owner to make,
    store, back up or rotate the master key that wrapped readers' own AI keys, or to set any other
    setting of that removed feature (ADR 0043 step 8). The book files' key stays."""
    removed = ("HOSTED_READER_KEYS", "HOSTED_CREDENTIAL_MASTER_KEY",
               "HOSTED_CREDENTIAL_KEY_VERSION", "HOSTED_CREDENTIAL_PREVIOUS_MASTER_KEYS",
               "HOSTED_PROVIDER_VALIDATION_TIMEOUT_SECONDS", "credential-rewrap")
    files = [*DEPLOY.glob("*.example"), *DEPLOY.glob(".*.example"), *DEPLOY.glob("*.yaml"),
             *DEPLOY.glob("*.sh"), DEPLOY / "Dockerfile",
             REPO / "docs" / "runbooks" / "hosted-deployment.md"]
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert not [name for name in removed if name in text], path.name
    assert "HOSTED_STORAGE_FILESYSTEM_KEY=" in (DEPLOY / "app.env.example").read_text(
        encoding="utf-8")


def template_values() -> dict[str, str]:
    """Every ``NAME=value`` line of deploy/app.env.example, as written."""
    text = (DEPLOY / "app.env.example").read_text(encoding="utf-8")
    return dict(line.split("=", 1) for line in text.splitlines() if re.match(r"^[A-Z_]+=", line))


# What compose.yaml sets for the web container (not in the template), with stand-in values.
COMPOSE_STAND_INS = {
    "DEPLOYMENT_MODE": "hosted",
    "TRUSTED_HOSTS": "reader.example",
    "OIDC_REDIRECT_URI": "https://reader.example/api/auth/callback",
    "HOSTED_AUTH_DSN": "postgresql://unused.invalid/mothli",
    "HOSTED_TENANT_DSN": "postgresql://unused.invalid/mothli",
    "HOSTED_STORAGE_BACKEND": "filesystem",
}


def template_settings(monkeypatch, tmp_path, **filled):
    """``Settings`` from the template exactly as an owner would run it: every line as written, the
    owner's placeholders replaced by stand-ins, and ``filled`` written over the empty lines."""
    import base64

    from app.config import Settings

    values = {**template_values(), **COMPOSE_STAND_INS,
              "HOSTED_STORAGE_FILESYSTEM_ROOT": str(tmp_path / "books"),
              "HOSTED_STORAGE_FILESYSTEM_KEY": base64.b64encode(b"k" * 32).decode("ascii"),
              "OIDC_ISSUER": "https://idp.example/", "OIDC_CLIENT_ID": "mothli",
              "OIDC_CLIENT_SECRET": "stand-in-secret", **filled}
    for name in [*Settings.model_fields]:
        monkeypatch.delenv(name.upper(), raising=False)
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return Settings(_env_file=None)


def test_the_template_with_both_ai_keys_filled_in_gives_a_working_checked_chat(
    monkeypatch, tmp_path
) -> None:
    """The break it catches: an owner following the runbook, filling in the two AI keys the
    template asks for, and getting a server whose chat can never answer (its switches commented
    out, as before this test), or one that refuses to start. The chat's switches are in the
    template, set; the worker's dispatcher is named, with the kinds the chat needs."""
    from app.hosted.companion.jobs import companion_kinds

    template = template_values()
    assert template["OPENAI_API_KEY"] == "" and template["TYPESAFE_API_KEY"] == ""
    for switch in ("READER_PAGE_CONTEXT", "READER_READ_MAP", "JEV_ENABLED",
                   "READER_CONVERSATION_JEV"):
        assert template[switch] == "true", switch
    # Streaming follows READER_CONVERSATION_JEV; hosted mode refuses it turned off.
    assert "READER_CONVERSATION_STREAM" not in template
    assert template["HOSTED_JOB_HANDLER"] == "app.hosted.companion.jobs:CompanionJobs"

    settings = template_settings(monkeypatch, tmp_path, OPENAI_API_KEY="sk-stand-in",
                                 TYPESAFE_API_KEY="ts-stand-in")
    assert settings.companion_chat()
    assert settings.jev_enabled and settings.reader_conversation_stream
    assert settings.reader_story_notes and settings.reader_recollections
    assert companion_kinds(settings) == {"chapter_notes", "recollect"}
    assert not settings.hosted_story_memory                  # the owner decides that one
    assert (settings.hosted_reader_usd_per_day, settings.hosted_reader_usd_per_month,
            settings.hosted_platform_usd_per_day) == (0.10, 2.00, 5.00)
    # And it starts, with the platform's writer and Jev built (no call is made).
    with TestClient(hosted_app(settings), base_url="https://reader.example") as client:
        platform = client.app.state.platform_client
        assert platform.provider == "openai-compatible" and platform.jev is not None
        platform.jev.close()


def test_the_template_with_the_ai_keys_left_empty_starts_without_a_chat(monkeypatch,
                                                                        tmp_path) -> None:
    """The break it catches: the runbook's promise that Mothli runs without AI accounts broken by
    the chat's switches being on in the template."""
    settings = template_settings(monkeypatch, tmp_path)
    assert not settings.companion_chat()
    assert not (settings.jev_enabled or settings.reader_story_notes
                or settings.reader_recollections)
    assert settings.reader_read_map and settings.reader_page_context
    with TestClient(hosted_app(settings), base_url="https://reader.example") as client:
        assert client.app.state.platform_client is None


def test_the_real_settings_files_are_never_committed() -> None:
    ignored = (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".env" in ignored and "deploy/app.env" in ignored
    docker_ignored = (REPO / ".dockerignore").read_text(encoding="utf-8").splitlines()
    # The image build sees nothing it is not allowed to see, and never an environment file.
    assert "**" in docker_ignored
    assert "**/.env" in docker_ignored and "**/.env.*" in docker_ignored
