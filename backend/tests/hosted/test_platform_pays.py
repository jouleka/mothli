"""The platform pays (ADR 0041 part 3), the parts that need no database.

The one construction of the model clients both modes share (local mode must build exactly what it
built before); what hosted mode refuses, at settings and at startup, without ever printing a key;
and the removed reader-key routes, which answer as unknown paths whatever the settings.
No model is called: building a client opens no connection.
"""

from __future__ import annotations

import base64
import logging
import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app import deps
from app.config import Settings
from app.llm.client import LLMClient
from app.llm.factory import PlatformClientsUnavailable, build_platform_clients
from app.main import create_app

KEY_CANARY = "sk-platform-key-canary-7Rq2"
JEV_CANARY = "ts-platform-jev-canary-9Lm4"


def _old_construction(settings: Settings):
    """``deps.build_state``'s client construction exactly as it was before part 3 (main at efc332f,
    ``deps.py:21-34``), kept here verbatim as the reference local mode must still match."""
    client = LLMClient(env=settings.llm_env(), allow_stub=settings.allow_stub, max_retries=0)
    if settings.jev_enabled:
        from app.llm.jev import JevClient, JevUnavailable, windows_credential

        if client.provider == "stub":
            raise JevUnavailable("Jev requires a configured generative provider")
        key = (settings.typesafe_api_key.get_secret_value() if settings.typesafe_api_key else
               windows_credential() if settings.jev_windows_credential else None)
        if not key:
            raise JevUnavailable("Configure a server-side Jev credential")
        client.jev = JevClient(api_key=key, model=settings.jev_model,
                               timeout=settings.jev_timeout_seconds)
    return client


def _described(build, settings):
    """Everything observable about the clients one construction builds, or how it refused."""
    try:
        client = build(settings)
    except Exception as exc:  # noqa: BLE001 - the refusal itself is what is compared
        return ("refused", type(exc).__name__, str(exc))
    try:
        jev = getattr(client, "jev", None)
        return {
            "type": type(client).__name__,
            "provider": client.provider,
            "cheap": client._model_for("cheap"),
            "large": client._model_for("large"),
            "billing": (client.billing_model("cheap"), client.billing_model("large")),
            "base_url": client.base_url,
            "native_openai": client._is_native_openai,
            "api_key": client.api_key,
            "anthropic_key": client.anthropic_key,
            "embed": (client.embed_provider, client.embed_base_url, client.embed_model,
                      client.embed_key),
            "retries": client.max_retries,
            "timeout": client.timeout,
            "jev": None if jev is None else (
                type(jev).__name__, jev.model, str(jev._http.base_url), jev._http.timeout,
                jev._http.headers.get("authorization")),
        }
    finally:
        if getattr(client, "jev", None) is not None:
            client.jev.close()


LOCAL_COMBINATIONS = [
    {"allow_stub": True},
    {"openai_api_key": KEY_CANARY},
    {"anthropic_api_key": KEY_CANARY},
    {"openai_api_key": KEY_CANARY, "openai_base_url": "https://llm.example/v1",
     "llm_cheap_model": "small-one", "llm_large_model": "large-one"},
    {"openai_api_key": KEY_CANARY, "embed_provider": "openai-compatible",
     "embed_base_url": "https://embed.example/v1", "embed_api_key": "embed-key",
     "embed_model": "embedder"},
    {"llm_transport": "codex-local"},
    {"openai_api_key": KEY_CANARY, "jev_enabled": True, "typesafe_api_key": JEV_CANARY},
    {"anthropic_api_key": KEY_CANARY, "jev_enabled": True, "typesafe_api_key": JEV_CANARY,
     "jev_model": "jev-1.13.0", "jev_timeout_seconds": 9.5},
    {"allow_stub": True, "jev_enabled": True, "typesafe_api_key": JEV_CANARY},
    {"openai_api_key": KEY_CANARY, "jev_enabled": True},
    {},
]


