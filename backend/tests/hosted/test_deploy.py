"""The deployment's files agree with the code they deploy (ADR 0041 part 9). No database needed."""

from __future__ import annotations

import ast
import importlib.util
import re
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.hosted.tenant.grants import (
    TENANT_FUNCTION_PRIVILEGES,
    TENANT_TABLE_PRIVILEGES,
    WORKER_COLUMN_PRIVILEGES,
    WORKER_FUNCTION_PRIVILEGES,
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
        table = re.fullmatch(r'GRANT ([A-Z, ]+) ON TABLE "public"\."(\w+)" TO "\w+";', line)
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
    for key in ("auth", "worker", "audit", "operator", "billing"):
        tables, functions = granted(roles.render(roles.grant_statements("mothli", by_key[key])))
        assert tables == by_key[key].tables
        assert functions == by_key[key].functions
    # Only the operator's gift command may give a gift (ADR 0045 step 2); no role but the tenant,
    # the operator and the billing role executes any function.
    assert by_key["operator"].functions == {
        "set_gift(uuid,date)": {"EXECUTE"},
        "reader_plan_state(uuid,integer,numeric)": {"EXECUTE"}}
    assert by_key["operator"].tables["reader_plans"] == {"SELECT"}
    assert [role.key for role in roles.RUNTIME_ROLES if role.functions] == [
        "tenant", "worker", "operator", "billing"]
    # ADR 0045 step 6: the worker reads where a reader stands, through the one function, and
    # when they signed up, through two columns of users and no other: never a name or an email.
    assert by_key["worker"].functions is WORKER_FUNCTION_PRIVILEGES == {
        "reader_plan_state(uuid,integer,numeric)": {"EXECUTE"}}
    assert by_key["worker"].tables["reader_plans"] == {"SELECT"}
    assert "users" not in by_key["worker"].tables
    assert by_key["worker"].columns is WORKER_COLUMN_PRIVILEGES == {"users": {"id", "created_at"}}
    worker_text = roles.render(roles.grant_statements("mothli", by_key["worker"]))
    assert 'GRANT SELECT ("created_at", "id") ON TABLE "public"."users" TO "mothli_worker";' \
        in worker_text.splitlines()
    assert [role.key for role in roles.RUNTIME_ROLES if role.columns] == ["worker"]
    assert not any("set_gift" in function for function in TENANT_FUNCTION_PRIVILEGES)
    # ADR 0045 step 3: Paddle's messages write a plan through the billing role alone: section 1's
    # rights on its two tables and the one look-up, row-level security not bypassed. No other
    # role may write a plan's Paddle part or record a message. Step 4: the checkouts the
    # subscribe route makes (no DELETE), and the list of stored subscriptions the regular check
    # fetches. No other role touches either.
    assert by_key["billing"].tables == {"reader_plans": {"SELECT", "INSERT", "UPDATE"},
                                        "paddle_events": {"SELECT", "INSERT"},
                                        "paddle_checkouts": {"SELECT", "INSERT", "UPDATE"}}
    assert by_key["billing"].functions == {"paddle_reader(text,uuid)": {"EXECUTE"},
                                           "paddle_plans_to_check(integer)": {"EXECUTE"}}
    assert by_key["billing"].password_env == "MOTHLI_DB_BILLING_PASSWORD"
    for role in roles.RUNTIME_ROLES:
        if role.key != "billing":
            assert not ({"INSERT", "UPDATE"} & role.tables.get("reader_plans", frozenset()))
            assert "paddle_events" not in role.tables
            assert "paddle_checkouts" not in role.tables
            assert not any("paddle_" in function for function in role.functions)
    assert [role.bypass_rls for role in roles.RUNTIME_ROLES] == [
        True, False, True, True, True, True, False]
    assert [role.key for role in roles.RUNTIME_ROLES if role.owner_tables] == ["purge"]
    # The worker reads the money records for its cap check and writes none of them: every living
    # job reserves, notes and settles its spend as the tenant role (companion/spend.py).
    assert WORKER_TABLE_PRIVILEGES["cost_ledger"] == {"SELECT"}
    assert WORKER_TABLE_PRIVILEGES["cost_reservations"] == {"SELECT"}
    assert TENANT_TABLE_PRIVILEGES["cost_ledger"] >= {"SELECT", "INSERT"}
    assert TENANT_TABLE_PRIVILEGES["cost_reservations"] >= {"SELECT", "INSERT", "UPDATE"}


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
        # ADR 0044 step 8: UPDATE on books, to mark an edition nobody holds and to leave an
        # erased edition's row as a bare id.
        "books": frozenset({"SELECT", "DELETE", "UPDATE"}),
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
               "HOSTED_JOB_HANDLER": "app.hosted.companion.jobs:CompanionJobs",
               "HOSTED_STORY_MEMORY": "true"}
    for name in (".env.example", "app.env.example"):
        for line in (DEPLOY / name).read_text(encoding="utf-8").splitlines():
            if re.match(r"^[A-Z_]+=", line):
                setting, value = line.split("=", 1)
                assert (value.startswith(("GENERATE_WITH_", "OWNER_MUST_FILL_IN", "https://OWNER"))
                        or value in {"1.00", "4.00", "15.00", "local-placeholder"}
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


def test_nothing_in_the_deployment_names_the_removed_library_command_or_its_role() -> None:
    """ADR 0044 step 6: every book's notes are built as readers reach its chapters, so the
    operator's command that built free-library books ahead of readers, its database role and its
    runbook step are gone. The break it catches: a template, compose service, script or runbook
    still asking the owner for the role's password or to run the command (the deployment would
    stop on a password nobody needs, or a step would name a module that is not there); the role
    script still making the role; or the command's module or its privilege list left behind."""
    from app.hosted.tenant import grants

    removed = ("library_memory", "MOTHLI_DB_LIBRARY_PASSWORD", "HOSTED_LIBRARY_DSN",
               "_library:", "LIBRARY_TABLE_PRIVILEGES", "PostgresLibraryAccount")
    files = [*DEPLOY.glob("*.example"), *DEPLOY.glob(".*.example"), *DEPLOY.glob("*.yaml"),
             *DEPLOY.glob("*.sh"), DEPLOY / "Dockerfile", BACKEND / "scripts" / "hosted_roles.py",
             REPO / "docs" / "runbooks" / "hosted-deployment.md"]
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert not [name for name in removed if name in text], path.name
    compose = (DEPLOY / "compose.yaml").read_text(encoding="utf-8")
    assert "\n  library:\n" not in compose
    roles = load_roles()
    assert "library" not in [role.key for role in roles.ALL_ROLES]
    assert not hasattr(grants, "LIBRARY_TABLE_PRIVILEGES")
    assert not (BACKEND / "app" / "hosted" / "library_memory.py").exists()


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
    out, as before this test), or one that refuses to start; or one whose story panels promise
    to keep track of who's who and never do (story memory off, as before ADR 0044 step 6). The
    chat's switches and story memory are in the template, set; the worker's dispatcher is named,
    with the kinds the chat and story memory need."""
    from app.hosted.companion.jobs import companion_kinds

    template = template_values()
    assert template["OPENAI_API_KEY"] == "" and template["TYPESAFE_API_KEY"] == ""
    for switch in ("READER_PAGE_CONTEXT", "READER_READ_MAP", "JEV_ENABLED",
                   "READER_CONVERSATION_JEV", "HOSTED_STORY_MEMORY"):
        assert template[switch] == "true", switch
    # Streaming follows READER_CONVERSATION_JEV; hosted mode refuses it turned off.
    assert "READER_CONVERSATION_STREAM" not in template
    assert template["HOSTED_JOB_HANDLER"] == "app.hosted.companion.jobs:CompanionJobs"

    settings = template_settings(monkeypatch, tmp_path, OPENAI_API_KEY="sk-stand-in",
                                 TYPESAFE_API_KEY="ts-stand-in")
    assert settings.companion_chat()
    assert settings.jev_enabled and settings.reader_conversation_stream
    assert settings.reader_story_notes and settings.reader_recollections
    assert settings.hosted_story_memory
    assert companion_kinds(settings) == {"chapter_notes", "recollect", "story_memory"}
    assert (settings.hosted_reader_usd_per_day, settings.hosted_reader_usd_per_month,
            settings.hosted_platform_usd_per_day) == (1.00, 4.00, 15.00)
    # Mothli pays for notes from the notes ceiling, under the platform's day (ADR 0044).
    assert (settings.hosted_story_memory_usd_per_day,
            settings.hosted_story_memory_new_chapters_per_day,
            settings.hosted_story_memory_new_chapters_per_month,
            settings.hosted_story_memory_new_files_per_month) == (10.00, 30, 300, 5)
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
    # Story memory follows the chat: its line in the template is on, and with no AI accounts it
    # is off, rather than stopping the server.
    assert not (settings.jev_enabled or settings.reader_story_notes
                or settings.reader_recollections or settings.hosted_story_memory)
    assert settings.reader_read_map and settings.reader_page_context
    with TestClient(hosted_app(settings), base_url="https://reader.example") as client:
        assert client.app.state.platform_client is None


def test_the_billing_role_reaches_the_web_process_and_paddle_stays_off_in_the_template(
        monkeypatch, tmp_path) -> None:
    """The break it catches (ADR 0045 step 3): the billing role's password missing from the
    deployment (the migration step would stop), its address not given to the web process, given
    to the worker or a tool, or the template switching Paddle's messages on with placeholders.
    Paddle's lines are there, commented out, holding placeholders only."""
    compose = (DEPLOY / "compose.yaml").read_text(encoding="utf-8")
    services = dict(re.findall(r"\n  ([a-z]+):\n(.*?)(?=\n  [a-z]+:\n|\nnetworks:)", compose,
                               flags=re.S))
    assert "MOTHLI_DB_BILLING_PASSWORD:" in services["migrate"]
    assert "HOSTED_BILLING_DSN: postgresql://${MOTHLI_DB_ROLE_PREFIX:-mothli}_billing:" in (
        services["web"])
    for name in ("worker", "tools", "purge"):
        assert "BILLING" not in services[name], name
    assert "MOTHLI_DB_BILLING_PASSWORD=GENERATE_WITH_openssl_rand_hex_32" in (
        DEPLOY / ".env.example").read_text(encoding="utf-8")
    template = (DEPLOY / "app.env.example").read_text(encoding="utf-8")
    for setting in ("PADDLE_ENVIRONMENT", "PADDLE_WEBHOOK_SECRET", "PADDLE_PRICE_MONTHLY",
                    "PADDLE_PRICE_YEARLY", "PADDLE_WEBHOOK_TOLERANCE_SECONDS", "PADDLE_API_KEY",
                    "PADDLE_CLIENT_TOKEN", "PADDLE_CHECKOUT_URL"):
        assert f"\n# {setting}=" in template, setting
        assert f"\n{setting}=" not in template, setting
    assert "\n# PADDLE_API_KEY=OWNER_MUST_FILL_IN\n" in template
    assert "\n# PADDLE_CLIENT_TOKEN=OWNER_MUST_FILL_IN\n" in template
    assert "\n# PADDLE_CHECKOUT_URL=https://OWNER_MUST_FILL_IN/\n" in template
    assert template_settings(monkeypatch, tmp_path).paddle_checkout_url == ""
    assert template_settings(monkeypatch, tmp_path).paddle_client_token == ""
    assert template_settings(monkeypatch, tmp_path).paddle_webhook_secret is None
    assert template_settings(monkeypatch, tmp_path).paddle_api_key is None


