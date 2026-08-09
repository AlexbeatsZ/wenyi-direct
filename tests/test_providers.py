from __future__ import annotations

import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace

import pytest

from wenyi_direct.config import Config, LLMConfig, TierConfig
from wenyi_direct.llm.base import ContentPolicyError, LLMClient, TransientProviderError
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


@pytest.mark.parametrize(
    "failure_detail",
    (
        "stream disconnected before completion",
        "request timeout",
        "context deadline exceeded",
        "请求超时",
    ),
)
def test_codex_cli_retries_transient_transport_failure(
    tmp_path, monkeypatch, failure_detail
) -> None:
    calls = 0
    delays = []

    def fake_run(args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return subprocess.CompletedProcess(args, 1, stdout="", stderr=failure_detail)
        return subprocess.CompletedProcess(args, 0, stdout='{"ok":true}', stderr="")

    monkeypatch.setattr("wenyi_direct.llm.providers.codex_cli.subprocess.run", fake_run)
    monkeypatch.setattr(
        "wenyi_direct.llm.providers.codex_cli.time.sleep", lambda delay: delays.append(delay)
    )
    client = CodexCLIClient(
        LLMConfig(
            provider="codex-cli",
            cwd=str(tmp_path),
            max_retries=3,
            tiers={"strong": TierConfig(model="gpt-test")},
        )
    )

    assert client.complete([{"role": "user", "content": "hello"}]) == '{"ok":true}'
    assert calls == 2
    assert delays == [1]


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


def test_agy_retries_transient_eligibility_eof(tmp_path, monkeypatch) -> None:
    calls = 0
    delays = []

    def fake_run(args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return subprocess.CompletedProcess(
                args,
                1,
                stdout="",
                stderr=(
                    "Error: Eligibility check failed: failed to get profile picture: "
                    'Get "https://lh3.googleusercontent.com/avatar": EOF'
                ),
            )
        return subprocess.CompletedProcess(args, 0, stdout='{"ok":true}', stderr="")

    monkeypatch.setattr("wenyi_direct.llm.providers.agy.subprocess.run", fake_run)
    monkeypatch.setattr(
        "wenyi_direct.llm.providers.agy.time.sleep", lambda delay: delays.append(delay)
    )
    client = AgyClient(
        LLMConfig(
            provider="agy",
            cwd=str(tmp_path),
            max_retries=3,
            tiers={"strong": TierConfig(model="gemini-3.6-flash-high")},
        )
    )

    assert client.complete([{"role": "user", "content": "hello"}]) == '{"ok":true}'
    assert calls == 2
    assert delays == [1]


@pytest.mark.parametrize(
    "failure_detail",
    (
        "Error: authentication failed or timed out",
        "Error: request timeout",
        "rpc error: context deadline exceeded",
        "Error: 认证超时",
    ),
)
def test_agy_retries_any_reported_timeout(
    tmp_path, monkeypatch, failure_detail
) -> None:
    calls = 0
    delays = []

    def fake_run(args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return subprocess.CompletedProcess(args, 1, stdout="", stderr=failure_detail)
        return subprocess.CompletedProcess(args, 0, stdout='{"ok":true}', stderr="")

    monkeypatch.setattr("wenyi_direct.llm.providers.agy.subprocess.run", fake_run)
    monkeypatch.setattr(
        "wenyi_direct.llm.providers.agy.time.sleep", lambda delay: delays.append(delay)
    )
    client = AgyClient(
        LLMConfig(
            provider="agy",
            cwd=str(tmp_path),
            max_retries=3,
            tiers={"strong": TierConfig(model="gemini-3.6-flash-high")},
        )
    )

    assert client.complete([{"role": "user", "content": "hello"}]) == '{"ok":true}'
    assert calls == 2
    assert delays == [1]


def test_agy_authentication_timeout_retries_are_bounded(tmp_path, monkeypatch) -> None:
    calls = 0
    delays = []

    def fake_run(args, **kwargs):
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess(
            args,
            1,
            stdout="",
            stderr="Error: authentication failed or timed out",
        )

    monkeypatch.setattr("wenyi_direct.llm.providers.agy.subprocess.run", fake_run)
    monkeypatch.setattr(
        "wenyi_direct.llm.providers.agy.time.sleep", lambda delay: delays.append(delay)
    )
    client = AgyClient(
        LLMConfig(
            provider="agy",
            cwd=str(tmp_path),
            max_retries=2,
            tiers={"strong": TierConfig(model="gemini-3.6-flash-high")},
        )
    )

    with pytest.raises(TransientProviderError, match="authentication failed"):
        client.complete([{"role": "user", "content": "hello"}])
    assert calls == 3
    assert delays == [1, 2]


def test_agy_does_not_retry_permanent_cli_failure(tmp_path, monkeypatch) -> None:
    calls = 0

    def fake_run(args, **kwargs):
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess(
            args, 1, stdout="", stderr="Error: authentication required"
        )

    monkeypatch.setattr("wenyi_direct.llm.providers.agy.subprocess.run", fake_run)
    client = AgyClient(
        LLMConfig(
            provider="agy",
            cwd=str(tmp_path),
            max_retries=3,
            tiers={"strong": TierConfig(model="gemini-3.6-flash-high")},
        )
    )

    with pytest.raises(RuntimeError, match="authentication required"):
        client.complete([{"role": "user", "content": "hello"}])
    assert calls == 1


def test_agy_same_provider_allows_two_isolated_calls_to_overlap(
    tmp_path, monkeypatch
) -> None:
    both_running = Barrier(2)

    def fake_run(args, **kwargs):
        assert (Path(kwargs["cwd"]) / "request.txt").is_file()
        both_running.wait(timeout=2)
        return subprocess.CompletedProcess(args, 0, stdout='{"ok":true}', stderr="")

    monkeypatch.setattr("wenyi_direct.llm.providers.agy.subprocess.run", fake_run)
    client = AgyClient(
        LLMConfig(
            provider="agy",
            cwd=str(tmp_path),
            tiers={"strong": TierConfig(model="gemini-3.6-flash-high")},
        )
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        calls = [
            executor.submit(
                client.complete,
                [{"role": "user", "content": f"request {index}"}],
                json_mode=True,
            )
            for index in range(2)
        ]
        assert [call.result() for call in calls] == ['{"ok":true}', '{"ok":true}']


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


def test_configured_fallback_handles_policy_transient_and_invalid_json_only() -> None:
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

    transient_primary = _ResultClient(TransientProviderError("temporary EOF"))
    transient_fallback = _ResultClient('{"route":"fallback"}')
    transient_client = ContentPolicyFallbackClient(
        transient_primary, transient_fallback
    )
    assert json.loads(transient_client.complete([], stage="validation")) == {
        "route": "fallback"
    }
    assert transient_client.usage_summary()["transient_error_fallback_events"] == [
        {"stage": "validation", "reason": "temporary EOF"}
    ]

    invalid_primary = _ResultClient("not valid json")
    invalid_fallback = _ResultClient('{"route":"fallback-json"}')
    invalid_client = ContentPolicyFallbackClient(invalid_primary, invalid_fallback)
    assert invalid_client.complete_json([], stage="factual_audit") == {
        "route": "fallback-json"
    }
    assert invalid_primary.calls == 2
    assert invalid_fallback.calls == 1
    assert len(
        invalid_client.usage_summary()["invalid_response_fallback_events"]
    ) == 1


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
