from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from wenyi_direct.assemble.writer import assemble
from wenyi_direct.cli import app
from wenyi_direct.config import Config
from wenyi_direct.ingest.models import Chapter, Segment
from wenyi_direct.llm.providers.fake import FakeClient
from wenyi_direct.pipeline.direct import DirectPipeline
from wenyi_direct.prompts import (
    CHINESE_FINDING_VALIDATION_SYSTEM,
    CHINESE_READER_SYSTEM,
    FACTUAL_AUDIT_SYSTEM,
    FIDELITY_SYSTEM,
    REPAIR_SYSTEM,
    TRANSLATION_SYSTEM,
    chinese_reader_messages,
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
        return json.dumps({"issues": issues}, ensure_ascii=False)
    if system == CHINESE_FINDING_VALIDATION_SYSTEM:
        issue = payload["reader_issue"]
        return json.dumps(
            {
                "safe_to_repair": True,
                "repair_start_id": issue["start_id"],
                "repair_end_id": issue["end_id"],
                "required_meaning": "他来了",
                "constraints": ["保持过去时事件"],
                "reason": "可在不改变事实的前提下口语化",
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
                target = "他来了。"
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
        "诺艾尔的低语消失在轰鸣中。",
    ]
    manifest = store.load_manifest()
    assert manifest["chapters"][0]["status"] == "done"
    assert manifest["chapters"][1]["status"] == "pending"
    assert manifest["future_chapters_required"] is False
    discovered = next(term for term in pipeline.terminology.terms if term.source == "ノエル")
    assert discovered.mode == "preferred"
    assert discovered.status == "active"
    assert discovered.valid_from == 0

    chinese_calls = [
        call for call in fake.calls if call["messages"][0]["content"] == CHINESE_READER_SYSTEM
    ]
    assert len(chinese_calls) == 1
    serialized = json.dumps(chinese_calls[0]["messages"], ensure_ascii=False)
    for source_text in ("彼が来た。", "光った。", "ノエル", "未来の秘密。", "夜の章"):
        assert source_text not in serialized
    all_calls = json.dumps(fake.calls, ensure_ascii=False)
    assert "未来の秘密。" not in all_calls

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


def test_resume_rejects_changed_source_file(tmp_path: Path) -> None:
    source = tmp_path / "book.json"
    _write_book(source)
    config = _config(tmp_path)
    pipeline = DirectPipeline(config, {}, config_dir=tmp_path)
    pipeline.prepare(source)
    source.write_text(source.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="source file changed"):
        pipeline.prepare(source)
