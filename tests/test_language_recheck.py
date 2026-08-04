from __future__ import annotations

import json
from pathlib import Path

from wenyi_direct.config import Config
from wenyi_direct.llm.providers.fake import FakeClient
from wenyi_direct.pipeline.direct import DirectPipeline
from wenyi_direct.pipeline.types import chapter_source_digest
from wenyi_direct.prompts import (
    CHINESE_FINDING_VALIDATION_SYSTEM,
    CHINESE_READER_SYSTEM,
    FIDELITY_SYSTEM,
    REPAIR_SYSTEM,
)


def _config(tmp_path: Path, *, rechecks: int = 1, past_context: int = 20) -> Config:
    return Config.model_validate(
        {
            "source_lang": "ja",
            "target_lang": "zh-CN",
            "state_dir": str(tmp_path / "state"),
            "output_dir": str(tmp_path / "outputs"),
            "providers": {
                "chinese": {"provider": "fake"},
                "repair": {"provider": "fake"},
                "validation": {"provider": "fake"},
            },
            "roles": {
                "translate": "repair",
                "factual_audit": "validation",
                "chinese_audit": "chinese",
                "repair": "repair",
                "validation": "validation",
            },
            "window": {
                "max_read_chars": 10000,
                "max_write_chars": 10000,
                "source_halo_chars": 1000,
                "past_context_chars": past_context,
            },
            "pipeline": {
                "factual_audit": False,
                "chinese_reader_audit": True,
                "repair_context_segments": 0,
                "max_language_rechecks": rechecks,
            },
        }
    )


