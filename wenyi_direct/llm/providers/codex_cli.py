"""Ephemeral, read-only ``codex exec`` adapter for any Wenyi Direct role."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Optional

from ...config import LLMConfig, TierConfig
from ..base import LLMClient, Messages, TransientProviderError
from ..tiers import resolve_tier
from ..usage import UsageSample

_DEFAULT_TIERS = {
    "strong": TierConfig(model="gpt-5.6-sol", options={"reasoning_effort": "high"}),
    "cheap": TierConfig(model="gpt-5.6-sol", options={"reasoning_effort": "high"}),
    "fast": TierConfig(model="gpt-5.6-sol", options={"reasoning_effort": "medium"}),
}
_ROLE_LABELS = {
    "system": "System instructions",
    "user": "Task input",
    "assistant": "Previous assistant response",
    "tool": "Tool result",
}
_TRANSIENT_CLI_ERROR_MARKERS = (
    "429",
    "502",
    "503",
    "504",
    "connection aborted",
    "connection refused",
    "connection reset",
    "error sending request",
    "internal server error",
    "quota exceeded",
    "rate limit",
    "resource exhausted",
    "service unavailable",
    "stream disconnected",
    "temporarily unavailable",
    "timed out",
    "timeout",
    "too many requests",
)


def format_codex_prompt(messages: Messages, *, json_mode: bool = False) -> str:
    sections = [
        "This is a self-contained literary translation task. Work only from the "
        "text in this prompt. Do not use tools, inspect files, browse, run commands, "
        "or modify anything. Return only the requested answer."
    ]
    for message in messages:
        content = str(message.get("content", "") or "").strip()
        if not content:
            continue
        role = str(message.get("role", "user") or "user").lower()
        sections.append(f"{_ROLE_LABELS.get(role, role.title())}:\n{content}")
    if json_mode:
        sections.append(
            "Output constraint:\nReturn exactly one valid JSON value matching the "
            "requested schema. Do not use Markdown fences or explanatory text."
        )
    return "\n\n".join(sections)


def _estimate_tokens(text: str) -> int:
    return max(1, round(len(text) / 4)) if text else 0


def _is_transient_cli_error(detail: str) -> bool:
    lowered = detail.casefold()
    return any(marker in lowered for marker in _TRANSIENT_CLI_ERROR_MARKERS)


class CodexCLIClient(LLMClient):
    """Launch an ephemeral, read-only Codex agent for each review request."""

    def __init__(self, cfg: LLMConfig) -> None:
        super().__init__()
        self.command = cfg.command or "codex"
        self.cwd = str(Path(cfg.cwd).expanduser()) if cfg.cwd else str(Path.cwd())
        if not Path(self.cwd).is_dir():
            raise ValueError(f"codex-cli provider 的 cwd 不是现有目录：{self.cwd}")
        self.timeout = max(1, int(cfg.timeout))
        self.max_retries = max(0, int(cfg.max_retries))
        self.tiers = {**_DEFAULT_TIERS, **cfg.tiers}

    def complete(
        self,
        messages: Messages,
        *,
        tier: str = "strong",
        json_mode: bool = False,
        max_tokens: Optional[int] = None,
        stage: Optional[str] = None,
    ) -> str:
        del max_tokens  # codex exec currently has no one-shot output-token flag.
        tier_config = resolve_tier(self.tiers, tier)
        model = tier_config.model
        if not model:
            raise ValueError(f"codex-cli provider 的 {tier} 档未配置 model")
        effort = str(tier_config.options.get("reasoning_effort", "high") or "high")
        if effort not in {"low", "medium", "high", "xhigh", "max", "ultra"}:
            raise ValueError(f"codex-cli reasoning_effort 无效：{effort}")
        prompt = format_codex_prompt(messages, json_mode=json_mode)
        if not prompt.strip():
            raise ValueError("codex-cli provider 收到空提示词")

        args = [
            self.command,
            "exec",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--model",
            model,
            "--config",
            f'model_reasoning_effort="{effort}"',
            "--color",
            "never",
            "-",
        ]
        try:
            for retry in range(self.max_retries + 1):
                try:
                    result = subprocess.run(
                        args,
                        cwd=self.cwd,
                        input=prompt,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=self.timeout,
                        check=False,
                    )
                except subprocess.TimeoutExpired:
                    if retry >= self.max_retries:
                        raise
                    time.sleep(min(2**retry, 8))
                    continue
                stdout = (result.stdout or "").strip()
                stderr = (result.stderr or "").strip()
                detail = stderr or stdout or "无错误输出"
                empty_success = result.returncode == 0 and not stdout
                if (
                    (result.returncode == 0 and not empty_success)
                    or retry >= self.max_retries
                    or (not empty_success and not _is_transient_cli_error(detail))
                ):
                    break
                time.sleep(min(2**retry, 8))
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"找不到 Codex CLI：{self.command!r}；请先安装并确认其位于 PATH"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise TransientProviderError(
                f"Codex CLI 调用在 {self.timeout} 秒后超时，且已耗尽重试"
            ) from exc

        if result.returncode != 0:
            error_type = (
                TransientProviderError
                if _is_transient_cli_error(detail)
                else RuntimeError
            )
            raise error_type(f"Codex CLI 退出码 {result.returncode}：{detail}")
        if not stdout:
            raise TransientProviderError(
                f"Codex CLI 耗尽重试后仍未返回审校文本：{stderr or '无错误输出'}"
            )

        self.usage.record(
            tier,
            UsageSample(
                prompt_tokens=_estimate_tokens(prompt),
                completion_tokens=_estimate_tokens(stdout),
                total_tokens=_estimate_tokens(prompt) + _estimate_tokens(stdout),
            ),
            stage=stage,
        )
        return stdout
