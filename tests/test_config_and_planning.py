from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from wenyi_direct.cli import app
from wenyi_direct.config import Config, WindowConfig
from wenyi_direct.ingest.models import Chapter, Segment
from wenyi_direct.pipeline.repair import RepairPlanner
from wenyi_direct.pipeline.window import WindowPlanner, split_write_scope


def _chapter(texts: list[str]) -> Chapter:
    return Chapter(
        index=0,
        title="test",
        segments=[Segment(index=index, source=text) for index, text in enumerate(texts)],
    )


def test_default_config_is_non_destructive(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    assert Config.create_default_file(path) is True
    first = path.read_text(encoding="utf-8")
    assert Config.create_default_file(path) is False
    assert path.read_text(encoding="utf-8") == first
    config = Config.load(path)
    assert config.pipeline.factual_audit is True
    assert config.pipeline.chinese_reader_audit is True


def test_window_keeps_full_chapter_when_it_fits_and_splits_only_output() -> None:
    chapter = _chapter(["aaa", "bbb", "ccc", "ddd"])
    planner = WindowPlanner(
        WindowConfig(max_read_chars=100, max_write_chars=6, source_halo_chars=10)
    )
    windows = planner.plan(chapter)
    assert windows[0].read_indexes == (0, 1, 2, 3)
    assert windows[0].write_indexes == (0, 1)
    left, right = split_write_scope(windows[0])
    assert left.read_indexes == right.read_indexes == (0, 1, 2, 3)
    assert left.write_indexes == (0,)
    assert right.write_indexes == (1,)


def test_long_chapter_has_source_on_both_sides() -> None:
    chapter = _chapter(["a" * 4 for _ in range(7)])
    planner = WindowPlanner(
        WindowConfig(max_read_chars=20, max_write_chars=8, source_halo_chars=8)
    )
    middle = planner.plan(chapter)[1]
    assert middle.write_indexes == (2, 3)
    assert min(middle.read_indexes) < 2
    assert max(middle.read_indexes) > 3


def test_repair_planner_keeps_write_scope_precise_and_expands_read_context() -> None:
    issues = [
        {"start": 10, "end": 10, "cause_start": 8, "cause_end": 10},
        {"start": 11, "end": 11, "cause_start": 11, "cause_end": 11},
    ]
    regions = RepairPlanner(context_segments=1).plan(issues, segment_count=20)
    assert len(regions) == 1
    assert (regions[0].start, regions[0].end) == (8, 11)
    assert RepairPlanner(context_segments=1).read_bounds(regions[0], 20) == (7, 12)
    assert len(regions[0].issues) == 2


def test_yesterday_failure_read_halo_never_becomes_write_scope() -> None:
    issues = [
        {"start": 49, "end": 51, "cause_start": 49, "cause_end": 51},
    ]
    planner = RepairPlanner(context_segments=2)
    region = planner.plan(issues, segment_count=89)[0]

    assert region.indexes == (49, 50, 51)
    assert planner.read_bounds(region, 89) == (47, 53)


def test_window_planner_can_budget_reader_visible_target_lengths() -> None:
    chapter = _chapter(["a", "b", "c"])
    planner = WindowPlanner(
        WindowConfig(max_read_chars=12, max_write_chars=6, source_halo_chars=6)
    )
    windows = planner.plan(chapter, {0: 6, 1: 6, 2: 6})
    assert [window.write_indexes for window in windows] == [(0,), (1,), (2,)]


def test_config_rejects_prompt_language_mismatch_and_invalid_window_budget() -> None:
    with pytest.raises(ValueError, match="Simplified Chinese only"):
        Config.model_validate(
            {"target_lang": "en", "providers": {"default": {"provider": "fake"}}}
        )
    with pytest.raises(ValueError, match="cannot exceed"):
        WindowConfig(max_read_chars=10, max_write_chars=11)


def test_sparse_project_config_uses_central_models_and_cli_overrides(tmp_path: Path) -> None:
    models = tmp_path / "models.yaml"
    models.write_text(
        """routes:
  first-route:
    transport: fake
    models:
      first-model: {model: first-upstream, runtime_name: first}
  second-route:
    transport: fake
    models:
      second-model: {model: second-upstream, runtime_name: second}
roles:
  translate: {route: first-route, model: first-model}
  factual_audit: {route: first-route, model: first-model}
  chinese_audit: {route: first-route, model: first-model}
  repair: {route: first-route, model: first-model}
  validation: {route: first-route, model: first-model}
""",
        encoding="utf-8",
    )
    project = tmp_path / "project.yaml"
    project.write_text("paths:\n  state_dir: work/state\n", encoding="utf-8")

    config = Config.load(
        project,
        models_path=models,
        model_overrides=[
            "audit=second-route/second-model",
            "repair=second-route/second-model",
        ],
    )

    assert config.roles.translate == "first"
    assert config.roles.factual_audit == "second"
    assert config.roles.chinese_audit == "second"
    assert config.roles.repair == "second"
    assert config.roles.validation == "first"


def test_legacy_self_contained_config_does_not_inherit_central_roles(tmp_path: Path) -> None:
    models = tmp_path / "models.yaml"
    models.write_text(
        """providers:
  central:
    provider: fake
    tiers:
      strong: {model: central-upstream}
roles:
  translate: central
  factual_audit: central
  chinese_audit: central
  repair: central
  validation: central
""",
        encoding="utf-8",
    )
    project = tmp_path / "legacy.yaml"
    project.write_text("providers:\n  default: {provider: fake}\n", encoding="utf-8")

    config = Config.load(project, models_path=models)

    assert config.roles.translate == "default"
    assert config.roles.repair == "default"

    overridden = Config.load(
        project,
        models_path=models,
        model_overrides=["repair=central/central"],
    )
    assert overridden.roles.translate == "default"
    assert overridden.roles.repair == "central"
    assert set(overridden.providers) == {"default", "central"}


def test_models_cli_persists_role_groups_in_one_catalog(tmp_path: Path) -> None:
    models = tmp_path / "models.yaml"
    runner = CliRunner()
    initialized = runner.invoke(app, ["models", "init", "--models", str(models)])
    assert initialized.exit_code == 0

    switched = runner.invoke(
        app,
        [
            "use",
            "translate",
            "deepseek-api",
            "deepseek-v4-flash",
            "--models",
            str(models),
        ],
    )
    assert switched.exit_code == 0
    assert "translate -> deepseek-api / deepseek-v4-flash" in switched.output

    alias_switched = runner.invoke(
        app,
        [
            "models",
            "use",
            "audit",
            "deepseek-api",
            "deepseek-v4-flash",
            "--models",
            str(models),
        ],
    )
    assert alias_switched.exit_code == 0

    project = tmp_path / "project.yaml"
    project.write_text("language: {source: ja, target: zh-CN}\n", encoding="utf-8")
    config = Config.load(project, models_path=models)
    assert config.roles.translate == "deepseek-api::deepseek-v4-flash"
    assert config.roles.factual_audit == "deepseek-api::deepseek-v4-flash"
    assert config.roles.chinese_audit == "deepseek-api::deepseek-v4-flash"
    assert config.roles.repair == "codex_sol"
    flash = config.providers["deepseek-api::deepseek-v4-flash"]
    assert flash.provider == "openai-compatible"
    assert flash.tiers["strong"].model == "deepseek-v4-flash"

    saved = models.read_text(encoding="utf-8")
    assert "routes:" in saved
    assert "providers:" not in saved
    assert "route: deepseek-api" in saved
    assert "model: deepseek-v4-flash" in saved

    listed = runner.invoke(app, ["models", "list", "--models", str(models)])
    assert listed.exit_code == 0
    assert "deepseek-api" in listed.output
    assert "deepseek-v4-flash" in listed.output
    assert "codex" in listed.output
    assert "gpt-5.6-sol-high" in listed.output
