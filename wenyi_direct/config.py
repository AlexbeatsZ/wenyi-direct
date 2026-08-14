"""Typed YAML configuration for Wenyi Direct."""

from __future__ import annotations

import os
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


MODEL_ROLE_NAMES = (
    "translate",
    "factual_audit",
    "chinese_audit",
    "repair",
    "validation",
    "content_policy_fallback",
)

MODEL_ROLE_ALIASES: dict[str, tuple[str, ...]] = {
    "translate": ("translate",),
    "translation": ("translate",),
    "factual-audit": ("factual_audit",),
    "factual_audit": ("factual_audit",),
    "chinese-audit": ("chinese_audit",),
    "chinese_audit": ("chinese_audit",),
    "repair": ("repair",),
    "validation": ("validation",),
    "fallback": ("content_policy_fallback",),
    "content-policy-fallback": ("content_policy_fallback",),
    "content_policy_fallback": ("content_policy_fallback",),
    "audit": ("factual_audit", "chinese_audit"),
    "all": ("translate", "factual_audit", "chinese_audit", "repair", "validation"),
}


class ModelRegistry(BaseModel):
    """User-level named model catalog and default logical-role routing."""

    model_config = ConfigDict(extra="forbid")

    providers: dict[str, LLMConfig] = Field(default_factory=dict)
    roles: ModelRoles = Field(default_factory=ModelRoles)

    @model_validator(mode="after")
    def validate_roles(self) -> "ModelRegistry":
        if not self.providers:
            raise ValueError("models.providers must define at least one model")
        for role, provider_name in self.roles.model_dump().items():
            if provider_name is not None and provider_name not in self.providers:
                raise ValueError(
                    f"models.roles.{role} references unknown model {provider_name!r}"
                )
        return self


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
        if self.max_write_chars > self.max_read_chars:
            raise ValueError("window.max_write_chars cannot exceed max_read_chars")
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
        target = self.target_lang.casefold().replace("_", "-")
        if target not in {"zh", "zh-cn", "zh-hans"}:
            raise ValueError(
                "Wenyi Direct currently emits Simplified Chinese only; "
                "target_lang must be zh-CN"
            )
        self.target_lang = "zh-CN"
        if not self.providers:
            raise ValueError("providers must define at least one model provider")
        for role, provider_name in self.roles.model_dump().items():
            if provider_name is None:
                continue
            if provider_name not in self.providers:
                raise ValueError(f"roles.{role} references unknown provider {provider_name!r}")
        return self

    @classmethod
    def load(
        cls,
        path: str | Path = "config.yaml",
        *,
        models_path: str | Path | None = None,
        model_overrides: list[str] | tuple[str, ...] = (),
    ) -> "Config":
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ValueError("project config must be a YAML object")
        registry = load_model_registry(models_path)
        local_providers = raw.get("providers") or {}
        local_roles = raw.get("roles")
        if local_providers:
            # A legacy/self-contained project config keeps the historical role
            # defaults instead of silently inheriting a user's global routing.
            roles = ModelRoles.model_validate(local_roles or {})
            providers = dict(local_providers)
            selected_names = {name for name in roles.model_dump().values() if name is not None}
            for override in model_overrides:
                if "=" in override:
                    selected_names.add(override.split("=", 1)[1].strip())
            for name in selected_names:
                if name not in providers and name in registry.providers:
                    providers[name] = registry.providers[name]
        else:
            providers = dict(registry.providers)
            role_values = registry.roles.model_dump()
            role_values.update(local_roles or {})
            roles = ModelRoles.model_validate(role_values)
        raw["providers"] = providers
        raw["roles"] = roles
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
        config = cls.model_validate(raw)
        return apply_model_overrides(config, model_overrides)

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

# Models and default stage routing live in the user-level models.yaml.
# Keep only book/project-specific overrides here.

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


