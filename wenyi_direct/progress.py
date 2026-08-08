"""Thread-safe pipeline progress events and Rich CLI rendering."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)


@dataclass(frozen=True)
class ProgressEvent:
    kind: str
    operation: str = ""
    chapters: tuple[int, ...] = ()
    chapter: int | None = None
    stage: str = ""
    completed: int | None = None
    total: int | None = None
    detail: str = ""
    error: str = ""
    payload: dict[str, Any] | None = None


ProgressCallback = Callable[[ProgressEvent], None]


_OPERATION_LABELS = {
    "translate": "翻译与质量门",
    "translate-parallel": "双线翻译与质量门",
    "review": "Formal 复审",
    "review-parallel": "双线 Formal 复审",
    "stage": "独立阶段",
}

_STAGE_LABELS = {
    "translate": "直接翻译",
    "factual-audit": "事实审校",
    "factual-repair": "事实修复",
    "chinese-audit": "中文阅读审校",
    "chinese-repair": "中文修复",
    "promote": "原子提升",
}


class RichProgressDisplay:
    """Render one overall bar plus one live row per concurrently active chapter."""

    def __init__(self, console: Console) -> None:
        self._lock = threading.RLock()
        self._progress = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TaskProgressColumn(),
            TextColumn("[dim]{task.fields[detail]}"),
            TimeElapsedColumn(),
            console=console,
            transient=False,
        )
        self._prepare_task: int | None = None
        self._overall_task: int | None = None
        self._stage_tasks: dict[int, int] = {}
        self._completed_chapters: set[int] = set()

    def __enter__(self) -> "RichProgressDisplay":
        self._progress.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._progress.stop()

    def __call__(self, event: ProgressEvent) -> None:
        with self._lock:
            handler = getattr(self, f"_on_{event.kind.replace('-', '_')}", None)
            if handler is not None:
                handler(event)

    def _on_prepare_started(self, event: ProgressEvent) -> None:
        if self._prepare_task is None:
            self._prepare_task = self._progress.add_task(
                "[cyan]解析并校验源文件", total=None, detail=event.detail
            )

    def _on_prepare_completed(self, event: ProgressEvent) -> None:
        if self._prepare_task is not None:
            self._progress.update(
                self._prepare_task,
                total=1,
                completed=1,
                detail=event.detail or "状态已就绪",
            )

    def _on_operation_started(self, event: ProgressEvent) -> None:
        label = _OPERATION_LABELS.get(event.operation, event.operation or "处理")
        self._completed_chapters.clear()
        self._overall_task = self._progress.add_task(
            f"[bold green]{label}",
            total=len(event.chapters),
            completed=0,
            detail=f"章节 {self._format_chapters(event.chapters)}",
        )

    def _on_stage_started(self, event: ProgressEvent) -> None:
        assert event.chapter is not None
        existing = self._stage_tasks.pop(event.chapter, None)
        if existing is not None:
            self._progress.remove_task(existing)
        label = _STAGE_LABELS.get(event.stage, event.stage)
        self._stage_tasks[event.chapter] = self._progress.add_task(
            f"  [cyan]ch{event.chapter}[/cyan] {label}",
            total=None,
            detail=event.detail or "模型处理中",
        )

    def _on_stage_progress(self, event: ProgressEvent) -> None:
        assert event.chapter is not None
        task_id = self._stage_tasks.get(event.chapter)
        if task_id is None:
            self._on_stage_started(event)
            task_id = self._stage_tasks[event.chapter]
        total = max(1, event.total or 1)
        completed = min(total, max(0, event.completed or 0))
        self._progress.update(
            task_id,
            total=total,
            completed=completed,
            detail=event.detail or "模型处理中",
        )

    def _on_stage_activity(self, event: ProgressEvent) -> None:
        if event.chapter is None:
            return
        task_id = self._stage_tasks.get(event.chapter)
        if task_id is not None:
            self._progress.update(task_id, detail=event.detail or "模型处理中")

    def _on_stage_completed(self, event: ProgressEvent) -> None:
        if event.chapter is None:
            return
        task_id = self._stage_tasks.pop(event.chapter, None)
        if task_id is not None:
            task = self._task(task_id)
            total = task.total or 1
            self._progress.update(task_id, total=total, completed=total, detail="完成")
            self._progress.remove_task(task_id)

    def _on_stage_failed(self, event: ProgressEvent) -> None:
        if event.chapter is None:
            return
        task_id = self._stage_tasks.get(event.chapter)
        if task_id is not None:
            self._progress.update(
                task_id,
                description=f"[red]ch{event.chapter} 失败",
                detail=event.error,
            )

    def _on_chapter_completed(self, event: ProgressEvent) -> None:
        if event.chapter is None or event.chapter in self._completed_chapters:
            return
        self._completed_chapters.add(event.chapter)
        if self._overall_task is not None:
            self._progress.advance(self._overall_task)

    def _on_operation_completed(self, _event: ProgressEvent) -> None:
        if self._overall_task is not None:
            task = self._task(self._overall_task)
            self._progress.update(
                self._overall_task,
                completed=task.total or len(self._completed_chapters),
                detail="完成",
            )

    def _on_audit_log(self, event: ProgressEvent) -> None:
        record = {
            "event": event.detail or "audit_log",
            "chapter": event.chapter,
            "stage": event.stage,
            "data": event.payload or {},
        }
        self._progress.console.print(
            json.dumps(record, ensure_ascii=False, sort_keys=True),
            markup=False,
            highlight=False,
            soft_wrap=True,
        )

    def _task(self, task_id: int):
        return next(task for task in self._progress.tasks if task.id == task_id)

    @staticmethod
    def _format_chapters(chapters: tuple[int, ...]) -> str:
        if not chapters:
            return "无待处理章节"
        if len(chapters) <= 5:
            return ",".join(map(str, chapters))
        return f"{chapters[0]}–{chapters[-1]}（{len(chapters)}章）"


__all__ = ["ProgressCallback", "ProgressEvent", "RichProgressDisplay"]
