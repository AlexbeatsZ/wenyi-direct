"""LLM provider 的稳定抽象接口。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional

from .json_parser import JSONParseError, parse_json_loose
from .usage import UsageTracker

Messages = list[dict[str, str]]

_JSON_PARSE_ATTEMPTS = 2
_JSON_RETRY_INSTRUCTION = (
    "上一轮返回的 JSON 无效。请重新生成完整且严格合法的 JSON："
    "必须匹配原请求 schema，完整闭合所有字符串、数组和对象；"
    "不得输出 Markdown、解释或额外字段；detail、suggestion、reason 等说明字段保持简洁。"
)


class ContentPolicyError(RuntimeError):
    """Provider 明确拒绝了包含敏感内容的请求。"""


class LLMClient(ABC):
    """所有 provider 实现此接口。"""

    def __init__(self) -> None:
        """为 provider 初始化独立的线程安全用量统计器。"""
        self.usage = UsageTracker()

    def usage_summary(self) -> dict[str, Any]:
        """返回累计 token 用量快照（totals + by_tier + cache_hit_rate）。"""
        return self.usage.summary()

    @abstractmethod
    def complete(
        self,
        messages: Messages,
        *,
        tier: str = "strong",
        json_mode: bool = False,
        max_tokens: Optional[int] = None,
        stage: Optional[str] = None,
    ) -> str:
        """返回模型回复的纯文本；stage 仅用于用量归因。"""
        raise NotImplementedError

    def complete_json(
        self,
        messages: Messages,
        *,
        tier: str = "strong",
        max_tokens: Optional[int] = None,
        stage: Optional[str] = None,
    ) -> Any:
        """要求 JSON 输出并解析；坏 JSON 用新请求带强化约束有限重试。"""
        last_error: JSONParseError | None = None
        for attempt in range(_JSON_PARSE_ATTEMPTS):
            attempt_messages = [dict(message) for message in messages]
            if attempt:
                attempt_messages.append(
                    {"role": "user", "content": _JSON_RETRY_INSTRUCTION}
                )
            text = self.complete(
                attempt_messages,
                tier=tier,
                json_mode=True,
                max_tokens=max_tokens,
                stage=stage,
            )
            try:
                return parse_json_loose(text)
            except JSONParseError as error:
                last_error = error
        assert last_error is not None
        raise last_error
