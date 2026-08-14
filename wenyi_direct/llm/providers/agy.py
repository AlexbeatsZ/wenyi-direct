"""通过本机 Antigravity ``agy`` CLI 完成普通非交互提示。"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from ...config import LLMConfig, TierConfig
from ..base import ContentPolicyError, LLMClient, Messages, TransientProviderError
from ..tiers import resolve_tier
from ..usage import UsageSample
from ._errors import is_explicit_timeout_error

_ANSI_RE = re.compile(r"\x1b(?:\][^\x07]*(?:\x07|\x1b\\)|\[[0-?]*[ -/]*[@-~])")
_DEFAULT_TIERS = {
    "strong": TierConfig(model="Gemini 3.1 Pro (High)"),
    "cheap": TierConfig(model="Gemini 3.6 Flash (Medium)"),
    "fast": TierConfig(model="Gemini 3.6 Flash (Low)"),
}
_MODEL_DISPLAY_NAMES = {
    "gemini-3.1-pro-low": "Gemini 3.1 Pro (Low)",
    "gemini-3.1-pro-high": "Gemini 3.1 Pro (High)",
    "gemini-3.6-flash-medium": "Gemini 3.6 Flash (Medium)",
    "gemini-3.6-flash-low": "Gemini 3.6 Flash (Low)",
    "gemini-3.6-flash-high": "Gemini 3.6 Flash (High)",
    "gemini-3.5-flash-medium": "Gemini 3.5 Flash (Medium)",
    "gemini-3.5-flash-low": "Gemini 3.5 Flash (Low)",
    "gemini-3.5-flash-high": "Gemini 3.5 Flash (High)",
}
_MODEL_ALIASES = {
    "gemini-3.1-pro": "gemini-3.1-pro-low",
    "gemini-3.6-flash": "gemini-3.6-flash-medium",
    "gemini-3.5-flash": "gemini-3.5-flash-medium",
}
_DISPLAY_NAME_TO_MODEL = {
    display.casefold(): model for model, display in _MODEL_DISPLAY_NAMES.items()
}
_SHORT_ID_ATTEMPTS = 2
_TEXT_RESPONSE_ATTEMPTS = 3
_TRANSIENT_CLI_ERROR_MARKERS = (
    "connection aborted",
    "connection refused",
    "connection reset",
    "context canceled",
    "eof",
    "http status 429",
    "http status 500",
    "http status 502",
    "http status 503",
    "http status 504",
    "quota exceeded",
    "rate limit",
    "resource exhausted",
    "service unavailable",
    "temporarily unavailable",
    "too many requests",
)
_ROLE_LABELS = {
    "system": "System",
    "user": "User",
    "assistant": "Assistant",
    "tool": "Tool result",
}
_REQUEST_FILE_NAME = "request.txt"
_FILE_PROMPT = (
    f"Read the UTF-8 file {_REQUEST_FILE_NAME} in the current workspace. "
    "Follow its instructions exactly. Do not read any other file. "
    "Return the requested answer in the response text."
)
_JSON_REQUIREMENT = (
    "Output requirement:\n"
    "Return only one valid JSON value matching the requested schema. "
    "Do not use Markdown fences or add explanatory text."
)


@contextmanager
def _temporary_request() -> Iterator[tuple[str, Path]]:
    """Keep Windows Agy child-process directory locks from failing a paid call.

    Agy 1.1 can return before a short-lived child releases its working directory.
    The request is blanked first, and Python may leave only an empty directory for
    the OS to release later instead of turning a valid model response into failure.
    """
    with tempfile.TemporaryDirectory(
        prefix="kamyi-agy-", ignore_cleanup_errors=True
    ) as request_dir:
        request_path = Path(request_dir) / _REQUEST_FILE_NAME
        try:
            yield request_dir, request_path
        finally:
            try:
                request_path.write_text("", encoding="utf-8")
            except OSError:
                pass


def format_agy_prompt(messages: Messages, *, json_mode: bool = False) -> str:
    """把多角色消息折叠为 agy ``--print`` 接受的一条普通提示词。

    agy 1.0.x 没有单次 system prompt 参数，因此 ``System`` 只是明确标注的
    普通提示词前缀，不冒充原生 system 消息。
    """
    sections: list[str] = []
    for message in messages:
        content = str(message.get("content", "")).strip()
        if not content:
            continue
        role = str(message.get("role", "user")).lower()
        label = _ROLE_LABELS.get(role, role.title() or "User")
        sections.append(f"{label}:\n{content}")
    if json_mode:
        sections.append(_JSON_REQUIREMENT)
    return "\n\n".join(sections).strip()


def _estimate_tokens(text: str) -> int:
    """agy 不返回 usage；按字符数给现有统计器提供明确的近似值。"""
    return max(1, round(len(text) / 4)) if text else 0


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text).strip()


def _model_candidates(model: str) -> list[str]:
    """优先返回 agy 1.1 短 ID，并为 agy 1.0 保留显示名回退。"""
    key = model.casefold()
    short_id = _MODEL_ALIASES.get(key, key)
    if short_id in _MODEL_DISPLAY_NAMES:
        return [short_id, _MODEL_DISPLAY_NAMES[short_id]]
    if key in _DISPLAY_NAME_TO_MODEL:
        short_id = _DISPLAY_NAME_TO_MODEL[key]
        return [short_id, _MODEL_DISPLAY_NAMES[short_id]]
    return [model]


def _is_unknown_model_error(detail: str) -> bool:
    """只对 agy 明确报告的模型名不识别错误启用兼容回退。"""
    lowered = detail.casefold()
    return "model" in lowered and "not recognized as a known model" in lowered


def _is_tool_permission_denial(detail: str) -> bool:
    """识别 agy headless 会话把文本任务误转成工具调用后的自动拒绝。"""
    lowered = detail.casefold()
    return (
        "tool required" in lowered
        and "permission" in lowered
        and ("auto-denied" in lowered or "headless mode" in lowered)
    )


def _is_content_policy_rejection(detail: str) -> bool:
    """识别 agy/Gemini 以普通文本返回的内容策略拒绝。"""
    lowered = detail.casefold()
    return (
        "prompt could not be submitted" in lowered
        and (
            "sensitive words" in lowered
            or "prohibited use policy" in lowered
        )
    )


def _is_transient_cli_error(detail: str) -> bool:
    """Retry only failures that Agy reports as transient transport/runtime faults."""
    lowered = detail.casefold()
    return is_explicit_timeout_error(detail) or any(
        marker in lowered for marker in _TRANSIENT_CLI_ERROR_MARKERS
    )


class AgyClient(LLMClient):
    """每次以全新 ``agy --print`` 调用执行请求的 CLI provider。"""

    def __init__(self, cfg: LLMConfig) -> None:
        super().__init__()
        self.command = cfg.command or "agy"
        self.cwd = str(Path(cfg.cwd).expanduser()) if cfg.cwd else None
        if self.cwd and not Path(self.cwd).is_dir():
            raise ValueError(f"agy provider 的 cwd 不是现有目录：{self.cwd}")
        self.env: dict[str, str] | None = None
        if cfg.isolate_user_config:
            if not self.cwd:
                raise ValueError("agy provider 启用用户配置隔离时必须配置 cwd")
            runtime_root = Path(self.cwd)
            isolated_home = runtime_root / "home"
            isolated_local_app_data = runtime_root / "local-app-data"
            isolated_home.mkdir(parents=True, exist_ok=True)
            isolated_local_app_data.mkdir(parents=True, exist_ok=True)
            self.env = os.environ.copy()
            self.env["HOME"] = str(isolated_home)
            if os.name == "nt":
                self.env["USERPROFILE"] = str(isolated_home)
                self.env["LOCALAPPDATA"] = str(isolated_local_app_data)
        self.timeout = max(1, int(cfg.timeout))
        self.max_retries = max(0, int(cfg.max_retries))
        self.tiers = {**_DEFAULT_TIERS, **cfg.tiers}
        self._resolved_models: dict[str, str] = {}

    def complete(
        self,
        messages: Messages,
        *,
        tier: str = "strong",
        json_mode: bool = False,
        max_tokens: Optional[int] = None,
        stage: Optional[str] = None,
    ) -> str:
        """把消息作为普通提示传给独立的 agy print 会话并返回纯文本。"""
        del max_tokens  # agy 1.0.x 的 print 模式没有输出 token 上限参数。
        tier_config = resolve_tier(self.tiers, tier)
        model = tier_config.model
        if not model:
            raise ValueError(f"agy provider 的 {tier} 档未配置 model")

        prompt = format_agy_prompt(messages, json_mode=json_mode)
        if not prompt:
            raise ValueError("agy provider 收到空提示词")

        model_key = model.casefold()
        candidates = (
            [self._resolved_models[model_key]]
            if model_key in self._resolved_models
            else _model_candidates(model)
        )
        try:
            with _temporary_request() as (request_dir, request_path):
                request_path.write_text(prompt, encoding="utf-8", newline="\n")
                completed = False
                for index, candidate in enumerate(candidates):
                    attempts = (
                        _SHORT_ID_ATTEMPTS
                        if candidate in _MODEL_DISPLAY_NAMES
                        else 1
                    )
                    for attempt in range(attempts):
                        unknown_model = False
                        for response_attempt in range(_TEXT_RESPONSE_ATTEMPTS):
                            request_instruction = _FILE_PROMPT
                            if response_attempt:
                                request_instruction += (
                                    " Your previous attempt did not return the requested "
                                    "response. Read the request file again and answer now."
                                )
                            args = [
                                self.command,
                                "--model",
                                candidate,
                                "--mode",
                                "plan",
                                "--sandbox",
                                "--dangerously-skip-permissions",
                                "--disable-slash-commands",
                                "--new-project",
                                "--print-timeout",
                                f"{self.timeout}s",
                                "--print",
                                request_instruction,
                            ]
                            for retry in range(self.max_retries + 1):
                                try:
                                    result = subprocess.run(
                                        args,
                                        cwd=request_dir,
                                        env=self.env,
                                        capture_output=True,
                                        text=True,
                                        encoding="utf-8",
                                        errors="replace",
                                        timeout=self.timeout + 5,
                                        check=False,
                                    )
                                except subprocess.TimeoutExpired:
                                    if retry >= self.max_retries:
                                        raise
                                    time.sleep(min(2**retry, 8))
                                    continue
                                stdout = _strip_ansi(result.stdout or "")
                                stderr = _strip_ansi(result.stderr or "")
                                detail = stderr or stdout or "无错误输出"
                                if (
                                    result.returncode == 0
                                    or retry >= self.max_retries
                                    or not _is_transient_cli_error(detail)
                                ):
                                    break
                                time.sleep(min(2**retry, 8))
                            if result.returncode == 0:
                                if _is_content_policy_rejection(detail):
                                    if response_attempt + 1 < _TEXT_RESPONSE_ATTEMPTS:
                                        continue
                                    raise ContentPolicyError(
                                        "agy/Gemini 连续拒绝当前提示的内容策略检查"
                                    )
                                if _is_tool_permission_denial(detail):
                                    if response_attempt + 1 < _TEXT_RESPONSE_ATTEMPTS:
                                        continue
                                    raise TransientProviderError(
                                        "agy CLI 连续把纯文本任务误判为工具调用；"
                                        "已拒绝授权并耗尽同供应商重试"
                                    )
                                if not stdout and not stderr:
                                    if response_attempt + 1 < _TEXT_RESPONSE_ATTEMPTS:
                                        continue
                                    raise TransientProviderError(
                                        "agy CLI 连续成功退出但未返回任何响应文本"
                                    )
                                self._resolved_models[model_key] = candidate
                                completed = True
                                break
                            unknown_model = _is_unknown_model_error(detail)
                            break
                        if completed:
                            break
                        if unknown_model and attempt + 1 < attempts:
                            continue
                        has_fallback = index + 1 < len(candidates)
                        if unknown_model and has_fallback:
                            break
                        error_type = (
                            TransientProviderError
                            if _is_transient_cli_error(detail)
                            else RuntimeError
                        )
                        raise error_type(f"agy CLI 退出码 {result.returncode}：{detail}")
                    if completed:
                        break
        except FileNotFoundError as exc:
            if getattr(exc, "winerror", None) == 206:
                raise RuntimeError(
                    "agy CLI 启动参数超过 Windows 命令行上限；"
                    "业务提示已通过临时文件传输，因此请检查 command 或环境路径"
                ) from exc
            raise RuntimeError(
                f"找不到 agy CLI：{self.command!r}；请先安装并确认其位于 PATH"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise TransientProviderError(
                f"agy CLI 调用在 {self.timeout} 秒后超时，且已耗尽重试"
            ) from exc

        if result.returncode != 0:
            error_type = (
                TransientProviderError
                if _is_transient_cli_error(detail)
                else RuntimeError
            )
            raise error_type(f"agy CLI 退出码 {result.returncode}：{detail}")

        text = stdout or stderr
        self.usage.record(
            tier,
            UsageSample(
                prompt_tokens=_estimate_tokens(prompt),
                completion_tokens=_estimate_tokens(text),
                total_tokens=_estimate_tokens(prompt) + _estimate_tokens(text),
            ),
            stage=stage,
        )
        return text
