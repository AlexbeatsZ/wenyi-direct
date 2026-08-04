from __future__ import annotations

import json
import threading
from pathlib import Path

from typer.testing import CliRunner

from wenyi_direct.command_app import app
from wenyi_direct.config import Config
from wenyi_direct.llm.providers.fake import FakeClient
from wenyi_direct.pipeline.tasks import TaskPipeline
from wenyi_direct.prompts import (
    CHINESE_FINDING_VALIDATION_SYSTEM,
    CHINESE_READER_SYSTEM,
    FACTUAL_AUDIT_SYSTEM,
    FIDELITY_SYSTEM,
    REPAIR_SYSTEM,
    TRANSLATION_SYSTEM,
)


def _payload(messages):
    return json.loads(messages[-1]["content"].split("\n", 1)[1])


def _config(tmp_path: Path) -> Config:
    return Config.model_validate(
        {
            "source_lang": "ja",
            "target_lang": "zh-CN",
            "state_dir": str(tmp_path / "state"),
            "output_dir": str(tmp_path / "output"),
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
                "past_context_chars": 10000,
            },
            "pipeline": {
                "repair_context_segments": 0,
                "max_language_rechecks": 0,
            },
        }
    )


def _write_book(path: Path, chapters: int = 1) -> None:
    path.write_text(
        json.dumps(
            {
                "title": "book",
                "chapters": [
                    {
                        "title": f"chapter {index}",
                        "segments": [{"source": f"原文{index}。"}],
                    }
                    for index in range(chapters)
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _stage_handler(messages, _tier, _json_mode):
    system = messages[0]["content"]
    payload = _payload(messages)
    if system == TRANSLATION_SYSTEM:
        return json.dumps(
            {
                "translations": [
                    {"id": item["id"], "target": "初译。"}
                    for item in payload["required_output"]["translations"]
                ]
            },
            ensure_ascii=False,
        )
    if system == FACTUAL_AUDIT_SYSTEM:
        row = next(item for item in payload["segments"] if item["audit"])
        return json.dumps(
            {
                "issues": [
                    {
                        "start_id": row["id"],
                        "end_id": row["id"],
                        "cause_start_id": row["id"],
                        "cause_end_id": row["id"],
                        "type": "mistranslation",
                        "detail": "事实错误",
                        "required_meaning": "事实修复",
                    }
                ],
                "term_candidates": [],
                "term_revisions": [],
            },
            ensure_ascii=False,
        )
    if system == CHINESE_READER_SYSTEM:
        row = next(item for item in payload["text"] if item["audit"])
        return json.dumps(
            {
                "issues": [
                    {
                        "start_id": row["id"],
                        "end_id": row["id"],
                        "type": "unnatural",
                        "detail": "中文不自然",
                        "evidence": row["text"],
                    }
                ]
            },
            ensure_ascii=False,
        )
    if system == CHINESE_FINDING_VALIDATION_SYSTEM:
        return json.dumps(
            {
                "results": [
                    {
                        "finding_id": issue["finding_id"],
                        "safe_to_repair": True,
                        "repair_start_id": issue["start_id"],
                        "repair_end_id": issue["end_id"],
                        "required_meaning": "保持原意",
                        "constraints": [],
                        "reason": "可以修复",
                    }
                    for issue in payload["reader_issues"]
                ]
            },
            ensure_ascii=False,
        )
    if system == REPAIR_SYSTEM:
        issue_type = payload["issues"][0]["type"]
        target = "事实已修复。" if issue_type == "mistranslation" else "中文已修复。"
        return json.dumps(
            {
                "translations": [
                    {"id": item["id"], "target": target}
                    for item in payload["required_output"]["translations"]
                ]
            },
            ensure_ascii=False,
        )
    if system == FIDELITY_SYSTEM:
        return json.dumps({"valid": True, "issues": []}, ensure_ascii=False)
    raise AssertionError(system)


def test_each_stage_stops_at_its_declared_boundary(tmp_path: Path) -> None:
    source = tmp_path / "book.json"
    _write_book(source)
    config = _config(tmp_path)
    fake = FakeClient(_stage_handler)
    pipeline = TaskPipeline(
        config,
        {role: fake for role in config.roles.model_dump()},
        config_dir=tmp_path,
    )

    store = pipeline.run_stage(source, "translate", chapters={0})
    shadow = store.load_shadow(0)
    assert shadow["phase"] == "factual_audit"
    assert [call["stage"] for call in fake.calls] == ["direct_translation"]

    pipeline.run_stage(source, "factual-audit", chapters={0})
    shadow = store.load_shadow(0)
    assert shadow["phase"] == "factual_audit"
    assert shadow["factual_state"]["audit_complete"] is True
    assert not any(call["stage"] == "factual_repair" for call in fake.calls)

    pipeline.run_stage(source, "factual-repair", chapters={0})
    shadow = store.load_shadow(0)
    assert shadow["phase"] == "chinese_audit"
    assert any(call["stage"] == "factual_repair" for call in fake.calls)

    factual_repair_count = sum(
        call["stage"] == "factual_repair" for call in fake.calls
    )
    pipeline.run_stage(source, "chinese-audit", chapters={0})
    shadow = store.load_shadow(0)
    assert shadow["phase"] == "chinese_audit"
    assert shadow["chinese_state"]["audit_complete"] is True
    assert not any(call["stage"] == "language_repair" for call in fake.calls)

    pipeline.run_stage(source, "chinese-repair", chapters={0})
    shadow = store.load_shadow(0)
    assert shadow["phase"] == "promote"
    assert any(call["stage"] == "language_repair" for call in fake.calls)
    assert sum(call["stage"] == "factual_repair" for call in fake.calls) == factual_repair_count

    pipeline.run_stage(source, "promote", chapters={0})
    assert store.load_shadow(0)["phase"] == "done"
    assert store.load_manifest()["chapters"][0]["status"] == "done"
    assert store.load_chapter(0).segments[0].target == "中文已修复。"


def test_fast_pipeline_overlaps_adjacent_chapter_lanes(tmp_path: Path) -> None:
    source = tmp_path / "book.json"
    _write_book(source, chapters=2)
    config = _config(tmp_path)
    chinese_zero_started = threading.Event()
    factual_one_started = threading.Event()
    saw_provisional_context = threading.Event()

    def handler(messages, _tier, _json_mode):
        system = messages[0]["content"]
        payload = _payload(messages)
        serialized = json.dumps(payload, ensure_ascii=False)
        if system == TRANSLATION_SYSTEM:
            return json.dumps(
                {
                    "translations": [
                        {"id": item["id"], "target": f"译文 {item['id']}"}
                        for item in payload["required_output"]["translations"]
                    ]
                },
                ensure_ascii=False,
            )
        if system == FACTUAL_AUDIT_SYSTEM:
            audited = next(item for item in payload["segments"] if item["audit"])
            if str(audited["id"]).startswith("ch1:"):
                factual_one_started.set()
                assert chinese_zero_started.wait(2), "chapter 0 Chinese lane did not overlap"
                if '"provisional": true' in serialized and "factual_target" in serialized:
                    saw_provisional_context.set()
            return json.dumps(
                {"issues": [], "term_candidates": [], "term_revisions": []},
                ensure_ascii=False,
            )
        if system == CHINESE_READER_SYSTEM:
            audited = next(item for item in payload["text"] if item["audit"])
            if str(audited["id"]).startswith("ch0:"):
                chinese_zero_started.set()
                assert factual_one_started.wait(2), "chapter 1 factual lane did not overlap"
            return json.dumps({"issues": []}, ensure_ascii=False)
        raise AssertionError(system)

    fake = FakeClient(handler)
    pipeline = TaskPipeline(
        config,
        {role: fake for role in config.roles.model_dump()},
        config_dir=tmp_path,
    )
    store = pipeline.run_fast(source, chapters={0, 1})

    assert chinese_zero_started.is_set()
    assert factual_one_started.is_set()
    assert saw_provisional_context.is_set()
    assert [item["status"] for item in store.load_manifest()["chapters"]] == [
        "done",
        "done",
    ]
    events = Path(store.event_log_path).read_text(encoding="utf-8")
    assert "staggered_pair_started" in events
    assert "staggered_pair_completed" in events


def test_composed_cli_registers_all_new_commands() -> None:
    runner = CliRunner()
    stage = runner.invoke(app, ["stage", "--help"])
    assert stage.exit_code == 0
    for command in (
        "translate",
        "factual-audit",
        "factual-repair",
        "chinese-audit",
        "chinese-repair",
        "promote",
    ):
        assert command in stage.output
    pipeline = runner.invoke(app, ["pipeline", "--help"])
    assert pipeline.exit_code == 0
    assert "fast" in pipeline.output