@pytest.mark.parametrize("values", LOCAL_COMBINATIONS)
def test_local_mode_builds_exactly_the_clients_it_built_before(values, tmp_path) -> None:
    """The break it catches: moving the construction into the shared function changing anything
    local mode builds (provider, models, keys, endpoints, retries, Jev's model, timeout and key)
    or how it refuses (the stub with Jev, Jev with no key, no provider without ALLOW_STUB)."""
    settings = Settings(_env_file=None, data_dir=str(tmp_path / "data"), **values)
    before = _described(_old_construction, settings)
    assert _described(build_platform_clients, settings) == before

    def through_build_state(settings):
        state = deps.build_state(settings)
        state["catalog"].close()
        state["store"].close()
        return state["client"]

    assert _described(through_build_state, settings) == before


def hosted_settings(tmp_path, env_file=None, **values) -> Settings:
    return Settings(
        _env_file=env_file,
        deployment_mode="hosted",
        hosted_auth_dsn="postgresql://unused.invalid/litlet",
        hosted_tenant_dsn="postgresql://unused.invalid/litlet",
        oidc_issuer="https://idp.example",
        oidc_client_id="litlet",
        oidc_client_secret="not-a-real-secret",
        oidc_redirect_uri="https://reader.example/api/auth/callback",
        trusted_hosts="reader.example",
        hosted_storage_backend="filesystem",
        hosted_storage_filesystem_root=str(tmp_path / "objects"),
        hosted_storage_filesystem_key=base64.b64encode(b"t" * 32).decode("ascii"),
        reader_page_context=True,
        reader_read_map=True,
        **values,
    )


PAID = {"openai_api_key": KEY_CANARY, "jev_enabled": True, "typesafe_api_key": JEV_CANARY}


def test_the_cap_settings_have_the_owners_provisional_defaults(tmp_path) -> None:
    """The break it catches: a cap default drifting from the amounts the owner was told (to be
    confirmed by the owner: $0.10 per reader per day, $2.00 per month, $5.00 platform per day)."""
    settings = hosted_settings(tmp_path)
    assert (settings.hosted_reader_usd_per_day, settings.hosted_reader_usd_per_month,
            settings.hosted_platform_usd_per_day) == (0.10, 2.00, 5.00)


@pytest.mark.parametrize("setting, value", [
    ("allow_stub", True), ("jev_windows_credential", True),
])
def test_hosted_settings_refuse_what_the_platform_must_not_run(setting, value, tmp_path) -> None:
    """The break it catches: hosted mode starting with the stub, a Jev key read from a Windows
    credential store (ADR 0041 section 7). The file caches it also refused were removed (ADR 0043)."""
    with pytest.raises(ValueError, match=setting.upper()):
        hosted_settings(tmp_path, **PAID, **{setting: value})
    Settings(_env_file=None, data_dir=str(tmp_path / "local"), **{setting: value})  # local: allowed


def test_hosted_settings_refuse_a_writer_address_outside_the_allowed_origins(tmp_path) -> None:
    """The break it catches: the owner's writer key sent to an address nobody allowed; the message
    names the settings, never the address (it could carry credentials)."""
    secret_address = "https://user:pw-canary@llm.evil.example/v1"
    for address in (secret_address, "http://api.openai.com/v1", "https://llm.evil.example/v1"):
        with pytest.raises(ValueError) as refused:
            hosted_settings(tmp_path, **PAID, openai_base_url=address)
        assert "HOSTED_PROVIDER_ALLOWED_ORIGINS" in str(refused.value)
        assert "pw-canary" not in str(refused.value) and "evil" not in str(refused.value)
    settings = hosted_settings(tmp_path, **PAID, openai_base_url="https://API.openai.com/v1")
    assert settings.openai_base_url == "https://API.openai.com/v1"


