"""Immediate, source-anchored migration after a terminology rule is revised."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .knowledge import TermRule, TerminologyStore
from .runstore import STATUS_DONE, RunStore

StorageKind = Literal["formal", "shadow"]


class TermRevision(BaseModel):
    """A confirmed replacement of one complete active terminology rule."""

    model_config = ConfigDict(extra="forbid")

    source: str
    old_target: str
    new_target: str
    valid_from: int | None = Field(default=None, ge=0)
    valid_to: int | None = Field(default=None, ge=0)
    reason: str = ""

    @model_validator(mode="after")
    def validate_revision(self) -> "TermRevision":
        self.source = self.source.strip()
        self.old_target = self.old_target.strip()
        self.new_target = self.new_target.strip()
        self.reason = self.reason.strip()
        if not self.source or not self.old_target or not self.new_target:
            raise ValueError("term revision source and targets must be non-empty")
        if self.old_target == self.new_target:
            raise ValueError("term revision must change the target")
        if (
            self.valid_from is not None
            and self.valid_to is not None
            and self.valid_from > self.valid_to
        ):
            raise ValueError("valid_from cannot exceed valid_to")
        return self


class MigrationEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chapter: int
    segment: int
    storage: StorageKind
    before: str
    after: str


class AmbiguousTermUse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chapter: int
    segment: int
    storage: StorageKind
    source_text: str
    current_target: str
    source_count: int
    old_target_count: int
    new_target_count: int


class TermMigrationPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    migration_id: str
    revision: TermRevision
    rule: TermRule
    safe_edits: list[MigrationEdit] = Field(default_factory=list)
    ambiguous_uses: list[AmbiguousTermUse] = Field(default_factory=list)
    invalidated_snapshots: dict[int, list[str]] = Field(default_factory=dict)


class TermMigrationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    migration_id: str
    replacement_rule: TermRule
    applied_edits: int
    model_resolved_edits: int
    invalidated_snapshots: dict[int, list[str]]


class TermMigrationNeedsReview(RuntimeError):
    def __init__(self, plan: TermMigrationPlan) -> None:
        self.plan = plan
        super().__init__(
            f"term migration {plan.migration_id} has "
            f"{len(plan.ambiguous_uses)} ambiguous translated occurrence(s)"
        )


Resolver = Callable[[AmbiguousTermUse, TermRevision], str]


class TermMigrationService:
    """Plan and atomically finish a whole-rule migration across Formal and Shadow."""

    def __init__(self, store: RunStore, terminology: TerminologyStore) -> None:
        self.store = store
        self.terminology = terminology
        self.migrations_dir = Path(store.run_dir) / "term_migrations"

    @staticmethod
    def _in_range(chapter: int, rule: TermRule) -> bool:
        return (
            (rule.valid_from is None or chapter >= rule.valid_from)
            and (rule.valid_to is None or chapter <= rule.valid_to)
        )

    @staticmethod
    def _rewrite_if_safe(
        target: str,
        *,
        source_count: int,
        old_target: str,
        new_target: str,
    ) -> str | None:
        """Return a deterministic replacement, an unchanged value, or None if ambiguous."""
        old_count = target.count(old_target)
        new_count = target.count(new_target)
        if old_count == 0 and new_count >= source_count:
            return target
        if source_count == 1 and old_count == 1:
            return target.replace(old_target, new_target, 1)
        return None

    def _write_plan_state(
        self,
        plan: TermMigrationPlan,
        *,
        status: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        self.migrations_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "status": status,
            "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "plan": plan.model_dump(mode="json"),
            **(extra or {}),
        }
        path = self.migrations_dir / f"{plan.migration_id}.json"
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(path)

    def plan(self, revision: TermRevision) -> TermMigrationPlan:
        rule = self.terminology.find_active_rule(
            revision.source,
            revision.old_target,
            valid_from=revision.valid_from,
            valid_to=revision.valid_to,
        )
        migration_id = f"tm-{uuid4().hex[:12]}"
        safe_edits: list[MigrationEdit] = []
        ambiguous: list[AmbiguousTermUse] = []
        invalidated: dict[int, list[str]] = {}
        manifest = self.store.load_manifest()
        statuses = {
            int(item["index"]): item.get("status")
            for item in manifest.get("chapters", [])
            if isinstance(item, dict) and "index" in item
        }
        for chapter_index in sorted(statuses):
            if not self._in_range(chapter_index, rule):
                continue
            chapter = self.store.load_chapter(chapter_index)
            shadow = self.store.load_shadow(chapter_index)
            shadow_targets = shadow.get("targets", {}) if isinstance(shadow, dict) else {}
            snapshots = (
                shadow.get("stage_snapshots", {}) if isinstance(shadow, dict) else {}
            )
            for segment in chapter.text_segments:
                source_count = self.terminology.selected_source_count(
                    chapter_index,
                    segment.source,
                    revision.source,
                    target=revision.old_target,
                )
                if source_count <= 0:
                    continue
                live: list[tuple[StorageKind, str]] = []
                if statuses[chapter_index] == STATUS_DONE and (segment.target or "").strip():
                    live.append(("formal", segment.target or ""))
                shadow_target = shadow_targets.get(str(segment.index))
                if isinstance(shadow_target, str) and shadow_target.strip():
                    live.append(("shadow", shadow_target))
                seen_live: set[tuple[StorageKind, str]] = set()
                for storage, target in live:
                    marker = (storage, target)
                    if marker in seen_live:
                        continue
                    seen_live.add(marker)
                    rewritten = self._rewrite_if_safe(
                        target,
                        source_count=source_count,
                        old_target=revision.old_target,
                        new_target=revision.new_target,
                    )
                    if rewritten is None:
                        ambiguous.append(
                            AmbiguousTermUse(
                                chapter=chapter_index,
                                segment=segment.index,
                                storage=storage,
                                source_text=segment.source,
                                current_target=target,
                                source_count=source_count,
                                old_target_count=target.count(revision.old_target),
                                new_target_count=target.count(revision.new_target),
                            )
                        )
                    elif rewritten != target:
                        safe_edits.append(
                            MigrationEdit(
                                chapter=chapter_index,
                                segment=segment.index,
                                storage=storage,
                                before=target,
                                after=rewritten,
                            )
                        )
                if not isinstance(snapshots, dict):
                    continue
                for snapshot_name, values in snapshots.items():
                    if not isinstance(values, dict):
                        continue
                    target = values.get(str(segment.index))
                    if not isinstance(target, str) or not target.strip():
                        continue
                    rewritten = self._rewrite_if_safe(
                        target,
                        source_count=source_count,
                        old_target=revision.old_target,
                        new_target=revision.new_target,
                    )
                    if rewritten is None:
                        invalidated.setdefault(chapter_index, []).append(str(snapshot_name))
                    elif rewritten != target:
                        # Snapshot edits are represented by a private storage label in state,
                        # not as live MigrationEdit rows exposed to a model resolver.
                        safe_edits.append(
                            MigrationEdit(
                                chapter=chapter_index,
                                segment=segment.index,
                                storage="shadow",
                                before=f"snapshot:{snapshot_name}\n{target}",
                                after=f"snapshot:{snapshot_name}\n{rewritten}",
                            )
                        )
        plan = TermMigrationPlan(
            migration_id=migration_id,
            revision=revision,
            rule=rule,
            safe_edits=safe_edits,
            ambiguous_uses=ambiguous,
            invalidated_snapshots={
                chapter: sorted(set(names)) for chapter, names in invalidated.items()
            },
        )
        self._write_plan_state(plan, status="planned")
        return plan

    @staticmethod
    def _decode_snapshot_edit(edit: MigrationEdit) -> tuple[str, str, str] | None:
        if edit.storage != "shadow" or not edit.before.startswith("snapshot:"):
            return None
        before_header, before_target = edit.before.split("\n", 1)
        after_header, after_target = edit.after.split("\n", 1)
        if before_header != after_header:
            raise ValueError("snapshot migration headers do not match")
        return before_header.removeprefix("snapshot:"), before_target, after_target

    @staticmethod
    def _validate_resolution(
        use: AmbiguousTermUse, revision: TermRevision, target: str
    ) -> str:
        target = target.strip()
        if not target:
            raise ValueError("term migration resolver returned empty target")
        if target.count(revision.new_target) < use.source_count:
            raise ValueError(
                f"resolved target for chapter {use.chapter} segment {use.segment} "
                f"does not contain {revision.new_target!r} enough times"
            )
        return target

    def apply(
        self,
        plan: TermMigrationPlan,
        *,
        resolver: Resolver | None = None,
    ) -> TermMigrationResult:
        if plan.ambiguous_uses and resolver is None:
            self._write_plan_state(plan, status="needs_review")
            raise TermMigrationNeedsReview(plan)
        resolved_edits: list[MigrationEdit] = []
        for use in plan.ambiguous_uses:
            assert resolver is not None
            target = self._validate_resolution(
                use,
                plan.revision,
                resolver(use, plan.revision),
            )
            resolved_edits.append(
                MigrationEdit(
                    chapter=use.chapter,
                    segment=use.segment,
                    storage=use.storage,
                    before=use.current_target,
                    after=target,
                )
            )
        all_edits = [*plan.safe_edits, *resolved_edits]
        by_chapter: dict[int, list[MigrationEdit]] = {}
        for edit in all_edits:
            by_chapter.setdefault(edit.chapter, []).append(edit)
        touched = sorted(set(by_chapter) | set(plan.invalidated_snapshots))
        for chapter_index in touched:
            chapter = self.store.load_chapter(chapter_index)
            shadow = self.store.load_shadow(chapter_index)
            shadow_changed = False
            formal_changes: dict[int, tuple[str, str]] = {}
            shadow_changes: dict[int, tuple[str, str]] = {}
            for edit in by_chapter.get(chapter_index, []):
                snapshot_edit = self._decode_snapshot_edit(edit)
                if snapshot_edit is not None:
                    if not isinstance(shadow, dict):
                        continue
                    snapshot_name, before_target, after_target = snapshot_edit
                    snapshots = shadow.get("stage_snapshots", {})
                    values = snapshots.get(snapshot_name) if isinstance(snapshots, dict) else None
                    if isinstance(values, dict) and values.get(str(edit.segment)) == before_target:
                        values[str(edit.segment)] = after_target
                        shadow_changed = True
                    continue
                if edit.storage == "formal":
                    segment = chapter.segments[edit.segment]
                    current = segment.target or ""
                    if current == edit.before:
                        segment.target = edit.after
                        formal_changes[edit.segment] = (edit.before, edit.after)
                    elif current != edit.after:
                        raise RuntimeError(
                            f"Formal text changed while applying term migration: "
                            f"chapter {chapter_index} segment {edit.segment}"
                        )
                else:
                    if not isinstance(shadow, dict):
                        continue
                    targets = shadow.get("targets", {})
                    current = targets.get(str(edit.segment)) if isinstance(targets, dict) else None
                    if current == edit.before:
                        targets[str(edit.segment)] = edit.after
                        shadow_changes[edit.segment] = (edit.before, edit.after)
                        shadow_changed = True
                    elif current != edit.after:
                        raise RuntimeError(
                            f"Shadow text changed while applying term migration: "
                            f"chapter {chapter_index} segment {edit.segment}"
                        )
            if isinstance(shadow, dict):
                snapshots = shadow.get("stage_snapshots", {})
                if isinstance(snapshots, dict):
                    for snapshot_name in plan.invalidated_snapshots.get(chapter_index, []):
                        if snapshot_name in snapshots:
                            snapshots.pop(snapshot_name, None)
                            shadow_changed = True
                if shadow_changed:
                    self.store.save_shadow(chapter_index, shadow)
            if formal_changes:
                self.store.save_chapter(chapter)
            for storage, changes in (("formal", formal_changes), ("shadow", shadow_changes)):
                for segment_index, (before, after) in changes.items():
                    self.store.record_translation_stage(
                        "term_migration",
                        chapter=chapter_index,
                        start_index=segment_index,
                        sources=[chapter.segments[segment_index].source],
                        targets=[after],
                        previous_targets=[before],
                        metadata={
                            "migration_id": plan.migration_id,
                            "storage": storage,
                            "source_term": plan.revision.source,
                            "old_target": plan.revision.old_target,
                            "new_target": plan.revision.new_target,
                        },
                    )
        replacement = self.terminology.replace_rule(plan.rule, plan.revision.new_target)
        result = TermMigrationResult(
            migration_id=plan.migration_id,
            replacement_rule=replacement,
            applied_edits=len(all_edits),
            model_resolved_edits=len(resolved_edits),
            invalidated_snapshots=plan.invalidated_snapshots,
        )
        self._write_plan_state(
            plan,
            status="done",
            extra={"result": result.model_dump(mode="json")},
        )
        self.store.log_event(
            "term_migration_completed",
            migration_id=plan.migration_id,
            source=plan.revision.source,
            old_target=plan.revision.old_target,
            new_target=plan.revision.new_target,
            applied_edits=len(all_edits),
            model_resolved_edits=len(resolved_edits),
            invalidated_snapshots=plan.invalidated_snapshots,
        )
        return result

    def revise(
        self,
        revision: TermRevision,
        *,
        resolver: Resolver | None = None,
    ) -> TermMigrationResult:
        return self.apply(self.plan(revision), resolver=resolver)
