"""Provider factory for stage-specific model roles."""

from __future__ import annotations

from ..config import Config, LLMConfig
from .base import LLMClient
from .policy_fallback import ContentPolicyFallbackClient

_STAGE_ROLES = ("translate", "factual_audit", "chinese_audit", "repair", "validation")


def build_client(config: Config, role: str = "translate") -> LLMClient:
    provider_name = getattr(config.roles, role)
    primary = build_client_from_llm(config.providers[provider_name])
    fallback_name = config.roles.content_policy_fallback
    if fallback_name is None:
        return primary
    fallback = build_client_from_llm(config.providers[fallback_name])
    return ContentPolicyFallbackClient(primary, fallback)


def build_clients(config: Config) -> dict[str, LLMClient]:
    """Build each named transport once and map logical roles to it."""
    named = {
        name: build_client_from_llm(provider_config)
        for name, provider_config in config.providers.items()
    }
    fallback_name = config.roles.content_policy_fallback
    fallback = named[fallback_name] if fallback_name is not None else None
    wrapped: dict[tuple[int, int], LLMClient] = {}
    result: dict[str, LLMClient] = {}
    for role in _STAGE_ROLES:
        primary = named[getattr(config.roles, role)]
        if fallback is None:
            result[role] = primary
            continue
        key = (id(primary), id(fallback))
        if key not in wrapped:
            wrapped[key] = ContentPolicyFallbackClient(primary, fallback)
        result[role] = wrapped[key]
    return result


def build_client_from_llm(llm: LLMConfig) -> LLMClient:
    provider = llm.provider.lower()
    if provider == "openai-compatible":
        from .providers.openai_compatible import OpenAICompatibleClient

        return OpenAICompatibleClient(llm)
    if provider == "anthropic-compatible":
        from .providers.anthropic_compatible import AnthropicCompatibleClient

        return AnthropicCompatibleClient(llm)
    if provider == "agy":
        from .providers.agy import AgyClient

        return AgyClient(llm)
    if provider == "codex-cli":
        from .providers.codex_cli import CodexCLIClient

        return CodexCLIClient(llm)
    if provider == "fake":
        from .providers.fake import FakeClient

        return FakeClient()
    raise ValueError(f"unknown LLM provider: {llm.provider}")
