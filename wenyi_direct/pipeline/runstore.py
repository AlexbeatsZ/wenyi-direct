"""运行态持久化：支持断点续跑。

目录结构（state_dir/<book-slug>/）：
  manifest.json     书籍元信息 + 各章状态
  chapters/ch{n}.json  各章（含 source/target 的 Segment）
  source/           输入预处理缓存（例如 PDF 转换后的 HTML）
  context.json      滚动上下文（梗概 + 前文尾段）
  analysis.json     全局分析结果
  usage.json        本书跨 translate/resume 累计的 LLM token 用量
  glossary.db       术语库 + 翻译记忆库
  report.json       QA 报告
  events.jsonl      追加式行为 / 改写 / 翻译结果日志
  artifacts/inputs/ 内容寻址的术语、上下文与叙事事实输入快照
  artifacts/translations/ch{n}.jsonl  初译、精修、标点与终审版本链
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from ..ingest.models import Chapter, Document

STATUS_PENDING = "pending"
STATUS_DONE = "done"
REVIEW_PENDING = "pending"
REVIEW_RUNNING = "running"
REVIEW_DONE = "done"
REVIEW_FAILED = "failed"


def slugify(name: str) -> str:
    """把书名转换为适合作为状态目录名的稳定短名。"""
    s = re.sub(r"[^\w一-鿿぀-ヿ-]+", "_", name).strip("_")
    return s or "book"


class RunStore:
    def __init__(self, run_dir: str, *, create: bool = True):
        """绑定一本书的状态目录，并按需创建章节子目录。"""
        self.run_dir = run_dir
        self.chapters_dir = os.path.join(run_dir, "chapters")
        self._batch_glossary_event_cache: dict[int, set[str]] | None = None
        self._thread_lock = threading.RLock()
        if create:
            self.ensure_dirs()

    def ensure_dirs(self) -> None:
        """创建运行目录及章节状态子目录。"""
        os.makedirs(self.chapters_dir, exist_ok=True)

    @contextmanager
    def lock(self) -> Iterator[None]:
        """Serialize mutations for one book across independent processes."""
        self.ensure_dirs()
        lock_path = os.path.join(self.run_dir, ".run.lock")
        with open(lock_path, "a+b") as lock_file:
            if os.name == "nt":  # pragma: no cover - Windows-specific
                import msvcrt

                lock_file.seek(0, os.SEEK_END)
                if lock_file.tell() == 0:
                    lock_file.write(b"\0")
                    lock_file.flush()
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    yield
                finally:
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def is_busy(self) -> bool:
        """不阻塞地判断是否有另一个进程持有本书运行锁。"""
        lock_path = os.path.join(self.run_dir, ".run.lock")
        try:
            lock_file = open(lock_path, "r+b")
        except FileNotFoundError:
            return False

        with lock_file:
            lock_file.seek(0, os.SEEK_END)
            if lock_file.tell() == 0:
                return False
            lock_file.seek(0)
            if os.name == "nt":  # pragma: no cover - Windows-specific
                import msvcrt

                try:
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError:
                    return True
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                return False

            import fcntl

            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return True
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            return False

    def require_formal_complete(self) -> None:
        """Reject every export path unless all formal chapters are truly complete."""
        manifest = self.load_manifest()
        incomplete = [
            item["index"]
            for item in manifest["chapters"]
            if item.get("status") != STATUS_DONE
        ]
        empty: list[tuple[int, int]] = []
        for item in manifest["chapters"]:
            if item.get("status") != STATUS_DONE:
                continue
            chapter = self.load_chapter(item["index"])
            empty.extend(
                (chapter.index, segment.index)
                for segment in chapter.text_segments
                if not (segment.target or "").strip()
            )
        if incomplete or empty:
            raise RuntimeError(
                "formal translation is incomplete: "
                f"chapters={incomplete}, empty_targets={empty}"
            )

    # ── 路径 ──────────────────────────────────────────────────────────────
    @property
    def manifest_path(self) -> str:
        """返回书籍清单文件路径。"""
        return os.path.join(self.run_dir, "manifest.json")

    @property
    def context_path(self) -> str:
        """返回滚动上下文文件路径。"""
        return os.path.join(self.run_dir, "context.json")

    @property
    def analysis_path(self) -> str:
        """返回全书风格分析文件路径。"""
        return os.path.join(self.run_dir, "analysis.json")

    @property
    def glossary_path(self) -> str:
        """返回术语及翻译记忆数据库路径。"""
        return os.path.join(self.run_dir, "glossary.db")

    @property
    def report_path(self) -> str:
        """返回质量报告文件路径。"""
        return os.path.join(self.run_dir, "report.json")

    @property
    def usage_path(self) -> str:
        """返回本书累计 token 用量文件路径。"""
        return os.path.join(self.run_dir, "usage.json")

    @property
    def event_log_path(self) -> str:
        """返回追加式 JSONL 事件日志路径。"""
        return os.path.join(self.run_dir, "events.jsonl")

    @property
    def artifacts_dir(self) -> str:
        """返回追加式阶段产物与内容寻址输入快照目录。"""
        return os.path.join(self.run_dir, "artifacts")

    def translation_artifact_path(self, chapter: int) -> str:
        """返回一章所有初译、精修和审校版本的追加式 JSONL 路径。"""
        return os.path.join(
            self.artifacts_dir,
            "translations",
            f"ch{chapter}.jsonl",
        )

    @property
    def shadows_dir(self) -> str:
        """Return resumable candidate state, which is never used by assembly."""
        return os.path.join(self.run_dir, "shadows")

    @property
    def superseded_shadows_dir(self) -> str:
        """Return immutable archives of Shadows replaced by another workflow."""
        return os.path.join(self.artifacts_dir, "superseded_shadows")

    def shadow_path(self, chapter: int) -> str:
        return os.path.join(self.shadows_dir, f"ch{chapter}.json")

    def audit_artifact_path(self, chapter: int) -> str:
        return os.path.join(self.artifacts_dir, "audits", f"ch{chapter}.jsonl")

    def chapter_path(self, ci: int) -> str:
        """返回指定章节索引对应的状态文件路径。"""
        return os.path.join(self.chapters_dir, f"ch{ci}.json")

    @property
    def source_dir(self) -> str:
        """返回输入预处理缓存目录；由具体读取器按需创建。"""
        return os.path.join(self.run_dir, "source")

    # ── 通用 JSON ─────────────────────────────────────────────────────────
    @staticmethod
    def _write_json(path: str, data) -> None:
        """通过同目录临时文件原子写入格式化 JSON。"""
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)  # 原子替换，防写一半中断

    @staticmethod
    def _read_json(path: str):
        """读取并解析 UTF-8 JSON 文件。"""
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def exists(self) -> bool:
        """判断运行状态是否已完成初始化并写入 manifest。"""
        return os.path.isfile(self.manifest_path)

    # ── manifest ──────────────────────────────────────────────────────────
    def stage_document(self, doc: Document) -> dict:
        """写入初始章节文件并返回 manifest 内容，但不提前写 manifest。

        manifest 是一次运行初始化完成的标志，由调用方在分析、术语库
        和上下文都已落盘后最后保存。
        """
        manifest = {
            "title": doc.title,
            "fmt": doc.fmt,
            "source_path": doc.source_path,
            "source_lang": doc.source_lang,
            "target_lang": doc.target_lang,
            "meta": doc.meta,
            "chapters": [
                {"index": c.index, "title": c.title,
                 "href": c.href,
                 "toc_entry_id": c.meta.get("toc_entry_id"),
                 "status": STATUS_PENDING,
                 "review_status": REVIEW_PENDING}
                for c in doc.chapters
            ],
        }
        for c in doc.chapters:
            self.save_chapter(c)
        return manifest

    def save_manifest(self, manifest: dict) -> None:
        """原子保存书籍清单和章节状态。"""
        with self._thread_lock:
            self._write_json(self.manifest_path, manifest)

    def load_manifest(self) -> dict:
        """读取书籍清单和章节状态。"""
        with self._thread_lock:
            return self._read_json(self.manifest_path)

    def set_chapter_status(self, ci: int, status: str) -> None:
        """更新指定章节状态并原子保存整份 manifest。"""
        with self._thread_lock:
            manifest = self.load_manifest()
            for c in manifest["chapters"]:
                if c["index"] == ci:
                    c["status"] = status
                    break
            self.save_manifest(manifest)

    def set_chapter_review_status(self, ci: int, status: str) -> None:
        """更新指定章节的独立审校状态并原子保存 manifest。"""
        with self._thread_lock:
            manifest = self.load_manifest()
            for chapter in manifest["chapters"]:
                if chapter["index"] == ci:
                    chapter["review_status"] = status
                    break
            self.save_manifest(manifest)

    def set_chapter_fields(self, ci: int, **fields: Any) -> None:
        """Atomically update arbitrary observable chapter pipeline fields."""
        with self._thread_lock:
            manifest = self.load_manifest()
            for chapter in manifest["chapters"]:
                if chapter["index"] == ci:
                    chapter.update(fields)
                    break
            self.save_manifest(manifest)

    def pending_chapters(self) -> list[int]:
        """返回尚未标记完成的章节索引。"""
        manifest = self.load_manifest()
        return [c["index"] for c in manifest["chapters"] if c["status"] != STATUS_DONE]

    # ── 章 ────────────────────────────────────────────────────────────────
    def save_chapter(self, chapter: Chapter) -> None:
        """原子保存一个章节的源文、译文和阶段元数据。"""
        self._write_json(self.chapter_path(chapter.index), chapter.to_dict())

    def load_chapter(self, ci: int) -> Chapter:
        """读取并校验指定章节状态。"""
        return Chapter.from_dict(self._read_json(self.chapter_path(ci)))

    def save_shadow(self, chapter: int, data: dict[str, Any]) -> None:
        """Atomically save the current candidate without altering formal text."""
        self._write_json(self.shadow_path(chapter), data)

    def load_shadow(self, chapter: int) -> dict[str, Any] | None:
        path = self.shadow_path(chapter)
        return self._read_json(path) if os.path.isfile(path) else None

    def archive_shadow(self, chapter: int, *, reason: str) -> str | None:
        """Preserve the current Shadow before intentionally replacing it."""
        with self._thread_lock:
            shadow = self.load_shadow(chapter)
            if shadow is None:
                return None
            stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%f%z")
            filename = f"ch{chapter}.{stamp}.{uuid.uuid4().hex}.json"
            path = os.path.join(self.superseded_shadows_dir, filename)
            self._write_json(
                path,
                {
                    "schema": 1,
                    "archived_at": datetime.now().astimezone().isoformat(
                        timespec="microseconds"
                    ),
                    "reason": reason,
                    "shadow": shadow,
                },
            )
            return path

    def record_audit(self, chapter: int, stage: str, payload: dict[str, Any]) -> None:
        """Append an immutable audit request/result summary."""
        row = {
            "ts": datetime.now().astimezone().isoformat(timespec="microseconds"),
            "stage": stage,
            "chapter": chapter,
            **payload,
        }
        path = self.audit_artifact_path(chapter)
        with self._thread_lock:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as file:
                file.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    # ── 上下文 / 分析 / 报告 ──────────────────────────────────────────────
    def save_context(self, data: dict) -> None:
        """原子保存滚动上下文快照。"""
        self._write_json(self.context_path, data)

    def load_context(self) -> dict | None:
        """读取滚动上下文；文件尚不存在时返回 None。"""
        return self._read_json(self.context_path) if os.path.isfile(self.context_path) else None

    def save_analysis(self, data: dict) -> None:
        """原子保存全书分析和概览数据。"""
        self._write_json(self.analysis_path, data)

    def load_analysis(self) -> dict | None:
        """读取全书分析；文件尚不存在时返回 None。"""
        return self._read_json(self.analysis_path) if os.path.isfile(self.analysis_path) else None

    def save_report(self, data: dict) -> None:
        """原子保存质量检查报告。"""
        self._write_json(self.report_path, data)

    def save_usage(self, data: dict) -> None:
        """原子保存本书累计 token 用量。"""
        self._write_json(self.usage_path, data)

    def load_usage(self) -> dict | None:
        """读取累计 token 用量；文件尚不存在时返回 None。"""
        return self._read_json(self.usage_path) if os.path.isfile(self.usage_path) else None

    # ── 可追溯阶段产物 ──────────────────────────────────────────────────
    def save_translation_input(self, data: dict[str, Any]) -> dict[str, str]:
        """内容寻址保存一次翻译请求的结构化输入，返回稳定引用。

        相同上下文只存一份，阶段日志引用其 SHA-256；这既保留了术语、叙事
        事实和滚动上下文，又避免在每条事件里重复整段提示输入。
        """
        encoded = json.dumps(
            data,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        relative = os.path.join("artifacts", "inputs", f"{digest}.json")
        path = os.path.join(self.run_dir, relative)
        with self._thread_lock:
            if not os.path.isfile(path):
                self._write_json(path, data)
        return {"path": relative.replace("\\", "/"), "sha256": digest}

    def record_translation_stage(
        self,
        stage: str,
        *,
        chapter: int,
        start_index: int,
        sources: list[str],
        targets: list[str],
        previous_targets: list[str] | None = None,
        input_ref: dict[str, str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        """追加保存一个阶段的逐段版本，不改写任何既有产物。"""
        if len(sources) != len(targets):
            raise ValueError("阶段产物 source/target 数量不一致")
        if previous_targets is not None and len(previous_targets) != len(targets):
            raise ValueError("阶段产物 previous_targets 数量不一致")
        record_id = f"stage-{uuid.uuid4().hex}"
        segments = []
        for offset, (source, target) in enumerate(zip(sources, targets)):
            item: dict[str, Any] = {
                "index": start_index + offset,
                "source": source,
                "target": target,
            }
            if previous_targets is not None:
                item["previous_target"] = previous_targets[offset]
            segments.append(item)
        row = {
            "ts": datetime.now().astimezone().isoformat(timespec="microseconds"),
            "record_id": record_id,
            "stage": stage,
            "chapter": chapter,
            "start_index": start_index,
            "count": len(segments),
            "input_ref": input_ref,
            "metadata": metadata or {},
            "segments": segments,
        }
        path = self.translation_artifact_path(chapter)
        with self._thread_lock:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as file:
                file.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        relative = os.path.relpath(path, self.run_dir).replace("\\", "/")
        return {"path": relative, "record_id": record_id}

    # ── 批次恢复检查点 ────────────────────────────────────────────────────
    @staticmethod
    def batch_glossary_key(start_index: int, count: int) -> str:
        """返回批次术语抽取检查点键；批次边界变化时不会误命中旧键。"""
        return f"{start_index}:{count}"

    def completed_batch_glossary_keys(self, chapter: int) -> set[str]:
        """从事件日志恢复已完成的批次术语抽取；每个实例最多扫描一次。"""
        if self._batch_glossary_event_cache is None:
            completed: dict[int, set[str]] = {}
            if os.path.isfile(self.event_log_path):
                with open(self.event_log_path, "r", encoding="utf-8") as f:
                    for line in f:
                        try:
                            row = json.loads(line)
                        except (json.JSONDecodeError, TypeError):
                            continue
                        if row.get("event") != "batch_glossary_extracted":
                            continue
                        ci = row.get("chapter")
                        start = row.get("start_index")
                        count = row.get("count")
                        if not (
                            isinstance(ci, int)
                            and isinstance(start, int)
                            and isinstance(count, int)
                        ):
                            continue
                        completed.setdefault(ci, set()).add(
                            self.batch_glossary_key(start, count)
                        )
            self._batch_glossary_event_cache = completed
        return set(self._batch_glossary_event_cache.get(chapter, set()))

    def refinement_pending_indexes(self, chapter: int) -> set[int]:
        """从追加事件恢复尚未成功精修的绝对段号。"""
        pending: set[int] = set()
        if not os.path.isfile(self.event_log_path):
            return pending
        with open(self.event_log_path, "r", encoding="utf-8") as file:
            for line in file:
                try:
                    row = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                if row.get("chapter") != chapter:
                    continue
                event = row.get("event")
                start = row.get("start_index")
                count = row.get("count")
                if not isinstance(start, int) or not isinstance(count, int):
                    continue
                batch_indexes = set(range(start, start + count))
                if event == "batch_translated":
                    pending.difference_update(batch_indexes)
                    if row.get("polish_requested"):
                        pending.update(
                            start + index
                            for index in row.get("polish_failed_indexes", [])
                            if isinstance(index, int) and 0 <= index < count
                        )
                elif event == "batch_repolished":
                    pending.difference_update(batch_indexes)
                    pending.update(
                        index
                        for index in row.get("failed_indexes", [])
                        if isinstance(index, int) and index in batch_indexes
                    )
        return pending

    # ── 追加式事件日志 ────────────────────────────────────────────────────
    def log_event(self, event: str, **data: Any) -> None:
        """追加一条 JSONL 事件，用于翻译行为、改写前后和产物对账。"""
        self.ensure_dirs()
        row = {
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            "event": event,
            **data,
        }
        with self._thread_lock:
            with open(self.event_log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            if event == "batch_glossary_extracted" and self._batch_glossary_event_cache is not None:
                chapter = data.get("chapter")
                start = data.get("start_index")
                count = data.get("count")
                if (
                    isinstance(chapter, int)
                    and isinstance(start, int)
                    and isinstance(count, int)
                ):
                    self._batch_glossary_event_cache.setdefault(chapter, set()).add(
                        self.batch_glossary_key(start, count)
                    )
