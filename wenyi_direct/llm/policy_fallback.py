"""Configured fallback for policy refusals and exhausted transient failures."""

from __future__ import annotations

from typing import Any, Optional

from .base import ContentPolicyError, LLMClient, Messages, TransientProviderError
from .json_parser import JSONParseError


def _sum_totals(*summaries: dict[str, Any]) -> dict[str, int | float]:
    keys = (
        "calls",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "cache_hit_tokens",
        "cache_miss_tokens",
    )
    totals = {
        key: sum(int(summary.get("totals", {}).get(key, 0) or 0) for summary in summaries)
        for key in keys
    }
    cache_total = totals["cache_hit_tokens"] + totals["cache_miss_tokens"]
    totals["cache_hit_rate"] = (
        totals["cache_hit_tokens"] / cache_total if cache_total else 0.0
    )
    return totals


class ContentPolicyFallbackClient(LLMClient):
    """Route eligible primary failures to the explicitly configured fallback.

    The historical class/config name is retained for compatibility. Permanent
    authentication, configuration, alignment, and quality failures still surface.
    """

    def __init__(self, primary: LLMClient, fallback: LLMClient) -> None:
        super().__init__()
        self.primary = primary
        self.fallback = fallback
        self.fallback_events: list[dict[str, str | None]] = []
        self.transient_fallback_events: list[dict[str, str | None]] = []
        self.invalid_response_fallback_events: list[dict[str, str | None]] = []

    def _complete_fallback(
        self,
        messages: Messages,
        *,
        tier: str,
        json_mode: bool,
        max_tokens: Optional[int],
        stage: Optional[str],
    ) -> str:
        return self.fallback.complete(
            messages,
            tier=tier,
            json_mode=json_mode,
            max_tokens=max_tokens,
            stage=stage,
        )

    def complete(
        self,
        messages: Messages,
        *,
        tier: str = "strong",
        json_mode: bool = False,
        max_tokens: Optional[int] = None,
        stage: Optional[str] = None,
    ) -> str:
        try:
            return self.primary.complete(
                messages,
                tier=tier,
                json_mode=json_mode,
                max_tokens=max_tokens,
                stage=stage,
            )
        except ContentPolicyError as error:
            self.fallback_events.append({"stage": stage, "reason": str(error)})
            return self._complete_fallback(
                messages,
                tier=tier,
                json_mode=json_mode,
                max_tokens=max_tokens,
                stage=stage,
            )
        except TransientProviderError as error:
            self.transient_fallback_events.append(
                {"stage": stage, "reason": str(error)}
            )
            return self._complete_fallback(
                messages,
                tier=tier,
                json_mode=json_mode,
                max_tokens=max_tokens,
                stage=stage,
            )

    def complete_json(
        self,
        messages: Messages,
        *,
        tier: str = "strong",
        max_tokens: Optional[int] = None,
        stage: Optional[str] = None,
    ) -> Any:
        """Keep JSON parsing/retries on one provider before routing the request."""
        try:
            return self.primary.complete_json(
                messages,
                tier=tier,
                max_tokens=max_tokens,
                stage=stage,
            )
        except ContentPolicyError as error:
            self.fallback_events.append({"stage": stage, "reason": str(error)})
        except TransientProviderError as error:
            self.transient_fallback_events.append(
                {"stage": stage, "reason": str(error)}
            )
        except JSONParseError as error:
            self.invalid_response_fallback_events.append(
                {"stage": stage, "reason": str(error)}
            )
        return self.fallback.complete_json(
            messages,
            tier=tier,
            max_tokens=max_tokens,
            stage=stage,
        )

    def usage_summary(self) -> dict[str, Any]:
        primary = self.primary.usage_summary()
        fallback = self.fallback.usage_summary()
        return {
            "totals": _sum_totals(primary, fallback),
            "primary": primary,
            "content_policy_fallback": fallback,
            "content_policy_fallback_events": list(self.fallback_events),
            "transient_error_fallback_events": list(self.transient_fallback_events),
            "invalid_response_fallback_events": list(
                self.invalid_response_fallback_events
            ),
        }
