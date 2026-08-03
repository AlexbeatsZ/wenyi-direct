"""Typed YAML configuration for Wenyi Direct."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class TierConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str | None = None
    options: dict[str, Any] = Field(default_factory=dict)


ReasoningStyle = Literal["none", "deepseek", "openai", "openrouter"]


class LLMConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal[
        "codex-cli", "agy", "openai-compatible", "anthropic-compatible", "fake"
    ] = "codex-cli"
    base_url: str | None = None
    api_key_env: str | None = None
    command: str | None = None
    cwd: str | None = None
    isolate_user_config: bool = False
    reasoning_style: ReasoningStyle = "none"
    timeout: int = 1200
    max_retries: int = 3
    tiers: dict[str, TierConfig] = Field(default_factory=dict)


class ModelRoles(BaseModel):
    model_config = ConfigDict(extra="forbid")

    translate: str = "default"
    factual_audit: str = "default"
    chinese_audit: str = "default"
    repair: str = "default"
    validation: str = "default"
    content_policy_fallback: str | None = None


class WindowConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_read_chars: int = 60_000
    max_write_chars: int = 18_000
    source_halo_chars: int = 8_000
    past_context_chars: int = 4_000

    @model_validator(mode="after")
    def validate_budgets(self) -> "WindowConfig":
        for name in (
            "max_read_chars",
            "max_write_chars",
            "source_halo_chars",
            "past_context_chars",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"window.{name} must be positive")
        return self


class PipelineConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    factual_audit: bool = True
    chinese_reader_audit: bool = True
    max_repair_attempts: int = 2
    repair_context_segments: int = 2
    translation_tier: str = "strong"
    audit_tier: str = "strong"
    repair_tier: str = "strong"
    validation_tier: str = "strong"


class SegmentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_chars_per_segment: int = 0


class OutputConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mono: bool = True
    bilingual: bool = False
    bilingual_order: Literal["target_first", "source_first"] = "target_first"
    bilingual_preserve_source_style: bool = False
    about_page: bool = True


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_lang: str = "auto"
    target_lang: str = "zh-CN"
    state_dir: str = "state"
    output_dir: str = "outputs"
    providers: dict[str, LLMConfig] = Field(default_factory=dict)
    roles: ModelRoles = Field(default_factory=ModelRoles)
    window: WindowConfig = Field(default_factory=WindowConfig)
    pipeline: PipelineConfig = Field(default_factory=PipelineConfig)
    segment: SegmentConfig = Field(default_factory=SegmentConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)
    terminology_file: str | None = None

    @model_validator(mode="after")
    def validate_roles(self) -> "Config":
        if not self.providers:
            raise ValueError("providers must define at least one model provider")
        for role, provider_name in self.roles.model_dump().items():
            if provider_name is None:
                continue
            if provider_name not in self.providers:
                raise ValueError(f"roles.{role} references unknown provider {provider_name!r}")
        return self

    @classmethod
    def load(cls, path: str | Path = "config.yaml") -> "Config":
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        language = raw.pop("language", {}) or {}
        paths = raw.pop("paths", {}) or {}
        raw["source_lang"] = language.get("source", raw.get("source_lang", "auto"))
        raw["target_lang"] = language.get("target", raw.get("target_lang", "zh-CN"))
        raw["state_dir"] = paths.get("state_dir", raw.get("state_dir", "state"))
        raw["output_dir"] = paths.get("output_dir", raw.get("output_dir", "outputs"))
        legacy_terms_file = raw.pop("hard_terms_file", None)
        raw["terminology_file"] = paths.get(
            "terminology_file",
            paths.get("hard_terms_file", raw.get("terminology_file", legacy_terms_file)),
        )
        return cls.model_validate(raw)

    @staticmethod
    def create_default_file(path: str | Path = "config.yaml") -> bool:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with target.open("x", encoding="utf-8", newline="\n") as file:
                file.write(DEFAULT_CONFIG)
        except FileExistsError:
            return False
        return True


DEFAULT_CONFIG = """\
language:
  source: auto
  target: zh-CN

providers:
  default:
    provider: codex-cli
    command: codex
    timeout: 1200
    tiers:
      strong:
        model: gpt-5.6-sol
        options:
          reasoning_effort: high

roles:
  translate: default
  factual_audit: default
  chinese_audit: default
  repair: default
  validation: default

window:
  max_read_chars: 60000
  max_write_chars: 18000
  source_halo_chars: 8000
  past_context_chars: 4000

pipeline:
  factual_audit: true
  chinese_reader_audit: true
  max_repair_attempts: 2
  repair_context_segments: 2

segment:
  # 0 keeps author paragraphs intact. Set only for exceptionally long paragraphs.
  max_chars_per_segment: 0

paths:
  state_dir: state
  output_dir: outputs
  terminology_file: terminology.yaml

output:
  mono: true
  bilingual: false
  about_page: true
"""
