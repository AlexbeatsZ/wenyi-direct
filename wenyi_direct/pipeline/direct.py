"""Chapter-first translation, audit, repair, validation, and atomic promotion."""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from ..config import Config
from ..ingest.models import Chapter
from ..ingest.segmenter import load_document
from ..llm.base import LLMClient
from ..llm.json_parser import JSONParseError
from ..progress import ProgressCallback, ProgressEvent
from ..prompts import (
    CHINESE_FINDING_VALIDATION_SYSTEM,
    CHINESE_READER_SYSTEM,
    FACTUAL_AUDIT_SYSTEM,
    FIDELITY_SYSTEM,
    LEGACY_REPAIR_SYSTEM,
    REPAIR_ARBITRATION_SYSTEM,
    REPAIR_SYSTEM,
    TRANSLATION_SYSTEM,
    chinese_finding_validation_messages,
    chinese_reader_messages,
    factual_audit_messages,
    fidelity_validation_messages,
    repair_arbitration_messages,
    repair_messages,
    translation_messages,
)
from .knowledge import TerminologyStore
from .repair import RepairPlanner
from .runstore import STATUS_DONE, RunStore, slugify
from .types import RepairRegion, TranslationWindow, chapter_source_digest, segment_id
from .window import WindowPlanner, split_write_scope


class AlignmentError(RuntimeError):
    """A structured response omitted, duplicated, or invented stable IDs."""


class StageTaskError(RuntimeError):
    """The requested standalone stage does not match persisted chapter state."""


STAGE_NAMES = (
    "translate",
    "factual-audit",
    "factual-repair",
    "chinese-audit",
    "chinese-repair",
    "promote",
)


_SPEAKER_METADATA_PREFIX_RE = re.compile(r"^\s*【話者：[^】]*】\s*")
_REPAIR_CHECKPOINT_VERSION = 2


def _clean_target_control_metadata(target: str) -> str:
    """Remove the synthetic speaker hint injected into source-only context."""
    return _SPEAKER_METADATA_PREFIX_RE.sub("", target, count=1).strip()


def _group_contiguous(indexes: Iterable[int]) -> list[tuple[int, ...]]:
    groups: list[list[int]] = []
    for index in sorted(set(indexes)):
        if not groups or index != groups[-1][-1] + 1:
            groups.append([index])
        else:
            groups[-1].append(index)
    return [tuple(group) for group in groups]


