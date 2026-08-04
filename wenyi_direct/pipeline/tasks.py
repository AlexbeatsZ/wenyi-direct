"""Granular pipeline tasks and a one-chapter staggered scheduler."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from ..ingest.models import Chapter
from ..prompts import (
    chinese_finding_validation_messages,
    chinese_reader_messages,
    factual_audit_messages,
)
from .direct import DirectPipeline
from .runstore import STATUS_DONE, STATUS_PENDING, RunStore
from .types import TranslationWindow, chapter_source_digest

_STAGE_NAMES = {
    "translate",
    "factual-audit",
    "factual-repair",
    "chinese-audit",
    "chinese-repair",
    "promote",
}


class StageTaskError(RuntimeError):
    """The requested task does not match the chapter's persisted state."""


class _ConcurrentStore:
    """Serialize only shared book-level writes while chapter files run in parallel."""

    def __init__(self, store: RunStore) -> None:
        self._store = store
        self._shared_lock = threading.RLock()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)

    def load_manifest(self) -> dict[str, Any]:
        with self._shared_lock:
            return self._store.load_manifest()

    def save_manifest(self, manifest: dict[str, Any]) -> None:
        with self._shared_lock:
            self._store.save_manifest(manifest)

    def set_chapter_fields(self, chapter: int, **fields: Any) -> None:
        with self._shared_lock:
            self._store.set_chapter_fields(chapter, **fields)

    def log_event(self, event: str, **data: Any) -> None:
        with self._shared_lock:
            self._store.log_event(event, **data)

    def record_audit(self, chapter: int, stage: str, payload: dict[str, Any]) -> None:
        with self._shared_lock:
            self._store.record_audit(chapter, stage, payload)

    def record_translation_stage(self, *args: Any, **kwargs: Any) -> dict[str, str]:
        with self._shared_lock:
            return self._store.record_translation_stage(*args, **kwargs)

    def save_translation_input(self, data: dict[str, Any]) -> dict[str, str]:
        with self._shared_lock:
            return self._store.save_translation_input(data)

    def save_usage(self, data: dict[str, Any]) -> None:
        with self._shared_lock:
            self._store.save_usage(data)


