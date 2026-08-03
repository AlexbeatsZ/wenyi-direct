from __future__ import annotations

import json
from pathlib import Path

from wenyi_direct.monitor import Observer


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _append_jsonl(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(value, ensure_ascii=False) + "\n")


def test_monitor_exposes_partial_shadow_and_audit_repair_trace(tmp_path: Path) -> None:
    _write_json(
        tmp_path / "manifest.json",
        {
            "title": "测试",
            "source_lang": "ja",
            "target_lang": "zh-CN",
            "chapters": [
                {
                    "index": 0,
                    "title": "第一章",
                    "status": "pending",
                    "phase": "chinese_audit",
                }
            ],
        },
    )
    _write_json(
        tmp_path / "chapters" / "ch0.json",
        {
            "index": 0,
            "title": "第一章",
            "segments": [
                {"index": 0, "kind": "text", "source": "光った。", "target": None},
                {"index": 1, "kind": "text", "source": "次。", "target": None},
            ],
        },
    )
    _write_json(
        tmp_path / "shadows" / "ch0.json",
        {"phase": "chinese_audit", "targets": {"0": "亮了。", "1": ""}},
    )
    _append_jsonl(
        tmp_path / "artifacts" / "audits" / "ch0.jsonl",
        {
            "ts": "2026-08-04T01:00:00+08:00",
            "stage": "chinese_reader_audit",
            "chapter": 0,
            "issues": [
                {
                    "start": 0,
                    "end": 0,
                    "type": "collocation",
                    "detail": "搭配不成立",
                }
            ],
        },
    )
    _append_jsonl(
        tmp_path / "artifacts" / "audits" / "ch0.jsonl",
        {
            "ts": "2026-08-04T01:01:00+08:00",
            "stage": "chinese_finding_validation",
            "chapter": 0,
            "reader_issue": {"start": 0, "end": 0},
            "result": {"safe_to_repair": True, "reason": "可安全改成自然中文"},
        },
    )
    _append_jsonl(
        tmp_path / "artifacts" / "translations" / "ch0.jsonl",
        {
            "ts": "2026-08-04T01:02:00+08:00",
            "stage": "language_repair_accepted",
            "chapter": 0,
            "segments": [
                {
                    "index": 0,
                    "source": "光った。",
                    "previous_target": "闪光了。",
                    "target": "亮了。",
                }
            ],
        },
    )

    observer = Observer(tmp_path)
    book = observer.book_payload()
    assert book["translated_segments"] == 1
    chapter = observer.chapter_payload(0)
    assert chapter["version"].startswith("Shadow")
    assert chapter["segments"][0]["target"] == "亮了。"
    assert chapter["segments"][1]["translated"] is False

    snapshot = observer.snapshot()
    assert snapshot["current"]["stage"] == "chinese_audit"
    assert snapshot["quality"]["issues"] == 1
    assert snapshot["quality"]["fixed"] == 1
    assert snapshot["issues"][0]["before"] == "闪光了。"
    assert snapshot["issues"][0]["after"] == "亮了。"
    assert "原文验证" in snapshot["issues"][0]["suggestion"]
