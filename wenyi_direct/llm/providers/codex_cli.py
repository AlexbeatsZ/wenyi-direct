"""Ephemeral, read-only ``codex exec`` adapter for any Wenyi Direct role."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional

from ...config import LLMConfig, TierConfig
from ..base import LLMClient, Messages
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


class CodexCLIClient(LLMClient):
    """Launch an ephemeral, read-only Codex agent for each review request."""

    def __init__(self, cfg: LLMConfig) -> None:
        super().__init__()
        self.command = cfg.command or "codex"
        self.cwd = str(Path(cfg.cwd).expanduser()) if cfg.cwd else str(Path.cwd())
        if not Path(self.cwd).is_dir():
            raise ValueError(f"codex-cli provider 的 cwd 不是现有目录：{self.cwd}")
        self.timeout = max(1, int(cfg.timeout))
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
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"找不到 Codex CLI：{self.command!r}；请先安装并确认其位于 PATH"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"Codex CLI 调用在 {self.timeout} 秒后超时") from exc

        stdout = (result.stdout or "").strip()
        stderr = (result.stderr or "").strip()
        if result.returncode != 0:
            detail = stderr or stdout or "无错误输出"
            raise RuntimeError(f"Codex CLI 退出码 {result.returncode}：{detail}")
        if not stdout:
            raise RuntimeError(f"Codex CLI 未返回审校文本：{stderr or '无错误输出'}")

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
