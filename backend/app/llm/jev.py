"""Typed TypeSafe transport. No generation, redirects, automatic retries or plaintext key files."""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import sys

import httpx


# A 4xx answer: the provider turned the call away before doing any work, so it cost nothing.
REFUSALS = frozenset({"no_credits", "unauthorized", "rate_limited", "refused"})
# Refusals about the account, not the request: their message cannot quote what was sent.
_ABOUT_THE_ACCOUNT = frozenset({"no_credits", "unauthorized", "rate_limited"})
_STATUS_REASONS = {401: "unauthorized", 402: "no_credits", 403: "unauthorized", 429: "rate_limited"}


def refusal_reason(status):
    """The stable word for a provider status. Only a 4xx is a refusal; anything else is unknown."""
    if 400 <= status < 500:
        return _STATUS_REASONS.get(status, "refused")
    return "unavailable"


class JevUnavailable(RuntimeError):
    """Public-safe failure: request bodies, credentials and provider errors stay private.

    ``reason`` says what kind of failure it was: ``no_credits``, ``unauthorized``,
    ``rate_limited`` or ``refused`` when the provider turned the call away, and ``unavailable``
    for everything else (server errors, timeouts, lost connections, answers that do not parse,
    and our own bounded-contract failures). ``detail`` is the provider's own short message about
    the account, for the owner's log only; it is never shown to a reader.
    """

    def __init__(self, message="Jev unavailable", *, reason="unavailable", detail=None):
        super().__init__(message)
        self.reason, self.detail = reason, detail

    @property
    def refused(self):
        """True only when the provider is known to have done no work, so nothing was spent."""
        return self.reason in REFUSALS


def windows_credential() -> str:
    """Read the existing current-user PowerShell SecureString DPAPI blob in memory only."""
    if sys.platform != "win32":
        raise JevUnavailable("Windows Jev credential is unavailable")

    class Blob(ctypes.Structure):
        _fields_ = [("size", ctypes.c_ulong), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    try:
        path = Path(os.environ["LOCALAPPDATA"]) / "Codex/Secrets/TypeSafeJev/api-key.dpapi"
        encrypted = bytes.fromhex(path.read_text(encoding="utf-8-sig").strip())
        raw = (ctypes.c_ubyte * len(encrypted)).from_buffer_copy(encrypted)
        source, target = Blob(len(encrypted), raw), Blob()
        crypt = ctypes.windll.crypt32
        crypt.CryptUnprotectData.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p,
                                             ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                             ctypes.c_ulong, ctypes.POINTER(Blob)]
        crypt.CryptUnprotectData.restype = ctypes.c_int
        if not crypt.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 1,
                                       ctypes.byref(target)):
            raise ValueError("DPAPI failure")
        try:
            key = ctypes.string_at(target.data, target.size).decode("utf-16-le")
        finally:
            ctypes.memset(target.data, 0, target.size)
            free = ctypes.windll.kernel32.LocalFree
            free.argtypes, free.restype = [ctypes.c_void_p], ctypes.c_void_p
            free(target.data)
        if not key.strip():
            raise ValueError("empty credential")
        return key
    except Exception:
        raise JevUnavailable("Stored Jev credential could not be loaded") from None


class JevClient:
    def __init__(self, *, api_key, model="jev-1.13.0", timeout=5.0, transport=None):
        self.model = model
        self.status = "configured"
        self._http = httpx.Client(
            base_url="https://api.typesafe.ai", timeout=timeout, follow_redirects=False,
            headers={"Authorization": f"Bearer {api_key}"}, transport=transport,
        )

    def close(self):
        self._http.close()

    def _provider_message(self, response):
        """The provider's one-line message about the account, or None. Never the key."""
        try:
            raw = b""
            for chunk in response.iter_bytes():
                raw += chunk
                if len(raw) > 4096:
                    return None
            error = json.loads(raw)
            error = error.get("error", error)
            text = error if isinstance(error, str) else error.get("message") or error.get("detail")
            if not isinstance(text, str):
                return None
            text = " ".join("".join(ch for ch in text if ch.isprintable() or ch.isspace()).split())[:200]
            key = self._http.headers["Authorization"].removeprefix("Bearer ")
            # A provider may repeat the key back, sometimes partly masked: drop the whole message.
            if any(key[i:i + 6] in text for i in range(max(1, len(key) - 5))):
                return None
            return text or None
        except Exception:
            return None

    def evaluate(self, state, questions, *, timeout_seconds=None):
        if not questions or len(questions) > 24:
            raise JevUnavailable("Jev question batch is outside the bounded contract")
        body = {"model": self.model, "state": state, "questions": questions}
        reason, detail = "unavailable", None
        try:
            options = {"timeout": timeout_seconds} if timeout_seconds is not None else {}
            with self._http.stream("POST", "/v1/systemone", json=body, **options) as response:
                if response.status_code != 200:
                    reason = refusal_reason(response.status_code)
                    if reason in _ABOUT_THE_ACCOUNT:
                        detail = self._provider_message(response)
                    raise ValueError("provider status")
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > 262144:
                        raise ValueError("oversized response")
                    chunks.append(chunk)
                value = json.loads(b"".join(chunks))
            if value.get("model") != self.model or set(value.get("answers", {})) != set(questions):
                raise ValueError("model or answer coverage")
            for name, question in questions.items():
                answer = value["answers"][name]
                if question["type"] == "noul":
                    probability = answer.get("noul")
                    if (answer.get("type") != "noul" or type(probability) not in (int, float)
                            or not 0 <= probability <= 1):
                        raise ValueError("invalid typed probability")
                    continue
                if question["type"] != "choice":
                    raise ValueError("unsupported question type")
                options = set(question["criteria"])
                probabilities = answer.get("probabilities", {})
                if (answer.get("type") != "choice" or answer.get("choice") not in options
                        or set(probabilities) != options
                        or any(type(p) not in (int, float) or not 0 <= p <= 1
                               for p in probabilities.values())
                        or abs(sum(probabilities.values()) - 1) > 0.02
                        or type(answer.get("confidence")) not in (int, float)
                        or not 0 <= answer["confidence"] <= 1
                        or probabilities[answer["choice"]] < max(probabilities.values()) - 1e-6):
                    raise ValueError("invalid typed answer")
            usage = value.get("usage", {})
            if any(type(usage.get(k)) is not int or usage[k] < 0
                   for k in ("input_tokens", "output_tokens")) or usage["input_tokens"] == 0:
                raise ValueError("invalid usage")
            self.status = "connected"
            return value["answers"], {"in": usage["input_tokens"], "out": usage["output_tokens"]}
        except Exception:
            self.status = "fallback"
            raise JevUnavailable("Jev evaluation unavailable or invalid",
                                 reason=reason, detail=detail) from None
