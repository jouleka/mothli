"""The settings that let the streamed chat run in hosted mode (ADR 0041 section 7, part 5 rules).

What is now allowed (conversation Jev, and so streamed replies, with the platform's own writer and
Jev keys, page context, the read map and caps the operator has set) and everything that must still
be refused, in hosted mode; local mode's rules are unchanged. No database and no model is needed.
"""

from __future__ import annotations

import base64
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.hosted.auth.api import SESSION_COOKIE
from app.hosted.auth.models import Principal
from app.hosted.auth.tokens import new_token
from app.hosted.companion.spend import SpendLimits, set_spend_limits, spend_limits
from tests.hosted.test_platform_pays import JEV_CANARY, KEY_CANARY, _Tenant, hosted_app


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
    it something the operator turned off (chapter notes here)."""
    settings = hosted_settings(tmp_path, **CHAT)
    assert settings.reader_conversation_jev and settings.reader_conversation_stream
    assert settings.reader_read_map and settings.reader_page_context
    assert settings.reader_story_notes is False and settings.reader_recollections is True
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
    ({"reader_recollections": True}, "recollections in hosted mode require READER_CONVERSATION_JEV"),
    ({"reader_story_notes": True}, "chapter notes in hosted mode require READER_CONVERSATION_JEV"),
    ({**CHAT, "llm_transport": "codex-local"}, "codex-local"),
    ({**CHAT, "allow_stub": True}, "ALLOW_STUB"),
    ({**CHAT, "jev_windows_credential": True}, "JEV_WINDOWS_CREDENTIAL"),
    ({**CHAT, "reader_memory_ingest": False}, "deferred memory ingestion"),
    ({**CHAT, "openai_base_url": "https://llm.elsewhere.example/v1"},
     "HOSTED_PROVIDER_ALLOWED_ORIGINS"),
])
def test_what_hosted_mode_still_refuses_with_the_chat_on(values, match, tmp_path) -> None:
    """The break it catches: the chat's settings opening a door section 7 keeps shut: the
    owner's subscription CLI, the stub, a Windows credential store, deferred memory, a writer
    address nobody allowed, or chapter notes or recollections without the chat's conditions (keys
    and caps)."""
    with pytest.raises(ValueError, match=match):
        hosted_settings(tmp_path, **values)


def test_chapter_notes_are_allowed_in_hosted_mode_with_the_chat(tmp_path) -> None:
    """ADR 0041 part 6 lifts the hosted refusal of chapter notes: on by default with the
    streamed chat, as locally, and only on the chat's conditions."""
    assert hosted_settings(tmp_path, **without("reader_story_notes")).reader_story_notes is True
    settings = hosted_settings(tmp_path, **{**CHAT, "reader_story_notes": True})
    assert settings.reader_story_notes and settings.reader_recollections
    assert hosted_settings(tmp_path).reader_story_notes is False


def test_recollections_are_allowed_in_hosted_mode_with_the_chat(tmp_path) -> None:
    """ADR 0041 part 7 lifts the hosted refusal of recollections, on the notes rule's conditions:
    on by default with the streamed chat, off with the read map alone (it calls no model), and
    refused without the chat's keys and caps."""
    assert hosted_settings(tmp_path, **CHAT).reader_recollections is True
    assert hosted_settings(tmp_path, **{**CHAT, "reader_recollections": False}
                           ).reader_recollections is False
    assert hosted_settings(tmp_path).reader_recollections is False
    with pytest.raises(ValueError, match="recollections in hosted mode require"):
        hosted_settings(tmp_path, reader_recollections=True)
    with pytest.raises(ValueError, match="the streamed chat in hosted mode requires"):
        hosted_settings(tmp_path, **{**without("typesafe_api_key"), "reader_recollections": True})


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