def compose_services() -> dict[str, str]:
    """Each service of deploy/compose.yaml, its block as written."""
    compose = (DEPLOY / "compose.yaml").read_text(encoding="utf-8")
    return dict(re.findall(r"\n  ([a-z]+):\n(.*?)(?=\n  [a-z]+:\n|\nnetworks:)", compose,
                           flags=re.S))


def compose_environment(block: str) -> dict[str, str]:
    """A service's ``environment:`` lines as Compose hands them over, ``${...}`` filled in from
    a stand-in deploy/.env (the domain ``reader.example``; a default where the file gives one)."""
    if "\n    environment:\n" not in block:
        return {}
    lines = block.split("\n    environment:\n", 1)[1].split("\n")
    section = []
    for line in lines:
        if line.strip() and not line.startswith("      "):
            break
        section.append(line)

    def fill(match: re.Match) -> str:
        name, kind, default = match.group(1), match.group(2), match.group(3)
        if name == "MOTHLI_DOMAIN":
            return "reader.example"
        return default if kind == "-" else "stand-in"

    environment = {}
    for name, value in re.findall(r"^      ([A-Z0-9_]+): (.*)$", "\n".join(section), flags=re.M):
        value = value.strip()
        if value[:1] == value[-1:] == '"' and len(value) >= 2:
            value = value[1:-1]
        environment[name] = re.sub(r"\$\{([A-Z0-9_]+)(?::([-?])([^}]*))?\}", fill, value)
    return environment


