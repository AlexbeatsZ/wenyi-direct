#!/usr/bin/env python3
"""Read-only live dashboard and reader for a Wenyi Direct run directory."""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import sys
import threading
import time
import webbrowser
from collections import Counter
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import yaml

ROOT = Path(__file__).resolve().parent

STAGE_EXPLANATIONS = {
    "translate": "直接读取当前章节与双向源文语境，生成第一版中文。",
    "factual_audit": "对照原文检查事实、指代、语气、遗漏、增译和术语，并修复后验证忠实度。",
    "chinese_audit": "纯中文 Reader 检查搭配、人物声音和跨段连贯；报告项随后回到原文验证和修复。",
    "promote": "全部质量门已完成，正在把 Shadow 原子提升为 Formal。",
    "idle": "当前持久化状态中没有正在处理的章节。",
}

EVENT_LABELS = {
    "prepared": "书籍状态已建立",
    "translation_window_completed": "直接翻译窗口完成",
    "chapter_promoted": "章节通过全部质量门",
    "chapter_failed": "章节处理失败",
    "factual_audit": "事实审查完成",
    "chinese_reader_audit": "纯中文阅读审查完成",
    "chinese_finding_validation": "中文问题原文验证完成",
    "factual_repair_fidelity": "事实修复忠实度验证",
    "language_repair_fidelity": "语言修复忠实度验证",
    "factual_repair_accepted": "事实修复已接受",
    "language_repair_accepted": "语言修复已接受",
}


def _read_json(path: Path, default: Any) -> Any:
    """Read a replace-written JSON file without ever locking or modifying it."""
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (FileNotFoundError, PermissionError, json.JSONDecodeError, OSError):
        return default


def _parse_ts(value: Any) -> float:
    if not isinstance(value, str) or not value:
        return 0.0
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return 0.0


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _has_text(value: Any) -> bool:
    return value is not None and bool(str(value).strip())


