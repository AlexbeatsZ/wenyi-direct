from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

from wenyi_direct.config import LLMConfig, TierConfig
from wenyi_direct.llm.providers.agy import AgyClient
from wenyi_direct.llm.providers.anthropic_compatible import (
    AnthropicCompatibleClient,
    _convert_messages,
    _endpoint,
)
from wenyi_direct.llm.providers.codex_cli import CodexCLIClient
from wenyi_direct.llm.providers.openai_compatible import OpenAICompatibleClient


def test_codex_cli_is_ephemeral_read_only_and_ignores_rules(tmp_path, monkeypatch) -> None:
    captured = {}

    def fake_run(args, **kwargs):
        captured.update({"args": args, **kwargs})
        return subprocess.CompletedProcess(args, 0, stdout='{"ok":true}', stderr="")

    monkeypatch.setattr("wenyi_direct.llm.providers.codex_cli.subprocess.run", fake_run)
    client = CodexCLIClient(
        LLMConfig(
            provider="codex-cli",
            cwd=str(tmp_path),
            tiers={"strong": TierConfig(model="gpt-test", options={"reasoning_effort": "high"})},
        )
    )
    assert client.complete([{"role": "user", "content": "hello"}], json_mode=True)
    args = captured["args"]
    assert args[:2] == ["codex", "exec"]
    assert "--ephemeral" in args
    assert "--ignore-user-config" in args
    assert "--ignore-rules" in args
    assert args[args.index("--sandbox") + 1] == "read-only"
    assert captured["input"].endswith("explanatory text.")


def test_agy_uses_fresh_print_plan_request(tmp_path, monkeypatch) -> None:
    captured = {}

    def fake_run(args, **kwargs):
        captured.update({"args": args, **kwargs})
        return subprocess.CompletedProcess(args, 0, stdout='{"ok":true}', stderr="")

    monkeypatch.setattr("wenyi_direct.llm.providers.agy.subprocess.run", fake_run)
    client = AgyClient(
        LLMConfig(
            provider="agy",
            cwd=str(tmp_path),
            tiers={"strong": TierConfig(model="gemini-3.6-flash-medium")},
        )
    )
    client.complete([{"role": "user", "content": "hello"}], json_mode=True)
    args = captured["args"]
    assert args[:2] == ["agy", "--model"]
    assert args[args.index("--mode") + 1] == "plan"
    assert "--print" in args
    assert "--print-timeout" in args


def test_anthropic_messages_wire_format(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_TEST_KEY", "secret")
    client = AnthropicCompatibleClient(
        LLMConfig(
            provider="anthropic-compatible",
            base_url="https://example.invalid/v1",
            api_key_env="ANTHROPIC_TEST_KEY",
            max_retries=0,
            tiers={"strong": TierConfig(model="claude-test", options={"max_tokens": 1234})},
        )
    )
    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "content": [{"type": "text", "text": '{"ok":true}'}],
                "usage": {"input_tokens": 4, "output_tokens": 2},
            }

    def fake_post(payload):
        captured.update(payload)
        return Response()

    monkeypatch.setattr(client, "_post", fake_post)
    result = client.complete(
        [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "task"},
        ],
        json_mode=True,
    )
    assert json.loads(result) == {"ok": True}
    assert client.url == "https://example.invalid/v1/messages"
    assert captured["system"] == "system"
    assert captured["messages"][0]["role"] == "user"
    assert "valid JSON" in captured["messages"][0]["content"]
    assert captured["max_tokens"] == 1234
    assert _endpoint("https://host/v1/messages") == "https://host/v1/messages"
    assert _convert_messages([{"role": "system", "content": "x"}])[0] == "x"


def test_openai_compatible_chat_completions_request() -> None:
    client = OpenAICompatibleClient(
        LLMConfig(
            provider="openai-compatible",
            base_url="https://example.invalid/v1",
            tiers={
                "strong": TierConfig(
                    model="model-test",
                    options={"request_overrides": {"temperature": 0.2}},
                )
            },
        )
    )
    captured = {}

    def create(**kwargs):
        captured.update(kwargs)
        message = SimpleNamespace(content='{"ok":true}')
        return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)

    client._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    result = client.complete(
        [{"role": "user", "content": "task"}], json_mode=True, max_tokens=500
    )
    assert json.loads(result) == {"ok": True}
    assert captured["model"] == "model-test"
    assert captured["response_format"] == {"type": "json_object"}
    assert captured["extra_body"]["temperature"] == 0.2
    assert captured["max_tokens"] == 500
