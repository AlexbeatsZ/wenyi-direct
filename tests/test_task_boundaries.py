from __future__ import annotations

from pathlib import Path

import pytest

from wenyi_direct.llm.providers.fake import FakeClient
from wenyi_direct.pipeline.tasks import StageTaskError, TaskPipeline

from .test_task_commands import _config, _stage_handler, _write_book


def _pipeline(tmp_path: Path) -> tuple[TaskPipeline, FakeClient]:
    config = _config(tmp_path)
    fake = FakeClient(_stage_handler)
    pipeline = TaskPipeline(
        config,
        {role: fake for role in config.roles.model_dump()},
        config_dir=tmp_path,
    )
    return pipeline, fake


def test_factual_audit_does_not_process_term_revisions(tmp_path: Path) -> None:
    source = tmp_path / "book.json"
    _write_book(source)
    pipeline, _fake = _pipeline(tmp_path)
    pipeline.run_stage(source, "translate", chapters={0})

    def forbidden(*_args, **_kwargs):
        raise AssertionError("audit-only task must not validate or migrate terminology")

    pipeline._process_term_revisions = forbidden  # type: ignore[method-assign]
    store = pipeline.run_stage(source, "factual-audit", chapters={0})

    shadow = store.load_shadow(0)
    assert shadow["phase"] == "factual_audit"
    assert shadow["factual_state"]["audit_complete"] is True
    assert "repair_regions" not in shadow["factual_state"]


def test_wrong_stage_does_not_change_done_chapter_status(tmp_path: Path) -> None:
    source = tmp_path / "book.json"
    _write_book(source)
    pipeline, _fake = _pipeline(tmp_path)
    for task in (
        "translate",
        "factual-audit",
        "factual-repair",
        "chinese-audit",
        "chinese-repair",
        "promote",
    ):
        pipeline.run_stage(source, task, chapters={0})

    store = pipeline.store_for(source)
    before = store.load_manifest()["chapters"][0].copy()
    with pytest.raises(StageTaskError):
        pipeline.run_stage(source, "translate", chapters={0})
    after = store.load_manifest()["chapters"][0]

    assert before["status"] == "done"
    assert after["status"] == "done"
    assert after["phase"] == "done"


def test_default_stage_selection_runs_only_ready_chapters(tmp_path: Path) -> None:
    source = tmp_path / "book.json"
    _write_book(source, chapters=2)
    pipeline, fake = _pipeline(tmp_path)
    pipeline.run_stage(source, "translate", chapters={0})
    calls_before = len(fake.calls)

    store = pipeline.run_stage(source, "factual-audit")

    assert store.load_shadow(0)["factual_state"]["audit_complete"] is True
    assert store.load_shadow(1) is None
    assert len(fake.calls) == calls_before + 1
