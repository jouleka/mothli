import json
from pathlib import Path
import subprocess

import httpx
import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from app.config import Settings
from app.cost import pricing_known, usd_of
from app.llm.client import LLMClient, ProviderUnavailable
from app.llm import codex_local
from app.ingest.extraction.schema import Extraction
from app.ask_selection import SourceSelection
from app.ask import AskVerdict


class Result(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str


@pytest.mark.parametrize("tier,schema,effort", [
    ("cheap", Extraction, "low"), ("cheap", SourceSelection, "low"),
    ("cheap", AskVerdict, "medium"), ("large", None, "medium"),
    ("cheap", None, "medium"),
    ("large", Extraction, "low"),
])
def test_terra_routes_effort_by_workload(tier, schema, effort):
    client = LLMClient(env={"LLM_TRANSPORT": "codex-local"})
    assert client._model_for(tier) == "gpt-6.1-terra"
    assert client.reasoning_effort(tier, schema) == effort


def test_native_terra_sends_effort_and_shared_reasoning_output_cap():
    calls = []
    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        return httpx.Response(200, json={
            "id": "test", "object": "chat.completion", "created": 0,
            "model": body["model"], "service_tier": "default",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {
                "role": "assistant", "content": '{"text":"Supported."}'}}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
        })
    client = LLMClient(provider="openai-compatible", api_key="synthetic", env={},
                       http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    client.complete("Write.", "Evidence.", tier="large", schema=Result, max_output_tokens=900)
    client.complete("Extract.", "Evidence.", tier="cheap", max_output_tokens=900)
    assert [body["reasoning_effort"] for body in calls] == ["medium", "medium"]
    assert all(body["model"] == "gpt-6.1-terra" and body["max_completion_tokens"] == 900
               and "temperature" not in body for body in calls)


def _fake_cli(monkeypatch, *, output='{"text":"Supported."}', tokens=25, authenticated=True):
    monkeypatch.setattr(codex_local.shutil, "which", lambda name: "/fake/codex")
    calls = []
    def run(command, **kwargs):
        if command[1] == "login":
            return subprocess.CompletedProcess(command, 0, "Logged in using ChatGPT" if authenticated
                                               else "Logged in using an API key", "")
        calls.append(command)
        assert kwargs["input"] == "Evidence only."
        instructions = next(value.split("=", 1)[1] for value in command
                            if value.startswith("model_instructions_file="))
        assert "Review carefully." in Path(json.loads(instructions)).read_text()
        Path(command[command.index("--output-last-message") + 1]).write_text(output)
        return subprocess.CompletedProcess(command, 0, json.dumps({
            "type": "turn.completed", "usage": {"input_tokens": 200, "output_tokens": tokens}}), "")
    monkeypatch.setattr(codex_local.subprocess, "run", run)
    return calls


def test_local_chatgpt_transport_validates_schema_and_reports_real_usage(monkeypatch):
    calls = _fake_cli(monkeypatch)
    client = LLMClient(env={"LLM_TRANSPORT": "codex-local"})
    value, usage = client.complete("Review carefully.", "Evidence only.", schema=Result,
                                  max_output_tokens=100)
    assert value == {"text": "Supported."} and usage == {"in": 200, "out": 25}
    assert "--output-schema" in calls[0] and "--ephemeral" in calls[0]
    assert "features.shell_tool=false" in calls[0] and "web_search=\"disabled\"" in calls[0]
    assert client.provider_status()["completion"]["status"] == "ready"
    assert client.billing_model("cheap") == "chatgpt-plan:gpt-6.1-terra"


@pytest.mark.parametrize("output,tokens,authenticated,error", [
    ('{"wrong":"field"}', 25, True, ValidationError),
    ('{"text":"ok"}', 101, True, ProviderUnavailable),
    ('{"text":"ok"}', 25, False, ProviderUnavailable),
])
def test_local_transport_fails_closed(monkeypatch, output, tokens, authenticated, error):
    _fake_cli(monkeypatch, output=output, tokens=tokens, authenticated=authenticated)
    client = LLMClient(env={"LLM_TRANSPORT": "codex-local"})
    with pytest.raises(error):
        client.complete("Review carefully.", "Evidence only.", schema=Result, max_output_tokens=100)


def test_local_transport_cannot_be_enabled_for_hosted_mode():
    with pytest.raises(ValueError, match="only supported for local testing"):
        Settings(_env_file=None, llm_transport="codex-local", deployment_mode="hosted")


def test_terra_api_cost_is_separate_from_chatgpt_plan_usage():
    assert pricing_known("gpt-6.1-terra")
    assert usd_of("gpt-6.1-terra", {"in": 1000, "out": 1000}) == pytest.approx(0.006)
    assert not pricing_known("gpt-6.1-terra-other")
    assert not pricing_known("chatgpt-plan:gpt-6.1-terra")
    assert usd_of("chatgpt-plan:gpt-6.1-terra", {"in": 1000000, "out": 1000000}) == 0
