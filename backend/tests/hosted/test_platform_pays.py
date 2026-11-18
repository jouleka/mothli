"""The platform pays (ADR 0041 part 3), the parts that need no database.

The one construction of the model clients both modes share (local mode must build exactly what it
built before); what hosted mode refuses, at settings and at startup, without ever printing a key;
and the reader-key routes, which answer as unknown paths unless HOSTED_READER_KEYS is on.
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
from app.hosted.tenant.api import READER_KEY_ROUTES
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


def hosted_settings(tmp_path, **values) -> Settings:
    return Settings(
        _env_file=None,
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
        hosted_credential_master_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        hosted_credential_key_version="test-v1",
        reader_page_context=True,
        reader_read_map=True,
        **values,
    )


PAID = {"openai_api_key": KEY_CANARY, "jev_enabled": True, "typesafe_api_key": JEV_CANARY}


def test_the_cap_settings_have_the_owners_provisional_defaults(tmp_path) -> None:
    """The break it catches: a cap default drifting from the amounts the owner was told (to be
    confirmed by the owner: $0.10 per reader per day, $2.00 per month, $5.00 platform per day), or
    reader keys switched on by default."""
    settings = hosted_settings(tmp_path)
    assert (settings.hosted_reader_usd_per_day, settings.hosted_reader_usd_per_month,
            settings.hosted_platform_usd_per_day) == (0.10, 2.00, 5.00)
    assert settings.hosted_reader_keys is False


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


def _concrete(path: str) -> str:
    return (path.replace("{book_id}", str(uuid.uuid4()))
            .replace("{credential_id}", str(uuid.uuid4()))
            .replace("{capability}", "synthesis"))


def test_reader_key_routes_answer_as_unknown_paths_unless_reader_keys_are_on(tmp_path) -> None:
    """The break it catches: a "paste your own key" or reader-key reply route that still answers
    (401, 403, 405, 413 or 422) while the platform pays, telling anyone it is there, or one that
    disappears while HOSTED_READER_KEYS is on. Unknown paths answer 404 {"detail": "Not Found"}."""
    methods = ("GET", "POST", "PUT", "DELETE", "PATCH")
    with TestClient(hosted_app(hosted_settings(tmp_path)), base_url="https://reader.example") as client:
        unknown = client.get(f"/api/no-such-route-{uuid.uuid4().hex}")
        assert (unknown.status_code, unknown.json()) == (404, {"detail": "Not Found"})
        for _method, path in sorted(READER_KEY_ROUTES):
            for method in methods:
                answer = client.request(method, _concrete(path), content=b"x" * 40_000,
                                        headers={"content-type": "application/json"})
                assert (answer.status_code, answer.json()) == (404, {"detail": "Not Found"}), (
                    method, path)
        assert client.get("/api/limits").status_code == 401         # the rest is still there

    with TestClient(hosted_app(hosted_settings(tmp_path, hosted_reader_keys=True)),
                    base_url="https://reader.example") as client:
        for method, path in sorted(READER_KEY_ROUTES):
            answer = client.request(method, _concrete(path), json={})
            assert answer.status_code == 401, (method, path, answer.text)
