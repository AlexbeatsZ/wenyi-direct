from __future__ import annotations

from pathlib import Path

import pytest

from wenyi_direct.ingest.models import Chapter, Segment
from wenyi_direct.pipeline.knowledge import (
    TermRule,
    TerminologyDocument,
    TerminologyStore,
)
from wenyi_direct.pipeline.runstore import RunStore
from wenyi_direct.pipeline.term_migration import (
    TermMigrationNeedsReview,
    TermMigrationService,
    TermRevision,
)


def _store(tmp_path: Path, chapter: Chapter, *, status: str = "done") -> RunStore:
    store = RunStore(str(tmp_path / "state" / "book"))
    store.save_chapter(chapter)
    store.save_manifest(
        {
            "title": "book",
            "source_path": str(tmp_path / "book.json"),
            "chapters": [
                {"index": chapter.index, "title": chapter.title, "status": status}
            ],
        }
    )
    return store


def _terms(store: RunStore) -> TerminologyStore:
    terminology = TerminologyStore(
        Path(store.run_dir) / "terminology.yaml",
        TerminologyDocument(
            terms=[
                TermRule(
                    source="黒炎",
                    target="黑色火焰",
                    mode="hard",
                    status="active",
                )
            ]
        ),
    )
    terminology.save()
    return terminology


def test_safe_migration_is_source_anchored_and_updates_formal_shadow_and_snapshots(
    tmp_path: Path,
) -> None:
    chapter = Chapter(
        index=0,
        title="chapter",
        segments=[
            Segment(index=0, source="黒炎を放った。", target="释放了黑色火焰。"),
            Segment(index=1, source="黒い炎が揺れた。", target="黑色火焰摇曳着。"),
        ],
    )
    store = _store(tmp_path, chapter)
    store.save_shadow(
        0,
        {
            "phase": "done",
            "targets": {
                "0": "释放了黑色火焰。",
                "1": "黑色火焰摇曳着。",
            },
            "stage_snapshots": {
                "direct": {
                    "0": "他释放了黑色火焰。",
                    "1": "黑色火焰摇曳着。",
                },
                "factual": {
                    "0": "释放了黑色火焰。",
                    "1": "黑色火焰摇曳着。",
                },
            },
        },
    )
    terminology = _terms(store)

    result = TermMigrationService(store, terminology).revise(
        TermRevision(
            source="黒炎",
            old_target="黑色火焰",
            new_target="黑炎",
            reason="固定能力名",
        )
    )

    formal = store.load_chapter(0)
    assert formal.segments[0].target == "释放了黑炎。"
    assert formal.segments[1].target == "黑色火焰摇曳着。"
    shadow = store.load_shadow(0)
    assert shadow["targets"]["0"] == "释放了黑炎。"
    assert shadow["targets"]["1"] == "黑色火焰摇曳着。"
    assert shadow["stage_snapshots"]["direct"]["0"] == "他释放了黑炎。"
    assert shadow["stage_snapshots"]["factual"]["0"] == "释放了黑炎。"
    assert result.replacement_rule.target == "黑炎"
    assert terminology.find_active_rule("黒炎", "黑炎").mode == "hard"


def test_ambiguous_live_use_blocks_all_changes_without_resolver(tmp_path: Path) -> None:
    chapter = Chapter(
        index=0,
        title="chapter",
        segments=[
            Segment(index=0, source="黒炎を放った。", target="释放了漆黑烈焰。"),
        ],
    )
    store = _store(tmp_path, chapter)
    terminology = _terms(store)
    service = TermMigrationService(store, terminology)
    plan = service.plan(
        TermRevision(source="黒炎", old_target="黑色火焰", new_target="黑炎")
    )

    with pytest.raises(TermMigrationNeedsReview) as captured:
        service.apply(plan)

    assert captured.value.plan.ambiguous_uses[0].current_target == "释放了漆黑烈焰。"
    assert store.load_chapter(0).segments[0].target == "释放了漆黑烈焰。"
    assert terminology.find_active_rule("黒炎", "黑色火焰").target == "黑色火焰"


def test_ambiguous_live_use_is_resolved_immediately_when_resolver_is_supplied(
    tmp_path: Path,
) -> None:
    chapter = Chapter(
        index=0,
        title="chapter",
        segments=[
            Segment(index=0, source="黒炎を放った。", target="释放了漆黑烈焰。"),
        ],
    )
    store = _store(tmp_path, chapter)
    terminology = _terms(store)
    service = TermMigrationService(store, terminology)

    result = service.revise(
        TermRevision(source="黒炎", old_target="黑色火焰", new_target="黑炎"),
        resolver=lambda use, revision: "释放了黑炎。",
    )

    assert result.model_resolved_edits == 1
    assert store.load_chapter(0).segments[0].target == "释放了黑炎。"
    assert terminology.find_active_rule("黒炎", "黑炎").target == "黑炎"


def test_ambiguous_snapshot_is_invalidated_instead_of_model_rewritten(tmp_path: Path) -> None:
    chapter = Chapter(
        index=0,
        title="chapter",
        segments=[
            Segment(index=0, source="黒炎を放った。", target="释放了黑色火焰。"),
        ],
    )
    store = _store(tmp_path, chapter)
    store.save_shadow(
        0,
        {
            "phase": "done",
            "targets": {"0": "释放了黑色火焰。"},
            "stage_snapshots": {
                "direct": {"0": "释放了漆黑烈焰。"},
                "factual": {"0": "释放了黑色火焰。"},
            },
        },
    )
    terminology = _terms(store)

    result = TermMigrationService(store, terminology).revise(
        TermRevision(source="黒炎", old_target="黑色火焰", new_target="黑炎")
    )

    shadow = store.load_shadow(0)
    assert "direct" not in shadow["stage_snapshots"]
    assert shadow["stage_snapshots"]["factual"]["0"] == "释放了黑炎。"
    assert result.invalidated_snapshots == {0: ["direct"]}


def test_longest_match_prevents_short_rule_from_claiming_nested_source(tmp_path: Path) -> None:
    terminology = TerminologyStore(
        tmp_path / "terminology.yaml",
        TerminologyDocument(
            terms=[
                TermRule(source="炎", target="火焰", mode="hard", status="active"),
                TermRule(source="黒炎", target="黑炎", mode="hard", status="active"),
            ]
        ),
    )

    assert terminology.selected_source_count(0, "黒炎が燃えた。", "炎") == 0
    assert terminology.selected_source_count(0, "黒炎が燃えた。", "黒炎") == 1