# deploy/app.env as the owner has it with Paddle on: the template, the sign-in and storage lines
# filled in, the operator's three lines, and Paddle's sandbox values (made-up stand-ins).
PADDLE_ON = {
    "OIDC_ISSUER": "https://idp.example/", "OIDC_CLIENT_ID": "mothli",
    "OIDC_CLIENT_SECRET": "stand-in-secret",
    "OPERATOR_LEGAL_NAME": "Stand-in Operator", "OPERATOR_COUNTRY": "Testland",
    "OPERATOR_CONTACT_EMAIL": "contact@reader.example",
    "PADDLE_ENVIRONMENT": "sandbox",
    "PADDLE_WEBHOOK_SECRET": "pdl_ntfset_" + "teststandin" * 3,
    "PADDLE_PRICE_MONTHLY": "pri_" + "a" * 26, "PADDLE_PRICE_YEARLY": "pri_" + "b" * 26,
    "PADDLE_API_KEY": "_".join(("pdl", "sdbx", "apikey", "teststandin" * 4)),
    "PADDLE_CLIENT_TOKEN": "test_" + "teststandin" * 2,
    "PADDLE_CHECKOUT_URL": "https://reader.example/",
}


def service_settings(monkeypatch, app_env: dict[str, str], environment: dict[str, str]):
    """``Settings`` as one container builds them: app.env, then compose's ``environment:`` over it
    (Compose gives ``environment`` precedence over ``env_file``)."""
    from app.config import Settings

    for name in [*Settings.model_fields]:
        monkeypatch.delenv(name.upper(), raising=False)
    for name, value in {**app_env, **environment}.items():
        monkeypatch.setenv(name, value)
    return Settings(_env_file=None)


