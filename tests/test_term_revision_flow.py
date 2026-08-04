from __future__ import annotations

import json
from pathlib import Path

from wenyi_direct.config import Config
from wenyi_direct.llm.providers.fake import FakeClient
from wenyi_direct.pipeline.direct import DirectPipeline
from wenyi_direct.pipeline.knowledge import TerminologyDocument, TerminologyStore, TermRule
from wenyi_direct.pipeline.types import chapter_source_digest, segment_id
from wenyi_direct.prompts import (
    FIDELITY_SYSTEM,
    TERM_MIGRATION_REPAIR_SYSTEM,
    TERM_REVISION_VALIDATION_SYSTEM,
)


def _config(tmp_path: Path) -> Config:
    return Config.model_validate(
        {
            "source_lang": "ja",
            "target_lang": "zh-CN",
            "state_dir": str(tmp_path / "state"),
            "output_dir": str(tmp_path / "outputs"),
            "terminology_file": str(tmp_path / "terminology.yaml"),
            "providers": {
                "factual": {"provider": "fake"},
                "repair": {"provider": "fake"},
                "validation": {"provider": "fake"},
            },
            "roles": {
                "translate": "repair",
                "factual_audit": "factual",
                "chinese_audit": "factual",
                "repair": "repair",
                "validation": "validation",
            },
            "pipeline": {
                "factual_audit": True,
                "chinese_reader_audit": False,
                "repair_context_segments": 0,
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
                        "segments": [{"source": "黒炎を放った。"}],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return source


def _seed_terms(tmp_path: Path) -> None:
    store = TerminologyStore(
        tmp_path / "terminology.yaml",
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
    store.save()


def _pipeline(
    tmp_path: Path,
    *,
    initial_target: str,
    approve: bool,
):
    _seed_terms(tmp_path)
    config = _config(tmp_path)
    repair_calls: list[str] = []
    validation_calls: list[str] = []

    def factual_handler(messages, _tier, _json_mode):
        payload = json.loads(messages[-1]["content"].split("\n", 1)[1])
        stable_id = payload["segments"][0]["id"]
        return json.dumps(
            {
                "issues": [],
                "term_candidates": [],
                "term_revisions": [
                    {
                        "source": "黒炎",
                        "old_target": "黑色火焰",
                        "new_target": "黑炎",
                        "scope": "entire_existing_rule",
                        "evidence_ids": [stable_id],
                        "reason": "固定能力名",
                    }
                ],
            },
            ensure_ascii=False,
        )

    def repair_handler(messages, _tier, _json_mode):
        system = messages[0]["content"]
        repair_calls.append(system)
        assert system == TERM_MIGRATION_REPAIR_SYSTEM
        payload = json.loads(messages[-1]["content"].split("\n", 1)[1])
        required = payload["required_output"]["translations"][0]
        return json.dumps(
            {
                "translations": [
                    {"id": required["id"], "target": "释放了黑炎。"}
                ]
            },
            ensure_ascii=False,
        )

    def validation_handler(messages, _tier, _json_mode):
        system = messages[0]["content"]
        validation_calls.append(system)
        if system == TERM_REVISION_VALIDATION_SYSTEM:
            return json.dumps({"approved": approve, "reason": "checked"})
        assert system == FIDELITY_SYSTEM
        return json.dumps({"valid": True, "issues": []})

    clients = {
        "translate": FakeClient(repair_handler),
        "factual_audit": FakeClient(factual_handler),
        "chinese_audit": FakeClient(factual_handler),
        "repair": FakeClient(repair_handler),
        "validation": FakeClient(validation_handler),
    }
    pipeline = DirectPipeline(config, clients, config_dir=tmp_path)
    source = _source(tmp_path)
    store = pipeline.prepare(source)
    chapter = store.load_chapter(0)
    stable_id = segment_id(0, chapter.text_segments[0])
    shadow = {
        "schema": 2,
        "chapter": 0,
        "source_digest": chapter_source_digest(chapter),
        "phase": "factual_audit",
        "targets": {"0": initial_target},
        "translated_ids": [stable_id],
        "stage_snapshots": {"direct": {"0": initial_target}},
    }
    store.save_shadow(0, shadow)
    return pipeline, store, chapter, shadow, repair_calls, validation_calls


def test_approved_safe_revision_migrates_without_repair_model(tmp_path: Path) -> None:
    pipeline, store, chapter, shadow, repair_calls, validation_calls = _pipeline(
        tmp_path,
        initial_target="释放了黑色火焰。",
        approve=True,
    )

    pipeline._factual_stage(store, chapter, shadow)

    saved = store.load_shadow(0)
    assert saved["targets"]["0"] == "释放了黑炎。"
    assert saved["phase"] == "promote"
    assert repair_calls == []
    assert validation_calls == [TERM_REVISION_VALIDATION_SYSTEM]
    assert pipeline.terminology.find_active_rule("黒炎", "黑炎").target == "黑炎"
    results = saved["factual_state"]["term_revision_results"]
    assert next(iter(results.values()))["status"] == "applied"


def test_approved_ambiguous_revision_uses_repair_and_fidelity_immediately(
    tmp_path: Path,
) -> None:
    pipeline, store, chapter, shadow, repair_calls, validation_calls = _pipeline(
        tmp_path,
        initial_target="释放了漆黑烈焰。",
        approve=True,
    )

    pipeline._factual_stage(store, chapter, shadow)

    saved = store.load_shadow(0)
    assert saved["targets"]["0"] == "释放了黑炎。"
    assert repair_calls == [TERM_MIGRATION_REPAIR_SYSTEM]
    assert validation_calls == [TERM_REVISION_VALIDATION_SYSTEM, FIDELITY_SYSTEM]
    assert pipeline.terminology.find_active_rule("黒炎", "黑炎").target == "黑炎"


def test_rejected_revision_changes_neither_rule_nor_text(tmp_path: Path) -> None:
    pipeline, store, chapter, shadow, repair_calls, validation_calls = _pipeline(
        tmp_path,
        initial_target="释放了黑色火焰。",
        approve=False,
    )

    pipeline._factual_stage(store, chapter, shadow)

    saved = store.load_shadow(0)
    assert saved["targets"]["0"] == "释放了黑色火焰。"
    assert repair_calls == []
    assert validation_calls == [TERM_REVISION_VALIDATION_SYSTEM]
    assert (
        pipeline.terminology.find_active_rule("黒炎", "黑色火焰").target
        == "黑色火焰"
    )
    results = saved["factual_state"]["term_revision_results"]
    assert next(iter(results.values()))["status"] == "rejected"