def _source(tmp_path: Path, chapters: list[list[str]]) -> Path:
    source = tmp_path / "book.json"
    source.write_text(
        json.dumps(
            {
                "title": "book",
                "chapters": [
                    {
                        "title": f"chapter-{chapter_index}",
                        "segments": [{"source": text} for text in segments],
                    }
                    for chapter_index, segments in enumerate(chapters)
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return source


def test_language_repair_is_rechecked_once_and_fidelity_validated(tmp_path: Path) -> None:
    config = _config(tmp_path)
    calls: list[str] = []

    def chinese_handler(messages, _tier, _json_mode):
        calls.append(messages[0]["content"])
        payload = json.loads(messages[-1]["content"].split("\n", 1)[1])
        stable_id = payload["text"][0]["id"]
        return json.dumps(
            {
                "issues": [
                    {
                        "start_id": stable_id,
                        "end_id": stable_id,
                        "type": "unnatural",
                        "detail": "量词仍不自然",
                        "evidence": "第一具猎物",
                    }
                ]
            },
            ensure_ascii=False,
        )

    def repair_handler(messages, _tier, _json_mode):
        calls.append(messages[0]["content"])
        payload = json.loads(messages[-1]["content"].split("\n", 1)[1])
        required = payload["required_output"]["translations"][0]
        return json.dumps(
            {
                "translations": [
                    {"id": required["id"], "target": "今晚的第一个猎物。"}
                ]
            },
            ensure_ascii=False,
        )

    def validation_handler(messages, _tier, _json_mode):
        system = messages[0]["content"]
        calls.append(system)
        if system == CHINESE_FINDING_VALIDATION_SYSTEM:
            payload = json.loads(messages[-1]["content"].split("\n", 1)[1])
            finding_id = payload["reader_issues"][0]["finding_id"]
            stable_id = payload["segments"][0]["id"]
            return json.dumps(
                {
                    "results": [
                        {
                            "finding_id": finding_id,
                            "safe_to_repair": True,
                            "repair_start_id": stable_id,
                            "repair_end_id": stable_id,
                            "required_meaning": "第一个猎物",
                            "constraints": [],
                            "reason": "中文量词错误",
                        }
                    ]
                },
                ensure_ascii=False,
            )
        assert system == FIDELITY_SYSTEM
        return json.dumps({"valid": True, "issues": []})

    clients = {
        "translate": FakeClient(repair_handler),
        "factual_audit": FakeClient(validation_handler),
        "chinese_audit": FakeClient(chinese_handler),
        "repair": FakeClient(repair_handler),
        "validation": FakeClient(validation_handler),
    }
    pipeline = DirectPipeline(config, clients, config_dir=tmp_path)
    source = _source(tmp_path, [["今夜最初の獲物。"]])
    store = pipeline.prepare(source)
    chapter = store.load_chapter(0)
    shadow = {
        "schema": 2,
        "chapter": 0,
        "source_digest": chapter_source_digest(chapter),
        "phase": "chinese_audit",
        "targets": {"0": "今晚的第一具猎物。"},
        "translated_ids": [],
        "stage_snapshots": {"direct": {"0": "今晚的第一具猎物。"}},
        "chinese_state": {
            "reader_batches": [],
            "validation_batches": {},
            "repair_regions": [
                {"id": "language-r0", "start": 0, "end": 0, "issues": []}
            ],
            "completed_repair_regions": ["language-r0"],
        },
    }
    store.save_shadow(0, shadow)

    pipeline._chinese_stage(store, chapter, shadow)

    saved = store.load_shadow(0)
    assert saved["targets"]["0"] == "今晚的第一个猎物。"
    assert saved["phase"] == "promote"
    assert saved["chinese_state"]["completed_language_rechecks"] == [
        "language-r0"
    ]
    assert calls == [
        CHINESE_READER_SYSTEM,
        CHINESE_FINDING_VALIDATION_SYSTEM,
        REPAIR_SYSTEM,
        FIDELITY_SYSTEM,
    ]

    pipeline._chinese_stage(store, chapter, saved)
    assert calls == [
        CHINESE_READER_SYSTEM,
        CHINESE_FINDING_VALIDATION_SYSTEM,
        REPAIR_SYSTEM,
        FIDELITY_SYSTEM,
    ]


def test_language_recheck_can_be_disabled(tmp_path: Path) -> None:
    config = _config(tmp_path, rechecks=0)
    chinese = FakeClient(lambda *_args: (_ for _ in ()).throw(AssertionError("called")))
    clients = {role: chinese for role in config.roles.model_dump() if role != "content_policy_fallback"}
    pipeline = DirectPipeline(config, clients, config_dir=tmp_path)
    source = _source(tmp_path, [["原文。"]])
    store = pipeline.prepare(source)
    chapter = store.load_chapter(0)
    shadow = {
        "phase": "chinese_audit",
        "targets": {"0": "译文。"},
        "chinese_state": {
            "reader_batches": [],
            "validation_batches": {},
            "repair_regions": [
                {"id": "language-r0", "start": 0, "end": 0, "issues": []}
            ],
            "completed_repair_regions": ["language-r0"],
        },
    }

    pipeline._chinese_stage(store, chapter, shadow)

    assert shadow["phase"] == "promote"
    assert "completed_language_rechecks" not in shadow["chinese_state"]


def test_past_context_is_strictly_bounded_even_for_one_huge_segment(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, past_context=10)
    clients = {}
    pipeline = DirectPipeline(config, clients, config_dir=tmp_path)
    source = _source(
        tmp_path,
        [["abcdefghijklmnopqrst"], ["current"]],
    )
    store = pipeline.prepare(source)
    previous = store.load_chapter(0)
    previous.segments[0].target = "ABCDEFGHIJKLMNOPQRST"
    store.save_chapter(previous)
    store.set_chapter_fields(0, status="done", phase="done")
    current = store.load_chapter(1)

    knowledge = pipeline._knowledge_for(store, current, "current")

    tail = knowledge["past_only_raw_tail"]
    assert len(tail) == 1
    assert tail[0]["truncated"] is True
    assert len(tail[0]["source"]) + len(tail[0]["formal_target"]) <= 10
