"""Anthropic Messages API-compatible transport."""

from __future__ import annotations

import os
import time
from typing import Any, Optional

import httpx

from ...config import LLMConfig, TierConfig
from ..base import LLMClient, Messages
from ..tiers import resolve_tier
from ..usage import UsageSample

_DEFAULT_TIERS = {
    "strong": TierConfig(model="claude-sonnet-4-5"),
    "cheap": TierConfig(model="claude-haiku-4-5"),
    "fast": TierConfig(model="claude-haiku-4-5"),
}


def _endpoint(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/v1/messages"):
        return base
    if base.endswith("/v1"):
        return base + "/messages"
    return base + "/v1/messages"


def _convert_messages(messages: Messages) -> tuple[str, list[dict[str, str]]]:
    systems: list[str] = []
    converted: list[dict[str, str]] = []
    for message in messages:
        role = str(message.get("role", "user")).lower()
        content = str(message.get("content", ""))
        if not content:
            continue
        if role == "system":
            systems.append(content)
        elif role == "assistant":
            converted.append({"role": "assistant", "content": content})
        else:
            converted.append({"role": "user", "content": content})
    if not converted:
        converted.append({"role": "user", "content": "Complete the task."})
    return "\n\n".join(systems), converted


class AnthropicCompatibleClient(LLMClient):
    def __init__(self, cfg: LLMConfig) -> None:
        super().__init__()
        if not cfg.base_url:
            raise ValueError("anthropic-compatible requires base_url")
        if not cfg.api_key_env:
            raise ValueError("anthropic-compatible requires api_key_env")
        api_key = os.environ.get(cfg.api_key_env)
        if not api_key:
            raise ValueError(f"environment variable {cfg.api_key_env!r} is not set")
        self.url = _endpoint(cfg.base_url)
        self.headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        self.timeout = max(1, int(cfg.timeout))
        self.max_retries = max(0, int(cfg.max_retries))
        self.tiers = {**_DEFAULT_TIERS, **cfg.tiers}
        self._client = httpx.Client(timeout=self.timeout)

    def _post(self, payload: dict[str, Any]) -> httpx.Response:
        return self._client.post(self.url, headers=self.headers, json=payload)

    def complete(
        self,
        messages: Messages,
        *,
        tier: str = "strong",
        json_mode: bool = False,
        max_tokens: Optional[int] = None,
        stage: Optional[str] = None,
    ) -> str:
        tier_config = resolve_tier(self.tiers, tier)
        if not tier_config.model:
            raise ValueError(f"anthropic-compatible tier {tier!r} has no model")
        system, converted = _convert_messages(messages)
        if json_mode:
            converted[-1]["content"] += (
                "\n\nReturn exactly one valid JSON value. Do not use Markdown fences."
            )
        options = dict(tier_config.options)
        payload: dict[str, Any] = {
            "model": tier_config.model,
            "max_tokens": max_tokens or int(options.pop("max_tokens", 8192)),
            "messages": converted,
        }
        if system:
            payload["system"] = system
        payload.update(options.pop("request_overrides", {}))
        payload.update(options)

        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self._post(payload)
                response.raise_for_status()
                data = response.json()
                blocks = data.get("content", [])
                text = "".join(
                    str(block.get("text", ""))
                    for block in blocks
                    if isinstance(block, dict) and block.get("type") == "text"
                ).strip()
                if not text:
                    raise RuntimeError("Anthropic-compatible response contained no text")
                usage = data.get("usage", {}) or {}
                prompt_tokens = int(usage.get("input_tokens", 0) or 0)
                completion_tokens = int(usage.get("output_tokens", 0) or 0)
                self.usage.record(
                    tier,
                    UsageSample(
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        total_tokens=prompt_tokens + completion_tokens,
                    ),
                    stage=stage,
                )
                return text
            except (httpx.HTTPError, ValueError, RuntimeError) as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    break
                time.sleep(min(2**attempt, 8))
        raise RuntimeError(f"Anthropic-compatible request failed: {last_error}") from last_error
