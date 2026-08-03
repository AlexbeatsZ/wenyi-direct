"""Provider factory for stage-specific model roles."""

from __future__ import annotations

from ..config import Config, LLMConfig
from .base import LLMClient


def build_client(config: Config, role: str = "translate") -> LLMClient:
    provider_name = getattr(config.roles, role)
    return build_client_from_llm(config.providers[provider_name])


def build_clients(config: Config) -> dict[str, LLMClient]:
    """Build each named transport once and map logical roles to it."""
    named = {
        name: build_client_from_llm(provider_config)
        for name, provider_config in config.providers.items()
    }
    return {
        role: named[provider_name]
        for role, provider_name in config.roles.model_dump().items()
    }


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