def _mtime_iso(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(
        timespec="seconds"
    )


class EventCache:
    """Cache the append-only event stream until size or mtime changes."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._signature: tuple[int, int] | None = None
        self._events: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def read(self) -> list[dict[str, Any]]:
        try:
            stat = self.path.stat()
            signature = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            return []
        with self._lock:
            if signature == self._signature:
                return self._events
            events: list[dict[str, Any]] = []
            try:
                with self.path.open("r", encoding="utf-8") as handle:
                    for line in handle:
                        try:
                            item = json.loads(line)
                        except json.JSONDecodeError:
                            # The writer may currently be appending the final line.
                            continue
                        if isinstance(item, dict):
                            events.append(item)
            except (FileNotFoundError, PermissionError, OSError):
                return self._events
            self._events = events
            self._signature = signature
            return events


def _config_models(path: Path | None) -> dict[str, dict[str, str]]:
    if path is None:
        return {}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    result: dict[str, dict[str, str]] = {}
    providers = raw.get("providers", {}) or {}
    roles = raw.get("roles", {}) or {}
    for role in ("translate", "factual_audit", "chinese_audit", "repair", "validation"):
        provider_name = roles.get(role)
        provider = providers.get(provider_name, {}) if provider_name else {}
        tiers = provider.get("tiers", {}) or {}
        tier = tiers.get("strong", {}) or {}
        if not provider:
            continue
        result[role] = {
            "provider": str(provider.get("provider", "")),
            "model": str(tier.get("model", "")),
        }
    return result


def _chapter_index(chapter: dict[str, Any]) -> int:
    return _safe_int(chapter.get("index"), -1)


def _current_work(manifest: dict[str, Any], models: dict[str, dict[str, str]]) -> dict[str, Any]:
    chapters = [item for item in manifest.get("chapters", []) if isinstance(item, dict)]
    active = next(
        (c for c in chapters if c.get("status") != "done" and c.get("phase")),
        None,
    )
    if active is None:
        active = next((c for c in chapters if c.get("status") != "done"), None)
    if active:
        raw_phase = str(active.get("phase") or "translate")
        if raw_phase.startswith("factual"):
            stage = "factual_audit"
        elif raw_phase.startswith("chinese") or raw_phase.startswith("language"):
            stage = "chinese_audit"
        elif raw_phase == "promote":
            stage = "promote"
        else:
            stage = "translate"
        model_role = {
            "translate": "translate",
            "factual_audit": "factual_audit",
            "chinese_audit": "chinese_audit",
            "promote": "validation",
        }[stage]
        model = models.get(model_role, {})
        labels = {
            "translate": "整章直接翻译",
            "factual_audit": "事实审查、修复与验证",
            "chinese_audit": "纯中文审查、原文验证与修复",
            "promote": "正式文本原子提升",
        }
        return {
            "stage": stage,
            "stage_label": labels[stage],
            "chapter": _chapter_index(active),
            "title": active.get("title", ""),
            "model": model.get("model", ""),
            "provider": model.get("provider", ""),
            "explanation": STAGE_EXPLANATIONS[stage],
            "precision": f"持久化阶段：{raw_phase}。页面同时显示尚未正式通过的 Shadow。",
        }
    return {
        "stage": "idle",
        "stage_label": "空闲或阶段切换",
        "chapter": None,
        "title": "",
        "model": "",
        "provider": "",
        "explanation": STAGE_EXPLANATIONS["idle"],
        "precision": "以最近事件时间和正在运行的外部命令共同判断是否仍在切换阶段。",
    }


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _event_detail(event: dict[str, Any]) -> str:
    kind = str(event.get("event", ""))
    chapter = event.get("chapter")
    index = event.get("index")
    prefix = ""
    if chapter is not None:
        prefix = f"第 {int(chapter) + 1} 章"
    if index is not None:
        prefix += f" · 段 {index}"
    if kind == "chapter_failed":
        return f"{prefix} · {event.get('error', '未知错误')}"
    if kind == "translation_window_completed":
        return f"{prefix} · 写入 {len(event.get('write_indexes') or [])} 段"
    if kind == "chapter_promoted":
        return f"{prefix} · Formal 已更新"
    if kind in {"factual_audit", "chinese_reader_audit"}:
        return f"{prefix} · 发现 {event.get('issue_count', 0)} 个问题"
    if kind.endswith("_fidelity"):
        return f"{prefix} · {'通过' if event.get('valid') else '未通过'}"
    return prefix or str(event.get("reason", "") or "状态已更新")


class Observer:
    def __init__(self, run_dir: Path, config_path: Path | None = None) -> None:
        self.run_dir = run_dir.resolve()
        self.config_path = config_path.resolve() if config_path else None
        self.events = EventCache(self.run_dir / "events.jsonl")

    def _chapter_view(
        self, item: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[int, str], str, Path]:
        index = _chapter_index(item)
        chapter_path = self.run_dir / "chapters" / f"ch{index}.json"
        shadow_path = self.run_dir / "shadows" / f"ch{index}.json"
        chapter = _read_json(chapter_path, {})
        shadow = _read_json(shadow_path, {})
        formal = item.get("status") == "done"
        if formal:
            targets = {
                _safe_int(segment.get("index"), offset): str(segment.get("target") or "")
                for offset, segment in enumerate(chapter.get("segments", []))
                if isinstance(segment, dict)
            }
            version = "Formal · 全部质量门已通过"
            updated_path = chapter_path
        else:
            targets = {
                _safe_int(index_text, -1): str(target or "")
                for index_text, target in (shadow.get("targets", {}) or {}).items()
            }
            targets.pop(-1, None)
            phase = str(shadow.get("phase") or item.get("phase") or "等待处理")
            version = f"Shadow · {phase} · 尚未正式通过"
            updated_path = shadow_path if shadow_path.is_file() else chapter_path
        return chapter, targets, version, updated_path

    def book_payload(self) -> dict[str, Any]:
        """Return Formal and partial Shadow availability for every chapter."""
        manifest = _read_json(self.run_dir / "manifest.json", {})
        chapters: list[dict[str, Any]] = []
        total_segments = 0
        translated_segments = 0
        latest_path = self.run_dir / "manifest.json"
        for item in manifest.get("chapters", []):
            if not isinstance(item, dict):
                continue
            index = _safe_int(item.get("index"), len(chapters))
            chapter, targets, version, updated_path = self._chapter_view(item)
            segments = [
                segment
                for segment in chapter.get("segments", [])
                if isinstance(segment, dict) and _has_text(segment.get("source"))
            ]
            translated = sum(
                _has_text(targets.get(_safe_int(segment.get("index"), -1)))
                for segment in segments
            )
            total = len(segments)
            total_segments += total
            translated_segments += translated
            try:
                if updated_path.stat().st_mtime > latest_path.stat().st_mtime:
                    latest_path = updated_path
                updated_at = _mtime_iso(updated_path)
                revision = updated_path.stat().st_mtime_ns
            except OSError:
                updated_at = ""
                revision = 0
            stored_status = str(item.get("status", "pending"))
            display_status = "done" if stored_status == "done" else (
                "shadow" if translated else "pending"
            )
            chapters.append({
                "index": index,
                "title": str(item.get("title") or chapter.get("title") or f"章节 {index + 1}"),
                "status": display_status,
                "stored_status": stored_status,
                "review_status": "done" if stored_status == "done" else (
                    "running" if translated else "pending"
                ),
                "phase": str(item.get("phase") or ""),
                "version": version,
                "translated_segments": translated,
                "total_segments": total,
                "updated_at": updated_at,
                "revision": revision,
            })
        done = sum(chapter["stored_status"] == "done" for chapter in chapters)
        reviewed = done
        failed = sum(bool(chapter.get("error")) for chapter in manifest.get("chapters", []))
        return {
            "title": str(manifest.get("title") or self.run_dir.name),
            "source_lang": str(manifest.get("source_lang", "")),
            "target_lang": str(manifest.get("target_lang", "")),
            "chapter_count": len(chapters),
            "done_chapters": done,
            "total_segments": total_segments,
            "translated_segments": translated_segments,
            "translation_complete": bool(chapters) and done == len(chapters),
            "review_done_chapters": reviewed,
            "review_failed_chapters": failed,
            "review_complete": bool(chapters) and reviewed == len(chapters),
            "updated_at": _mtime_iso(latest_path) if latest_path.is_file() else "",
            "chapters": chapters,
        }

    def chapter_payload(self, index: int) -> dict[str, Any]:
        """Return one chapter using Formal first and the latest partial Shadow otherwise."""
        manifest = _read_json(self.run_dir / "manifest.json", {})
        item = next((
            chapter for chapter in manifest.get("chapters", [])
            if isinstance(chapter, dict) and _safe_int(chapter.get("index"), -1) == index
        ), None)
        if item is None:
            raise KeyError(index)
        chapter, targets, version, updated_path = self._chapter_view(item)
        segments: list[dict[str, Any]] = []
        for raw in chapter.get("segments", []):
            if not isinstance(raw, dict) or not _has_text(raw.get("source")):
                continue
            segment_index = _safe_int(raw.get("index"), len(segments))
            target = targets.get(segment_index, "")
            segments.append({
                "index": segment_index,
                "kind": str(raw.get("kind", "text")),
                "source": str(raw.get("source", "")),
                "target": str(target),
                "translated": _has_text(target),
            })
        stored_status = str(item.get("status", "pending"))
        return {
            "index": index,
            "title": str(item.get("title") or chapter.get("title") or f"章节 {index + 1}"),
            "status": stored_status,
            "review_status": "done" if stored_status == "done" else (
                "running" if any(segment["translated"] for segment in segments) else "pending"
            ),
            "phase": str(item.get("phase") or ""),
            "version": version,
            "translated_segments": sum(segment["translated"] for segment in segments),
            "total_segments": len(segments),
            "updated_at": _mtime_iso(updated_path) if updated_path.is_file() else "",
            "revision": updated_path.stat().st_mtime_ns if updated_path.is_file() else 0,
            "segments": segments,
        }

    def _quality_rows(self) -> tuple[list[dict[str, Any]], Counter[str]]:
        manifest = _read_json(self.run_dir / "manifest.json", {})
        fixes: dict[tuple[int, int], dict[str, Any]] = {}
        validations: dict[tuple[int, int, int], dict[str, Any]] = {}
        audit_rows: list[dict[str, Any]] = []
        for item in manifest.get("chapters", []):
            if not isinstance(item, dict):
                continue
            chapter = _chapter_index(item)
            translation_path = (
                self.run_dir / "artifacts" / "translations" / f"ch{chapter}.jsonl"
            )
            for stage_row in _read_jsonl(translation_path):
                stage = str(stage_row.get("stage", ""))
                if not stage.endswith("_accepted"):
                    continue
                for segment in stage_row.get("segments", []):
                    if not isinstance(segment, dict):
                        continue
                    fixes[(chapter, _safe_int(segment.get("index"), -1))] = {
                        "ts": stage_row.get("ts", ""),
                        "stage": stage,
                        "source": segment.get("source", ""),
                        "before": segment.get("previous_target", ""),
                        "after": segment.get("target", ""),
                    }
            chapter_audits = _read_jsonl(
                self.run_dir / "artifacts" / "audits" / f"ch{chapter}.jsonl"
            )
            for audit in chapter_audits:
                if audit.get("stage") == "chinese_finding_validation":
                    issue = audit.get("reader_issue", {}) or {}
                    result = audit.get("result", {}) or {}
                    key = (
                        chapter,
                        _safe_int(issue.get("start"), -1),
                        _safe_int(issue.get("end"), -1),
                    )
                    validations[key] = result
                audit_rows.append(audit)

        rows: list[dict[str, Any]] = []
        types: Counter[str] = Counter()
        for audit in audit_rows:
            stage = str(audit.get("stage", ""))
            if stage not in {"factual_audit", "chinese_reader_audit"}:
                continue
            chapter = _safe_int(audit.get("chapter"), -1)
            for issue in audit.get("issues", []):
                if not isinstance(issue, dict):
                    continue
                start = _safe_int(issue.get("start"), -1)
                end = _safe_int(issue.get("end"), start)
                issue_type = str(issue.get("type") or "other")
                types[issue_type] += 1
                validation = validations.get((chapter, start, end), {})
                fix = next(
                    (
                        fixes[(chapter, index)]
                        for index in range(start, end + 1)
                        if (chapter, index) in fixes
                    ),
                    {},
                )
                safe = validation.get("safe_to_repair")
                suggestion_parts = [str(issue.get("required_meaning") or "")]
                if validation.get("reason"):
                    suggestion_parts.append(f"原文验证：{validation['reason']}")
                rows.append(
                    {
                        "chapter": chapter,
                        "index": start,
                        "type": issue_type,
                        "detail": f"[{stage}] {issue.get('detail', '')}",
                        "suggestion": "\n".join(part for part in suggestion_parts if part),
                        "fixed": bool(fix),
                        "fix_status": (
                            "autofix_applied" if fix else (
                                "autofix_rejected" if safe is False else ""
                            )
                        ),
                        "source": fix.get("source", ""),
                        "before": fix.get("before", ""),
                        "after": fix.get("after", ""),
                        "proposed": "",
                        "ts": fix.get("ts") or audit.get("ts", ""),
                    }
                )
        rows.sort(
            key=lambda row: (_parse_ts(row.get("ts")), row["chapter"], row["index"]),
            reverse=True,
        )
        return rows, types

    def _activity_rows(self, limit: int) -> list[dict[str, Any]]:
        rows = list(self.events.read())
        manifest = _read_json(self.run_dir / "manifest.json", {})
        for item in manifest.get("chapters", []):
            if not isinstance(item, dict):
                continue
            chapter = _chapter_index(item)
            for audit in _read_jsonl(
                self.run_dir / "artifacts" / "audits" / f"ch{chapter}.jsonl"
            ):
                stage = str(audit.get("stage", ""))
                if stage not in EVENT_LABELS:
                    continue
                rows.append(
                    {
                        "event": stage,
                        "chapter": chapter,
                        "ts": audit.get("ts", ""),
                        "issue_count": len(audit.get("issues", []) or []),
                        "valid": audit.get("valid"),
                    }
                )
            for stage_row in _read_jsonl(
                self.run_dir / "artifacts" / "translations" / f"ch{chapter}.jsonl"
            ):
                stage = str(stage_row.get("stage", ""))
                if stage in EVENT_LABELS:
                    rows.append(
                        {
                            "event": stage,
                            "chapter": chapter,
                            "ts": stage_row.get("ts", ""),
                            "count": stage_row.get("count", 0),
                        }
                    )
        rows.sort(key=lambda row: _parse_ts(row.get("ts")), reverse=True)
        result = []
        for event in rows[:limit]:
            kind = str(event.get("event", ""))
            result.append(
                {
                    "event": kind,
                    "label": EVENT_LABELS.get(kind, kind or "状态更新"),
                    "ts": event.get("ts", ""),
                    "detail": _event_detail(event),
                    "chapter": event.get("chapter"),
                    "severity": "error" if kind == "chapter_failed" else "normal",
                }
            )
        return result

    @staticmethod
    def _usage_view(usage: dict[str, Any]) -> dict[str, Any]:
        totals: Counter[str] = Counter()
        by_stage: dict[str, Counter[str]] = {}
        for provider in usage.get("providers", []) or []:
            if not isinstance(provider, dict):
                continue
            summaries = [provider.get("primary", provider)]
            if isinstance(provider.get("content_policy_fallback"), dict):
                summaries.append(provider["content_policy_fallback"])
            for summary in summaries:
                for key, value in (summary.get("totals", {}) or {}).items():
                    if isinstance(value, (int, float)):
                        totals[key] += value
                for stage, values in (summary.get("by_stage", {}) or {}).items():
                    counter = by_stage.setdefault(stage, Counter())
                    for key, value in (values or {}).items():
                        if isinstance(value, (int, float)):
                            counter[key] += value
        return {
            "totals": dict(totals),
            "by_stage": {stage: dict(values) for stage, values in by_stage.items()},
            "by_tier": {},
        }

    def snapshot(self, limit: int = 60) -> dict[str, Any]:
        manifest = _read_json(self.run_dir / "manifest.json", {})
        usage = _read_json(self.run_dir / "usage.json", {})
        events = self.events.read()
        models = _config_models(self.config_path)
        chapters = [item for item in manifest.get("chapters", []) if isinstance(item, dict)]
        translated = sum(item.get("status") == "done" for item in chapters)
        shadowed = sum(
            any(
                _has_text(value)
                for value in (
                    _read_json(
                        self.run_dir / "shadows" / f"ch{_chapter_index(item)}.json",
                        {},
                    ).get("targets", {})
                    or {}
                ).values()
            )
            for item in chapters
        )
        reviewed = translated
        review_failed = sum(bool(item.get("error")) for item in chapters)
        issues, issue_types = self._quality_rows()
        latest_event = events[-1] if events else {}
        event_rows = self._activity_rows(limit)
        fixed = sum(bool(row.get("fixed")) for row in issues)
        return {
            "book": manifest.get("title") or self.run_dir.name,
            "observed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "run_dir": str(self.run_dir),
            "last_event_at": latest_event.get("ts", ""),
            "last_event_age_seconds": max(0, round(time.time() - _parse_ts(latest_event.get("ts")))) if latest_event else None,
            "current": _current_work(manifest, models),
            "progress": {
                "chapters": len(chapters),
                "translated": max(translated, shadowed),
                "reviewed": reviewed,
                "review_running": sum(bool(item.get("phase")) for item in chapters),
                "review_failed": review_failed,
            },
            "quality": {
                "issues": len(issues),
                "fixed": fixed,
                "unfixed": len(issues) - fixed,
                "types": dict(issue_types.most_common()),
            },
            "usage": self._usage_view(usage),
            "models": models,
            "issues": issues[:limit],
            "events": event_rows,
            "visibility": {
                "available": ["阶段与章节", "已落盘的原文/译文", "结构化审校理由", "修复前后", "错误与回退", "调用与 token"],
                "unavailable": "模型私有思维链不会由当前接口返回；这里不生成或猜测它。",
            },
        }


class Handler(BaseHTTPRequestHandler):
    observer: Observer

    def _send_json(self, data: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        parsed = urlparse(self.path)
        if parsed.path == "/api/snapshot":
            query = parse_qs(parsed.query)
            limit = max(10, min(200, _safe_int((query.get("limit") or [60])[0], 60)))
            try:
                self._send_json(self.observer.snapshot(limit))
            except Exception as error:  # keep the observer from affecting the run
                self._send_json({"error": f"读取观察数据失败：{type(error).__name__}: {error}"}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        if parsed.path == "/api/book":
            try:
                self._send_json(self.observer.book_payload())
            except Exception as error:
                self._send_json({"error": f"读取书籍状态失败：{type(error).__name__}: {error}"}, HTTPStatus.SERVICE_UNAVAILABLE)
            return
        chapter_match = re.fullmatch(r"/api/chapters/(\d+)", parsed.path)
        if chapter_match:
            try:
                self._send_json(self.observer.chapter_payload(int(chapter_match.group(1))))
            except KeyError:
                self._send_json({"error": "chapter_not_found"}, HTTPStatus.NOT_FOUND)
            except Exception as error:
                self._send_json({"error": f"读取章节失败：{type(error).__name__}: {error}"}, HTTPStatus.SERVICE_UNAVAILABLE)
            return
        relative = "monitor.html" if parsed.path == "/" else parsed.path.lstrip("/")
        if relative not in {"monitor.html"}:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        path = ROOT / relative
        try:
            payload = path.read_bytes()
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))


def serve(
    run_dir: Path,
    config_path: Path | None = None,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
) -> None:
    run_dir = run_dir.expanduser().resolve()
    if not (run_dir / "manifest.json").is_file():
        raise FileNotFoundError(run_dir / "manifest.json")
    config_path = config_path.expanduser().resolve() if config_path else None
    if config_path is not None and not config_path.is_file():
        raise FileNotFoundError(config_path)
    Handler.observer = Observer(run_dir, config_path)
    server = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{server.server_port}/"
    print(f"Wenyi Direct Monitor: {url}", flush=True)
    print(f"Read-only run directory: {run_dir}", flush=True)
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main() -> int:
    if os.name == "nt":
        # Keep Chinese help/status readable in modern PowerShell and redirected logs.
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(
        description="为指定 Wenyi 书籍启动只读的审核监控与情节阅读页面"
    )
    parser.add_argument(
        "run_dir",
        type=Path,
        help="包含 manifest.json、events.jsonl 和 chapters/ 的书籍状态目录",
    )
    parser.add_argument("--config", type=Path, help="可选：只读配置文件，用于显示各阶段模型")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--open",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="启动后打开默认浏览器；使用 --no-open 禁用（默认：打开）",
    )
    args = parser.parse_args()
    run_dir = args.run_dir.expanduser().resolve()
    if not (run_dir / "manifest.json").is_file():
        parser.error(f"找不到 {run_dir / 'manifest.json'}")
    config_path = args.config.expanduser().resolve() if args.config else None
    if config_path is not None and not config_path.is_file():
        parser.error(f"找不到配置文件：{config_path}")
    serve(
        run_dir,
        config_path,
        host=args.host,
        port=args.port,
        open_browser=args.open,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
