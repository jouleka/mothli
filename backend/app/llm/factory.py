"""The platform's model clients, built once per process from environment settings (ADR 0041 §6).

One function for both modes. Locally it is exactly what ``deps.build_state`` always built: the
writer from ``settings.llm_env()`` with no SDK retries, and Jev beside it when ``JEV_ENABLED``.
In hosted mode the owner pays, so the same construction first refuses whatever is not the
owner's own paid API account: the stub, ``codex-local`` (the owner's subscription CLI), a Jev key
read from a Windows credential store, and a missing writer or Jev key. Every message names the
setting and never its value; keys stay in the environment and in the client objects only.
"""

from __future__ import annotations

from app.config import Settings
from app.llm.client import LLMClient

# The writer providers the platform may pay for in hosted mode (ADR 0041 section 6).
HOSTED_WRITER_PROVIDERS = frozenset({"openai-compatible", "anthropic"})


class PlatformClientsUnavailable(RuntimeError):
    """Hosted startup refused: a platform model setting is missing or not allowed. The message
    names settings only, never their values."""


def _check_hosted(settings: Settings) -> None:
    if settings.allow_stub:
        raise PlatformClientsUnavailable("hosted mode refuses ALLOW_STUB")
    if settings.llm_transport == "codex-local":
        raise PlatformClientsUnavailable("hosted mode refuses LLM_TRANSPORT=codex-local")
    if settings.jev_windows_credential:
        raise PlatformClientsUnavailable("hosted mode refuses JEV_WINDOWS_CREDENTIAL")
    if not (settings.openai_api_key or settings.anthropic_api_key):
        raise PlatformClientsUnavailable(
            "hosted mode requires OPENAI_API_KEY or ANTHROPIC_API_KEY for the platform's writer")
    if not settings.jev_enabled:
        raise PlatformClientsUnavailable("hosted mode requires JEV_ENABLED for the platform's checks")
    key = settings.typesafe_api_key
    if key is None or not key.get_secret_value():
        raise PlatformClientsUnavailable("hosted mode requires TYPESAFE_API_KEY for the platform's checks")


def build_platform_clients(settings: Settings) -> LLMClient:
    """The writer ``LLMClient``, with ``.jev`` set when Jev is enabled. Raises to refuse startup."""
    hosted = settings.deployment_mode == "hosted"
    if hosted:
        _check_hosted(settings)
    # A reservation covers one wire attempt. SDK retries after ambiguous timeouts can be
    # billed again; the explicit application repair paths reserve each attempt separately.
    client = LLMClient(env=settings.llm_env(), allow_stub=settings.allow_stub, max_retries=0)
    if hosted and client.provider not in HOSTED_WRITER_PROVIDERS:
        raise PlatformClientsUnavailable("hosted mode allows only an OpenAI-compatible or Anthropic writer")
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
