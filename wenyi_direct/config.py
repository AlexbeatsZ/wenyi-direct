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
TransportKind = Literal[
    "codex-cli", "agy", "openai-compatible", "anthropic-compatible", "fake"
]


class LLMConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: TransportKind = "codex-cli"
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


class NamedModelConfig(BaseModel):
    """A user-named model available through one route."""

    model_config = ConfigDict(extra="forbid")

    model: str
    options: dict[str, Any] = Field(default_factory=dict)


class RouteConfig(BaseModel):
    """A user-named way to reach one model service or CLI."""

    model_config = ConfigDict(extra="forbid")

    transport: TransportKind
    base_url: str | None = None
    api_key_env: str | None = None
    command: str | None = None
    cwd: str | None = None
    isolate_user_config: bool = False
    reasoning_style: ReasoningStyle = "none"
    timeout: int = 1200
    max_retries: int = 3
    models: dict[str, NamedModelConfig] = Field(default_factory=dict)


class RouteSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    route: str
    model: str


class RouteRoles(BaseModel):
    model_config = ConfigDict(extra="forbid")

    translate: RouteSelection
    factual_audit: RouteSelection
    chinese_audit: RouteSelection
    repair: RouteSelection
    validation: RouteSelection
    content_policy_fallback: RouteSelection | None = None


class ModelRegistry(BaseModel):
    """User-level route catalog, per-route models, and default role routing."""

    model_config = ConfigDict(extra="forbid")

    routes: dict[str, RouteConfig] = Field(default_factory=dict)
    roles: RouteRoles

    @model_validator(mode="after")
    def validate_roles(self) -> "ModelRegistry":
        if not self.routes:
            raise ValueError("models.routes must define at least one route")
        for route_name, route in self.routes.items():
            if not route.models:
                raise ValueError(f"models.routes.{route_name}.models must not be empty")
        for role in MODEL_ROLE_NAMES:
            selection = getattr(self.roles, role)
            if selection is not None:
                self.require_selection(selection, role=role)
        return self

    def require_selection(
        self, selection: RouteSelection, *, role: str | None = None
    ) -> NamedModelConfig:
        prefix = f"models.roles.{role}" if role else "model selection"
        route = self.routes.get(selection.route)
        if route is None:
            raise ValueError(f"{prefix} references unknown route {selection.route!r}")
        model = route.models.get(selection.model)
        if model is None:
            raise ValueError(
                f"{prefix} references unknown model {selection.model!r} "
                f"for route {selection.route!r}"
            )
        return model


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
        if "providers" in raw:
            raise ValueError(
                "project config must not define providers; define routes and models "
                "in the user-level models.yaml"
            )
        registry = load_model_registry(models_path)
        route_roles = merge_project_route_roles(registry, raw.get("roles") or {})
        route_roles = apply_route_overrides(registry, route_roles, model_overrides)
        providers, roles = materialize_registry(registry, route_roles)
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

# Routes, their models, and default stage routing live in the user-level models.yaml.
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
routes:
  codex:
    transport: codex-cli
    command: codex
    timeout: 1200
    models:
      gpt-5.6-sol-high:
        model: gpt-5.6-sol
        options:
          reasoning_effort: high
  deepseek-api:
    transport: openai-compatible
    base_url: https://api.deepseek.com
    api_key_env: DEEPSEEK_API_KEY
    reasoning_style: deepseek
    timeout: 1200
    models:
      deepseek-v4-pro-max:
        model: deepseek-v4-pro
        options:
          thinking: true
          reasoning_effort: max
      deepseek-v4-flash:
        model: deepseek-v4-flash
roles:
  translate: {route: codex, model: gpt-5.6-sol-high}
  factual_audit: {route: codex, model: gpt-5.6-sol-high}
  chinese_audit: {route: codex, model: gpt-5.6-sol-high}
  repair: {route: codex, model: gpt-5.6-sol-high}
  validation: {route: codex, model: gpt-5.6-sol-high}
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
    """Load named routes, their named models, and default role selections."""
    target = resolve_models_path(path)
    if target.exists():
        raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ValueError(f"model registry must be a YAML object: {target}")
    else:
        raw = _default_registry_raw()
    unknown = set(raw) - {"routes", "roles"}
    if unknown:
        raise ValueError(
            "model registry accepts only routes and roles; "
            f"unknown keys: {sorted(unknown)}"
        )
    if "routes" not in raw:
        raise ValueError("model registry must define routes")
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


def resolve_route_selection(
    registry: ModelRegistry, route_name: str, model_name: str
) -> RouteSelection:
    selection = RouteSelection(route=route_name, model=model_name)
    registry.require_selection(selection)
    return selection


def parse_model_override(
    registry: ModelRegistry, override: str
) -> tuple[tuple[str, ...], RouteSelection]:
    if "=" not in override:
        raise ValueError(
            f"model override {override!r} must use ROLE=ROUTE/MODEL, "
            "for example audit=deepseek-api/deepseek-v4-flash"
        )
    role_name, reference = (part.strip() for part in override.split("=", 1))
    if "/" not in reference:
        raise ValueError(
            f"model override {override!r} must use ROLE=ROUTE/MODEL, "
            "for example audit=deepseek-api/deepseek-v4-flash"
        )
    route_name, model_name = (part.strip() for part in reference.split("/", 1))
    if not route_name or not model_name:
        raise ValueError(f"model override {override!r} has an empty route/model")
    selection = resolve_route_selection(registry, route_name, model_name)
    return expand_model_role(role_name), selection


def apply_route_overrides(
    registry: ModelRegistry,
    roles: RouteRoles,
    overrides: list[str] | tuple[str, ...],
) -> RouteRoles:
    role_values = roles.model_dump()
    for override in overrides:
        role_names, selection = parse_model_override(registry, override)
        for role_name in role_names:
            role_values[role_name] = selection.model_dump()
    return RouteRoles.model_validate(role_values)


def merge_project_route_roles(
    registry: ModelRegistry, overrides: dict[str, Any]
) -> RouteRoles:
    values = registry.roles.model_dump()
    for role, selection in overrides.items():
        values[role] = selection
    merged = RouteRoles.model_validate(values)
    for role in MODEL_ROLE_NAMES:
        selection = getattr(merged, role)
        if selection is not None:
            registry.require_selection(selection, role=role)
    return merged


def materialize_selection(
    registry: ModelRegistry, selection: RouteSelection
) -> tuple[str, LLMConfig]:
    model = registry.require_selection(selection)
    route = registry.routes[selection.route]
    runtime_name = f"{selection.route}::{selection.model}"
    route_values = route.model_dump(exclude={"models"})
    route_values["provider"] = route_values.pop("transport")
    route_values["tiers"] = {
        "strong": {"model": model.model, "options": model.options}
    }
    return runtime_name, LLMConfig.model_validate(route_values)


def materialize_registry(
    registry: ModelRegistry, route_roles: RouteRoles
) -> tuple[dict[str, LLMConfig], ModelRoles]:
    providers: dict[str, LLMConfig] = {}

    def add(selection: RouteSelection) -> str:
        runtime_name, llm = materialize_selection(registry, selection)
        previous = providers.setdefault(runtime_name, llm)
        if previous != llm:
            raise ValueError(f"runtime provider name collision: {runtime_name!r}")
        return runtime_name

    role_values: dict[str, str | None] = {}
    for role in MODEL_ROLE_NAMES:
        selection = getattr(route_roles, role)
        role_values[role] = None if selection is None else add(selection)
    return providers, ModelRoles.model_validate(role_values)