def test_story_memory_settings_refuse_what_cannot_work(tmp_path) -> None:
    """The break it catches (ADR 0042 sections 3 and 6): the hosted feature switched on in local
    mode (which builds its own), or owner-paid extraction without the chat's keys and caps; the
    worker running the kind without HOSTED_STORY_MEMORY; or an extraction client that embeds for
    real or follows the chat's model settings. ADR 0044 step 4 removed the setting for a
    reader's own books (HOSTED_READER_STORY_MEMORY): every book is built for once the feature
    is on, and the old line in an environment file changes nothing."""
    from app.hosted.companion.jobs import companion_kinds
    from app.llm.factory import build_extraction_client

    off = hosted_settings(tmp_path, **CHAT)
    assert not off.hosted_story_memory and "hosted_reader_story_memory" not in Settings.model_fields
    assert companion_kinds(off) == {"recollect"}
    with pytest.MonkeyPatch.context() as env:
        env.setenv("HOSTED_READER_STORY_MEMORY", "false")         # a line left from before
        on = hosted_settings(tmp_path, **CHAT, hosted_story_memory=True,
                             embed_provider="openai-compatible", llm_cheap_model="gpt-6-luna")
    assert companion_kinds(on) == {"recollect", "story_memory"}
    client = build_extraction_client(on)
    assert client.embed_identity() == "stub:lexical-stub-256"
    assert client.provider == "openai-compatible"
    assert client._model_for("cheap") == client._model_for("large") != "gpt-6-luna"
    with pytest.raises(ValueError, match="story memory in hosted mode requires"):
        hosted_settings(tmp_path, hosted_story_memory=True)
    with pytest.raises(ValueError, match="the streamed chat in hosted mode requires"):
        hosted_settings(tmp_path, **without("openai_api_key"), hosted_story_memory=True)
    with pytest.raises(ValueError, match="HOSTED_STORY_MEMORY is a hosted-mode setting"):
        Settings(_env_file=None, data_dir=str(tmp_path / "local"), hosted_story_memory=True)


def test_story_memory_guards_and_ceilings_start_at_the_owners_numbers(tmp_path) -> None:
    """ADR 0044, the owner's rule on acceptance: one reader's runs may be the first to build at
    most 30 new chapters a UTC day and 300 a month, and be first to chapter 1 of at most 5 new
    files a month; the notes ceiling for everyone is $10 a day and the platform's day $15. The
    break it catches: a default that differs from what the owner decided, a ceiling that needs
    an operator's step on a fresh deployment (it is a default in code), or a process whose
    caps do not carry the notes ceiling."""
    settings = hosted_settings(tmp_path, **without("hosted_platform_usd_per_day"),
                               hosted_platform_usd_per_day=15.00)
    defaults = Settings.model_fields
    assert (defaults["hosted_story_memory_new_chapters_per_day"].default,
            defaults["hosted_story_memory_new_chapters_per_month"].default,
            defaults["hosted_story_memory_new_files_per_month"].default) == (30, 300, 5)
    assert defaults["hosted_story_memory_usd_per_day"].default == 10.00
    assert defaults["hosted_platform_usd_per_day"].default == 15.00
    assert (SpendLimits().notes_usd_per_day, SpendLimits().platform_usd_per_day) == (
        Decimal("10.00"), Decimal("15.00"))
    before = spend_limits()
    try:
        with TestClient(hosted_app(settings), base_url="https://reader.example"):
            assert spend_limits().notes_usd_per_day == Decimal("10.00")
        with TestClient(hosted_app(hosted_settings(tmp_path, **CHAT,
                                                   hosted_story_memory_usd_per_day=2.5)),
                        base_url="https://reader.example"):
            assert spend_limits().notes_usd_per_day == Decimal("2.5")
    finally:
        set_spend_limits(reader_usd_per_day=before.reader_usd_per_day,
                         reader_usd_per_month=before.reader_usd_per_month,
                         platform_usd_per_day=before.platform_usd_per_day,
                         notes_usd_per_day=before.notes_usd_per_day)


# ---- whether this server has a chat at all, and the one mixed state that pays without replying ----

SWITCHES = {"reader_conversation_jev": True, "jev_enabled": True,
            "hosted_reader_usd_per_day": 0.10, "hosted_platform_usd_per_day": 5.00}


def test_hosted_refuses_the_paid_chat_with_streaming_turned_off(tmp_path) -> None:
    """The break it catches: a hosted server started with the chat's keys, caps and Jev setting,
    but READER_CONVERSATION_STREAM=false. Hosted mode has no other reply (the non-streamed one was
    removed, ADR 0043), so nothing could ever answer a reader while chapter notes or story memory
    could still be paid for. The message names the settings to fix, never a value."""
    with pytest.raises(ValueError, match="READER_CONVERSATION_STREAM") as refused:
        hosted_settings(tmp_path, **CHAT, reader_conversation_stream=False)
    assert "READER_CONVERSATION_JEV" in str(refused.value)
    assert KEY_CANARY not in str(refused.value) and JEV_CANARY not in str(refused.value)
    with pytest.raises(ValueError, match="READER_CONVERSATION_STREAM"):
        hosted_settings(tmp_path, **{**CHAT, "reader_story_notes": True},
                        reader_conversation_stream=False)
    # Streaming left to follow the Jev setting, or set on: the chat, as before.
    assert hosted_settings(tmp_path, **CHAT, reader_conversation_stream=True).companion_chat()
    # Local mode keeps its reviewed, non-streamed replies.
    local = Settings(_env_file=None, data_dir=str(tmp_path / "local"), reader_page_context=True,
                     jev_enabled=True, reader_conversation_jev=True,
                     reader_conversation_stream=False)
    assert local.reader_conversation_jev and not local.reader_conversation_stream