def paddle_app_env(**filled) -> dict[str, str]:
    import base64

    return {**template_values(), **PADDLE_ON,
            "HOSTED_STORAGE_FILESYSTEM_KEY": base64.b64encode(b"k" * 32).decode("ascii"),
            **filled}


def test_every_process_that_reads_app_env_starts_with_paddle_on(monkeypatch) -> None:
    """The break it catches (the 4:00 purge on 25 November 2026): Paddle switched on in app.env,
    and every container that reads app.env but is not the web process stopping at startup on
    "Paddle's messages require HOSTED_BILLING_DSN", an address only the web process may hold.
    Each such container is built here from compose.yaml's own lines, with both AI keys empty and
    filled in. The worker and the purge get none of Paddle's secrets, nor the billing role, and
    read every other number (free weeks, caps, warm-up) exactly as the web process does."""
    from app.hosted.companion.jobs import companion_kinds

    services = compose_services()
    readers = sorted(name for name, block in services.items() if "env_file:" in block)
    # A new container that reads app.env must be added here, and so to this check.
    assert readers == ["purge", "web", "worker"]
    secrets = ("PADDLE_WEBHOOK_SECRET", "PADDLE_API_KEY", "PADDLE_CLIENT_TOKEN",
               "PADDLE_CHECKOUT_URL")
    for keys in ({}, {"OPENAI_API_KEY": "sk-stand-in", "TYPESAFE_API_KEY": "ts-stand-in"}):
        app_env = paddle_app_env(**keys)
        web_env = compose_environment(services["web"])
        web = service_settings(monkeypatch, app_env, web_env)
        assert web.paddle_webhook_secret is not None and web.paddle_api_key is not None
        assert web.hosted_billing_dsn is not None and web.paddle_client_token
        assert web.paddle_checkout_url == "https://reader.example/"
        for name in ("worker", "purge"):
            environment = compose_environment(services[name])
            assert "HOSTED_BILLING_DSN" not in environment, name
            assert {secret: environment.get(secret) for secret in secrets} == dict.fromkeys(
                secrets, ""), name
            settings = service_settings(monkeypatch, app_env, environment)
            assert settings.paddle_webhook_secret is None and settings.paddle_api_key is None
            assert settings.paddle_client_token == "" and settings.paddle_checkout_url == ""
            assert settings.hosted_billing_dsn is None
            # The same prices, plans, caps and switches as the web process.
            assert settings.paddle_prices() == web.paddle_prices()
            for field in type(settings).model_fields:
                upper = field.upper()
                if upper in web_env or upper in environment:
                    continue
                assert getattr(settings, field) == getattr(web, field), (name, field)
            if name == "worker" and keys:
                assert companion_kinds(settings) == {"chapter_notes", "recollect",
                                                     "story_memory"}


