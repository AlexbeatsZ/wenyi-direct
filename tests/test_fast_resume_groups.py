from __future__ import annotations

from pathlib import Path

from test_task_commands import _config, _stage_handler, _write_book

from wenyi_direct.llm.providers.fake import FakeClient
from wenyi_direct.pipeline.tasks import TaskPipeline


def test_fast_pipeline_splits_pending_chapters_at_completed_gaps(tmp_path: Path) -> None:
    source = tmp_path / "book.json"
    _write_book(source, chapters=3)
    config = _config(tmp_path)
    fake = FakeClient(_stage_handler)
    pipeline = TaskPipeline(
        config,
        {role: fake for role in config.roles.model_dump()},
        config_dir=tmp_path,
    )

    for task in (
        "translate",
        "factual-audit",
        "factual-repair",
        "chinese-audit",
        "chinese-repair",
        "promote",
    ):
        pipeline.run_stage(source, task, chapters={1})

    store = pipeline.run_fast(source)

    assert [item["status"] for item in store.load_manifest()["chapters"]] == [
        "done",
        "done",
        "done",
    ]
    events = Path(store.event_log_path).read_text(encoding="utf-8")
    assert '"chinese_chapter": 0, "factual_chapter": 2' not in events