@pytest.mark.parametrize("values, named", [
    ({"jev_enabled": True, "typesafe_api_key": JEV_CANARY}, "OPENAI_API_KEY or ANTHROPIC_API_KEY"),
    ({"openai_api_key": KEY_CANARY, "jev_enabled": True}, "TYPESAFE_API_KEY"),
    ({"openai_api_key": KEY_CANARY, "typesafe_api_key": JEV_CANARY}, "JEV_ENABLED"),
    ({**PAID, "allow_stub": True}, "ALLOW_STUB"),
    ({**PAID, "llm_transport": "codex-local"}, "LLM_TRANSPORT=codex-local"),
    ({**PAID, "jev_windows_credential": True}, "JEV_WINDOWS_CREDENTIAL"),
])
def test_hosted_construction_refuses_anything_but_the_owners_paid_accounts(values, named,
                                                                          tmp_path) -> None:
    """The break it catches: hosted mode building the stub, the owner's subscription CLI or a
    Windows credential reader, or starting without the platform's writer or Jev key; and a refusal
    that prints a key. The settings layer already refuses some of these; the construction refuses
    them again on its own, so a settings object built some other way cannot slip past."""
    settings = hosted_settings(tmp_path).model_copy(update=values)
    with pytest.raises(PlatformClientsUnavailable) as refused:
        build_platform_clients(settings)
    assert named in str(refused.value)
    assert KEY_CANARY not in str(refused.value) and JEV_CANARY not in str(refused.value)


def test_hosted_construction_builds_the_platform_writer_and_jev(tmp_path) -> None:
    """The break it catches: hosted mode building different clients from the same settings than
    local mode would (one construction, not two)."""
    hosted = hosted_settings(tmp_path, **PAID)
    local = Settings(_env_file=None, data_dir=str(tmp_path / "local"), **PAID)
    assert _described(build_platform_clients, hosted) == _described(build_platform_clients, local)
    client = build_platform_clients(hosted)
    try:
        assert client.provider == "openai-compatible" and client.max_retries == 0
        assert client.jev.model == "jev-1.14.0"
    finally:
        client.jev.close()


class _Sessions:
    async def check_runtime_role(self) -> None:
        return None

    async def authenticate_session(self, *, session_digest, now, idle_ttl):
        return None


class _Tenant:
    async def check_runtime_role(self) -> None:
        return None


def hosted_app(settings: Settings):
    return create_app(settings, auth_repository=_Sessions(), tenant_repository=_Tenant(),
                      oidc_client=object(), auth_clock=lambda: datetime(2026, 7, 16, tzinfo=UTC))


def test_a_missing_platform_key_stops_hosted_startup_naming_the_setting_not_its_value(
    tmp_path, caplog
) -> None:
    """The break it catches: a hosted server that starts with half the platform's AI settings and
    fails at a reader's first question, or a startup failure that prints a key."""
    caplog.set_level(logging.DEBUG)
    settings = hosted_settings(tmp_path, jev_enabled=True, typesafe_api_key=JEV_CANARY)
    with pytest.raises(PlatformClientsUnavailable, match="OPENAI_API_KEY or ANTHROPIC_API_KEY") as refused:
        with TestClient(hosted_app(settings), base_url="https://reader.example"):
            pass
    assert JEV_CANARY not in str(refused.value) and JEV_CANARY not in caplog.text

    with TestClient(hosted_app(hosted_settings(tmp_path, **PAID)),
                    base_url="https://reader.example") as client:
        platform = client.app.state.platform_client
        assert platform.provider == "openai-compatible" and platform.jev.model == "jev-1.14.0"
    with TestClient(hosted_app(hosted_settings(tmp_path)), base_url="https://reader.example") as client:
        assert client.app.state.platform_client is None     # nothing set to call a model yet
    assert KEY_CANARY not in caplog.text and JEV_CANARY not in caplog.text


# What readers paying with their own AI key used (ADR 0043 step 6): the key and model settings
# screen's routes and the replies, Explain / Define and chapter look-back that ran on that key.
REMOVED_PATHS = (
    "/api/credentials",
    "/api/credentials/{credential_id}",
    "/api/provider-settings",
    "/api/provider-settings/{capability}",
    "/api/provider-settings/{capability}/validate",
    "/api/books/{book_id}/ask",
    "/api/books/{book_id}/selection-action",
    "/api/books/{book_id}/chapter-closeout",
)
CHAT_ON = {**PAID, "reader_conversation_jev": True, "hosted_reader_usd_per_day": 0.10,
           "hosted_platform_usd_per_day": 5.00}