def test_paddle_half_set_up_still_stops_the_web_process_and_would_stop_the_jobs(
        monkeypatch) -> None:
    """The web process keeps refusing a half-done Paddle, exactly as before; and without the
    lines compose.yaml empties, the worker and the purge would stop as the purge did on the
    server, so the check above is what keeps them starting."""
    services = compose_services()
    web_env = compose_environment(services["web"])
    with pytest.raises(ValidationError, match="PADDLE_API_KEY requires PADDLE_WEBHOOK_SECRET"):
        service_settings(monkeypatch, paddle_app_env(PADDLE_WEBHOOK_SECRET=""), web_env)
    with pytest.raises(ValidationError, match="Paddle's messages require PADDLE_PRICE_MONTHLY"):
        service_settings(monkeypatch, paddle_app_env(PADDLE_PRICE_MONTHLY="",
                                                     PADDLE_PRICE_YEARLY=""), web_env)
    for name in ("worker", "purge"):
        kept = {key: value for key, value in compose_environment(services[name]).items()
                if not key.startswith("PADDLE_")}
        with pytest.raises(ValidationError, match="Paddle's messages require HOSTED_BILLING_DSN"):
            service_settings(monkeypatch, paddle_app_env(), kept)


def test_the_real_settings_files_are_never_committed() -> None:
    ignored = (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".env" in ignored and "deploy/app.env" in ignored
    docker_ignored = (REPO / ".dockerignore").read_text(encoding="utf-8").splitlines()
    # The image build sees nothing it is not allowed to see, and never an environment file.
    assert "**" in docker_ignored
    assert "**/.env" in docker_ignored and "**/.env.*" in docker_ignored


def test_the_web_process_has_no_path_that_gives_a_gift() -> None:
    """The break it catches (ADR 0045 step 2): a route, the worker or any module the web process
    loads that could give a gift. Only the operator's command names ``set_gift`` or loads it, and
    the tenant role is not granted it (its refusal is tested on PostgreSQL in test_plans)."""
    app = BACKEND / "app"
    sources = {path.relative_to(app).as_posix(): path.read_text(encoding="utf-8")
               for path in app.rglob("*.py")}
    assert sorted(name for name, text in sources.items() if "set_gift" in text) == [
        "hosted/plans.py"]
    assert sorted(name for name, text in sources.items()
                  if re.search(r"hosted\.plans|hosted import plans", text)) == ["hosted/plans.py"]
    assert not any("set_gift" in function for function in TENANT_FUNCTION_PRIVILEGES)
