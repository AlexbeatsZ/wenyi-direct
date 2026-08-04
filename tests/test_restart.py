from __future__ import annotations

import json
from pathlib import Path

import pytest

from wenyi_direct.config import Config
from wenyi_direct.pipeline.direct import DirectPipeline


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
        }
    )


def _source(tmp_path: Path) -> Path:
    source = tmp_path / "book.json"
    source.write_text(
        json.dumps(
            {
                "title": "book",
                "chapters": [
                    {
                        "title": "chapter",
                        "segments": [
                            {"source": "原文一。"},
                            {"source": "原文二。"},
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return source


def _prepared(tmp_path: Path) -> tuple[DirectPipeline, object, Path]:
    config = _config(tmp_path)
    pipeline = DirectPipeline(config, {}, config_dir=tmp_path)
    source = _source(tmp_path)
    store = pipeline.prepare(source)
    return pipeline, store, source


def test_shadow_saves_do_not_add_policy_fingerprint(tmp_path: Path) -> None:
    pipeline, store, _source_path = _prepared(tmp_path)
    shadow = {"phase": "translate", "targets": {"0": ""}}

    pipeline._save_shadow(store, 0, shadow)

    saved = store.load_shadow(0)
    assert saved == shadow
    assert "policy_fingerprint" not in saved


def test_restart_translate_preserves_formal_and_resets_shadow(tmp_path: Path) -> None:
    pipeline, store, _source_path = _prepared(tmp_path)
    chapter = store.load_chapter(0)
    chapter.segments[0].target = "正式译文一。"
    chapter.segments[1].target = "正式译文二。"
    store.save_chapter(chapter)
    store.set_chapter_fields(0, status="done", phase="done")

    pipeline._restart_chapter(store, 0, "translate")

    formal = store.load_chapter(0)
    assert [segment.target for segment in formal.text_segments] == [
        "正式译文一。",
        "正式译文二。",
    ]
    shadow = store.load_shadow(0)
    assert shadow["phase"] == "translate"
    assert shadow["translated_ids"] == []
    assert shadow["targets"] == {"0": "", "1": ""}
    assert shadow["stage_snapshots"] == {}
    manifest = store.load_manifest()
    assert manifest["chapters"][0]["status"] == "pending"


def test_restart_factual_restores_direct_snapshot(tmp_path: Path) -> None:
    pipeline, store, _source_path = _prepared(tmp_path)
    store.save_shadow(
        0,
        {
            "schema": 2,
            "chapter": 0,
            "source_digest": "unused-in-direct-unit-test",
            "phase": "done",
            "targets": {"0": "最终一", "1": "最终二"},
            "translated_ids": [],
            "stage_snapshots": {
                "direct": {"0": "初译一", "1": "初译二"},
                "factual": {"0": "事实一", "1": "事实二"},
            },
            "factual_state": {"old": True},
            "chinese_state": {"old": True},
        },
    )

    pipeline._restart_chapter(store, 0, "factual_audit")

    shadow = store.load_shadow(0)
    assert shadow["phase"] == "factual_audit"
    assert shadow["targets"] == {"0": "初译一", "1": "初译二"}
    assert "factual_state" not in shadow
    assert "chinese_state" not in shadow
    assert "factual" not in shadow["stage_snapshots"]


def test_restart_chinese_restores_factual_snapshot(tmp_path: Path) -> None:
    pipeline, store, _source_path = _prepared(tmp_path)
    chapter = store.load_chapter(0)
    from wenyi_direct.pipeline.types import chapter_source_digest

    store.save_shadow(
        0,
        {
            "schema": 2,
            "chapter": 0,
            "source_digest": chapter_source_digest(chapter),
            "phase": "done",
            "targets": {"0": "最终一", "1": "最终二"},
            "translated_ids": [],
            "stage_snapshots": {
                "direct": {"0": "初译一", "1": "初译二"},
                "factual": {"0": "事实一", "1": "事实二"},
            },
            "chinese_state": {"old": True},
        },
    )

    pipeline._restart_chapter(store, 0, "chinese_audit")

    shadow = store.load_shadow(0)
    assert shadow["phase"] == "chinese_audit"
    assert shadow["targets"] == {"0": "事实一", "1": "事实二"}
    assert "chinese_state" not in shadow


def test_restart_requires_snapshot_for_later_stage(tmp_path: Path) -> None:
    pipeline, store, _source_path = _prepared(tmp_path)
    chapter = store.load_chapter(0)
    from wenyi_direct.pipeline.types import chapter_source_digest

    store.save_shadow(
        0,
        {
            "schema": 1,
            "chapter": 0,
            "source_digest": chapter_source_digest(chapter),
            "phase": "factual_audit",
            "targets": {"0": "旧一", "1": "旧二"},
            "translated_ids": [],
        },
    )

    with pytest.raises(RuntimeError, match="restart from translate"):
        pipeline._restart_chapter(store, 0, "factual_audit")


def test_restart_stage_names_are_explicit() -> None:
    assert DirectPipeline._normalise_restart_stage("factual-audit") == "factual_audit"
    assert DirectPipeline._normalise_restart_stage("chinese_audit") == "chinese_audit"
    with pytest.raises(ValueError, match="restart_from"):
        DirectPipeline._normalise_restart_stage("automatic")
