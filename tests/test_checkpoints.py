from __future__ import annotations

import json
from pathlib import Path

from wenyi_direct.config import Config
from wenyi_direct.llm.providers.fake import FakeClient
from wenyi_direct.pipeline.direct import DirectPipeline
from wenyi_direct.pipeline.types import TranslationWindow, segment_id


def _config(tmp_path: Path) -> Config:
    return Config.model_validate(
        {
            "source_lang": "ja",
            "target_lang": "zh-CN",
            "state_dir": str(tmp_path / "state"),
            "output_dir": str(tmp_path / "outputs"),
            "providers": {"default": {"provider": "fake"}},
            "roles": {
                "translate": "default",
                "factual_audit": "default",
                "chinese_audit": "default",
                "repair": "default",
                "validation": "default",
            },
            "window": {
                "max_read_chars": 10000,
                "max_write_chars": 10000,
                "source_halo_chars": 1000,
            },
            "pipeline": {"repair_context_segments": 0, "max_language_rechecks": 0},
        }
    )


def _source(tmp_path: Path, count: int = 4) -> Path:
    source = tmp_path / "book.json"
    source.write_text(
        json.dumps(
            {
                "title": "book",
                "chapters": [
                    {
                        "title": "chapter",
                        "segments": [
                            {"source": f"原文{index}。"} for index in range(count)
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return source


def _payload(messages):
    return json.loads(messages[-1]["content"].split("\n", 1)[1])


def test_factual_resume_only_calls_missing_batches(tmp_path: Path) -> None:
    config = _config(tmp_path)
    calls = 0

    def handler(messages, _tier, _json_mode):
        nonlocal calls
        calls += 1
        return json.dumps({"issues": [], "term_candidates": []}, ensure_ascii=False)

    fake = FakeClient(handler)
    pipeline = DirectPipeline(
        config,
        {role: fake for role in config.roles.model_dump()},
        config_dir=tmp_path,
    )
    source = _source(tmp_path)
    store = pipeline.prepare(source)
    chapter = store.load_chapter(0)
    targets = {str(index): f"译文{index}。" for index in range(4)}
    shadow = {
        "schema": 2,
        "chapter": 0,
        "source_digest": "not-used-by-direct-stage-call",
        "phase": "factual_audit",
        "targets": targets,
        "translated_ids": [segment_id(0, segment) for segment in chapter.text_segments],
        "stage_snapshots": {"direct": dict(targets)},
        "factual_state": {
            "plan": [
                {"read_indexes": [0, 1], "write_indexes": [0, 1]},
                {"read_indexes": [2, 3], "write_indexes": [2, 3]},
            ],
            "audit_batches": {
                "0": {
                    "read_indexes": [0, 1],
                    "write_indexes": [0, 1],
                    "issues": [],
                    "term_candidates": [],
                    "added_terms": [],
                }
            },
            "completed_repair_regions": [],
        },
    }
    store.save_shadow(0, shadow)

    pipeline._factual_stage(store, chapter, shadow)

    assert calls == 1
    saved = store.load_shadow(0)
    assert set(saved["factual_state"]["audit_batches"]) == {"0", "1"}
    assert saved["stage_snapshots"]["factual"] == targets


def test_overlapping_reader_repairs_are_merged_once(tmp_path: Path) -> None:
    config = _config(tmp_path)
    pipeline = DirectPipeline(config, {}, config_dir=tmp_path)
    source = _source(tmp_path, count=6)
    store = pipeline.prepare(source)
    chapter = store.load_chapter(0)
    targets = {str(index): f"译文{index}。" for index in range(6)}
    shadow = {
        "schema": 2,
        "chapter": 0,
        "source_digest": "not-used-by-direct-stage-call",
        "phase": "chinese_audit",
        "targets": targets,
        "translated_ids": [],
        "stage_snapshots": {"factual": dict(targets)},
        "chinese_state": {
            "reader_batches": [
                {
                    "read_indexes": [0, 1, 2, 3],
                    "write_indexes": [0, 1, 2],
                    "issues": [{"start": 1, "end": 2, "detail": "a"}],
                },
                {
                    "read_indexes": [2, 3, 4, 5],
                    "write_indexes": [3, 4, 5],
                    "issues": [{"start": 3, "end": 4, "detail": "b"}],
                },
            ],
            "validation_batches": {
                "0": [
                    {
                        "finding_id": "old-f0",
                        "safe_to_repair": True,
                        "start": 1,
                        "end": 2,
                        "cause_start": 1,
                        "cause_end": 2,
                        "detail": "a",
                    }
                ],
                "1": [
                    {
                        "finding_id": "old-f0",
                        "safe_to_repair": True,
                        "start": 3,
                        "end": 4,
                        "cause_start": 3,
                        "cause_end": 4,
                        "detail": "b",
                    }
                ],
            },
        },
    }
    store.save_shadow(0, shadow)
    repairs = []

    def accept(_store, _chapter, current, region, stage, **kwargs):
        repairs.append(
            {
                "stage": stage,
                "start": region.start,
                "end": region.end,
                "write_indexes": kwargs.get("write_indexes"),
            }
        )
        return current

    pipeline._repair_and_validate = accept  # type: ignore[method-assign]

    pipeline._chinese_stage(store, chapter, shadow)

    assert repairs == [
        {
            "stage": "language_repair",
            "start": 1,
            "end": 4,
            "write_indexes": (1, 2, 3, 4),
        }
    ]
    saved = store.load_shadow(0)
    assert len(saved["chinese_state"]["repair_regions"]) == 1
    assert saved["chinese_state"]["completed_repair_regions"] == ["language-r0"]


def test_translation_split_is_recorded_with_reason_and_depth(tmp_path: Path) -> None:
    config = _config(tmp_path)

    def handler(messages, _tier, _json_mode):
        payload = _payload(messages)
        required = payload["required_output"]["translations"]
        if len(required) > 1:
            return json.dumps({"translations": []})
        return json.dumps(
            {
                "translations": [
                    {"id": required[0]["id"], "target": "译文。"}
                ]
            },
            ensure_ascii=False,
        )

    fake = FakeClient(handler)
    pipeline = DirectPipeline(
        config,
        {role: fake for role in config.roles.model_dump()},
        config_dir=tmp_path,
    )
    source = _source(tmp_path, count=2)
    store = pipeline.prepare(source)
    chapter = store.load_chapter(0)

    result = pipeline._translate_window(
        store,
        chapter,
        TranslationWindow((0, 1), (0, 1)),
    )

    assert result == {0: "译文。", 1: "译文。"}
    events = [
        json.loads(line)
        for line in Path(store.event_log_path).read_text(encoding="utf-8").splitlines()
    ]
    split = next(event for event in events if event["event"] == "translation_window_split")
    assert split["depth"] == 0
    assert split["reason_type"] == "AlignmentError"
    assert split["child_write_indexes"] == [[0], [1]]
    completed = [
        event for event in events if event["event"] == "translation_window_completed"
    ]
    assert {event["split_depth"] for event in completed} == {1}