class TaskPipeline(DirectPipeline):
    """Expose every major model task without changing the default full pipeline."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._allow_provisional_factual_context = False
        self._terminology_lock = threading.RLock()

    @staticmethod
    def normalise_task(value: str) -> str:
        task = value.strip().casefold().replace("_", "-")
        if task not in _STAGE_NAMES:
            raise ValueError(f"unknown task {value!r}; choose from {sorted(_STAGE_NAMES)}")
        return task

    def run_stage(
        self,
        source_path: str | Path,
        task: str,
        *,
        chapters: set[int] | None = None,
    ) -> RunStore:
        """Run exactly one persisted task for each selected chapter."""
        task = self.normalise_task(task)
        store = self.prepare(source_path)
        with store.lock():
            selected = self._selected_chapters(store, chapters)
            if chapters is None:
                selected = [
                    index
                    for index in selected
                    if self._task_is_ready(store, index, task)
                ]
            for chapter_index in selected:
                try:
                    self._run_task_locked(store, chapter_index, task)
                finally:
                    self._save_usage(store)
        return store

    def run_fast(
        self,
        source_path: str | Path,
        *,
        chapters: set[int] | None = None,
    ) -> RunStore:
        """Overlap chapter N Chinese review with chapter N+1 factual work."""
        store = self.prepare(source_path)
        selected = self._selected_chapters(store, chapters, pending_only=True)
        if not selected:
            return store
        concurrent_store = _ConcurrentStore(store)
        with store.lock():
            self._allow_provisional_factual_context = True
            try:
                self._run_upstream_full(concurrent_store, selected[0])
                previous = selected[0]
                for current in selected[1:]:
                    concurrent_store.log_event(
                        "staggered_pair_started",
                        chinese_chapter=previous,
                        factual_chapter=current,
                    )
                    with ThreadPoolExecutor(max_workers=2) as executor:
                        downstream = executor.submit(
                            self._run_downstream, concurrent_store, previous
                        )
                        upstream = executor.submit(
                            self._run_upstream_audit, concurrent_store, current
                        )
                        downstream.result()
                        upstream.result()
                    self._run_upstream_repair(concurrent_store, current)
                    concurrent_store.log_event(
                        "staggered_pair_completed",
                        chinese_chapter=previous,
                        factual_chapter=current,
                    )
                    self._save_usage(concurrent_store)
                    previous = current
                self._run_downstream(concurrent_store, previous)
                self._save_usage(concurrent_store)
            finally:
                self._allow_provisional_factual_context = False
        return store

    def _task_is_ready(self, store: RunStore, chapter_index: int, task: str) -> bool:
        manifest = store.load_manifest()
        status = {
            int(item["index"]): item.get("status") for item in manifest["chapters"]
        }
        if status.get(chapter_index) == STATUS_DONE:
            return False
        shadow = store.load_shadow(chapter_index)
        if not isinstance(shadow, dict):
            return task == "translate"
        return self._next_task(shadow) == task

    @staticmethod
    def _selected_chapters(
        store: RunStore,
        chapters: set[int] | None,
        *,
        pending_only: bool = False,
    ) -> list[int]:
        manifest = store.load_manifest()
        known = {int(item["index"]) for item in manifest["chapters"]}
        selected = known if chapters is None else set(chapters)
        unknown = selected - known
        if unknown:
            raise ValueError(f"unknown chapter indexes: {sorted(unknown)}")
        if pending_only:
            status = {
                int(item["index"]): item.get("status") for item in manifest["chapters"]
            }
            selected = {index for index in selected if status.get(index) != STATUS_DONE}
        return sorted(selected)

    def _ensure_shadow(
        self, store: RunStore, chapter: Chapter
    ) -> dict[str, Any]:
        shadow = store.load_shadow(chapter.index)
        digest = chapter_source_digest(chapter)
        if shadow is not None:
            if shadow.get("source_digest") != digest:
                raise RuntimeError(
                    f"chapter {chapter.index} source changed after shadow creation"
                )
            return shadow
        shadow = {
            "schema": 2,
            "chapter": chapter.index,
            "source_digest": digest,
            "phase": "translate",
            "targets": {
                str(segment.index): segment.target or "" for segment in chapter.segments
            },
            "translated_ids": [],
            "stage_snapshots": {},
        }
        self._save_shadow(store, chapter.index, shadow)
        return shadow

    def _require_phase(
        self,
        chapter: int,
        shadow: dict[str, Any],
        expected: str,
        task: str,
    ) -> None:
        actual = str(shadow.get("phase", "unknown"))
        if actual != expected:
            raise StageTaskError(
                f"chapter {chapter} cannot run {task}: phase is {actual}, expected {expected}"
            )

    def _validate_task(
        self, chapter_index: int, shadow: dict[str, Any], task: str
    ) -> None:
        expected = {
            "translate": "translate",
            "factual-audit": "factual_audit",
            "factual-repair": "factual_audit",
            "chinese-audit": "chinese_audit",
            "chinese-repair": "chinese_audit",
            "promote": "promote",
        }[task]
        self._require_phase(chapter_index, shadow, expected, task)
        if task == "factual-repair":
            state = shadow.get("factual_state", {})
            if not isinstance(state, dict) or state.get("audit_complete") is not True:
                raise StageTaskError(
                    f"chapter {chapter_index} requires factual-audit before factual-repair"
                )
        if task == "chinese-repair":
            state = shadow.get("chinese_state", {})
            if not isinstance(state, dict) or state.get("audit_complete") is not True:
                raise StageTaskError(
                    f"chapter {chapter_index} requires chinese-audit before chinese-repair"
                )

    def _run_task_locked(
        self, store: RunStore, chapter_index: int, task: str
    ) -> None:
        chapter = store.load_chapter(chapter_index)
        shadow = self._ensure_shadow(store, chapter)
        self._validate_task(chapter_index, shadow, task)
        self._validate_task(chapter_index, shadow, task)
        self._validate_task(chapter_index, shadow, task)
        self._validate_task(chapter_index, shadow, task)
        self._validate_task(chapter_index, shadow, task)
        self._validate_task(chapter_index, shadow, task)
        self._validate_task(chapter_index, shadow, task)
        self._validate_task(chapter_index, shadow, task)
        store.set_chapter_fields(
            chapter_index,
            status=STATUS_PENDING,
            phase=shadow["phase"],
            task=task,
            error=None,
        )
        store.log_event("stage_task_started", chapter=chapter_index, task=task)
        try:
            if task == "translate":
                self._require_phase(chapter_index, shadow, "translate", task)
                self._translate_chapter(store, chapter, shadow)
            elif task == "factual-audit":
                self._require_phase(chapter_index, shadow, "factual_audit", task)
                self._factual_audit_only(store, chapter, shadow)
            elif task == "factual-repair":
                self._require_phase(chapter_index, shadow, "factual_audit", task)
                state = shadow.get("factual_state", {})
                if not isinstance(state, dict) or state.get("audit_complete") is not True:
                    raise StageTaskError(
                        f"chapter {chapter_index} requires factual-audit before factual-repair"
                    )
                super()._factual_stage(store, chapter, shadow)
            elif task == "chinese-audit":
                self._require_phase(chapter_index, shadow, "chinese_audit", task)
                self._chinese_audit_only(store, chapter, shadow)
            elif task == "chinese-repair":
                self._require_phase(chapter_index, shadow, "chinese_audit", task)
                state = shadow.get("chinese_state", {})
                if not isinstance(state, dict) or state.get("audit_complete") is not True:
                    raise StageTaskError(
                        f"chapter {chapter_index} requires chinese-audit before chinese-repair"
                    )
                super()._chinese_stage(store, chapter, shadow)
            else:
                self._require_phase(chapter_index, shadow, "promote", task)
                self._promote(store, chapter, shadow)
        except Exception as error:
            store.set_chapter_fields(
                chapter_index,
                phase=shadow.get("phase", "unknown"),
                task=task,
                error=str(error),
            )
            store.log_event(
                "stage_task_failed",
                chapter=chapter_index,
                task=task,
                error=str(error),
            )
            raise
        store.set_chapter_fields(
            chapter_index,
            phase=shadow.get("phase", "unknown"),
            task=self._next_task(shadow),
            error=None,
        )
        store.log_event(
            "stage_task_completed",
            chapter=chapter_index,
            task=task,
            next_task=self._next_task(shadow),
        )

    @staticmethod
    def _next_task(shadow: dict[str, Any]) -> str:
        phase = str(shadow.get("phase", "unknown"))
        if phase == "factual_audit":
            state = shadow.get("factual_state", {})
            if isinstance(state, dict) and state.get("audit_complete") is True:
                return "factual-repair"
            return "factual-audit"
        if phase == "chinese_audit":
            state = shadow.get("chinese_state", {})
            if isinstance(state, dict) and state.get("audit_complete") is True:
                return "chinese-repair"
            return "chinese-audit"
        return {"translate": "translate", "promote": "promote", "done": "done"}.get(
            phase, phase
        )

    def _run_upstream_full(self, store: RunStore, chapter_index: int) -> None:
        self._run_upstream_audit(store, chapter_index)
        self._run_upstream_repair(store, chapter_index)

    def _run_upstream_audit(self, store: RunStore, chapter_index: int) -> None:
        while True:
            chapter = store.load_chapter(chapter_index)
            shadow = self._ensure_shadow(store, chapter)
            phase = str(shadow.get("phase"))
            if phase == "translate":
                self._run_task_locked(store, chapter_index, "translate")
                continue
            if phase == "factual_audit":
                state = shadow.get("factual_state", {})
                if not isinstance(state, dict) or state.get("audit_complete") is not True:
                    self._run_task_locked(store, chapter_index, "factual-audit")
                return
            return

    def _run_upstream_repair(self, store: RunStore, chapter_index: int) -> None:
        chapter = store.load_chapter(chapter_index)
        shadow = self._ensure_shadow(store, chapter)
        if str(shadow.get("phase")) != "factual_audit":
            return
        state = shadow.get("factual_state", {})
        if not isinstance(state, dict) or state.get("audit_complete") is not True:
            raise StageTaskError(
                f"chapter {chapter_index} factual audit did not complete before repair"
            )
        self._run_task_locked(store, chapter_index, "factual-repair")

    def _run_downstream(self, store: RunStore, chapter_index: int) -> None:
        while True:
            chapter = store.load_chapter(chapter_index)
            shadow = self._ensure_shadow(store, chapter)
            phase = str(shadow.get("phase"))
            if phase == "chinese_audit":
                state = shadow.get("chinese_state", {})
                if not isinstance(state, dict) or state.get("audit_complete") is not True:
                    self._run_task_locked(store, chapter_index, "chinese-audit")
                self._run_task_locked(store, chapter_index, "chinese-repair")
                continue
            if phase == "promote":
                self._run_task_locked(store, chapter_index, "promote")
                continue
            if phase == "done":
                return
            raise StageTaskError(
                f"chapter {chapter_index} is not ready for downstream work: phase={phase}"
            )

    def _factual_audit_only(
        self, store: RunStore, chapter: Chapter, shadow: dict[str, Any]
    ) -> None:
        if not self.config.pipeline.factual_audit:
            raise StageTaskError("factual audit is disabled in the current config")
        targets = self._targets(shadow)
        state = shadow.setdefault("factual_state", {})
        if state.get("audit_complete") is True:
            return
        plan = state.get("plan")
        if not isinstance(plan, list):
            audit_lengths = {
                segment.index: len(segment.source) + len(targets.get(segment.index, ""))
                for segment in chapter.text_segments
            }
            plan = [
                {
                    "read_indexes": list(window.read_indexes),
                    "write_indexes": list(window.write_indexes),
                }
                for window in self.window_planner.plan(chapter, audit_lengths)
            ]
            state["plan"] = plan
            state["audit_batches"] = {}
            state["completed_repair_regions"] = []
            state["term_revision_results"] = {}
            self._save_shadow(store, chapter.index, shadow)
        audit_batches = state.setdefault("audit_batches", {})
        issues: list[dict[str, Any]] = []
        for batch_index, item in enumerate(plan):
            key = str(batch_index)
            if key in audit_batches:
                issues.extend(list(audit_batches[key].get("issues", [])))
                continue
            window = TranslationWindow(
                tuple(item["read_indexes"]), tuple(item["write_indexes"])
            )
            source = "\n".join(
                chapter.segments[index].source for index in window.read_indexes
            )
            messages = factual_audit_messages(
                chapter,
                window.read_indexes,
                window.write_indexes,
                targets,
                self._knowledge_for(store, chapter, source),
            )
            input_ref = store.save_translation_input(
                {"stage": "factual_audit", "messages": messages}
            )
            response = self.clients["factual_audit"].complete_json(
                messages,
                tier=self.config.pipeline.audit_tier,
                stage="factual_audit",
            )
            parsed = self._parse_issues(
                chapter,
                response,
                set(window.write_indexes),
                set(window.read_indexes),
            )
            issues.extend(parsed)
            discoveries = (
                list(response.get("term_candidates", []))
                if isinstance(response, dict)
                and isinstance(response.get("term_candidates", []), list)
                else []
            )
            revisions = (
                list(response.get("term_revisions", []))
                if isinstance(response, dict)
                and isinstance(response.get("term_revisions", []), list)
                else []
            )
            with self._terminology_lock:
                added_terms = self.terminology.add_discoveries(
                    chapter.index,
                    discoveries,
                    source,
                    "\n".join(targets[index] for index in window.read_indexes),
                )
            batch = {
                "read_indexes": list(window.read_indexes),
                "write_indexes": list(window.write_indexes),
                "issues": parsed,
                "term_candidates": discoveries,
                "term_revisions": revisions,
                "added_terms": [
                    term.model_dump(exclude_none=True) for term in added_terms
                ],
            }
            audit_batches[key] = batch
            store.record_audit(
                chapter.index,
                "factual_audit",
                {
                    "input_ref": input_ref,
                    "issues": parsed,
                    "term_candidates": discoveries,
                    "term_revisions": revisions,
                    "added_terms": batch["added_terms"],
                },
            )
            self._save_shadow(store, chapter.index, shadow)
        state = shadow.setdefault("factual_state", state)
        state["issue_count"] = len(issues)
        state["term_revision_count"] = sum(
            len(batch.get("term_revisions", []))
            for batch in audit_batches.values()
            if isinstance(batch, dict)
        )
        state.pop("repair_regions", None)
        state["audit_complete"] = True
        self._save_shadow(store, chapter.index, shadow)

    def _chinese_audit_only(
        self, store: RunStore, chapter: Chapter, shadow: dict[str, Any]
    ) -> None:
        if not self.config.pipeline.chinese_reader_audit:
            raise StageTaskError("Chinese reader audit is disabled in the current config")
        targets = self._targets(shadow)
        state = shadow.setdefault("chinese_state", {})
        if state.get("audit_complete") is True:
            return
        stored_reader = state.get("reader_batches")
        if not isinstance(stored_reader, list):
            stored_reader = []
            reader_lengths = {
                segment.index: len(targets.get(segment.index, ""))
                for segment in chapter.text_segments
            }
            for window in self.window_planner.plan(chapter, reader_lengths):
                messages = chinese_reader_messages(
                    chapter, targets, window.read_indexes, window.write_indexes
                )
                input_ref = store.save_translation_input(
                    {"stage": "chinese_reader_audit", "messages": messages}
                )
                response = self.clients["chinese_audit"].complete_json(
                    messages,
                    tier=self.config.pipeline.audit_tier,
                    stage="chinese_reader_audit",
                )
                parsed = self._parse_issues(
                    chapter,
                    response,
                    set(window.write_indexes),
                    set(window.read_indexes),
                )
                stored_reader.append(
                    {
                        "read_indexes": list(window.read_indexes),
                        "write_indexes": list(window.write_indexes),
                        "issues": parsed,
                    }
                )
                store.record_audit(
                    chapter.index,
                    "chinese_reader_audit",
                    {"input_ref": input_ref, "issues": parsed},
                )
                state["reader_batches"] = stored_reader
                self._save_shadow(store, chapter.index, shadow)
        stored_validations = state.setdefault("validation_batches", {})
        accepted: list[dict[str, Any]] = []
        for batch_index, item in enumerate(stored_reader):
            issues = list(item.get("issues", []))
            if not issues:
                continue
            window = TranslationWindow(
                tuple(item["read_indexes"]), tuple(item["write_indexes"])
            )
            tagged = [
                {**issue, "finding_id": f"b{batch_index}-f{index}"}
                for index, issue in enumerate(issues)
            ]
            key = str(batch_index)
            if key in stored_validations:
                results = list(stored_validations[key])
            else:
                source = "\n".join(
                    chapter.segments[index].source for index in window.read_indexes
                )
                messages = chinese_finding_validation_messages(
                    chapter,
                    targets,
                    tagged,
                    window.read_indexes,
                    self._knowledge_for(store, chapter, source),
                )
                input_ref = store.save_translation_input(
                    {"stage": "chinese_finding_validation", "messages": messages}
                )
                response = self.clients["validation"].complete_json(
                    messages,
                    tier=self.config.pipeline.validation_tier,
                    stage="chinese_finding_validation",
                )
                results = self._parse_reader_validations(
                    chapter, response, tagged, set(window.read_indexes)
                )
                for issue, result in zip(tagged, results, strict=True):
                    store.record_audit(
                        chapter.index,
                        "chinese_finding_validation",
                        {
                            "input_ref": input_ref,
                            "reader_issue": issue,
                            "result": result,
                        },
                    )
                stored_validations[key] = results
                self._save_shadow(store, chapter.index, shadow)
            accepted.extend(
                result for result in results if result.get("safe_to_repair") is True
            )
        if not isinstance(state.get("repair_regions"), list):
            if state.get("completed_repair_batches"):
                raise RuntimeError(
                    "legacy Chinese repair checkpoints cannot be safely merged; "
                    "rerun with --restart-from chinese-audit"
                )
            state["repair_regions"] = [
                {
                    "id": f"language-r{index}",
                    "start": region.start,
                    "end": region.end,
                    "issues": list(region.issues),
                }
                for index, region in enumerate(
                    self.repair_planner.plan(accepted, len(chapter.segments))
                )
            ]
            state["completed_repair_regions"] = []
        state["audit_complete"] = True
        self._save_shadow(store, chapter.index, shadow)

    def _promote(
        self, store: RunStore, chapter: Chapter, shadow: dict[str, Any]
    ) -> None:
        with self._terminology_lock:
            super()._promote(store, chapter, shadow)

    def _knowledge_for(
        self, store: RunStore, chapter: Chapter, read_source: str
    ) -> dict[str, Any]:
        with self._terminology_lock:
            knowledge = super()._knowledge_for(store, chapter, read_source)
        if not self._allow_provisional_factual_context or chapter.index <= 0:
            return knowledge
        manifest = store.load_manifest()
        status = {int(item["index"]): item.get("status") for item in manifest["chapters"]}
        previous_index = chapter.index - 1
        if status.get(previous_index) == STATUS_DONE:
            return knowledge
        shadow = store.load_shadow(previous_index)
        if not isinstance(shadow, dict):
            return knowledge
        snapshots = shadow.get("stage_snapshots", {})
        factual = snapshots.get("factual") if isinstance(snapshots, dict) else None
        if not isinstance(factual, dict):
            return knowledge
        previous = store.load_chapter(previous_index)
        remaining = self.config.window.past_context_chars
        tail: list[dict[str, Any]] = []
        for segment in reversed(previous.text_segments):
            target = str(factual.get(str(segment.index), ""))
            if not target:
                continue
            source = segment.source
            cost = len(source) + len(target)
            if cost <= remaining:
                tail.append(
                    {
                        "chapter": previous_index,
                        "segment": segment.index,
                        "source": source,
                        "factual_target": target,
                        "provisional": True,
                    }
                )
                remaining -= cost
                continue
            if remaining <= 0:
                break
            source_budget = round(remaining * len(source) / max(cost, 1))
            source_budget = max(0, min(source_budget, len(source), remaining))
            target_budget = min(len(target), remaining - source_budget)
            tail.append(
                {
                    "chapter": previous_index,
                    "segment": segment.index,
                    "source": source[-source_budget:] if source_budget else "",
                    "factual_target": target[-target_budget:] if target_budget else "",
                    "provisional": True,
                    "truncated": True,
                }
            )
            break
        if tail:
            knowledge["past_only_raw_tail"] = list(reversed(tail))
            knowledge["evidence_priority"] = (
                "current_and_nearby_source > active hard terms > active preferred terms > "
                "previous factual snapshot > past formal target"
            )
        return knowledge


__all__ = ["StageTaskError", "TaskPipeline"]