class DirectPipeline:
    def __init__(
        self,
        config: Config,
        clients: dict[str, LLMClient],
        *,
        config_dir: str | Path = ".",
        on_progress: ProgressCallback | None = None,
    ) -> None:
        self.config = config
        self.clients = clients
        self.config_dir = Path(config_dir).resolve()
        self.window_planner = WindowPlanner(config.window)
        self.repair_planner = RepairPlanner(config.pipeline.repair_context_segments)
        terms_path = Path(config.terminology_file or "terminology.yaml")
        if not terms_path.is_absolute():
            terms_path = self.config_dir / terms_path
        self._base_terminology = TerminologyStore.load(terms_path)
        self.terminology = self._base_terminology
        self._run_terminologies: dict[str, TerminologyStore] = {}
        self._terminology_lock = threading.RLock()
        self._allow_provisional_factual_context = False
        self._on_progress = on_progress
        self._progress_lock = threading.RLock()

    def _emit_progress(self, kind: str, **data: Any) -> None:
        if self._on_progress is None:
            return
        with self._progress_lock:
            self._on_progress(ProgressEvent(kind=kind, **data))

    def _stage_progress(
        self,
        chapter: int,
        stage: str,
        completed: int,
        total: int,
        detail: str,
    ) -> None:
        self._emit_progress(
            "stage_progress",
            chapter=chapter,
            stage=stage,
            completed=completed,
            total=total,
            detail=detail,
        )

    def _audit_log(
        self,
        chapter: int,
        stage: str,
        event: str,
        payload: dict[str, Any],
    ) -> None:
        self._emit_progress(
            "audit_log",
            chapter=chapter,
            stage=stage,
            detail=event,
            payload=payload,
        )

    def _activate_terminology_for(self, store: RunStore) -> None:
        """Keep model discoveries inside one book's state, never in shared config."""
        key = str(Path(store.run_dir).resolve())
        terminology = self._run_terminologies.get(key)
        if terminology is None:
            path = Path(store.run_dir) / "terminology.yaml"
            if path.exists():
                terminology = TerminologyStore.load(path)
            else:
                terminology = TerminologyStore(
                    path, self._base_terminology.document.model_copy(deep=True)
                )
                terminology.save()
            self._run_terminologies[key] = terminology
        self.terminology = terminology

    def store_for(self, source_path: str | Path) -> RunStore:
        state_root = Path(self.config.state_dir)
        if not state_root.is_absolute():
            state_root = self.config_dir / state_root
        return RunStore(str(state_root / slugify(Path(source_path).stem)))

    def prepare(self, source_path: str | Path) -> RunStore:
        source = str(Path(source_path).resolve())
        self._emit_progress("prepare_started", detail=Path(source).name)
        source_sha256 = hashlib.sha256(Path(source).read_bytes()).hexdigest()
        store = self.store_for(source)
        if store.exists():
            manifest = store.load_manifest()
            recorded = str(Path(manifest["source_path"]).resolve())
            if recorded != source:
                raise ValueError(f"state belongs to another source: {manifest['source_path']}")
            recorded_sha256 = manifest.get("source_sha256")
            if not recorded_sha256:
                self._migrate_legacy_source_sha256(store, manifest, source, source_sha256)
            elif recorded_sha256 != source_sha256:
                raise RuntimeError(
                    "source file changed after state creation; create a new state or "
                    "explicitly migrate the source instead of resuming stale segments"
                )
            self._activate_terminology_for(store)
            self._emit_progress(
                "prepare_completed",
                total=len(manifest["chapters"]),
                detail=f"恢复 {len(manifest['chapters'])} 章状态",
            )
            return store
        document = load_document(
            source,
            self.config.source_lang,
            self.config.target_lang,
            self.config.segment.max_chars_per_segment,
            cache_dir=store.source_dir,
        )
        manifest = store.stage_document(document)
        manifest["pipeline"] = "wenyi-direct-v1"
        manifest["future_chapters_required"] = False
        manifest["source_sha256"] = source_sha256
        store.save_manifest(manifest)
        self._activate_terminology_for(store)
        store.log_event("prepared", chapters=len(document.chapters), source_path=source)
        self._emit_progress(
            "prepare_completed",
            total=len(document.chapters),
            detail=f"解析完成，共 {len(document.chapters)} 章",
        )
        return store

    def _migrate_legacy_source_sha256(
        self,
        store: RunStore,
        manifest: dict[str, Any],
        source: str,
        source_sha256: str,
    ) -> None:
        """Backfill a missing digest only after a complete structural comparison."""
        document = load_document(
            source,
            self.config.source_lang,
            self.config.target_lang,
            self.config.segment.max_chars_per_segment,
            cache_dir=store.source_dir,
        )
        manifest_rows = list(manifest.get("chapters", []))
        if len(document.chapters) != len(manifest_rows):
            raise RuntimeError(
                "legacy state has no source digest and chapter count differs; "
                "explicit source migration is required"
            )
        for fresh, row in zip(document.chapters, manifest_rows):
            chapter_index = int(row["index"])
            stored = store.load_chapter(chapter_index)
            if (
                fresh.index != chapter_index
                or fresh.title != stored.title
                or fresh.href != stored.href
                or len(fresh.segments) != len(stored.segments)
            ):
                raise RuntimeError(
                    "legacy state has no source digest and chapter structure differs "
                    f"at chapter {chapter_index}; explicit source migration is required"
                )
            fresh_segments = [
                (segment.index, segment.source, segment.kind, segment.anchor)
                for segment in fresh.segments
            ]
            stored_segments = [
                (segment.index, segment.source, segment.kind, segment.anchor)
                for segment in stored.segments
            ]
            if fresh_segments != stored_segments:
                raise RuntimeError(
                    "legacy state has no source digest and segment structure differs "
                    f"at chapter {chapter_index}; explicit source migration is required"
                )
        manifest["source_sha256"] = source_sha256
        store.save_manifest(manifest)
        store.log_event(
            "legacy_source_digest_migrated",
            source_path=source,
            source_sha256=source_sha256,
            chapters=len(document.chapters),
        )

    def run(
        self,
        source_path: str | Path,
        *,
        chapters: set[int] | None = None,
    ) -> RunStore:
        store = self.prepare(source_path)
        with store.lock():
            pending = self._selected_chapters(store, chapters, pending_only=True)
            self._emit_progress("operation_started", operation="translate", chapters=tuple(pending))
            for chapter_index in pending:
                try:
                    self._run_chapter(store, chapter_index)
                    self._emit_progress("chapter_completed", chapter=chapter_index)
                finally:
                    # Keep the live monitor useful during long books and retain
                    # provider/fallback evidence even when a chapter fails.
                    self._save_usage(store)
            self._emit_progress("operation_completed", operation="translate")
        return store

    def review_formal(
        self,
        source_path: str | Path,
        *,
        chapters: set[int] | None = None,
        parallel: bool = False,
        force: bool = False,
    ) -> RunStore:
        """Re-audit accepted Formal text without direct translation."""
        store = self.prepare(source_path)
        with store.lock():
            selected = self._formal_review_chapters(store, chapters, force=force)
            operation = "review-parallel" if parallel else "review"
            self._emit_progress("operation_started", operation=operation, chapters=tuple(selected))
            if not selected:
                self._emit_progress("operation_completed", operation=operation)
                return store
            self._allow_provisional_factual_context = parallel
            try:
                if parallel:
                    for contiguous in self._contiguous_runs(selected):
                        self._run_formal_review_parallel_sequence(store, contiguous)
                else:
                    for chapter_index in selected:
                        self._ensure_formal_review_shadow(store, chapter_index)
                        try:
                            self._run_chapter(store, chapter_index)
                            self._emit_progress("chapter_completed", chapter=chapter_index)
                        finally:
                            self._save_usage(store)
            finally:
                self._allow_provisional_factual_context = False
                self._save_usage(store)
            self._emit_progress("operation_completed", operation=operation)
        return store

    def run_stage(
        self,
        source_path: str | Path,
        stage: str,
        *,
        chapters: set[int] | None = None,
    ) -> RunStore:
        """Run exactly one ready stage per selected chapter, then stop."""
        stage = self._normalise_stage(stage)
        store = self.prepare(source_path)
        with store.lock():
            selected = self._selected_chapters(store, chapters)
            if chapters is None:
                selected = [
                    index for index in selected if self._stage_is_ready(store, index, stage)
                ]
            self._emit_progress("operation_started", operation="stage", chapters=tuple(selected))
            for chapter_index in selected:
                try:
                    self._run_stage_for_chapter(store, chapter_index, stage)
                    self._emit_progress("chapter_completed", chapter=chapter_index)
                finally:
                    self._save_usage(store)
            self._emit_progress("operation_completed", operation="stage")
        return store

    def run_parallel(
        self,
        source_path: str | Path,
        *,
        chapters: set[int] | None = None,
    ) -> RunStore:
        """Overlap downstream review of chapter N with upstream work on N+1."""
        store = self.prepare(source_path)
        with store.lock():
            selected = self._selected_chapters(store, chapters, pending_only=True)
            self._emit_progress(
                "operation_started",
                operation="translate-parallel",
                chapters=tuple(selected),
            )
            if not selected:
                self._emit_progress("operation_completed", operation="translate-parallel")
                return store
            self._allow_provisional_factual_context = True
            try:
                for contiguous in self._contiguous_runs(selected):
                    self._run_parallel_sequence(store, contiguous)
            finally:
                self._allow_provisional_factual_context = False
                self._save_usage(store)
            self._emit_progress("operation_completed", operation="translate-parallel")
        return store

    @staticmethod
    def _normalise_stage(value: str) -> str:
        stage = value.strip().casefold().replace("_", "-")
        if stage not in STAGE_NAMES:
            raise ValueError(f"unknown stage {value!r}; choose from {list(STAGE_NAMES)}")
        return stage

    @staticmethod
    def _selected_chapters(
        store: RunStore,
        chapters: set[int] | None,
        *,
        pending_only: bool = False,
    ) -> list[int]:
        manifest = store.load_manifest()
        status = {int(item["index"]): item.get("status") for item in manifest["chapters"]}
        known = set(status)
        selected = known if chapters is None else set(chapters)
        unknown = selected - known
        if unknown:
            raise ValueError(f"unknown chapter indexes: {sorted(unknown)}")
        if pending_only:
            selected = {index for index in selected if status[index] != STATUS_DONE}
        return sorted(selected)

    @staticmethod
    def _contiguous_runs(indexes: list[int]) -> list[list[int]]:
        runs: list[list[int]] = []
        for index in indexes:
            if not runs or index != runs[-1][-1] + 1:
                runs.append([index])
            else:
                runs[-1].append(index)
        return runs

    def _run_parallel_sequence(self, store: RunStore, chapters: list[int]) -> None:
        self._run_upstream(store, chapters[0], include_repair=True)
        previous = chapters[0]
        for current in chapters[1:]:
            store.log_event(
                "parallel_pair_started",
                downstream_chapter=previous,
                upstream_chapter=current,
            )
            with ThreadPoolExecutor(max_workers=2) as executor:
                downstream = executor.submit(self._run_downstream, store, previous)
                upstream = executor.submit(
                    self._run_upstream, store, current, include_repair=False, defer_terms=True
                )
                downstream.result()
                upstream.result()
            self._run_upstream(store, current, include_repair=True)
            store.log_event(
                "parallel_pair_completed",
                downstream_chapter=previous,
                upstream_chapter=current,
            )
            self._save_usage(store)
            previous = current
        self._run_downstream(store, previous)

    def _run_formal_review_parallel_sequence(self, store: RunStore, chapters: list[int]) -> None:
        self._ensure_formal_review_shadow(store, chapters[0])
        self._run_upstream(store, chapters[0], include_repair=True)
        previous = chapters[0]
        for current in chapters[1:]:
            self._ensure_formal_review_shadow(store, current)
            store.log_event(
                "formal_review_parallel_pair_started",
                downstream_chapter=previous,
                upstream_chapter=current,
            )
            with ThreadPoolExecutor(max_workers=2) as executor:
                downstream = executor.submit(self._run_downstream, store, previous)
                upstream = executor.submit(
                    self._run_upstream, store, current, include_repair=False, defer_terms=True
                )
                downstream.result()
                upstream.result()
            self._run_upstream(store, current, include_repair=True)
            store.log_event(
                "formal_review_parallel_pair_completed",
                downstream_chapter=previous,
                upstream_chapter=current,
            )
            self._save_usage(store)
            previous = current
        self._run_downstream(store, previous)

    def _formal_review_chapters(
        self,
        store: RunStore,
        chapters: set[int] | None,
        *,
        force: bool,
    ) -> list[int]:
        manifest = store.load_manifest()
        rows = {int(item["index"]): item for item in manifest["chapters"]}
        selected = set(rows) if chapters is None else set(chapters)
        unknown = selected - set(rows)
        if unknown:
            raise ValueError(f"unknown chapter indexes: {sorted(unknown)}")
        result: list[int] = []
        for chapter_index in sorted(selected):
            row = rows[chapter_index]
            shadow = store.load_shadow(chapter_index)
            marker = shadow.get("formal_review") if shadow else None
            phase = shadow.get("phase") if shadow else None
            if row.get("status") == STATUS_DONE:
                if marker and phase == "done" and not force:
                    continue
                if marker and phase not in {None, "done"}:
                    raise StageTaskError(
                        f"chapter {chapter_index} has an inconsistent unfinished "
                        "Formal-review Shadow while its Formal status is done"
                    )
                result.append(chapter_index)
                continue
            if marker and phase == "done":
                self._reconcile_completed_promotion(
                    store, store.load_chapter(chapter_index), shadow
                )
                continue
            if marker:
                if force:
                    raise StageTaskError(
                        f"chapter {chapter_index} already has an unfinished Formal review"
                    )
                result.append(chapter_index)
                continue
            raise StageTaskError(f"chapter {chapter_index} is not Formal and cannot be re-reviewed")
        return result

    def _ensure_formal_review_shadow(self, store: RunStore, chapter_index: int) -> None:
        manifest = store.load_manifest()
        row = next(item for item in manifest["chapters"] if item["index"] == chapter_index)
        existing = store.load_shadow(chapter_index)
        if row.get("status") != STATUS_DONE:
            if existing and existing.get("formal_review"):
                return
            raise StageTaskError(f"chapter {chapter_index} has no resumable Formal review")

        chapter = store.load_chapter(chapter_index)
        targets = {segment.index: segment.target or "" for segment in chapter.segments}
        missing = [
            segment.index
            for segment in chapter.text_segments
            if not targets.get(segment.index, "").strip()
        ]
        if missing:
            raise AlignmentError(f"cannot review Formal chapter with empty targets: {missing}")
        if existing:
            reason = (
                "new_forced_formal_review"
                if existing.get("formal_review")
                else "formal_review_superseded_legacy_shadow"
            )
            archived_path = store.archive_shadow(chapter_index, reason=reason)
            store.log_event(
                "shadow_superseded_for_formal_review",
                chapter=chapter_index,
                previous_phase=existing.get("phase"),
                previous_schema=existing.get("schema"),
                archive_path=archived_path,
                reason=reason,
            )
        baseline = json.dumps(targets, ensure_ascii=False, sort_keys=True).encode("utf-8")
        shadow = {
            "schema": 2,
            "chapter": chapter_index,
            "source_digest": chapter_source_digest(chapter),
            "phase": "factual_audit",
            "targets": {str(index): target for index, target in targets.items()},
            "translated_ids": sorted(
                segment_id(chapter.index, segment) for segment in chapter.text_segments
            ),
            "stage_snapshots": {
                "direct": {str(index): target for index, target in targets.items()}
            },
            "formal_review": {
                "schema": 1,
                "baseline_sha256": hashlib.sha256(baseline).hexdigest(),
            },
        }
        self._archive_targets(
            store,
            chapter,
            "formal_review_baseline",
            targets,
            metadata=shadow["formal_review"],
        )
        self._save_shadow(store, chapter_index, shadow)
        store.set_chapter_fields(
            chapter_index,
            status="pending",
            phase="factual_audit",
            task="factual-audit",
            error=None,
        )
        store.log_event(
            "formal_review_opened",
            chapter=chapter_index,
            baseline_sha256=shadow["formal_review"]["baseline_sha256"],
        )

    def _run_upstream(
        self,
        store: RunStore,
        chapter_index: int,
        *,
        include_repair: bool,
        defer_terms: bool = False,
    ) -> None:
        chapter, shadow = self._load_shadow(store, chapter_index)
        if shadow["phase"] == "translate":
            self._run_stage_for_chapter(store, chapter_index, "translate")
            chapter, shadow = self._load_shadow(store, chapter_index)
        if shadow["phase"] == "factual_audit" and not self._factual_audit_complete(shadow):
            self._run_stage_for_chapter(
                store, chapter_index, "factual-audit", defer_terms=defer_terms
            )
            chapter, shadow = self._load_shadow(store, chapter_index)
        if include_repair and shadow["phase"] == "factual_audit":
            self._run_stage_for_chapter(store, chapter_index, "factual-repair")

    def _run_downstream(self, store: RunStore, chapter_index: int) -> None:
        while True:
            _chapter, shadow = self._load_shadow(store, chapter_index)
            stage = self._next_stage(shadow)
            if stage in {"chinese-audit", "chinese-repair", "promote"}:
                self._run_stage_for_chapter(store, chapter_index, stage)
                continue
            if stage == "done":
                self._emit_progress("chapter_completed", chapter=chapter_index)
                return
            raise StageTaskError(
                f"chapter {chapter_index} is not ready for downstream work: {stage}"
            )

    def _run_chapter(self, store: RunStore, chapter_index: int) -> None:
        while True:
            _chapter, shadow = self._load_shadow(store, chapter_index)
            stage = self._next_stage(shadow)
            if stage == "done":
                return
            self._run_stage_for_chapter(store, chapter_index, stage)

    def _load_shadow(self, store: RunStore, chapter_index: int) -> tuple[Chapter, dict[str, Any]]:
        chapter = store.load_chapter(chapter_index)
        digest = chapter_source_digest(chapter)
        shadow = store.load_shadow(chapter_index)
        if shadow and shadow.get("source_digest") != digest:
            raise RuntimeError(f"chapter {chapter_index} source changed after shadow creation")
        if shadow and shadow.get("phase") == "done":
            self._reconcile_completed_promotion(store, chapter, shadow)
            return chapter, shadow
        current_policy = self._policy_fingerprint()
        legacy_policy = self._policy_fingerprint(include_repair_arbitration=False)
        if (
            shadow
            and legacy_policy != current_policy
            and shadow.get("policy_fingerprint") == legacy_policy
        ):
            previous_policy = shadow.get("policy_fingerprint")
            self._save_shadow(store, chapter.index, shadow)
            store.log_event(
                "shadow_policy_migrated",
                chapter=chapter.index,
                previous_policy=previous_policy,
                current_policy=current_policy,
                migration="additive_repair_arbitration",
            )
        elif shadow and shadow.get("policy_fingerprint") not in {None, current_policy}:
            expected = {segment_id(chapter.index, segment) for segment in chapter.text_segments}
            completed = set(shadow.get("translated_ids", []))
            if completed != expected:
                phase = "translate"
            elif self.config.pipeline.factual_audit:
                phase = "factual_audit"
            elif self.config.pipeline.chinese_reader_audit:
                phase = "chinese_audit"
            else:
                phase = "promote"
            previous_policy = shadow.get("policy_fingerprint")
            shadow["phase"] = phase
            shadow.pop("factual_state", None)
            shadow.pop("chinese_state", None)
            shadow.pop("promotion_state", None)
            shadow.pop("terminology_repair_state", None)
            shadow.setdefault("stage_snapshots", {}).pop("factual", None)
            self._save_shadow(store, chapter.index, shadow)
            store.log_event(
                "shadow_policy_invalidated",
                chapter=chapter.index,
                previous_policy=previous_policy,
                current_policy=current_policy,
                restart_phase=phase,
            )
        if not shadow:
            shadow = {
                "schema": 2,
                "chapter": chapter_index,
                "source_digest": digest,
                "phase": "translate",
                "targets": {
                    str(segment.index): segment.target or "" for segment in chapter.segments
                },
                "translated_ids": [],
                "stage_snapshots": {},
            }
            self._save_shadow(store, chapter_index, shadow)
        elif shadow.get("policy_fingerprint") is None:
            # Legacy shadows can resume without discarding paid model work. All current
            # promotion gates still run, and subsequent policy changes are fingerprinted.
            self._save_shadow(store, chapter_index, shadow)
        shadow.setdefault("stage_snapshots", {})
        self._clean_shadow_control_metadata(store, chapter, shadow)
        return chapter, shadow

    def _reconcile_completed_promotion(
        self, store: RunStore, chapter: Chapter, shadow: dict[str, Any]
    ) -> None:
        """Finish the manifest commit when Formal and a done Shadow already agree."""
        manifest = store.load_manifest()
        item = next(row for row in manifest["chapters"] if row["index"] == chapter.index)
        if item.get("status") == STATUS_DONE:
            return

        targets = self._targets(shadow)
        expected_ids = {segment_id(chapter.index, segment) for segment in chapter.text_segments}
        translated_ids = set(shadow.get("translated_ids", []))
        mismatched = [
            segment.index
            for segment in chapter.text_segments
            if not targets.get(segment.index, "").strip()
            or segment.target != targets.get(segment.index)
        ]
        if translated_ids != expected_ids or mismatched:
            raise StageTaskError(
                f"chapter {chapter.index} has a done Shadow that does not match Formal: "
                f"translated_ids_match={translated_ids == expected_ids}, "
                f"mismatched_targets={mismatched}"
            )

        store.set_chapter_fields(
            chapter.index,
            status=STATUS_DONE,
            phase="done",
            task="",
            error=None,
            factual_audit=self.config.pipeline.factual_audit,
            chinese_reader_audit=self.config.pipeline.chinese_reader_audit,
        )
        store.log_event(
            "chapter_promotion_recovered",
            chapter=chapter.index,
            formal_review=bool(shadow.get("formal_review")),
        )

    @staticmethod
    def _factual_audit_complete(shadow: dict[str, Any]) -> bool:
        state = shadow.get("factual_state", {})
        return isinstance(state, dict) and state.get("audit_complete") is True

    @staticmethod
    def _chinese_audit_complete(shadow: dict[str, Any]) -> bool:
        state = shadow.get("chinese_state", {})
        return isinstance(state, dict) and state.get("audit_complete") is True

    def _next_stage(self, shadow: dict[str, Any]) -> str:
        phase = str(shadow.get("phase", "unknown"))
        if phase == "factual_audit":
            return "factual-repair" if self._factual_audit_complete(shadow) else "factual-audit"
        if phase == "chinese_audit":
            return "chinese-repair" if self._chinese_audit_complete(shadow) else "chinese-audit"
        return {"translate": "translate", "promote": "promote", "done": "done"}.get(phase, phase)

    def _stage_is_ready(self, store: RunStore, chapter_index: int, stage: str) -> bool:
        manifest = store.load_manifest()
        item = next(row for row in manifest["chapters"] if row["index"] == chapter_index)
        if item.get("status") == STATUS_DONE:
            return False
        _chapter, shadow = self._load_shadow(store, chapter_index)
        return self._next_stage(shadow) == stage

    def _run_stage_for_chapter(
        self,
        store: RunStore,
        chapter_index: int,
        stage: str,
        *,
        defer_terms: bool = False,
    ) -> None:
        manifest = store.load_manifest()
        item = next(row for row in manifest["chapters"] if row["index"] == chapter_index)
        if item.get("status") == STATUS_DONE:
            raise StageTaskError(f"chapter {chapter_index} is already done")
        chapter, shadow = self._load_shadow(store, chapter_index)
        expected = self._next_stage(shadow)
        if expected != stage:
            raise StageTaskError(
                f"chapter {chapter_index} cannot run {stage}: next stage is {expected}"
            )
        store.set_chapter_fields(
            chapter_index,
            status="pending",
            phase=shadow["phase"],
            task=stage,
            error=None,
        )
        store.log_event("stage_started", chapter=chapter_index, stage=stage)
        self._emit_progress(
            "stage_started",
            chapter=chapter_index,
            stage=stage,
            detail="准备模型请求",
        )
        try:
            if stage == "translate":
                self._translate_chapter(store, chapter, shadow)
            elif stage == "factual-audit":
                self._factual_audit_task(store, chapter, shadow, defer_terms=defer_terms)
            elif stage == "factual-repair":
                self._factual_repair_task(store, chapter, shadow)
            elif stage == "chinese-audit":
                self._chinese_audit_task(store, chapter, shadow)
            elif stage == "chinese-repair":
                self._chinese_repair_task(store, chapter, shadow)
            elif stage == "promote":
                self._promote(store, chapter, shadow)
        except Exception as exc:
            store.set_chapter_fields(
                chapter_index,
                phase=shadow.get("phase", "unknown"),
                task=stage,
                error=str(exc),
            )
            store.log_event(
                "stage_failed",
                chapter=chapter_index,
                stage=stage,
                error=str(exc),
            )
            self._emit_progress(
                "stage_failed",
                chapter=chapter_index,
                stage=stage,
                error=str(exc),
            )
            raise
        next_stage = self._next_stage(shadow)
        store.set_chapter_fields(
            chapter_index,
            phase=shadow.get("phase", "unknown"),
            task=next_stage,
            error=None,
        )
        store.log_event(
            "stage_completed", chapter=chapter_index, stage=stage, next_stage=next_stage
        )
        self._emit_progress(
            "stage_completed", chapter=chapter_index, stage=stage, detail=next_stage
        )

    @staticmethod
    def _targets(shadow: dict[str, Any]) -> dict[int, str]:
        return {int(index): str(target) for index, target in shadow["targets"].items()}

    @staticmethod
    def _save_targets(shadow: dict[str, Any], targets: dict[int, str]) -> None:
        shadow["targets"] = {str(index): target for index, target in targets.items()}

    @staticmethod
    def _targets_sha256(targets: dict[int, str]) -> str:
        payload = {str(index): targets[index] for index in sorted(targets)}
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _repair_checkpoint_id(stage: str, region: RepairRegion) -> str:
        payload = {
            "stage": stage,
            "start": region.start,
            "end": region.end,
            "issues": list(region.issues),
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return f"{stage}-{hashlib.sha256(encoded).hexdigest()[:16]}"

    def _policy_fingerprint(self, *, include_repair_arbitration: bool = True) -> str:
        with self._terminology_lock:
            prompts = [
                TRANSLATION_SYSTEM,
                FACTUAL_AUDIT_SYSTEM,
                CHINESE_READER_SYSTEM,
                CHINESE_FINDING_VALIDATION_SYSTEM,
                REPAIR_SYSTEM if include_repair_arbitration else LEGACY_REPAIR_SYSTEM,
                FIDELITY_SYSTEM,
            ]
            if include_repair_arbitration:
                prompts.append(REPAIR_ARBITRATION_SYSTEM)
            payload = {
                "config": self.config.model_dump(mode="json"),
                "terminology": self.terminology.document.model_dump(mode="json"),
                "prompts": prompts,
            }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _save_shadow(self, store: RunStore, chapter_index: int, shadow: dict[str, Any]) -> None:
        shadow["policy_fingerprint"] = self._policy_fingerprint()
        store.save_shadow(chapter_index, shadow)

    def _clean_shadow_control_metadata(
        self, store: RunStore, chapter: Chapter, shadow: dict[str, Any]
    ) -> None:
        targets = self._targets(shadow)
        cleaned = {
            index: _clean_target_control_metadata(target)
            for index, target in targets.items()
            if _clean_target_control_metadata(target) != target
        }
        if not cleaned:
            return
        self._archive_targets(
            store,
            chapter,
            "control_metadata_cleanup",
            cleaned,
            previous=targets,
        )
        targets.update(cleaned)
        self._save_targets(shadow, targets)
        self._save_shadow(store, chapter.index, shadow)
        store.log_event("control_metadata_cleanup", chapter=chapter.index, count=len(cleaned))

    def _translate_chapter(self, store: RunStore, chapter: Chapter, shadow: dict[str, Any]) -> None:
        completed = set(shadow.get("translated_ids", []))
        plan = list(self.window_planner.plan(chapter))
        completed_windows = sum(
            all(
                segment_id(chapter.index, chapter.segments[index]) in completed
                for index in planned.write_indexes
            )
            for planned in plan
        )
        self._stage_progress(
            chapter.index,
            "translate",
            completed_windows,
            len(plan),
            "检查可恢复翻译窗口",
        )
        for window_index, planned in enumerate(plan, start=1):
            missing = tuple(
                index
                for index in planned.write_indexes
                if segment_id(chapter.index, chapter.segments[index]) not in completed
            )
            if not missing:
                continue
            self._emit_progress(
                "stage_activity",
                chapter=chapter.index,
                stage="translate",
                detail=f"翻译窗口 {window_index}/{len(plan)}，段落 {missing[0]}–{missing[-1]}",
            )
            window = TranslationWindow(planned.read_indexes, missing)
            translated = self._translate_window(store, chapter, window)
            targets = self._targets(shadow)
            targets.update(translated)
            self._save_targets(shadow, targets)
            completed.update(
                segment_id(chapter.index, chapter.segments[index]) for index in translated
            )
            shadow["translated_ids"] = sorted(completed)
            self._save_shadow(store, chapter.index, shadow)
            completed_windows += 1
            self._stage_progress(
                chapter.index,
                "translate",
                completed_windows,
                len(plan),
                f"窗口 {window_index}/{len(plan)} 已保存",
            )
        expected = {segment_id(chapter.index, segment) for segment in chapter.text_segments}
        if completed != expected:
            raise AlignmentError(
                f"chapter translation incomplete: missing {sorted(expected - completed)}"
            )
        shadow.setdefault("stage_snapshots", {})["direct"] = dict(shadow["targets"])
        shadow["phase"] = "factual_audit" if self.config.pipeline.factual_audit else "chinese_audit"
        if not self.config.pipeline.factual_audit:
            shadow["stage_snapshots"]["factual"] = dict(shadow["targets"])
        if not self.config.pipeline.factual_audit and not self.config.pipeline.chinese_reader_audit:
            shadow["phase"] = "promote"
        self._save_shadow(store, chapter.index, shadow)
        store.set_chapter_fields(chapter.index, phase=shadow["phase"])

    def _translate_window(
        self, store: RunStore, chapter: Chapter, window: TranslationWindow
    ) -> dict[int, str]:
        read_source = "\n".join(chapter.segments[index].source for index in window.read_indexes)
        messages = translation_messages(
            chapter,
            window.read_indexes,
            window.write_indexes,
            self._knowledge_for(store, chapter, read_source),
        )
        input_ref = store.save_translation_input(
            {"stage": "direct_translation", "messages": messages}
        )
        try:
            response = self.clients["translate"].complete_json(
                messages,
                tier=self.config.pipeline.translation_tier,
                stage="direct_translation",
            )
            translated = self._parse_translations(chapter, window.write_indexes, response)
        except (AlignmentError, JSONParseError):
            if len(window.write_indexes) < 2:
                raise
            result: dict[int, str] = {}
            for smaller in split_write_scope(window):
                result.update(self._translate_window(store, chapter, smaller))
            return result
        self._archive_targets(
            store,
            chapter,
            "direct_translation",
            translated,
            input_ref=input_ref,
        )
        store.log_event(
            "translation_window_completed",
            chapter=chapter.index,
            read_indexes=list(window.read_indexes),
            write_indexes=list(window.write_indexes),
        )
        return translated

    def _factual_audit_task(
        self,
        store: RunStore,
        chapter: Chapter,
        shadow: dict[str, Any],
        *,
        defer_terms: bool = False,
    ) -> None:
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
            self._save_shadow(store, chapter.index, shadow)
        audit_batches = state.setdefault("audit_batches", {})
        completed_batches = sum(str(index) in audit_batches for index in range(len(plan)))
        self._stage_progress(
            chapter.index,
            "factual-audit",
            completed_batches,
            len(plan),
            "检查可恢复审校批次",
        )
        for batch_index, item in enumerate(plan):
            key = str(batch_index)
            if key in audit_batches:
                continue
            window = TranslationWindow(tuple(item["read_indexes"]), tuple(item["write_indexes"]))
            source = "\n".join(chapter.segments[index].source for index in window.read_indexes)
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
            self._emit_progress(
                "stage_activity",
                chapter=chapter.index,
                stage="factual-audit",
                detail=f"AI 审核批次 {batch_index + 1}/{len(plan)}",
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
            discoveries = (
                list(response.get("term_candidates", []))
                if isinstance(response, dict)
                and isinstance(response.get("term_candidates", []), list)
                else []
            )
            added_terms = []
            discoveries_applied = not defer_terms
            if discoveries_applied:
                with self._terminology_lock:
                    added_terms = self.terminology.add_discoveries(
                        chapter.index,
                        discoveries,
                        source,
                        "\n".join(targets[index] for index in window.read_indexes),
                    )
            audit_batches[key] = {
                "read_indexes": list(window.read_indexes),
                "write_indexes": list(window.write_indexes),
                "issues": parsed,
                "term_candidates": discoveries,
                "discoveries_applied": discoveries_applied,
                "added_terms": [term.model_dump(exclude_none=True) for term in added_terms],
            }
            store.record_audit(
                chapter.index,
                "factual_audit",
                {
                    "input_ref": input_ref,
                    "issues": parsed,
                    "term_candidates": discoveries,
                    "discoveries_deferred": defer_terms,
                    "added_terms": [term.model_dump(exclude_none=True) for term in added_terms],
                },
            )
            self._audit_log(
                chapter.index,
                "factual-audit",
                "factual_audit_result",
                {
                    "batch": batch_index,
                    "read_indexes": list(window.read_indexes),
                    "write_indexes": list(window.write_indexes),
                    "issues": parsed,
                    "term_candidates": discoveries,
                },
            )
            self._save_shadow(store, chapter.index, shadow)
            completed_batches += 1
            self._stage_progress(
                chapter.index,
                "factual-audit",
                completed_batches,
                len(plan),
                f"批次 {batch_index + 1}/{len(plan)}，发现 {len(parsed)} 个问题",
            )
        state["audit_complete"] = True
        self._save_shadow(store, chapter.index, shadow)

    def _apply_deferred_discoveries(
        self, store: RunStore, chapter: Chapter, shadow: dict[str, Any]
    ) -> None:
        state = shadow.get("factual_state", {})
        batches = state.get("audit_batches", {}) if isinstance(state, dict) else {}
        targets = self._targets(shadow)
        changed = False
        for batch in batches.values():
            if not isinstance(batch, dict) or batch.get("discoveries_applied") is True:
                continue
            read_indexes = tuple(int(index) for index in batch.get("read_indexes", []))
            source = "\n".join(chapter.segments[index].source for index in read_indexes)
            with self._terminology_lock:
                added_terms = self.terminology.add_discoveries(
                    chapter.index,
                    list(batch.get("term_candidates", [])),
                    source,
                    "\n".join(targets[index] for index in read_indexes),
                )
            batch["discoveries_applied"] = True
            batch["added_terms"] = [term.model_dump(exclude_none=True) for term in added_terms]
            changed = True
        if changed:
            self._save_shadow(store, chapter.index, shadow)
            store.log_event("deferred_terms_activated", chapter=chapter.index)

    def _factual_repair_task(
        self, store: RunStore, chapter: Chapter, shadow: dict[str, Any]
    ) -> None:
        if not self._factual_audit_complete(shadow):
            raise StageTaskError(
                f"chapter {chapter.index} requires factual-audit before factual-repair"
            )
        self._apply_deferred_discoveries(store, chapter, shadow)
        targets = self._targets(shadow)
        state = shadow["factual_state"]
        batches = state.get("audit_batches", {})
        issues = [
            issue
            for batch in batches.values()
            if isinstance(batch, dict)
            for issue in batch.get("issues", [])
        ]
        issues.extend(
            self._terminology_issues(
                chapter,
                targets,
                tuple(segment.index for segment in chapter.text_segments),
            )
        )
        if not isinstance(state.get("repair_regions"), list):
            state["repair_regions"] = [
                {
                    "id": f"factual-r{index}",
                    "start": region.start,
                    "end": region.end,
                    "issues": list(region.issues),
                }
                for index, region in enumerate(
                    self.repair_planner.plan(issues, len(chapter.segments))
                )
            ]
            self._save_shadow(store, chapter.index, shadow)
        completed = set(state.setdefault("completed_repair_regions", []))
        repair_regions = state["repair_regions"]
        self._stage_progress(
            chapter.index,
            "factual-repair",
            sum(str(item["id"]) in completed for item in repair_regions),
            len(repair_regions),
            f"共 {len(repair_regions)} 个事实修复范围",
        )
        for region_index, item in enumerate(repair_regions, start=1):
            region_id = str(item["id"])
            if region_id in completed:
                continue
            region = RepairRegion(int(item["start"]), int(item["end"]), tuple(item["issues"]))
            targets = self._repair_and_validate(
                store,
                chapter,
                targets,
                region,
                "factual_repair",
                shadow=shadow,
                checkpoint_state=state,
                checkpoint_id=region_id,
            )
            self._save_targets(shadow, targets)
            completed.add(region_id)
            state["completed_repair_regions"] = sorted(completed)
            state.pop("pending_repair", None)
            self._save_shadow(store, chapter.index, shadow)
            self._stage_progress(
                chapter.index,
                "factual-repair",
                len(completed),
                len(repair_regions),
                f"修复范围 {region_index}/{len(repair_regions)} 已完成裁决",
            )
        shadow.setdefault("stage_snapshots", {})["factual"] = dict(shadow["targets"])
        shadow["phase"] = (
            "chinese_audit" if self.config.pipeline.chinese_reader_audit else "promote"
        )
        self._save_shadow(store, chapter.index, shadow)
        store.set_chapter_fields(chapter.index, phase=shadow["phase"])

    def _chinese_audit_task(
        self, store: RunStore, chapter: Chapter, shadow: dict[str, Any]
    ) -> None:
        targets = self._targets(shadow)
        state = shadow.setdefault("chinese_state", {})
        stored_reader = state.get("reader_batches")
        if not isinstance(stored_reader, list):
            stored_reader = []
            reader_lengths = {
                segment.index: len(targets.get(segment.index, ""))
                for segment in chapter.text_segments
            }
            reader_plan = list(self.window_planner.plan(chapter, reader_lengths))
            self._stage_progress(
                chapter.index,
                "chinese-audit",
                0,
                len(reader_plan),
                "准备中文纯文本阅读审校",
            )
            for batch_index, window in enumerate(reader_plan):
                messages = chinese_reader_messages(
                    chapter, targets, window.read_indexes, window.write_indexes
                )
                input_ref = store.save_translation_input(
                    {"stage": "chinese_reader_audit", "messages": messages}
                )
                self._emit_progress(
                    "stage_activity",
                    chapter=chapter.index,
                    stage="chinese-audit",
                    detail=f"AI 中文阅读批次 {batch_index + 1}/{len(reader_plan)}",
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
                self._audit_log(
                    chapter.index,
                    "chinese-audit",
                    "chinese_reader_result",
                    {
                        "batch": batch_index,
                        "read_indexes": list(window.read_indexes),
                        "write_indexes": list(window.write_indexes),
                        "issues": parsed,
                    },
                )
                state["reader_batches"] = stored_reader
                self._save_shadow(store, chapter.index, shadow)
                self._stage_progress(
                    chapter.index,
                    "chinese-audit",
                    batch_index + 1,
                    len(reader_plan),
                    f"阅读批次 {batch_index + 1}/{len(reader_plan)}，发现 {len(parsed)} 个问题",
                )
        stored_validations = state.setdefault("validation_batches", {})
        accepted_batches: list[tuple[int, list[dict[str, Any]]]] = []
        validation_indexes = [
            index for index, item in enumerate(stored_reader) if item.get("issues")
        ]
        completed_validations = sum(
            str(index) in stored_validations for index in validation_indexes
        )
        self._stage_progress(
            chapter.index,
            "chinese-audit",
            completed_validations,
            len(validation_indexes),
            f"源文验证 {len(validation_indexes)} 个问题批次",
        )
        for batch_index, item in enumerate(stored_reader):
            window = TranslationWindow(tuple(item["read_indexes"]), tuple(item["write_indexes"]))
            batch = list(item["issues"])
            if not batch:
                continue
            tagged = [
                {**issue, "finding_id": f"b{batch_index}-f{index}"}
                for index, issue in enumerate(batch)
            ]
            key = str(batch_index)
            if key in stored_validations:
                results = list(stored_validations[key])
            else:
                validation_source = "\n".join(
                    chapter.segments[index].source for index in window.read_indexes
                )
                messages = chinese_finding_validation_messages(
                    chapter,
                    targets,
                    tagged,
                    window.read_indexes,
                    self._knowledge_for(store, chapter, validation_source),
                )
                input_ref = store.save_translation_input(
                    {"stage": "chinese_finding_validation", "messages": messages}
                )
                self._emit_progress(
                    "stage_activity",
                    chapter=chapter.index,
                    stage="chinese-audit",
                    detail=f"源文验证阅读发现，批次 {batch_index + 1}",
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
                    self._audit_log(
                        chapter.index,
                        "chinese-audit",
                        "chinese_finding_validation",
                        {"reader_issue": issue, "result": result},
                    )
                stored_validations[key] = results
                self._save_shadow(store, chapter.index, shadow)
                completed_validations += 1
                self._stage_progress(
                    chapter.index,
                    "chinese-audit",
                    completed_validations,
                    len(validation_indexes),
                    f"问题批次 {completed_validations}/{len(validation_indexes)} 已验证",
                )
            batch_accepted = [result for result in results if result["safe_to_repair"]]
            if batch_accepted:
                accepted_batches.append((batch_index, batch_accepted))
        if not isinstance(state.get("repair_regions"), list):
            repair_regions: list[dict[str, Any]] = []
            legacy_completed = {int(index) for index in state.get("completed_repair_batches", [])}
            completed_region_ids: list[str] = []
            for batch_index, batch in accepted_batches:
                planned = self.repair_planner.plan(batch, len(chapter.segments))
                write_indexes = sorted(
                    {
                        index
                        for region in planned
                        for index in region.indexes
                        if chapter.segments[index].source.strip()
                    }
                )
                if not write_indexes:
                    continue
                repair_regions.append(
                    {
                        "id": f"language-b{batch_index}",
                        "start": write_indexes[0],
                        "end": write_indexes[-1],
                        "write_indexes": write_indexes,
                        "issues": [issue for region in planned for issue in region.issues],
                    }
                )
                if batch_index in legacy_completed:
                    completed_region_ids.append(f"language-b{batch_index}")
            state["repair_regions"] = repair_regions
            state["completed_repair_regions"] = completed_region_ids
            state.pop("completed_repair_batches", None)
        state["audit_complete"] = True
        self._save_shadow(store, chapter.index, shadow)

    def _chinese_repair_task(
        self, store: RunStore, chapter: Chapter, shadow: dict[str, Any]
    ) -> None:
        if not self._chinese_audit_complete(shadow):
            raise StageTaskError(
                f"chapter {chapter.index} requires chinese-audit before chinese-repair"
            )
        targets = self._targets(shadow)
        state = shadow["chinese_state"]
        completed = set(state.setdefault("completed_repair_regions", []))
        repair_regions = state.get("repair_regions", [])
        self._stage_progress(
            chapter.index,
            "chinese-repair",
            sum(str(item["id"]) in completed for item in repair_regions),
            len(repair_regions),
            f"共 {len(repair_regions)} 个中文修复范围",
        )
        for region_index, item in enumerate(repair_regions, start=1):
            region_id = str(item["id"])
            if region_id in completed:
                continue
            region = RepairRegion(int(item["start"]), int(item["end"]), tuple(item["issues"]))
            write_indexes = tuple(
                int(index)
                for index in item.get("write_indexes", region.indexes)
                if chapter.segments[int(index)].source.strip()
            )
            if not write_indexes:
                completed.add(region_id)
                state["completed_repair_regions"] = sorted(completed)
                self._save_shadow(store, chapter.index, shadow)
                self._stage_progress(
                    chapter.index,
                    "chinese-repair",
                    len(completed),
                    len(repair_regions),
                    f"空范围 {region_index}/{len(repair_regions)} 已跳过",
                )
                continue
            targets = self._repair_and_validate(
                store,
                chapter,
                targets,
                region,
                "language_repair",
                read_indexes=self._read_scope_for_range(
                    chapter, write_indexes[0], write_indexes[-1], targets
                ),
                write_indexes=write_indexes,
                shadow=shadow,
                checkpoint_state=state,
                checkpoint_id=region_id,
            )
            self._save_targets(shadow, targets)
            completed.add(region_id)
            state["completed_repair_regions"] = sorted(completed)
            state.pop("pending_repair", None)
            self._save_shadow(store, chapter.index, shadow)
            self._stage_progress(
                chapter.index,
                "chinese-repair",
                len(completed),
                len(repair_regions),
                f"修复范围 {region_index}/{len(repair_regions)} 已完成裁决",
            )
        shadow["phase"] = "promote"
        self._save_shadow(store, chapter.index, shadow)
        store.set_chapter_fields(chapter.index, phase="promote")

    def _repair_and_validate(
        self,
        store: RunStore,
        chapter: Chapter,
        targets: dict[int, str],
        region: RepairRegion,
        stage: str,
        *,
        read_indexes: tuple[int, ...] | None = None,
        write_indexes: tuple[int, ...] | None = None,
        shadow: dict[str, Any],
        checkpoint_state: dict[str, Any],
        checkpoint_id: str,
    ) -> dict[int, str]:
        write_indexes = write_indexes or tuple(
            index for index in region.indexes if chapter.segments[index].source.strip()
        )
        if not write_indexes:
            return targets
        read_indexes = read_indexes or self._read_scope_for_range(
            chapter, write_indexes[0], write_indexes[-1], targets
        )
        if not set(write_indexes).issubset(read_indexes):
            raise AlignmentError("repair write scope must be contained in visible read scope")
        visible_stage = {
            "factual_repair": "factual-repair",
            "language_repair": "chinese-repair",
            "terminology_repair": "promote",
        }.get(stage, stage.replace("_", "-"))
        base_targets_sha256 = self._targets_sha256(targets)

        def save_checkpoint(
            status: str,
            attempt: int,
            feedback: list[dict],
            *,
            changes: dict[int, str] | None = None,
            input_ref: dict[str, str] | None = None,
            metadata: dict[str, Any] | None = None,
        ) -> None:
            checkpoint: dict[str, Any] = {
                "checkpoint_version": _REPAIR_CHECKPOINT_VERSION,
                "region_id": checkpoint_id,
                "stage": stage,
                "status": status,
                "attempt": attempt,
                "base_targets_sha256": base_targets_sha256,
                "read_indexes": list(read_indexes),
                "write_indexes": list(write_indexes),
                "feedback": feedback,
            }
            if changes is not None:
                checkpoint["changes"] = {str(index): value for index, value in changes.items()}
            if input_ref is not None:
                checkpoint["input_ref"] = input_ref
            if metadata:
                checkpoint.update(metadata)
            checkpoint_state["pending_repair"] = checkpoint
            self._save_shadow(store, chapter.index, shadow)

        def load_changes(pending: dict[str, Any]) -> dict[int, str]:
            raw_changes = pending.get("changes")
            if not isinstance(raw_changes, dict):
                raise StageTaskError("persisted repair checkpoint changes are not an object")
            changes = {int(index): str(value) for index, value in raw_changes.items()}
            if set(changes) != set(write_indexes):
                raise StageTaskError(
                    "persisted repair checkpoint changed IDs do not match write scope"
                )
            return changes

        def record_skipped(reason: str, current_feedback: list[dict]) -> None:
            skipped = checkpoint_state.setdefault("skipped_repair_regions", [])
            if not isinstance(skipped, list):
                raise StageTaskError("persisted skipped repair regions are not a list")
            entry = {
                "region_id": checkpoint_id,
                "stage": stage,
                "reason": reason,
                "feedback": list(current_feedback),
            }
            for index, existing in enumerate(skipped):
                if isinstance(existing, dict) and existing.get("region_id") == checkpoint_id:
                    skipped[index] = entry
                    break
            else:
                skipped.append(entry)

        def finish_arbitration(
            decision: str,
            reason: str,
            changes: dict[int, str],
            current_feedback: list[dict],
            input_ref: dict[str, str] | None,
            attempt: int,
        ) -> dict[int, str]:
            if decision == "accept":
                arbitrated = dict(targets)
                arbitrated.update(changes)
                term_feedback = self._terminology_issues(chapter, arbitrated, write_indexes)
                if term_feedback:
                    decision = "skip"
                    reason = (
                        f"{reason}; final arbitration violated active hard terminology: "
                        f"{term_feedback}"
                    )
                    current_feedback = term_feedback
                    changes = {index: targets[index] for index in write_indexes}
                else:
                    self._archive_targets(
                        store,
                        chapter,
                        f"{stage}_arbitrated",
                        changes,
                        previous=targets,
                        input_ref=input_ref,
                        metadata={"attempt": attempt, "reason": reason},
                    )
                    self._audit_log(
                        chapter.index,
                        visible_stage,
                        "repair_arbitrated",
                        {
                            "attempt": attempt,
                            "reason": reason,
                            "issues": list(region.issues),
                            "validation_feedback": current_feedback,
                        },
                    )
                    save_checkpoint(
                        "accepted",
                        attempt,
                        [],
                        changes=changes,
                        input_ref=input_ref,
                        metadata={"decision": "accept", "reason": reason},
                    )
                    return arbitrated

            record_skipped(reason, current_feedback)
            self._audit_log(
                chapter.index,
                visible_stage,
                "repair_skipped",
                {
                    "attempt": attempt,
                    "reason": reason,
                    "issues": list(region.issues),
                    "validation_feedback": current_feedback,
                },
            )
            save_checkpoint(
                "accepted",
                attempt,
                current_feedback,
                changes={index: targets[index] for index in write_indexes},
                input_ref=input_ref,
                metadata={"decision": "skip", "reason": reason},
            )
            return targets

        def run_arbitration(
            rejected_changes: dict[int, str],
            current_feedback: list[dict],
            attempt: int,
        ) -> dict[int, str]:
            proposed = dict(targets)
            proposed.update(rejected_changes)
            source = "\n".join(chapter.segments[index].source for index in read_indexes)
            knowledge = self._knowledge_for(store, chapter, source)
            messages = repair_arbitration_messages(
                chapter,
                targets,
                proposed,
                read_indexes,
                write_indexes,
                region.issues,
                current_feedback,
                knowledge,
            )
            input_ref = store.save_translation_input(
                {"stage": f"{stage}_arbitration", "messages": messages}
            )
            response = self.clients["repair"].complete_json(
                messages,
                tier=self.config.pipeline.repair_tier,
                stage=f"{stage}_arbitration",
            )
            decision = (
                str(response.get("decision", "")).strip().casefold()
                if isinstance(response, dict)
                else ""
            )
            reason = str(response.get("reason", "")).strip() if isinstance(response, dict) else ""
            if decision == "accept" and not reason:
                decision = "skip"
                reason = "Sol final arbitration did not provide a source-based reason"
                changes = {index: targets[index] for index in write_indexes}
            elif decision == "accept":
                try:
                    changes = self._parse_translations(chapter, write_indexes, response)
                except (AlignmentError, KeyError, TypeError, ValueError) as exc:
                    decision = "skip"
                    reason = f"Sol final arbitration response was invalid: {exc}"
                    changes = {index: targets[index] for index in write_indexes}
            elif decision == "skip":
                changes = {index: targets[index] for index in write_indexes}
                reason = reason or "Sol could not confirm a fidelity-safe repair"
            else:
                decision = "skip"
                reason = "Sol final arbitration did not return accept or skip"
                changes = {index: targets[index] for index in write_indexes}
            store.record_audit(
                chapter.index,
                f"{stage}_arbitration",
                {
                    "input_ref": input_ref,
                    "attempt": attempt,
                    "decision": decision,
                    "reason": reason,
                    "issues": list(region.issues),
                    "validation_feedback": current_feedback,
                    "write_indexes": list(write_indexes),
                },
            )
            save_checkpoint(
                "arbitration_result",
                attempt,
                current_feedback,
                changes=changes,
                input_ref=input_ref,
                metadata={"decision": decision, "reason": reason},
            )
            return finish_arbitration(
                decision,
                reason,
                changes,
                current_feedback,
                input_ref,
                attempt,
            )

        feedback: list[dict] = []
        start_attempt = 1
        resumed_changes: dict[int, str] | None = None
        resumed_input_ref: dict[str, str] | None = None
        resumed_status: str | None = None
        pending = checkpoint_state.get("pending_repair")
        if pending is not None:
            expected = {
                "region_id": checkpoint_id,
                "stage": stage,
                "base_targets_sha256": base_targets_sha256,
                "read_indexes": list(read_indexes),
                "write_indexes": list(write_indexes),
            }
            mismatched = [key for key, value in expected.items() if pending.get(key) != value]
            if mismatched:
                raise StageTaskError(
                    f"persisted repair checkpoint does not match the current task: {mismatched}"
                )
            resumed_status = str(pending.get("status", ""))
            if resumed_status not in {
                "proposal",
                "retry",
                "accepted",
                "arbitration_required",
                "arbitration_result",
                "hard_terminology_failed",
            }:
                raise StageTaskError(
                    f"persisted repair checkpoint has invalid status: {resumed_status!r}"
                )
            try:
                start_attempt = int(pending["attempt"])
            except (KeyError, TypeError, ValueError) as exc:
                raise StageTaskError("persisted repair checkpoint has an invalid attempt") from exc
            raw_feedback = pending.get("feedback", [])
            if not isinstance(raw_feedback, list):
                raise StageTaskError("persisted repair checkpoint feedback is not a list")
            feedback = list(raw_feedback)
            if resumed_status == "hard_terminology_failed":
                raise RuntimeError(
                    f"{stage} failed source-fidelity validation after "
                    f"{self.config.pipeline.max_repair_attempts} attempts: {feedback}"
                )
            if (
                resumed_status == "retry"
                and start_attempt > self.config.pipeline.max_repair_attempts
            ):
                if any(
                    isinstance(item, dict) and item.get("terminology_violation")
                    for item in feedback
                ):
                    save_checkpoint(
                        "hard_terminology_failed",
                        self.config.pipeline.max_repair_attempts,
                        feedback,
                        metadata={"migration": "legacy_hard_terminology_failure"},
                    )
                    raise RuntimeError(
                        f"{stage} failed source-fidelity validation after "
                        f"{self.config.pipeline.max_repair_attempts} attempts: "
                        f"{feedback}"
                    )
                resumed_status = "arbitration_required"
                resumed_changes = {index: targets[index] for index in write_indexes}
                save_checkpoint(
                    "arbitration_required",
                    self.config.pipeline.max_repair_attempts,
                    feedback,
                    changes=resumed_changes,
                    metadata={"migration": "legacy_exhausted_retry"},
                )
            if resumed_status in {
                "proposal",
                "accepted",
                "arbitration_required",
                "arbitration_result",
            }:
                resumed_changes = (
                    load_changes(pending) if resumed_changes is None else resumed_changes
                )
                raw_input_ref = pending.get("input_ref")
                if isinstance(raw_input_ref, dict):
                    resumed_input_ref = {
                        str(key): str(value) for key, value in raw_input_ref.items()
                    }
                if resumed_status == "accepted":
                    decision = str(pending.get("decision", "accept"))
                    if decision in {"skip", "reject_finding"}:
                        record_skipped(str(pending.get("reason", "")), feedback)
                        self._save_shadow(store, chapter.index, shadow)
                    accepted = dict(targets)
                    accepted.update(resumed_changes)
                    return accepted
                if resumed_status == "arbitration_result":
                    return finish_arbitration(
                        str(pending.get("decision", "skip")),
                        str(pending.get("reason", "")),
                        resumed_changes,
                        feedback,
                        resumed_input_ref,
                        start_attempt,
                    )
                if resumed_status == "arbitration_required":
                    return run_arbitration(resumed_changes, feedback, start_attempt)

        for attempt in range(start_attempt, self.config.pipeline.max_repair_attempts + 1):
            self._emit_progress(
                "stage_activity",
                chapter=chapter.index,
                stage=visible_stage,
                detail=(f"AI 修复第 {attempt} 次，段落 {write_indexes[0]}–{write_indexes[-1]}"),
            )
            source = "\n".join(chapter.segments[index].source for index in read_indexes)
            knowledge = self._knowledge_for(store, chapter, source)
            input_ref: dict[str, str] | None
            reused_proposal = False
            if resumed_status == "proposal" and resumed_changes is not None:
                changes = resumed_changes
                input_ref = resumed_input_ref
                reused_proposal = True
                resumed_changes = None
                resumed_input_ref = None
                resumed_status = None
            else:
                messages = repair_messages(
                    chapter,
                    targets,
                    read_indexes,
                    write_indexes,
                    region.issues,
                    knowledge,
                    feedback,
                )
                input_ref = store.save_translation_input(
                    {"stage": stage, "attempt": attempt, "messages": messages}
                )
                response = self.clients["repair"].complete_json(
                    messages,
                    tier=self.config.pipeline.repair_tier,
                    stage=stage,
                )
                repair_decision = (
                    str(response.get("decision", "repair")).strip().casefold()
                    if isinstance(response, dict)
                    else "repair"
                )
                if repair_decision == "reject_finding":
                    reason = str(response.get("reason", "")).strip()
                    if not reason:
                        raise AlignmentError(
                            "repair decision reject_finding requires a source-based reason"
                        )
                    record_skipped(reason, feedback)
                    self._audit_log(
                        chapter.index,
                        visible_stage,
                        "repair_finding_rejected",
                        {
                            "attempt": attempt,
                            "reason": reason,
                            "issues": list(region.issues),
                        },
                    )
                    save_checkpoint(
                        "accepted",
                        attempt,
                        feedback,
                        changes={index: targets[index] for index in write_indexes},
                        input_ref=input_ref,
                        metadata={
                            "decision": "reject_finding",
                            "reason": reason,
                        },
                    )
                    return targets
                if repair_decision != "repair":
                    raise AlignmentError("repair decision must be repair or reject_finding")
                changes = self._parse_translations(chapter, write_indexes, response)
            proposed = dict(targets)
            proposed.update(changes)
            proposal = [
                {
                    "id": segment_id(chapter.index, chapter.segments[index]),
                    "index": index,
                    "before": targets[index],
                    "after": changes[index],
                }
                for index in write_indexes
            ]
            if not reused_proposal:
                self._audit_log(
                    chapter.index,
                    visible_stage,
                    "repair_proposal",
                    {
                        "attempt": attempt,
                        "issues": list(region.issues),
                        "changes": proposal,
                    },
                )
                self._archive_targets(
                    store,
                    chapter,
                    f"{stage}_proposal",
                    changes,
                    previous=targets,
                    input_ref=input_ref,
                    metadata={"attempt": attempt},
                )
                save_checkpoint(
                    "proposal",
                    attempt,
                    feedback,
                    changes=changes,
                    input_ref=input_ref,
                )
            term_feedback = self._terminology_issues(chapter, proposed, write_indexes)
            if term_feedback:
                feedback = term_feedback
                store.record_audit(
                    chapter.index,
                    f"{stage}_fidelity",
                    {
                        "input_ref": input_ref,
                        "attempt": attempt,
                        "valid": False,
                        "issues": feedback,
                        "write_indexes": list(write_indexes),
                        "validator": "hard_terminology",
                    },
                )
                self._audit_log(
                    chapter.index,
                    visible_stage,
                    "repair_validation",
                    {
                        "attempt": attempt,
                        "valid": False,
                        "validator": "hard_terminology",
                        "issues": feedback,
                        "write_indexes": list(write_indexes),
                    },
                )
                if attempt >= self.config.pipeline.max_repair_attempts:
                    save_checkpoint(
                        "hard_terminology_failed",
                        attempt,
                        feedback,
                        changes=changes,
                        input_ref=input_ref,
                    )
                    raise RuntimeError(
                        f"{stage} failed source-fidelity validation after "
                        f"{self.config.pipeline.max_repair_attempts} attempts: "
                        f"{feedback}"
                    )
                save_checkpoint("retry", attempt + 1, feedback)
                continue
            validation_messages = fidelity_validation_messages(
                chapter, proposed, read_indexes, write_indexes, knowledge
            )
            validation_ref = store.save_translation_input(
                {"stage": f"{stage}_fidelity", "messages": validation_messages}
            )
            validation = self.clients["validation"].complete_json(
                validation_messages,
                tier=self.config.pipeline.validation_tier,
                stage=f"{stage}_fidelity",
            )
            valid = isinstance(validation, dict) and validation.get("valid") is True
            feedback = list(validation.get("issues", [])) if isinstance(validation, dict) else []
            store.record_audit(
                chapter.index,
                f"{stage}_fidelity",
                {
                    "input_ref": validation_ref,
                    "attempt": attempt,
                    "valid": valid,
                    "issues": feedback,
                    "write_indexes": list(write_indexes),
                },
            )
            self._audit_log(
                chapter.index,
                visible_stage,
                "repair_validation",
                {
                    "attempt": attempt,
                    "valid": valid,
                    "issues": feedback,
                    "write_indexes": list(write_indexes),
                },
            )
            if valid:
                self._archive_targets(
                    store,
                    chapter,
                    f"{stage}_accepted",
                    changes,
                    previous=targets,
                    input_ref=validation_ref,
                    metadata={"attempt": attempt},
                )
                self._audit_log(
                    chapter.index,
                    visible_stage,
                    "repair_accepted",
                    {"attempt": attempt, "changes": proposal},
                )
                save_checkpoint("accepted", attempt, [], changes=changes)
                return proposed
            if attempt >= self.config.pipeline.max_repair_attempts:
                save_checkpoint(
                    "arbitration_required",
                    attempt,
                    feedback,
                    changes=changes,
                    input_ref=input_ref,
                )
                return run_arbitration(changes, feedback, attempt)
            save_checkpoint("retry", attempt + 1, feedback)
        raise RuntimeError(
            f"{stage} failed source-fidelity validation after "
            f"{self.config.pipeline.max_repair_attempts} attempts: {feedback}"
        )

    def _promote(self, store: RunStore, chapter: Chapter, shadow: dict[str, Any]) -> None:
        self._stage_progress(
            chapter.index,
            "promote",
            0,
            1,
            "最终结构与术语检查",
        )
        targets = self._targets(shadow)
        missing = [
            segment.index
            for segment in chapter.text_segments
            if not targets.get(segment.index, "").strip()
        ]
        if missing:
            raise AlignmentError(f"cannot promote chapter with empty targets: {missing}")
        term_issues = self._terminology_issues(
            chapter,
            targets,
            tuple(segment.index for segment in chapter.text_segments),
        )
        if term_issues:
            self._audit_log(
                chapter.index,
                "promote",
                "hard_terminology_findings",
                {"issues": term_issues},
            )
            term_state = shadow.setdefault("terminology_repair_state", {})
            for region in self.repair_planner.plan(term_issues, len(chapter.segments)):
                region_id = self._repair_checkpoint_id("terminology_repair", region)
                targets = self._repair_and_validate(
                    store,
                    chapter,
                    targets,
                    region,
                    "terminology_repair",
                    shadow=shadow,
                    checkpoint_state=term_state,
                    checkpoint_id=region_id,
                )
                self._save_targets(shadow, targets)
                term_state.pop("pending_repair", None)
                self._save_shadow(store, chapter.index, shadow)
            remaining = self._terminology_issues(
                chapter,
                targets,
                tuple(segment.index for segment in chapter.text_segments),
            )
            if remaining:
                raise RuntimeError(
                    "cannot promote chapter with hard terminology violations after repair: "
                    f"{remaining}"
                )
        shadow.pop("terminology_repair_state", None)
        previous = {segment.index: segment.target or "" for segment in chapter.segments}
        for segment in chapter.segments:
            if segment.source.strip():
                segment.target = targets[segment.index]
        self._archive_targets(
            store,
            chapter,
            "formal_promotion",
            {segment.index: segment.target or "" for segment in chapter.text_segments},
            previous=previous,
        )
        store.save_chapter(chapter)
        shadow["phase"] = "done"
        self._save_shadow(store, chapter.index, shadow)
        store.set_chapter_fields(
            chapter.index,
            status=STATUS_DONE,
            phase="done",
            error=None,
            factual_audit=self.config.pipeline.factual_audit,
            chinese_reader_audit=self.config.pipeline.chinese_reader_audit,
        )
        store.log_event("chapter_promoted", chapter=chapter.index)
        self._stage_progress(
            chapter.index,
            "promote",
            1,
            1,
            "Formal 已原子保存",
        )

    def _parse_translations(
        self, chapter: Chapter, indexes: tuple[int, ...], response: Any
    ) -> dict[int, str]:
        if not isinstance(response, dict) or not isinstance(response.get("translations"), list):
            raise AlignmentError("response must be an object with a translations array")
        expected = {segment_id(chapter.index, chapter.segments[index]): index for index in indexes}
        result: dict[int, str] = {}
        seen: set[str] = set()
        for item in response["translations"]:
            if not isinstance(item, dict):
                raise AlignmentError("translation item must be an object")
            stable_id = str(item.get("id", ""))
            target = _clean_target_control_metadata(str(item.get("target", "")))
            if stable_id not in expected or stable_id in seen or not target:
                raise AlignmentError(f"invalid translation item for id {stable_id!r}")
            seen.add(stable_id)
            result[expected[stable_id]] = target
        if seen != set(expected):
            raise AlignmentError(
                f"translation IDs mismatch: expected {sorted(expected)}, got {sorted(seen)}"
            )
        return result

    def _parse_issues(
        self,
        chapter: Chapter,
        response: Any,
        audited_indexes: set[int],
        visible_read_indexes: set[int],
    ) -> list[dict]:
        if not isinstance(response, dict) or not isinstance(response.get("issues"), list):
            raise AlignmentError("audit response must be an object with an issues array")
        id_map = {
            segment_id(chapter.index, segment): segment.index for segment in chapter.text_segments
        }
        parsed: list[dict] = []
        for raw in response["issues"]:
            if not isinstance(raw, dict):
                continue
            try:
                start = self._resolve_audit_id(chapter, id_map, raw["start_id"])
                end = self._resolve_audit_id(chapter, id_map, raw.get("end_id", raw["start_id"]))
                cause_start = self._resolve_audit_id(
                    chapter, id_map, raw.get("cause_start_id", raw["start_id"])
                )
                cause_end = self._resolve_audit_id(
                    chapter,
                    id_map,
                    raw.get("cause_end_id", raw.get("end_id", raw["start_id"])),
                )
            except (KeyError, TypeError):
                raise AlignmentError(f"audit issue contains an unknown stable ID: {raw}")
            symptom_indexes = {
                segment.index
                for segment in chapter.text_segments
                if min(start, end) <= segment.index <= max(start, end)
            }
            cause_indexes = {
                segment.index
                for segment in chapter.text_segments
                if min(cause_start, cause_end) <= segment.index <= max(cause_start, cause_end)
            }
            if not (symptom_indexes | cause_indexes).issubset(visible_read_indexes):
                raise AlignmentError("audit issue escaped outside the visible read scope")
            if not symptom_indexes & audited_indexes:
                continue
            parsed.append(
                {
                    **raw,
                    "start": min(start, end),
                    "end": max(start, end),
                    "cause_start": min(cause_start, cause_end),
                    "cause_end": max(cause_start, cause_end),
                }
            )
        return parsed

    @staticmethod
    def _resolve_audit_id(chapter: Chapter, id_map: dict[str, int], value: Any) -> int:
        """Recover an audit location when only the copied digest suffix is wrong."""
        stable_id = str(value)
        if stable_id in id_map:
            return id_map[stable_id]
        match = re.fullmatch(r"ch(\d+):s(\d+)(?::[0-9A-Za-z_-]+)?", stable_id)
        if match and int(match.group(1)) == chapter.index:
            index = int(match.group(2))
            if any(segment.index == index for segment in chapter.text_segments):
                return index
        raise KeyError(stable_id)

    def _parse_reader_validations(
        self,
        chapter: Chapter,
        response: Any,
        original_issues: list[dict],
        visible_read_indexes: set[int],
    ) -> list[dict]:
        if not isinstance(response, dict) or not isinstance(response.get("results"), list):
            raise AlignmentError("reader validation response is malformed")
        expected = {str(issue["finding_id"]): issue for issue in original_issues}
        received: dict[str, dict] = {}
        for result in response["results"]:
            if not isinstance(result, dict):
                raise AlignmentError("reader validation result is malformed")
            finding_id = str(result.get("finding_id", ""))
            if finding_id not in expected or finding_id in received:
                raise AlignmentError(
                    f"reader validation returned unknown or duplicate finding_id {finding_id!r}"
                )
            received[finding_id] = self._parse_reader_validation(
                chapter, result, expected[finding_id], visible_read_indexes
            )
        if set(received) != set(expected):
            raise AlignmentError("reader validation omitted one or more finding IDs")
        return [received[str(issue["finding_id"])] for issue in original_issues]

    def _parse_reader_validation(
        self,
        chapter: Chapter,
        response: Any,
        original_issue: dict,
        visible_read_indexes: set[int],
    ) -> dict:
        if not isinstance(response.get("safe_to_repair"), bool):
            raise AlignmentError("reader validation response is malformed")
        result = {**original_issue, **response}
        if not response["safe_to_repair"]:
            return result
        id_map = {
            segment_id(chapter.index, segment): segment.index for segment in chapter.text_segments
        }
        try:
            start = self._resolve_audit_id(chapter, id_map, response["repair_start_id"])
            end = self._resolve_audit_id(chapter, id_map, response["repair_end_id"])
        except (KeyError, TypeError):
            raise AlignmentError("reader validation returned an unknown repair range")
        repair_indexes = {
            segment.index
            for segment in chapter.text_segments
            if min(start, end) <= segment.index <= max(start, end)
        }
        if not repair_indexes.issubset(visible_read_indexes):
            raise AlignmentError("reader validation escaped outside the visible read scope")
        result.update(
            {
                "start": min(start, end),
                "end": max(start, end),
                "cause_start": min(start, end),
                "cause_end": max(start, end),
            }
        )
        return result

    def _read_scope_for_range(
        self,
        chapter: Chapter,
        start: int,
        end: int,
        targets: dict[int, str] | None = None,
    ) -> tuple[int, ...]:
        indexes = [segment.index for segment in chapter.text_segments]
        lengths = {
            index: len(chapter.segments[index].source) + len((targets or {}).get(index, ""))
            for index in indexes
        }
        total = sum(lengths.values())
        if total <= self.config.window.max_read_chars:
            return tuple(indexes)
        positions = {index: position for position, index in enumerate(indexes)}
        left = positions[start]
        right = positions[end]
        chars = sum(lengths[index] for index in indexes[left : right + 1])
        before = after = 0
        while True:
            changed = False
            if left > 0:
                length = lengths[indexes[left - 1]]
                if (
                    before + length <= self.config.window.source_halo_chars
                    and chars + length <= self.config.window.max_read_chars
                ):
                    left -= 1
                    before += length
                    chars += length
                    changed = True
            if right + 1 < len(indexes):
                length = lengths[indexes[right + 1]]
                if (
                    after + length <= self.config.window.source_halo_chars
                    and chars + length <= self.config.window.max_read_chars
                ):
                    right += 1
                    after += length
                    chars += length
                    changed = True
            if not changed:
                break
        return tuple(indexes[left : right + 1])

    def _archive_targets(
        self,
        store: RunStore,
        chapter: Chapter,
        stage: str,
        changed: dict[int, str],
        *,
        previous: dict[int, str] | None = None,
        input_ref: dict[str, str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        for group in _group_contiguous(changed):
            store.record_translation_stage(
                stage,
                chapter=chapter.index,
                start_index=group[0],
                sources=[chapter.segments[index].source for index in group],
                targets=[changed[index] for index in group],
                previous_targets=(
                    [previous.get(index, "") for index in group] if previous is not None else None
                ),
                input_ref=input_ref,
                metadata=metadata,
            )

    def _save_usage(self, store: RunStore) -> None:
        unique: dict[int, LLMClient] = {id(client): client for client in self.clients.values()}
        store.save_usage(
            {
                "providers": [client.usage_summary() for client in unique.values()],
                "note": "CLI token counts may be estimates",
            }
        )

    def _knowledge_for(self, store: RunStore, chapter: Chapter, read_source: str) -> dict[str, Any]:
        """Combine confirmed knowledge with a bounded, past-only raw chapter tail."""
        with self._terminology_lock:
            visible = self.terminology.visible(chapter.index, read_source)
        remaining = self.config.window.past_context_chars
        tail: list[dict[str, Any]] = []
        manifest = store.load_manifest()
        status = {item["index"]: item.get("status") for item in manifest["chapters"]}
        for previous_index in range(chapter.index - 1, -1, -1):
            if remaining <= 0:
                break
            provisional: dict[str, str] | None = None
            if status.get(previous_index) != STATUS_DONE:
                if not self._allow_provisional_factual_context:
                    break
                shadow = store.load_shadow(previous_index)
                snapshots = shadow.get("stage_snapshots", {}) if shadow else {}
                candidate = snapshots.get("factual") if isinstance(snapshots, dict) else None
                if not isinstance(candidate, dict):
                    break
                provisional = {str(key): str(value) for key, value in candidate.items()}
            previous = store.load_chapter(previous_index)
            for segment in reversed(previous.text_segments):
                target = (
                    provisional.get(str(segment.index), "")
                    if provisional is not None
                    else segment.target or ""
                )
                cost = len(segment.source) + len(target)
                if tail and cost > remaining:
                    break
                row: dict[str, Any] = {
                    "chapter": previous_index,
                    "segment": segment.index,
                    "source": segment.source,
                }
                if provisional is None:
                    row["formal_target"] = target
                else:
                    row["factual_target"] = target
                    row["provisional"] = True
                tail.append(row)
                remaining -= cost
                if remaining <= 0:
                    break
        visible["past_only_raw_tail"] = list(reversed(tail))
        past_label = (
            "previous factual snapshot > past formal target"
            if any(item.get("provisional") for item in tail)
            else "past formal target"
        )
        visible["evidence_priority"] = (
            f"current_and_nearby_source > active hard terms > active preferred terms > {past_label}"
        )
        return visible

    def _terminology_issues(
        self,
        chapter: Chapter,
        targets: dict[int, str],
        indexes: tuple[int, ...],
    ) -> list[dict[str, Any]]:
        issues: list[dict[str, Any]] = []
        with self._terminology_lock:
            for index in indexes:
                segment = chapter.segments[index]
                violations = self.terminology.hard_violations(
                    chapter.index, segment.source, targets.get(index, "")
                )
                for violation in violations:
                    issues.append(
                        {
                            "start": index,
                            "end": index,
                            "cause_start": index,
                            "cause_end": index,
                            "start_id": segment_id(chapter.index, segment),
                            "end_id": segment_id(chapter.index, segment),
                            "type": "term",
                            "detail": (
                                f"硬术语 {violation['source']!r} 必须采用 "
                                f"{violation['required_target']!r}"
                            ),
                            "required_meaning": violation["required_target"],
                            "terminology_violation": violation,
                        }
                    )
        return issues


def export_json(store: RunStore, output_path: str | Path) -> str:
    store.require_formal_complete()
    manifest = store.load_manifest()
    chapters = []
    for item in manifest["chapters"]:
        chapter = store.load_chapter(item["index"])
        chapters.append(
            {
                "index": chapter.index,
                "title": chapter.title,
                "segments": [segment.to_dict() for segment in chapter.segments],
            }
        )
    payload = {
        "title": manifest["title"],
        "source_lang": manifest["source_lang"],
        "target_lang": manifest["target_lang"],
        "chapters": chapters,
    }
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)