def _concrete(path: str) -> str:
    return (path.replace("{book_id}", str(uuid.uuid4()))
            .replace("{credential_id}", str(uuid.uuid4()))
            .replace("{capability}", "synthesis"))


@pytest.mark.parametrize("values", [
    {}, CHAT_ON, {**CHAT_ON, "hosted_story_memory": True, "hosted_reader_story_memory": True},
], ids=["nothing-paid", "chat", "chat-and-story-memory"])
def test_the_reader_key_paths_do_not_exist_in_hosted_mode_whatever_the_settings(values,
                                                                                tmp_path) -> None:
    """The break it catches: a "paste your own key" route, or a reply, Explain / Define or chapter
    look-back that runs on a reader's own key, answering at all in hosted mode (401, 403, 405, 413
    or 422 would tell anyone it is there). Nothing in hosted mode serves Explain / Define or the
    chapter look-back now, so no hosted spoiler test is needed for them until they are built on
    the platform's models (ADR 0043). Unknown paths answer 404 {"detail": "Not Found"}."""
    methods = ("GET", "POST", "PUT", "DELETE", "PATCH")
    with TestClient(hosted_app(hosted_settings(tmp_path, **values)),
                    base_url="https://reader.example") as client:
        unknown = client.get(f"/api/no-such-route-{uuid.uuid4().hex}")
        assert (unknown.status_code, unknown.json()) == (404, {"detail": "Not Found"})
        for path in REMOVED_PATHS:
            for method in methods:
                answer = client.request(method, _concrete(path), content=b"x" * 40_000,
                                        headers={"content-type": "application/json"})
                assert (answer.status_code, answer.json()) == (404, {"detail": "Not Found"}), (
                    method, path)
        assert client.get("/api/limits").status_code == 401         # the rest is still there


# The settings of readers paying with their own AI key, removed with the key tables (ADR 0043
# step 8). An old line naming one is read and dropped (Settings ignores unknown names).
REMOVED_SETTINGS = {
    "HOSTED_READER_KEYS": "true",
    "HOSTED_CREDENTIAL_MASTER_KEY": base64.b64encode(b"m" * 32).decode("ascii"),
    "HOSTED_CREDENTIAL_KEY_VERSION": "v1",
    "HOSTED_CREDENTIAL_PREVIOUS_MASTER_KEYS": '{"v0": "' + base64.b64encode(b"o" * 32).decode()
                                              + '"}',
    "HOSTED_PROVIDER_VALIDATION_TIMEOUT_SECONDS": "99",
}


@pytest.mark.parametrize("values", [{}, CHAT_ON], ids=["nothing-paid", "chat"])
def test_hosted_mode_starts_without_the_reader_key_settings_and_ignores_old_lines(
    values, tmp_path, monkeypatch
) -> None:
    """The break it catches: hosted mode still asking for the master key that wrapped readers'
    keys (or its version) before it starts, or one of the removed names becoming a setting again,
    so that an old line left in a settings file could switch something on."""
    for name in REMOVED_SETTINGS:
        assert name.lower() not in Settings.model_fields, name
    without = hosted_settings(tmp_path, **values)
    with TestClient(hosted_app(without), base_url="https://reader.example") as client:
        assert client.get("/api/limits").status_code == 401           # up, and asks for sign-in

    old_file = tmp_path / "old.env"
    old_file.write_text("".join(f"{name}={value}\n" for name, value in REMOVED_SETTINGS.items()),
                        encoding="utf-8")
    for name, value in REMOVED_SETTINGS.items():
        monkeypatch.setenv(name, value)
    with_old_lines = hosted_settings(tmp_path, env_file=old_file, **values)
    assert with_old_lines.model_dump() == without.model_dump()