DEFAULT_MODELS_CONFIG = """\
providers:
  codex_sol:
    provider: codex-cli
    command: codex
    timeout: 1200
    tiers:
      strong:
        model: gpt-5.6-sol
        options:
          reasoning_effort: high
  deepseek_pro:
    provider: openai-compatible
    base_url: https://api.deepseek.com
    api_key_env: DEEPSEEK_API_KEY
    reasoning_style: deepseek
    timeout: 1200
    tiers:
      strong:
        model: deepseek-v4-pro
        options:
          thinking: true
          reasoning_effort: max
roles:
  translate: codex_sol
  factual_audit: codex_sol
  chinese_audit: codex_sol
  repair: codex_sol
  validation: codex_sol
"""


def default_models_path() -> Path:
    """Return the platform user-level model registry path."""
    configured = os.environ.get("WENYI_DIRECT_MODELS")
    if configured:
        return Path(configured).expanduser()
    if os.name == "nt" and os.environ.get("APPDATA"):
        root = Path(os.environ["APPDATA"])
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / "wenyi-direct" / "models.yaml"


def resolve_models_path(path: str | Path | None = None) -> Path:
    return Path(path).expanduser() if path is not None else default_models_path()


def _default_registry_raw() -> dict[str, Any]:
    raw = yaml.safe_load(DEFAULT_MODELS_CONFIG) or {}
    assert isinstance(raw, dict)
    return raw


def load_model_registry(path: str | Path | None = None) -> ModelRegistry:
    """Load built-in models plus optional user overrides from one central file."""
    raw = _default_registry_raw()
    target = resolve_models_path(path)
    if target.exists():
        user_raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
        if not isinstance(user_raw, dict):
            raise ValueError(f"model registry must be a YAML object: {target}")
        unknown = set(user_raw) - {"providers", "roles"}
        if unknown:
            raise ValueError(f"unknown model registry keys: {sorted(unknown)}")
        providers = dict(raw.get("providers") or {})
        providers.update(user_raw.get("providers") or {})
        roles = dict(raw.get("roles") or {})
        roles.update(user_raw.get("roles") or {})
        raw = {"providers": providers, "roles": roles}
    return ModelRegistry.model_validate(raw)


def create_default_models_file(path: str | Path | None = None) -> bool:
    target = resolve_models_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with target.open("x", encoding="utf-8", newline="\n") as file:
            file.write(DEFAULT_MODELS_CONFIG)
    except FileExistsError:
        return False
    return True


def save_model_registry(registry: ModelRegistry, path: str | Path | None = None) -> Path:
    """Atomically save the central registry without embedding API-key values."""
    target = resolve_models_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = registry.model_dump(exclude_none=True)
    temporary = target.with_suffix(f"{target.suffix}.tmp")
    temporary.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
        newline="\n",
    )
    temporary.replace(target)
    return target


def expand_model_role(name: str) -> tuple[str, ...]:
    normalized = name.strip().casefold().replace(" ", "-")
    try:
        return MODEL_ROLE_ALIASES[normalized]
    except KeyError as error:
        choices = ", ".join(MODEL_ROLE_ALIASES)
        raise ValueError(f"unknown model role {name!r}; choose one of: {choices}") from error


def apply_model_overrides(
    config: Config,
    overrides: list[str] | tuple[str, ...],
) -> Config:
    if not overrides:
        return config
    role_values = config.roles.model_dump()
    for override in overrides:
        if "=" not in override:
            raise ValueError(
                f"model override {override!r} must use ROLE=MODEL, for example audit=deepseek_pro"
            )
        role_name, model_name = (part.strip() for part in override.split("=", 1))
        if not model_name:
            raise ValueError(f"model override {override!r} has an empty model name")
        if model_name not in config.providers:
            raise ValueError(f"model override references unknown model {model_name!r}")
        for role in expand_model_role(role_name):
            role_values[role] = model_name
    values = config.model_dump()
    values["roles"] = role_values
    return Config.model_validate(values)