@pytest.mark.parametrize("empty", [{}, {"openai_api_key": "", "typesafe_api_key": ""}],
                         ids=["keys-left-out", "keys-left-empty"])
def test_hosted_with_no_ai_account_starts_without_a_chat_whatever_the_switches(empty,
                                                                               tmp_path) -> None:
    """The break it catches: a hosted server whose owner has not filled in either AI account
    refusing to start because the template's chat switches are on, or starting with a chat that can
    only fail. With neither account, nothing that calls a model is on and the server says it has no
    chat; reading and the read map (which calls no model) stay as they were."""
    settings = hosted_settings(tmp_path, **SWITCHES, **empty)
    assert not settings.companion_chat()
    assert not (settings.reader_conversation_jev or settings.reader_conversation_stream
                or settings.jev_enabled or settings.reader_story_notes
                or settings.reader_recollections)
    assert settings.typesafe_api_key is None and not settings.openai_api_key
    assert settings.reader_page_context and settings.reader_read_map
    with TestClient(hosted_app(settings), base_url="https://reader.example") as client:
        assert client.app.state.platform_client is None      # nothing set up to call a model
        assert client.get("/api/limits").status_code == 401  # up, and asks for sign-in


@pytest.mark.parametrize("values, named", [
    ({**SWITCHES, "openai_api_key": KEY_CANARY}, "TYPESAFE_API_KEY"),
    ({**SWITCHES, "typesafe_api_key": JEV_CANARY}, "OPENAI_API_KEY or ANTHROPIC_API_KEY"),
    ({**SWITCHES, "openai_api_key": KEY_CANARY, "typesafe_api_key": ""}, "TYPESAFE_API_KEY"),
])
def test_one_ai_account_without_the_other_is_refused(values, named, tmp_path) -> None:
    """The break it catches: the no-account rule above swallowing a half-filled template, which
    would start a server that pays one account and can never answer."""
    with pytest.raises(ValueError, match="the streamed chat in hosted mode requires") as refused:
        hosted_settings(tmp_path, **values)
    assert named in str(refused.value)
    assert KEY_CANARY not in str(refused.value) and JEV_CANARY not in str(refused.value)


class _SignedIn:
    """A sign-in service that knows one reader, so ``GET /api/auth/session`` answers."""

    async def check_runtime_role(self) -> None:
        return None

    async def authenticate_session(self, *, session_digest, now, idle_ttl):
        return Principal(owner_id=uuid.uuid4(), session_id=uuid.uuid4(), display_name="Reader",
                         email=None, csrf_digest=b"c" * 32,
                         expires_at=datetime(2026, 7, 17, tzinfo=UTC))


@pytest.mark.parametrize("values, chat", [({}, False), (SWITCHES, False), (CHAT, True)],
                         ids=["nothing-set", "switches-without-keys", "chat"])
def test_the_session_says_whether_this_server_has_a_chat(values, chat, tmp_path) -> None:
    """The break it catches: the app learning that there is no chat only from a failed request
    (a reply that 404s, then the old /ask that 404s too), so every reader's first question ends
    in "I couldn't get a reply". The answer the app reads when it loads says it plainly."""
    from app.main import create_app

    app = create_app(hosted_settings(tmp_path, **values), auth_repository=_SignedIn(),
                     tenant_repository=_Tenant(), oidc_client=object(),
                     auth_clock=lambda: datetime(2026, 7, 16, tzinfo=UTC))
    with TestClient(app, base_url="https://reader.example") as client:
        client.cookies.set(SESSION_COOKIE, new_token())
        answer = client.get("/api/auth/session")
        assert answer.status_code == 200
        assert answer.json()["chat"] is chat
        assert client.app.state.settings.companion_chat() is chat
