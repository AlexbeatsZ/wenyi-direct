from __future__ import annotations

from pathlib import Path

import pytest

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
