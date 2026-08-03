from __future__ import annotations

from pathlib import Path

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


def test_repair_planner_expands_causal_range_and_merges_neighbors() -> None:
    issues = [
        {"start": 10, "end": 10, "cause_start": 8, "cause_end": 10},
        {"start": 11, "end": 11, "cause_start": 11, "cause_end": 11},
    ]
    regions = RepairPlanner(context_segments=1).plan(issues, segment_count=20)
    assert len(regions) == 1
    assert (regions[0].start, regions[0].end) == (7, 12)
    assert len(regions[0].issues) == 2
