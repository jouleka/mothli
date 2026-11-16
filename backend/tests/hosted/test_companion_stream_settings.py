"""The settings that let the streamed chat run in hosted mode (ADR 0041 section 7, part 5 rules).

What is now allowed (conversation Jev, and so streamed replies, with the platform's own writer and
Jev keys, page context, the read map and caps the operator has set) and everything that must still
be refused, in hosted mode; local mode's rules are unchanged. No database and no model is needed.
"""

from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.hosted.companion.spend import set_spend_limits, spend_limits
from tests.hosted.test_platform_pays import JEV_CANARY, KEY_CANARY, hosted_app


def hosted_settings(tmp_path, **values) -> Settings:
    """Part 3's hosted settings, where ``values`` may also replace the page context or the map."""
    base = dict(
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
    )
    return Settings(_env_file=None, **{**base, **values})


CHAT = {"reader_conversation_jev": True, "jev_enabled": True, "typesafe_api_key": JEV_CANARY,
        "openai_api_key": KEY_CANARY, "reader_story_notes": False,
        "hosted_reader_usd_per_day": 0.10, "hosted_platform_usd_per_day": 5.00}


def without(*names: str) -> dict:
    return {name: value for name, value in CHAT.items() if name not in names}


def test_the_streamed_chat_is_allowed_in_hosted_mode_on_the_part_5_conditions(tmp_path) -> None:
    """The break it catches: hosted mode still refusing the chat it now serves, or turning on with
    it something parts 6 and 7 have not built (chapter notes, recollections)."""
    settings = hosted_settings(tmp_path, **CHAT)
    assert settings.reader_conversation_jev and settings.reader_conversation_stream
    assert settings.reader_read_map and settings.reader_page_context
    assert settings.reader_story_notes is False and settings.reader_recollections is False
    # The same with the caps given in the environment, as an operator sets them.
    values = without("hosted_reader_usd_per_day", "hosted_platform_usd_per_day")
    with pytest.MonkeyPatch.context() as env:
        env.setenv("HOSTED_READER_USD_PER_DAY", "0.10")
        env.setenv("HOSTED_PLATFORM_USD_PER_DAY", "5")
        assert hosted_settings(tmp_path, **values).reader_conversation_stream


def test_hosted_startup_builds_the_platform_clients_for_the_chat(tmp_path) -> None:
    before = spend_limits()
    try:
        with TestClient(hosted_app(hosted_settings(tmp_path, **CHAT)),
                        base_url="https://reader.example") as client:
            platform = client.app.state.platform_client
            assert platform.provider == "openai-compatible" and platform.jev.model == "jev-1.14.0"
    finally:
        set_spend_limits(reader_usd_per_day=before.reader_usd_per_day,
                         reader_usd_per_month=before.reader_usd_per_month,
                         platform_usd_per_day=before.platform_usd_per_day)


@pytest.mark.parametrize("values, named", [
    ({**CHAT, "reader_read_map": False}, "READER_READ_MAP"),
    (without("typesafe_api_key"), "TYPESAFE_API_KEY"),
    ({**CHAT, "typesafe_api_key": ""}, "TYPESAFE_API_KEY"),
    (without("openai_api_key"), "OPENAI_API_KEY or ANTHROPIC_API_KEY"),
    (without("hosted_reader_usd_per_day"), "HOSTED_READER_USD_PER_DAY"),
    (without("hosted_platform_usd_per_day"), "HOSTED_PLATFORM_USD_PER_DAY"),
])
def test_the_chat_is_refused_without_each_part_5_condition(values, named, tmp_path) -> None:
    """The break it catches: owner-paid AI turned on without the platform's own keys, without the
    read map (hosted keeps no confirmed reading anchor), or with caps nobody set (the defaults are
    provisional amounts, to be confirmed by the owner). The message names settings, never values."""
    with pytest.raises(ValueError, match="the streamed chat in hosted mode requires") as refused:
        hosted_settings(tmp_path, **values)
    assert named in str(refused.value)
    assert KEY_CANARY not in str(refused.value) and JEV_CANARY not in str(refused.value)


@pytest.mark.parametrize("values, match", [
    (without("jev_enabled"), "requires local page context and Jev"),
    ({**CHAT, "reader_page_context": False}, "requires local page context and Jev"),
    (without("reader_story_notes"), "not available in hosted mode yet"),
    ({**CHAT, "reader_story_notes": True}, "not available in hosted mode yet"),
    ({**CHAT, "reader_recollections": True}, "not available in hosted mode yet"),
    ({**CHAT, "llm_transport": "codex-local"}, "codex-local"),
    ({**CHAT, "allow_stub": True}, "ALLOW_STUB"),
    ({**CHAT, "jev_windows_credential": True}, "JEV_WINDOWS_CREDENTIAL"),
    ({**CHAT, "reader_answer_cache": True}, "READER_ANSWER_CACHE"),
    ({**CHAT, "recap_disk_cache": True}, "RECAP_DISK_CACHE"),
    ({**CHAT, "reader_prepare_enabled": True}, "READER_PREPARE_ENABLED"),
    ({**CHAT, "reader_memory_ingest": False}, "deferred memory ingestion"),
    ({**CHAT, "openai_base_url": "https://llm.elsewhere.example/v1"},
     "HOSTED_PROVIDER_ALLOWED_ORIGINS"),
])
def test_what_hosted_mode_still_refuses_with_the_chat_on(values, match, tmp_path) -> None:
    """The break it catches: the chat's settings opening a door section 7 keeps shut: the
    owner's subscription CLI, the stub, a Windows credential store, files shared by every reader,
    deferred memory, a writer address nobody allowed, or chapter notes and recollections before
    their parts land."""
    with pytest.raises(ValueError, match=match):
        hosted_settings(tmp_path, **values)


def test_local_rules_for_the_conversation_are_unchanged(tmp_path) -> None:
    """The break it catches: lifting the hosted refusal loosening local mode."""
    local = dict(_env_file=None, data_dir=str(tmp_path / "local"))
    with pytest.raises(ValueError, match="requires local page context and Jev"):
        Settings(**local, reader_conversation_jev=True, jev_enabled=True)
    with pytest.raises(ValueError, match="requires local page context and Jev"):
        Settings(**local, reader_conversation_jev=True, reader_page_context=True)
    settings = Settings(**local, reader_conversation_jev=True, reader_page_context=True,
                        jev_enabled=True)
    assert settings.reader_conversation_stream and settings.reader_read_map
    assert settings.reader_story_notes and settings.reader_recollections
