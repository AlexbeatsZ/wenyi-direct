from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from wenyi_direct.config import Config, LLMConfig, TierConfig
from wenyi_direct.llm.base import ContentPolicyError, LLMClient
from wenyi_direct.llm.factory import build_clients
from wenyi_direct.llm.policy_fallback import ContentPolicyFallbackClient
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
        request_path = Path(kwargs["cwd"]) / "request.txt"
        captured.update(
            {
                "args": args,
                "request_path": request_path,
                "request_text": request_path.read_text(encoding="utf-8"),
                **kwargs,
            }
        )
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
    assert "--sandbox" in args
    assert "--dangerously-skip-permissions" in args
    assert "--disable-slash-commands" in args
    assert "--new-project" in args
    assert "--print" in args
    assert "--print-timeout" in args
    assert "hello" not in args[-1]
    assert "request.txt" in args[-1]
    assert "hello" in captured["request_text"]
    assert captured["request_text"].endswith("explanatory text.")
    assert not captured["request_path"].exists()


def test_agy_large_prompt_never_enters_windows_argv(tmp_path, monkeypatch) -> None:
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["request_length"] = len(
            (Path(kwargs["cwd"]) / "request.txt").read_text(encoding="utf-8")
        )
        return subprocess.CompletedProcess(args, 0, stdout='{"ok":true}', stderr="")

    monkeypatch.setattr("wenyi_direct.llm.providers.agy.subprocess.run", fake_run)
    client = AgyClient(
        LLMConfig(
            provider="agy",
            cwd=str(tmp_path),
            tiers={"strong": TierConfig(model="gemini-3.6-flash-high")},
        )
    )
    client.complete([{"role": "user", "content": "x" * 70_000}], json_mode=True)

    assert captured["request_length"] > 70_000
    assert max(map(len, captured["args"])) < 1_000


def test_agy_never_uses_stderr_as_model_response(tmp_path, monkeypatch) -> None:
    calls = 0

    def fake_run(args, **kwargs):
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess(
            args,
            0,
            stdout="",
            stderr="warning: no response body",
        )

    monkeypatch.setattr("wenyi_direct.llm.providers.agy.subprocess.run", fake_run)
    client = AgyClient(
        LLMConfig(
            provider="agy",
            cwd=str(tmp_path),
            tiers={"strong": TierConfig(model="gemini-3.6-flash-high")},
        )
    )

    with pytest.raises(RuntimeError, match="没有返回模型正文"):
        client.complete([{"role": "user", "content": "hello"}], json_mode=True)
    assert calls == 3


def test_agy_returns_stdout_when_stderr_contains_warning(tmp_path, monkeypatch) -> None:
    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(
            args,
            0,
            stdout='{"ok":true}',
            stderr="warning: diagnostic only",
        )

    monkeypatch.setattr("wenyi_direct.llm.providers.agy.subprocess.run", fake_run)
    client = AgyClient(
        LLMConfig(
            provider="agy",
            cwd=str(tmp_path),
            tiers={"strong": TierConfig(model="gemini-3.6-flash-high")},
        )
    )

    assert json.loads(
        client.complete([{"role": "user", "content": "hello"}], json_mode=True)
    ) == {"ok": True}


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


class _ResultClient(LLMClient):
    def __init__(self, result: str | Exception) -> None:
        super().__init__()
        self.result = result
        self.calls = 0

    def complete(self, messages, **kwargs):
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_policy_fallback_only_handles_explicit_content_refusal() -> None:
    primary = _ResultClient(ContentPolicyError("refused"))
    fallback = _ResultClient('{"ok":true}')
    client = ContentPolicyFallbackClient(primary, fallback)

    assert json.loads(client.complete([], json_mode=True, stage="translate")) == {"ok": True}
    assert primary.calls == 1
    assert fallback.calls == 1
    assert client.usage_summary()["content_policy_fallback_events"] == [
        {"stage": "translate", "reason": "refused"}
    ]

    runtime_primary = _ResultClient(RuntimeError("quota"))
    untouched_fallback = _ResultClient('{"ok":true}')
    runtime_client = ContentPolicyFallbackClient(runtime_primary, untouched_fallback)
    with pytest.raises(RuntimeError, match="quota"):
        runtime_client.complete([])
    assert untouched_fallback.calls == 0


def test_factory_reuses_one_policy_fallback_wrapper(monkeypatch) -> None:
    config = Config.model_validate(
        {
            "providers": {
                "gemini": {"provider": "fake"},
                "deepseek": {"provider": "fake"},
            },
            "roles": {
                "translate": "gemini",
                "factual_audit": "gemini",
                "chinese_audit": "gemini",
                "repair": "gemini",
                "validation": "gemini",
                "content_policy_fallback": "deepseek",
            },
        }
    )
    built = iter([_ResultClient("primary"), _ResultClient("fallback")])
    monkeypatch.setattr(
        "wenyi_direct.llm.factory.build_client_from_llm", lambda _config: next(built)
    )
    clients = build_clients(config)
    assert set(clients) == {
        "translate",
        "factual_audit",
        "chinese_audit",
        "repair",
        "validation",
    }
    assert len({id(client) for client in clients.values()}) == 1
