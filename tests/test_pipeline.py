from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path

import pytest
from typer.testing import CliRunner

from wenyi_direct.assemble.writer import assemble
from wenyi_direct.cli import app
from wenyi_direct.config import Config
from wenyi_direct.ingest.models import Chapter, Segment
from wenyi_direct.llm.providers.fake import FakeClient
from wenyi_direct.pipeline.direct import (
    AlignmentError,
    DirectPipeline,
    StageTaskError,
    export_json,
)
from wenyi_direct.pipeline.types import segment_id
from wenyi_direct.prompts import (
    CHINESE_FINDING_VALIDATION_SYSTEM,
    CHINESE_READER_SYSTEM,
    FACTUAL_AUDIT_SYSTEM,
    FIDELITY_SYSTEM,
    REPAIR_SYSTEM,
    TRANSLATION_SYSTEM,
    chinese_reader_messages,
    translation_messages,
)
from wenyi_direct.validate import validate_epub


def _payload(messages):
    return json.loads(messages[-1]["content"].split("\n", 1)[1])


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
            "pipeline": {"repair_context_segments": 0},
        }
    )


def _write_book(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "title": "夜の章",
                "chapters": [
                    {
                        "title": "第0章",
                        "segments": [
                            {"source": "彼が来た。"},
                            {"source": "光った。"},
                            {"source": "ノエルの呟きは轟音に消えた。"},
                        ],
                    },
                    {
                        "title": "未来章",
                        "segments": [{"source": "未来の秘密。"}],
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_chinese_reader_has_translation_acceptance_motive_but_no_source() -> None:
    chapter = Chapter(
        index=0,
        title="原文章名",
        segments=[Segment(index=0, source="光った。", target="闪光了。")],
    )
    messages = chinese_reader_messages(chapter, {0: "闪光了。"})
    serialized = json.dumps(messages, ensure_ascii=False)

    assert "机器翻译文稿上线前的中文阅读验收" in messages[0]["content"]
    assert "短句或低语" in messages[0]["content"]
    assert "闪光了。" in serialized
    assert "光った。" not in serialized
    assert "原文章名" not in serialized


def test_japanese_translation_guardrails_are_general_not_case_specific() -> None:
    for principle in (
        "省略的主语",
        "话语功能",
        "连体修饰顺序",
        "不成立搭配",
        "不得为了自然或文采擅自扩大",
    ):
        assert principle in TRANSLATION_SYSTEM
    for case_specific_answer in ("光った", "闪光了", "亮了", "第一具猎物"):
        assert case_specific_answer not in TRANSLATION_SYSTEM
    assert "不得仅因候选未采用 preferred" in FIDELITY_SYSTEM
    assert "逐一检查每个 changed=true" in FIDELITY_SYSTEM
    assert "条目数、ID 集合和顺序" in TRANSLATION_SYSTEM
    assert "词典义直拼" in FACTUAL_AUDIT_SYSTEM


def test_model_prompt_projection_excludes_renderer_metadata() -> None:
    chapter = Chapter(
        index=0,
        title="章",
        segments=[
            Segment(
                index=0,
                source="続く。",
                cont=True,
                meta={"epub_inline": {"nodes": ["renderer-only-secret"]}},
            )
        ],
    )
    messages = translation_messages(chapter, (0,), (0,), {})
    payload = _payload(messages)
    assert payload["segments"][0]["continuation"] is True
    assert "renderer-only-secret" not in json.dumps(messages, ensure_ascii=False)


def _handler(messages, _tier, _json_mode):
    system = messages[0]["content"]
    payload = _payload(messages)
    if system == TRANSLATION_SYSTEM:
        source_by_id = {row["id"]: row["source"] for row in payload["segments"]}
        initial = {
            "彼が来た。": "他到来了。",
            "光った。": "闪光了。",
            "ノエルの呟きは轟音に消えた。": "诺艾尔的低语消失在轰鸣中。",
            "未来の秘密。": "未来的秘密。",
        }
        return json.dumps(
            {
                "translations": [
                    {"id": item["id"], "target": initial[source_by_id[item["id"]]]}
                    for item in payload["required_output"]["translations"]
                ]
            },
            ensure_ascii=False,
        )
    if system == FACTUAL_AUDIT_SYSTEM:
        row = next((row for row in payload["segments"] if row["source"] == "光った。"), None)
        noel = next((row for row in payload["segments"] if "ノエル" in row["source"]), None)
        issues = []
        if row is not None and row["audit"]:
            issues.append(
                {
                    "start_id": row["id"],
                    "end_id": row["id"],
                    "cause_start_id": row["id"],
                    "cause_end_id": row["id"],
                    "type": "mistranslation",
                    "detail": "中文搭配不成立",
                    "required_meaning": "人物低声指出某处亮了",
                }
            )
        candidates = [{"source": "ノエル", "target": "诺艾尔"}] if noel is not None else []
        return json.dumps({"issues": issues, "term_candidates": candidates}, ensure_ascii=False)
    if system == CHINESE_READER_SYSTEM:
        row = next((row for row in payload["text"] if row["text"] == "他到来了。"), None)
        noel = next(
            (
                row
                for row in payload["text"]
                if row["text"] == "诺艾尔的低语消失在轰鸣中。"
            ),
            None,
        )
        issues = []
        if row is not None and row["audit"]:
            issues.append(
                {
                    "start_id": row["id"],
                    "end_id": row["id"],
                    "type": "unnatural",
                    "detail": "人物动作叙述过度书面",
                    "evidence": "他到来了",
                }
            )
        if noel is not None and noel["audit"]:
            issues.append(
                {
                    "start_id": noel["id"],
                    "end_id": noel["id"],
                    "type": "unnatural",
                    "detail": "消失在轰鸣中搭配生硬",
                    "evidence": "低语消失在轰鸣中",
                }
            )
        return json.dumps({"issues": issues}, ensure_ascii=False)
    if system == CHINESE_FINDING_VALIDATION_SYSTEM:
        issues = payload["reader_issues"]
        return json.dumps(
            {
                "results": [
                    {
                        "finding_id": issue["finding_id"],
                        "safe_to_repair": True,
                        "repair_start_id": issue["start_id"],
                        "repair_end_id": issue["end_id"],
                        "required_meaning": "他来了",
                        "constraints": ["保持过去时事件"],
                        "reason": "可在不改变事实的前提下口语化",
                    }
                    for issue in issues
                ]
            },
            ensure_ascii=False,
        )
    if system == REPAIR_SYSTEM:
        issue_type = payload["issues"][0]["type"]
        translations = []
        for item in payload["required_output"]["translations"]:
            row = next(row for row in payload["segments"] if row["id"] == item["id"])
            target = row["current_target"]
            if issue_type == "mistranslation":
                target = "亮了。"
            elif issue_type == "unnatural":
                target = (
                    "他来了。"
                    if row["source"] == "彼が来た。"
                    else "诺艾尔的低语被轰鸣声淹没了。"
                )
            translations.append({"id": item["id"], "target": target})
        return json.dumps({"translations": translations}, ensure_ascii=False)
    if system == FIDELITY_SYSTEM:
        return json.dumps({"valid": True, "issues": []}, ensure_ascii=False)
    raise AssertionError(f"unexpected prompt: {system}")


def test_full_pipeline_and_chinese_audit_information_boundary(tmp_path: Path) -> None:
    source = tmp_path / "book.json"
    _write_book(source)
    config = _config(tmp_path)
    fake = FakeClient(_handler)
    clients = {role: fake for role in config.roles.model_dump()}
    pipeline = DirectPipeline(config, clients, config_dir=tmp_path)

    store = pipeline.run(source, chapters={0})
    chapter = store.load_chapter(0)
    assert [segment.target for segment in chapter.text_segments] == [
        "他来了。",
        "亮了。",
        "诺艾尔的低语被轰鸣声淹没了。",
    ]
    manifest = store.load_manifest()
    assert manifest["chapters"][0]["status"] == "done"
    assert manifest["chapters"][1]["status"] == "pending"
    assert manifest["future_chapters_required"] is False
    discovered = next(term for term in pipeline.terminology.terms if term.source == "ノエル")
    assert discovered.mode == "preferred"
    assert discovered.status == "active"
    assert discovered.valid_from == 0
    assert not (tmp_path / "terminology.yaml").exists()
    assert (Path(store.run_dir) / "terminology.yaml").exists()

    chinese_calls = [
        call for call in fake.calls if call["messages"][0]["content"] == CHINESE_READER_SYSTEM
    ]
    assert len(chinese_calls) == 1
    serialized = json.dumps(chinese_calls[0]["messages"], ensure_ascii=False)
    for source_text in ("彼が来た。", "光った。", "ノエル", "未来の秘密。", "夜の章"):
        assert source_text not in serialized
    all_calls = json.dumps(fake.calls, ensure_ascii=False)
    assert "未来の秘密。" not in all_calls

    language_repair_calls = [
        call
        for call in fake.calls
        if call["messages"][0]["content"] == REPAIR_SYSTEM
        and _payload(call["messages"])["issues"][0]["type"] == "unnatural"
    ]
    assert len(language_repair_calls) == 1
    language_payload = _payload(language_repair_calls[0]["messages"])
    assert len(language_payload["required_output"]["translations"]) == 2

    calls_before_second_chapter = len(fake.calls)
    pipeline.run(source, chapters={1})
    second_chapter_calls = json.dumps(fake.calls[calls_before_second_chapter:], ensure_ascii=False)
    assert "未来の秘密。" in second_chapter_calls
    assert "彼が来た。" in second_chapter_calls
    assert "他来了。" in second_chapter_calls

    artifact = Path(store.translation_artifact_path(0)).read_text(encoding="utf-8")
    assert "direct_translation" in artifact
    assert "factual_repair_proposal" in artifact
    assert "language_repair_accepted" in artifact
    assert "formal_promotion" in artifact

    epub_path = tmp_path / "book.zh.epub"
    assemble(store, str(source), str(epub_path), out_format="epub")
    assert validate_epub(epub_path)["ok"] is True


def test_failed_repair_never_changes_formal_chapter(tmp_path: Path) -> None:
    source = tmp_path / "book.json"
    _write_book(source)
    config = _config(tmp_path)
    config.pipeline.max_repair_attempts = 1

    def rejecting(messages, tier, json_mode):
        if messages[0]["content"] == FIDELITY_SYSTEM:
            return json.dumps({"valid": False, "issues": [{"detail": "still wrong"}]})
        return _handler(messages, tier, json_mode)

    fake = FakeClient(rejecting)
    pipeline = DirectPipeline(
        config, {role: fake for role in config.roles.model_dump()}, config_dir=tmp_path
    )
    with pytest.raises(RuntimeError, match="failed source-fidelity validation"):
        pipeline.run(source, chapters={0})
    formal = pipeline.store_for(source).load_chapter(0)
    assert all(segment.target is None for segment in formal.text_segments)
    shadow = pipeline.store_for(source).load_shadow(0)
    assert shadow is not None
    assert shadow["targets"]["1"] == "闪光了。"
    usage = json.loads(Path(pipeline.store_for(source).usage_path).read_text(encoding="utf-8"))
    assert usage["providers"]


def test_chinese_stage_resume_reuses_reader_and_validation_checkpoints(
    tmp_path: Path,
) -> None:
    source = tmp_path / "book.json"
    _write_book(source)
    config = _config(tmp_path)
    config.pipeline.max_repair_attempts = 1
    reject_language_once = True

    def unstable(messages, tier, json_mode):
        nonlocal reject_language_once
        if messages[0]["content"] == FIDELITY_SYSTEM:
            payload = _payload(messages)
            if reject_language_once and any(
                row.get("changed") and row.get("candidate_target") == "他来了。"
                for row in payload["segments"]
            ):
                reject_language_once = False
                return json.dumps(
                    {"valid": False, "issues": [{"detail": "retry language batch"}]}
                )
        return _handler(messages, tier, json_mode)

    fake = FakeClient(unstable)
    pipeline = DirectPipeline(
        config, {role: fake for role in config.roles.model_dump()}, config_dir=tmp_path
    )
    with pytest.raises(RuntimeError, match="language_repair failed"):
        pipeline.run(source, chapters={0})

    reader_calls = sum(
        call["messages"][0]["content"] == CHINESE_READER_SYSTEM for call in fake.calls
    )
    validation_calls = sum(
        call["messages"][0]["content"] == CHINESE_FINDING_VALIDATION_SYSTEM
        for call in fake.calls
    )
    pipeline.run(source, chapters={0})
    assert sum(
        call["messages"][0]["content"] == CHINESE_READER_SYSTEM for call in fake.calls
    ) == reader_calls
    assert sum(
        call["messages"][0]["content"] == CHINESE_FINDING_VALIDATION_SYSTEM
        for call in fake.calls
    ) == validation_calls
    assert pipeline.store_for(source).load_manifest()["chapters"][0]["status"] == "done"


def test_audit_id_digest_copy_error_is_recovered_but_unknown_segment_is_rejected(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    pipeline = DirectPipeline(config, {}, config_dir=tmp_path)
    chapter = Chapter(
        index=3,
        title="章",
        segments=[Segment(index=0, source="光った。", target="亮了。")],
    )
    response = {
        "issues": [
            {
                "start_id": "ch3:s0:wrongdigest",
                "end_id": "ch3:s0:wrongdigest",
                "type": "other",
                "detail": "test",
            }
        ]
    }
    parsed = pipeline._parse_issues(chapter, response, {0}, {0})
    assert parsed[0]["start"] == 0

    response["issues"][0]["start_id"] = "ch3:s0"
    parsed = pipeline._parse_issues(chapter, response, {0}, {0})
    assert parsed[0]["start"] == 0

    response["issues"][0]["start_id"] = "ch3:s99:wrongdigest"
    with pytest.raises(AlignmentError, match="unknown stable ID"):
        pipeline._parse_issues(chapter, response, {0}, {0})


def test_audit_and_reader_validation_cannot_escape_visible_scope(tmp_path: Path) -> None:
    pipeline = DirectPipeline(_config(tmp_path), {}, config_dir=tmp_path)
    chapter = Chapter(
        index=0,
        title="章",
        segments=[Segment(index=index, source=f"source-{index}") for index in range(8)],
    )
    response = {
        "issues": [
            {
                "start_id": "ch0:s2",
                "end_id": "ch0:s7",
                "type": "other",
                "detail": "range typo",
            }
        ]
    }
    with pytest.raises(AlignmentError, match="outside the visible read scope"):
        pipeline._parse_issues(chapter, response, {2}, {1, 2, 3})

    original = [{"finding_id": "f0", "start": 2, "end": 2, "detail": "x"}]
    validation = {
        "results": [
            {
                "finding_id": "f0",
                "safe_to_repair": True,
                "repair_start_id": "ch0:s0",
                "repair_end_id": "ch0:s7",
                "required_meaning": "x",
            }
        ]
    }
    with pytest.raises(AlignmentError, match="outside the visible read scope"):
        pipeline._parse_reader_validations(chapter, validation, original, {1, 2, 3})


def test_synthetic_speaker_metadata_never_enters_target(tmp_path: Path) -> None:
    config = _config(tmp_path)
    pipeline = DirectPipeline(config, {}, config_dir=tmp_path)
    chapter = Chapter(
        index=0,
        title="章",
        segments=[
            Segment(
                index=0,
                source="【話者：黒澤／中文名：黑泽】「待て」",
            )
        ],
    )
    stable_id = next(iter({
        segment_id(0, segment): segment.index for segment in chapter.text_segments
    }))
    parsed = pipeline._parse_translations(
        chapter,
        (0,),
        {
            "translations": [
                {
                    "id": stable_id,
                    "target": "【話者：黒澤／中文名：黑泽】「等等」",
                }
            ]
        },
    )
    assert parsed == {0: "「等等」"}


def test_cli_prepare_and_status_need_no_model_credentials(tmp_path: Path) -> None:
    source = tmp_path / "book.json"
    _write_book(source)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """providers:\n  default:\n    provider: fake\npaths:\n  state_dir: state\n""",
        encoding="utf-8",
    )
    runner = CliRunner()
    prepared = runner.invoke(app, ["prepare", str(source), "--config", str(config_path)])
    assert prepared.exit_code == 0, prepared.output
    status = runner.invoke(app, ["status", str(source), "--config", str(config_path)])
    assert status.exit_code == 0, status.output
    assert "0/2 formal chapters complete" in status.output


def test_low_level_exports_reject_incomplete_formal_state(tmp_path: Path) -> None:
    source = tmp_path / "book.json"
    _write_book(source)
    pipeline = DirectPipeline(_config(tmp_path), {}, config_dir=tmp_path)
    store = pipeline.prepare(source)

    with pytest.raises(RuntimeError, match="formal translation is incomplete"):
        export_json(store, tmp_path / "incomplete.json")
    with pytest.raises(RuntimeError, match="formal translation is incomplete"):
        assemble(store, str(source), str(tmp_path / "incomplete.epub"))


def test_resume_rejects_changed_source_file(tmp_path: Path) -> None:
    source = tmp_path / "book.json"
    _write_book(source)
    config = _config(tmp_path)
    pipeline = DirectPipeline(config, {}, config_dir=tmp_path)
    pipeline.prepare(source)
    source.write_text(source.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="source file changed"):
        pipeline.prepare(source)


def test_legacy_state_backfills_missing_source_digest_after_full_match(
    tmp_path: Path,
) -> None:
    source = tmp_path / "book.json"
    _write_book(source)
    pipeline = DirectPipeline(_config(tmp_path), {}, config_dir=tmp_path)
    store = pipeline.prepare(source)
    manifest = store.load_manifest()
    manifest.pop("source_sha256")
    store.save_manifest(manifest)

    pipeline.prepare(source)

    expected = hashlib.sha256(source.read_bytes()).hexdigest()
    assert store.load_manifest()["source_sha256"] == expected
    events = Path(store.event_log_path).read_text(encoding="utf-8")
    assert "legacy_source_digest_migrated" in events


def test_legacy_state_without_digest_rejects_changed_segment_source(
    tmp_path: Path,
) -> None:
    source = tmp_path / "book.json"
    _write_book(source)
    pipeline = DirectPipeline(_config(tmp_path), {}, config_dir=tmp_path)
    store = pipeline.prepare(source)
    manifest = store.load_manifest()
    manifest.pop("source_sha256")
    store.save_manifest(manifest)
    payload = json.loads(source.read_text(encoding="utf-8"))
    payload["chapters"][0]["segments"][0]["source"] = "別の原文。"
    source.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(RuntimeError, match="segment structure differs"):
        pipeline.prepare(source)


def test_each_stage_stops_at_its_declared_boundary(tmp_path: Path) -> None:
    source = tmp_path / "book.json"
    _write_book(source)
    config = _config(tmp_path)
    fake = FakeClient(_handler)
    pipeline = DirectPipeline(
        config, {role: fake for role in config.roles.model_dump()}, config_dir=tmp_path
    )

    store = pipeline.run_stage(source, "translate", chapters={0})
    assert store.load_shadow(0)["phase"] == "factual_audit"
    assert [call["stage"] for call in fake.calls] == ["direct_translation"]

    pipeline.run_stage(source, "factual-audit", chapters={0})
    shadow = store.load_shadow(0)
    assert shadow["phase"] == "factual_audit"
    assert shadow["factual_state"]["audit_complete"] is True
    assert not any(call["stage"] == "factual_repair" for call in fake.calls)

    pipeline.run_stage(source, "factual-repair", chapters={0})
    assert store.load_shadow(0)["phase"] == "chinese_audit"
    factual_repairs = sum(call["stage"] == "factual_repair" for call in fake.calls)
    assert factual_repairs == 1

    pipeline.run_stage(source, "chinese-audit", chapters={0})
    shadow = store.load_shadow(0)
    assert shadow["phase"] == "chinese_audit"
    assert shadow["chinese_state"]["audit_complete"] is True
    assert not any(call["stage"] == "language_repair" for call in fake.calls)

    pipeline.run_stage(source, "chinese-repair", chapters={0})
    assert store.load_shadow(0)["phase"] == "promote"
    assert any(call["stage"] == "language_repair" for call in fake.calls)
    assert sum(call["stage"] == "factual_repair" for call in fake.calls) == factual_repairs

    pipeline.run_stage(source, "promote", chapters={0})
    assert store.load_shadow(0)["phase"] == "done"
    assert store.load_manifest()["chapters"][0]["status"] == "done"
    with pytest.raises(StageTaskError, match="already done"):
        pipeline.run_stage(source, "translate", chapters={0})


def test_formal_review_uses_existing_text_and_dual_lane_without_retranslation(
    tmp_path: Path,
) -> None:
    source = tmp_path / "book.json"
    _write_book(source)
    config = _config(tmp_path)
    fake = FakeClient(_handler)
    pipeline = DirectPipeline(
        config, {role: fake for role in config.roles.model_dump()}, config_dir=tmp_path
    )
    pipeline.run(source)
    fake.calls.clear()

    reviewed = pipeline.review_formal(source, parallel=True)

    stages = [call["stage"] for call in fake.calls]
    assert "direct_translation" not in stages
    assert "factual_audit" in stages
    assert "chinese_reader_audit" in stages
    assert all(
        item["status"] == "done" for item in reviewed.load_manifest()["chapters"]
    )
    assert all(
        reviewed.load_shadow(index)["formal_review"]["baseline_sha256"]
        for index in (0, 1)
    )
    events = [
        json.loads(line)
        for line in Path(reviewed.event_log_path).read_text(encoding="utf-8").splitlines()
    ]
    assert any(row["event"] == "formal_review_opened" for row in events)
    assert any(
        row["event"] == "formal_review_parallel_pair_started" for row in events
    )

    calls_after_review = len(fake.calls)
    pipeline.review_formal(source, parallel=True)
    assert len(fake.calls) == calls_after_review


def test_formal_review_archives_unfinished_legacy_shadow_when_formal_exists(
    tmp_path: Path,
) -> None:
    source = tmp_path / "book.json"
    _write_book(source)
    config = _config(tmp_path)
    fake = FakeClient(_handler)
    pipeline = DirectPipeline(
        config, {role: fake for role in config.roles.model_dump()}, config_dir=tmp_path
    )
    store = pipeline.run(source)
    formal_targets = {
        str(segment.index): segment.target or ""
        for segment in store.load_chapter(0).segments
    }
    legacy_shadow = {
        "schema": 1,
        "chapter": 0,
        "phase": "translate",
        "source_digest": store.load_shadow(0)["source_digest"],
        "targets": {"0": "discarded candidate"},
        "translated_ids": ["ch0:s0"],
    }
    store.save_shadow(0, legacy_shadow)
    fake.calls.clear()

    pipeline.review_formal(source, chapters={0}, parallel=False)

    stages = [call["stage"] for call in fake.calls]
    assert "direct_translation" not in stages
    assert "factual_audit" in stages
    reviewed = store.load_chapter(0)
    assert {
        str(segment.index): segment.target or "" for segment in reviewed.segments
    } == formal_targets
    archives = list(Path(store.superseded_shadows_dir).glob("ch0.*.json"))
    assert len(archives) == 1
    archived = json.loads(archives[0].read_text(encoding="utf-8"))
    assert archived["reason"] == "formal_review_superseded_legacy_shadow"
    assert archived["shadow"] == legacy_shadow


def test_parallel_pipeline_really_overlaps_and_defers_future_terms(tmp_path: Path) -> None:
    source = tmp_path / "parallel.json"
    source.write_text(
        json.dumps(
            {
                "title": "parallel",
                "chapters": [
                    {"title": "c0", "segments": [{"source": "共通語。"}]},
                    {"title": "c1", "segments": [{"source": "共通語。"}]},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    config = _config(tmp_path)
    chinese_zero_started = threading.Event()
    factual_one_started = threading.Event()
    saw_provisional_context = threading.Event()
    future_term_absent_from_previous = threading.Event()

    def handler(messages, _tier, _json_mode):
        system = messages[0]["content"]
        payload = _payload(messages)
        if system == TRANSLATION_SYSTEM:
            required = payload["required_output"]["translations"]
            if str(required[0]["id"]).startswith("ch1:"):
                tail = payload["knowledge"]["past_only_raw_tail"]
                if tail and tail[-1].get("provisional") is True:
                    saw_provisional_context.set()
            return json.dumps(
                {"translations": [{"id": item["id"], "target": "共同词。"} for item in required]},
                ensure_ascii=False,
            )
        if system == FACTUAL_AUDIT_SYSTEM:
            audited = next(item for item in payload["segments"] if item["audit"])
            if str(audited["id"]).startswith("ch1:"):
                factual_one_started.set()
                assert chinese_zero_started.wait(2)
                return json.dumps(
                    {
                        "issues": [],
                        "term_candidates": [{"source": "共通語", "target": "共同词"}],
                    },
                    ensure_ascii=False,
                )
            return json.dumps({"issues": [], "term_candidates": []})
        if system == CHINESE_READER_SYSTEM:
            audited = next(item for item in payload["text"] if item["audit"])
            if str(audited["id"]).startswith("ch0:"):
                chinese_zero_started.set()
                assert factual_one_started.wait(2)
                return json.dumps(
                    {
                        "issues": [
                            {
                                "start_id": audited["id"],
                                "end_id": audited["id"],
                                "type": "unnatural",
                                "detail": "check boundary",
                                "evidence": "共同词",
                            }
                        ]
                    },
                    ensure_ascii=False,
                )
            return json.dumps({"issues": []})
        if system == CHINESE_FINDING_VALIDATION_SYSTEM:
            preferred = payload["knowledge"].get("preferred_terms", [])
            if not any(item.get("source") == "共通語" for item in preferred):
                future_term_absent_from_previous.set()
            finding = payload["reader_issues"][0]
            return json.dumps(
                {
                    "results": [
                        {
                            "finding_id": finding["finding_id"],
                            "safe_to_repair": False,
                            "reason": "no repair",
                        }
                    ]
                },
                ensure_ascii=False,
            )
        raise AssertionError(system)

    fake = FakeClient(handler)
    pipeline = DirectPipeline(
        config, {role: fake for role in config.roles.model_dump()}, config_dir=tmp_path
    )
    store = pipeline.run_parallel(source)

    assert chinese_zero_started.is_set()
    assert factual_one_started.is_set()
    assert saw_provisional_context.is_set()
    assert future_term_absent_from_previous.is_set()
    assert [item["status"] for item in store.load_manifest()["chapters"]] == [
        "done",
        "done",
    ]
    assert any(term.source == "共通語" for term in pipeline.terminology.terms)
    events = Path(store.event_log_path).read_text(encoding="utf-8")
    assert "parallel_pair_started" in events
    assert "parallel_pair_completed" in events


def test_cli_exposes_one_stage_command_and_parallel_flag() -> None:
    runner = CliRunner()
    root = runner.invoke(app, ["--help"])
    assert root.exit_code == 0
    assert "stage" in root.output
    assert "Run factual audit only" not in root.output
    stage_help = runner.invoke(app, ["stage", "--help"])
    assert stage_help.exit_code == 0
    for stage in (
        "translate",
        "factual-audit",
        "factual-repair",
        "chinese-audit",
        "chinese-repair",
        "promote",
    ):
        assert stage in stage_help.output
    translate_help = runner.invoke(app, ["translate", "--help"])
    assert translate_help.exit_code == 0
    assert "--parallel" in translate_help.output
