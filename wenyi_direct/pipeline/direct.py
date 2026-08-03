"""Chapter-first translation, audit, repair, validation, and atomic promotion."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ..config import Config
from ..ingest.models import Chapter
from ..ingest.segmenter import load_document
from ..llm.base import LLMClient
from ..llm.json_parser import JSONParseError
from ..prompts import (
    chinese_finding_validation_messages,
    chinese_reader_messages,
    factual_audit_messages,
    fidelity_validation_messages,
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


_SPEAKER_METADATA_PREFIX_RE = re.compile(r"^\s*【話者：[^】]*】\s*")


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
    ) -> None:
        self.config = config
        self.clients = clients
        self.config_dir = Path(config_dir).resolve()
        self.window_planner = WindowPlanner(config.window)
        self.repair_planner = RepairPlanner(config.pipeline.repair_context_segments)
        terms_path = Path(config.terminology_file or "terminology.yaml")
        if not terms_path.is_absolute():
            terms_path = self.config_dir / terms_path
        self.terminology = TerminologyStore.load(terms_path)

    def store_for(self, source_path: str | Path) -> RunStore:
        state_root = Path(self.config.state_dir)
        if not state_root.is_absolute():
            state_root = self.config_dir / state_root
        return RunStore(str(state_root / slugify(Path(source_path).stem)))

    def prepare(self, source_path: str | Path) -> RunStore:
        source = str(Path(source_path).resolve())
        source_sha256 = hashlib.sha256(Path(source).read_bytes()).hexdigest()
        store = self.store_for(source)
        if store.exists():
            manifest = store.load_manifest()
            recorded = str(Path(manifest["source_path"]).resolve())
            if recorded != source:
                raise ValueError(
                    f"state belongs to another source: {manifest['source_path']}"
                )
            if manifest.get("source_sha256") != source_sha256:
                raise RuntimeError(
                    "source file changed after state creation; create a new state or "
                    "explicitly migrate the source instead of resuming stale segments"
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
        store.log_event("prepared", chapters=len(document.chapters), source_path=source)
        return store

    def run(
        self,
        source_path: str | Path,
        *,
        chapters: set[int] | None = None,
    ) -> RunStore:
        store = self.prepare(source_path)
        with store.lock():
            pending = store.pending_chapters()
            if chapters is not None:
                pending = [index for index in pending if index in chapters]
            for chapter_index in pending:
                try:
                    self._run_chapter(store, chapter_index)
                finally:
                    # Keep the live monitor useful during long books and retain
                    # provider/fallback evidence even when a chapter fails.
                    self._save_usage(store)
        return store

    def _run_chapter(self, store: RunStore, chapter_index: int) -> None:
        chapter = store.load_chapter(chapter_index)
        digest = chapter_source_digest(chapter)
        shadow = store.load_shadow(chapter_index)
        if shadow and shadow.get("source_digest") != digest:
            raise RuntimeError(
                f"chapter {chapter_index} source changed after shadow creation"
            )
        if not shadow:
            shadow = {
                "schema": 1,
                "chapter": chapter_index,
                "source_digest": digest,
                "phase": "translate",
                "targets": {
                    str(segment.index): segment.target or "" for segment in chapter.segments
                },
                "translated_ids": [],
            }
            store.save_shadow(chapter_index, shadow)
        self._clean_shadow_control_metadata(store, chapter, shadow)
        store.set_chapter_fields(chapter_index, phase=shadow["phase"], error=None)
        try:
            if shadow["phase"] == "translate":
                self._translate_chapter(store, chapter, shadow)
            if shadow["phase"] == "factual_audit":
                self._factual_stage(store, chapter, shadow)
            if shadow["phase"] == "chinese_audit":
                self._chinese_stage(store, chapter, shadow)
            if shadow["phase"] == "promote":
                self._promote(store, chapter, shadow)
        except Exception as exc:
            store.set_chapter_fields(
                chapter_index, phase=shadow.get("phase", "unknown"), error=str(exc)
            )
            store.log_event(
                "chapter_failed",
                chapter=chapter_index,
                phase=shadow.get("phase"),
                error=str(exc),
            )
            raise

    @staticmethod
    def _targets(shadow: dict[str, Any]) -> dict[int, str]:
        return {int(index): str(target) for index, target in shadow["targets"].items()}

    @staticmethod
    def _save_targets(shadow: dict[str, Any], targets: dict[int, str]) -> None:
        shadow["targets"] = {str(index): target for index, target in targets.items()}

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
        store.save_shadow(chapter.index, shadow)
        store.log_event(
            "control_metadata_cleanup", chapter=chapter.index, count=len(cleaned)
        )

    def _translate_chapter(
        self, store: RunStore, chapter: Chapter, shadow: dict[str, Any]
    ) -> None:
        completed = set(shadow.get("translated_ids", []))
        for planned in self.window_planner.plan(chapter):
            missing = tuple(
                index
                for index in planned.write_indexes
                if segment_id(chapter.index, chapter.segments[index]) not in completed
            )
            if not missing:
                continue
            window = TranslationWindow(planned.read_indexes, missing)
            translated = self._translate_window(store, chapter, window)
            targets = self._targets(shadow)
            targets.update(translated)
            self._save_targets(shadow, targets)
            completed.update(
                segment_id(chapter.index, chapter.segments[index]) for index in translated
            )
            shadow["translated_ids"] = sorted(completed)
            store.save_shadow(chapter.index, shadow)
        expected = {
            segment_id(chapter.index, segment) for segment in chapter.text_segments
        }
        if completed != expected:
            raise AlignmentError(
                f"chapter translation incomplete: missing {sorted(expected - completed)}"
            )
        shadow["phase"] = (
            "factual_audit" if self.config.pipeline.factual_audit else "chinese_audit"
        )
        if not self.config.pipeline.factual_audit and not self.config.pipeline.chinese_reader_audit:
            shadow["phase"] = "promote"
        store.save_shadow(chapter.index, shadow)
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

    def _factual_stage(
        self, store: RunStore, chapter: Chapter, shadow: dict[str, Any]
    ) -> None:
        targets = self._targets(shadow)
        issues: list[dict] = []
        for window in self.window_planner.plan(chapter):
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
            response = self.clients["factual_audit"].complete_json(
                messages,
                tier=self.config.pipeline.audit_tier,
                stage="factual_audit",
            )
            parsed = self._parse_issues(chapter, response, set(window.write_indexes))
            issues.extend(parsed)
            discoveries = (
                list(response.get("term_candidates", []))
                if isinstance(response, dict)
                and isinstance(response.get("term_candidates", []), list)
                else []
            )
            added_terms = self.terminology.add_discoveries(
                chapter.index,
                discoveries,
                source,
                "\n".join(targets[index] for index in window.read_indexes),
            )
            store.record_audit(
                chapter.index,
                "factual_audit",
                {
                    "input_ref": input_ref,
                    "issues": parsed,
                    "added_terms": [term.model_dump(exclude_none=True) for term in added_terms],
                },
            )
        issues.extend(
            self._terminology_issues(
                chapter,
                targets,
                tuple(segment.index for segment in chapter.text_segments),
            )
        )
        for region in self.repair_planner.plan(issues, len(chapter.segments)):
            targets = self._repair_and_validate(
                store, chapter, targets, region, "factual_repair"
            )
            self._save_targets(shadow, targets)
            store.save_shadow(chapter.index, shadow)
        shadow["phase"] = (
            "chinese_audit" if self.config.pipeline.chinese_reader_audit else "promote"
        )
        store.save_shadow(chapter.index, shadow)
        store.set_chapter_fields(chapter.index, phase=shadow["phase"])

    def _chinese_stage(
        self, store: RunStore, chapter: Chapter, shadow: dict[str, Any]
    ) -> None:
        targets = self._targets(shadow)
        reader_batches: list[tuple[TranslationWindow, list[dict]]] = []
        for window in self.window_planner.plan(chapter):
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
            parsed = self._parse_issues(chapter, response, set(window.write_indexes))
            reader_batches.append((window, parsed))
            store.record_audit(
                chapter.index,
                "chinese_reader_audit",
                {"input_ref": input_ref, "issues": parsed},
            )

        accepted_batches: list[tuple[TranslationWindow, list[dict]]] = []
        for window, batch in reader_batches:
            if not batch:
                continue
            tagged = [
                {**issue, "finding_id": f"f{index}"}
                for index, issue in enumerate(batch)
            ]
            messages = chinese_finding_validation_messages(
                chapter, targets, tagged, window.read_indexes
            )
            input_ref = store.save_translation_input(
                {"stage": "chinese_finding_validation", "messages": messages}
            )
            response = self.clients["validation"].complete_json(
                messages,
                tier=self.config.pipeline.validation_tier,
                stage="chinese_finding_validation",
            )
            results = self._parse_reader_validations(chapter, response, tagged)
            batch_accepted: list[dict] = []
            for issue, result in zip(tagged, results, strict=True):
                store.record_audit(
                    chapter.index,
                    "chinese_finding_validation",
                    {"input_ref": input_ref, "reader_issue": issue, "result": result},
                )
                if result["safe_to_repair"]:
                    batch_accepted.append(result)
            if batch_accepted:
                accepted_batches.append((window, batch_accepted))

        for window, batch in accepted_batches:
            regions = self.repair_planner.plan(batch, len(chapter.segments))
            write_indexes = tuple(
                sorted({index for region in regions for index in region.indexes})
            )
            combined = RepairRegion(
                write_indexes[0],
                write_indexes[-1],
                tuple(issue for region in regions for issue in region.issues),
            )
            targets = self._repair_and_validate(
                store,
                chapter,
                targets,
                combined,
                "language_repair",
                read_indexes=window.read_indexes,
                write_indexes=write_indexes,
            )
            self._save_targets(shadow, targets)
            store.save_shadow(chapter.index, shadow)
        shadow["phase"] = "promote"
        store.save_shadow(chapter.index, shadow)
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
    ) -> dict[int, str]:
        write_indexes = write_indexes or tuple(
            index for index in region.indexes if chapter.segments[index].source.strip()
        )
        if not write_indexes:
            return targets
        read_indexes = read_indexes or self._read_scope_for_range(
            chapter, write_indexes[0], write_indexes[-1]
        )
        feedback: list[dict] = []
        for attempt in range(1, self.config.pipeline.max_repair_attempts + 1):
            source = "\n".join(
                chapter.segments[index].source for index in read_indexes
            )
            knowledge = self._knowledge_for(store, chapter, source)
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
            changes = self._parse_translations(chapter, write_indexes, response)
            proposed = dict(targets)
            proposed.update(changes)
            self._archive_targets(
                store,
                chapter,
                f"{stage}_proposal",
                changes,
                previous=targets,
                input_ref=input_ref,
                metadata={"attempt": attempt},
            )
            term_feedback = self._terminology_issues(
                chapter, proposed, write_indexes
            )
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
                return proposed
        raise RuntimeError(
            f"{stage} failed source-fidelity validation after "
            f"{self.config.pipeline.max_repair_attempts} attempts: {feedback}"
        )

    def _promote(
        self, store: RunStore, chapter: Chapter, shadow: dict[str, Any]
    ) -> None:
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
            raise RuntimeError(
                f"cannot promote chapter with hard terminology violations: {term_issues}"
            )
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
        store.save_shadow(chapter.index, shadow)
        store.set_chapter_fields(
            chapter.index,
            status=STATUS_DONE,
            phase="done",
            error=None,
            factual_audit=self.config.pipeline.factual_audit,
            chinese_reader_audit=self.config.pipeline.chinese_reader_audit,
        )
        store.log_event("chapter_promoted", chapter=chapter.index)

    def _parse_translations(
        self, chapter: Chapter, indexes: tuple[int, ...], response: Any
    ) -> dict[int, str]:
        if not isinstance(response, dict) or not isinstance(response.get("translations"), list):
            raise AlignmentError("response must be an object with a translations array")
        expected = {
            segment_id(chapter.index, chapter.segments[index]): index for index in indexes
        }
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
            raise AlignmentError(f"translation IDs mismatch: expected {sorted(expected)}, got {sorted(seen)}")
        return result

    def _parse_issues(
        self, chapter: Chapter, response: Any, allowed_starts: set[int]
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
                end = self._resolve_audit_id(
                    chapter, id_map, raw.get("end_id", raw["start_id"])
                )
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
            if not set(range(min(start, end), max(start, end) + 1)) & allowed_starts:
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
    def _resolve_audit_id(
        chapter: Chapter, id_map: dict[str, int], value: Any
    ) -> int:
        """Recover an audit location when only the copied digest suffix is wrong."""
        stable_id = str(value)
        if stable_id in id_map:
            return id_map[stable_id]
        match = re.fullmatch(
            r"ch(\d+):s(\d+)(?::[0-9A-Za-z_-]+)?", stable_id
        )
        if match and int(match.group(1)) == chapter.index:
            index = int(match.group(2))
            if any(segment.index == index for segment in chapter.text_segments):
                return index
        raise KeyError(stable_id)

    def _parse_reader_validations(
        self, chapter: Chapter, response: Any, original_issues: list[dict]
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
                chapter, result, expected[finding_id]
            )
        if set(received) != set(expected):
            raise AlignmentError("reader validation omitted one or more finding IDs")
        return [received[str(issue["finding_id"])] for issue in original_issues]

    def _parse_reader_validation(
        self, chapter: Chapter, response: Any, original_issue: dict
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
            start = self._resolve_audit_id(
                chapter, id_map, response["repair_start_id"]
            )
            end = self._resolve_audit_id(chapter, id_map, response["repair_end_id"])
        except (KeyError, TypeError):
            raise AlignmentError("reader validation returned an unknown repair range")
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
        self, chapter: Chapter, start: int, end: int
    ) -> tuple[int, ...]:
        indexes = [segment.index for segment in chapter.text_segments]
        total = sum(len(chapter.segments[index].source) for index in indexes)
        if total <= self.config.window.max_read_chars:
            return tuple(indexes)
        positions = {index: position for position, index in enumerate(indexes)}
        left = positions[start]
        right = positions[end]
        chars = sum(len(chapter.segments[index].source) for index in indexes[left : right + 1])
        before = after = 0
        while True:
            changed = False
            if left > 0:
                length = len(chapter.segments[indexes[left - 1]].source)
                if before + length <= self.config.window.source_halo_chars and chars + length <= self.config.window.max_read_chars:
                    left -= 1
                    before += length
                    chars += length
                    changed = True
            if right + 1 < len(indexes):
                length = len(chapter.segments[indexes[right + 1]].source)
                if after + length <= self.config.window.source_halo_chars and chars + length <= self.config.window.max_read_chars:
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

    def _knowledge_for(
        self, store: RunStore, chapter: Chapter, read_source: str
    ) -> dict[str, Any]:
        """Combine confirmed knowledge with a bounded, past-only raw chapter tail."""
        visible = self.terminology.visible(chapter.index, read_source)
        remaining = self.config.window.past_context_chars
        tail: list[dict[str, Any]] = []
        manifest = store.load_manifest()
        status = {item["index"]: item.get("status") for item in manifest["chapters"]}
        for previous_index in range(chapter.index - 1, -1, -1):
            if remaining <= 0 or status.get(previous_index) != STATUS_DONE:
                break
            previous = store.load_chapter(previous_index)
            for segment in reversed(previous.text_segments):
                target = segment.target or ""
                cost = len(segment.source) + len(target)
                if tail and cost > remaining:
                    break
                tail.append(
                    {
                        "chapter": previous_index,
                        "segment": segment.index,
                        "source": segment.source,
                        "formal_target": target,
                    }
                )
                remaining -= cost
                if remaining <= 0:
                    break
        visible["past_only_raw_tail"] = list(reversed(tail))
        visible["evidence_priority"] = (
            "current_and_nearby_source > active hard terms > active preferred terms > "
            "past formal target"
        )
        return visible

    def _terminology_issues(
        self,
        chapter: Chapter,
        targets: dict[int, str],
        indexes: tuple[int, ...],
    ) -> list[dict[str, Any]]:
        issues: list[dict[str, Any]] = []
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
