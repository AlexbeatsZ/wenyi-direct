"""Public pipeline with bounded post-repair language rechecks."""

from __future__ import annotations

from typing import Any

from ..ingest.models import Chapter
from ..prompts import (
    chinese_finding_validation_messages,
    chinese_reader_messages,
)
from .direct_revision import (
    AlignmentError,
    export_json,
)
from .direct_revision import (
    DirectPipeline as _RevisionDirectPipeline,
)
from .term_migration import TermRevision


class DirectPipeline(_RevisionDirectPipeline):
    """Revision-aware pipeline plus one bounded Chinese recheck and strict context."""

    @staticmethod
    def _issue_covered_by_revision(
        chapter: Chapter,
        issue: dict[str, Any],
        revisions: list[TermRevision],
    ) -> bool:
        """Discard only a term issue explicitly tied to the migrated source rule."""
        del chapter
        if issue.get("type") != "term":
            return False
        term_source = str(issue.get("term_source", "")).strip()
        return bool(term_source) and any(
            term_source == revision.source for revision in revisions
        )

    def _knowledge_for(
        self, store, chapter: Chapter, read_source: str
    ) -> dict[str, Any]:
        knowledge = super()._knowledge_for(store, chapter, read_source)
        tail = knowledge.get("past_only_raw_tail", [])
        if not isinstance(tail, list):
            return knowledge
        remaining = self.config.window.past_context_chars
        selected: list[dict[str, Any]] = []

        def suffix(text: str, count: int) -> str:
            return text[-count:] if count > 0 else ""

        for raw in reversed(tail):
            if remaining <= 0 or not isinstance(raw, dict):
                break
            source = str(raw.get("source", ""))
            target = str(raw.get("formal_target", ""))
            cost = len(source) + len(target)
            if cost <= remaining:
                selected.append(dict(raw))
                remaining -= cost
                continue
            if cost == 0:
                continue
            source_budget = round(remaining * len(source) / cost)
            source_budget = max(0, min(source_budget, len(source), remaining))
            target_budget = min(len(target), remaining - source_budget)
            unused = remaining - source_budget - target_budget
            if unused > 0:
                add_source = min(unused, len(source) - source_budget)
                source_budget += add_source
                unused -= add_source
            if unused > 0:
                target_budget += min(unused, len(target) - target_budget)
            truncated = {
                **raw,
                "source": suffix(source, source_budget),
                "formal_target": suffix(target, target_budget),
                "truncated": True,
            }
            if truncated["source"] or truncated["formal_target"]:
                selected.append(truncated)
            remaining = 0
            break
        knowledge["past_only_raw_tail"] = list(reversed(selected))
        return knowledge

    def _chinese_stage(
        self, store, chapter: Chapter, shadow: dict[str, Any]
    ) -> None:
        super()._chinese_stage(store, chapter, shadow)
        if self.config.pipeline.max_language_rechecks <= 0:
            return
        state = shadow.setdefault("chinese_state", {})
        regions = state.get("repair_regions", [])
        if not isinstance(regions, list):
            return
        completed_repairs = set(state.get("completed_repair_regions", []))
        completed_rechecks = set(state.setdefault("completed_language_rechecks", []))
        pending = [
            item
            for item in regions
            if isinstance(item, dict)
            and str(item.get("id", "")) in completed_repairs
            and str(item.get("id", "")) not in completed_rechecks
        ]
        if not pending:
            return

        # Keep resume in the audit phase until every bounded recheck is persisted.
        shadow["phase"] = "chinese_audit"
        self._save_shadow(store, chapter.index, shadow)
        store.set_chapter_fields(chapter.index, phase="chinese_audit")
        targets = self._targets(shadow)
        batches = state.setdefault("language_recheck_batches", {})

        for item in pending:
            region_id = str(item["id"])
            start = int(item["start"])
            end = int(item["end"])
            write_indexes = tuple(
                index
                for index in range(start, end + 1)
                if 0 <= index < len(chapter.segments)
                and chapter.segments[index].source.strip()
            )
            if not write_indexes:
                completed_rechecks.add(region_id)
                state["completed_language_rechecks"] = sorted(completed_rechecks)
                self._save_shadow(store, chapter.index, shadow)
                continue
            read_indexes = self._read_scope_for_range(
                chapter,
                write_indexes[0],
                write_indexes[-1],
                targets,
            )
            batch = batches.setdefault(
                region_id,
                {
                    "read_indexes": list(read_indexes),
                    "write_indexes": list(write_indexes),
                },
            )
            issues = batch.get("issues")
            if not isinstance(issues, list):
                messages = chinese_reader_messages(
                    chapter,
                    targets,
                    read_indexes,
                    write_indexes,
                )
                input_ref = store.save_translation_input(
                    {"stage": "language_recheck", "messages": messages}
                )
                response = self.clients["chinese_audit"].complete_json(
                    messages,
                    tier=self.config.pipeline.audit_tier,
                    stage="language_recheck",
                )
                issues = self._parse_issues(
                    chapter,
                    response,
                    set(write_indexes),
                    set(read_indexes),
                )
                batch["issues"] = issues
                batch["reader_input_ref"] = input_ref
                store.record_audit(
                    chapter.index,
                    "language_recheck",
                    {
                        "input_ref": input_ref,
                        "repair_region": region_id,
                        "issues": issues,
                    },
                )
                self._save_shadow(store, chapter.index, shadow)
            validations = batch.get("validations")
            if issues and not isinstance(validations, list):
                tagged = [
                    {
                        **issue,
                        "finding_id": f"lr-{region_id}-f{index}",
                    }
                    for index, issue in enumerate(issues)
                ]
                source = "\n".join(
                    chapter.segments[index].source for index in read_indexes
                )
                messages = chinese_finding_validation_messages(
                    chapter,
                    targets,
                    tagged,
                    read_indexes,
                    self._knowledge_for(store, chapter, source),
                )
                input_ref = store.save_translation_input(
                    {
                        "stage": "language_recheck_validation",
                        "messages": messages,
                    }
                )
                response = self.clients["validation"].complete_json(
                    messages,
                    tier=self.config.pipeline.validation_tier,
                    stage="language_recheck_validation",
                )
                validations = self._parse_reader_validations(
                    chapter,
                    response,
                    tagged,
                    set(read_indexes),
                )
                batch["validations"] = validations
                batch["validation_input_ref"] = input_ref
                store.record_audit(
                    chapter.index,
                    "language_recheck_validation",
                    {
                        "input_ref": input_ref,
                        "repair_region": region_id,
                        "reader_issues": tagged,
                        "results": validations,
                    },
                )
                self._save_shadow(store, chapter.index, shadow)
            accepted = [
                result
                for result in (validations or [])
                if isinstance(result, dict)
                and result.get("safe_to_repair") is True
            ]
            for repair_region in self.repair_planner.plan(
                accepted, len(chapter.segments)
            ):
                repair_write = tuple(
                    index
                    for index in repair_region.indexes
                    if chapter.segments[index].source.strip()
                )
                if not repair_write:
                    continue
                repair_read = self._read_scope_for_range(
                    chapter,
                    repair_write[0],
                    repair_write[-1],
                    targets,
                )
                targets = self._repair_and_validate(
                    store,
                    chapter,
                    targets,
                    repair_region,
                    "language_recheck",
                    read_indexes=repair_read,
                    write_indexes=repair_write,
                )
                self._save_targets(shadow, targets)
                self._save_shadow(store, chapter.index, shadow)
            completed_rechecks.add(region_id)
            state["completed_language_rechecks"] = sorted(completed_rechecks)
            self._save_shadow(store, chapter.index, shadow)

        shadow["phase"] = "promote"
        self._save_shadow(store, chapter.index, shadow)
        store.set_chapter_fields(chapter.index, phase="promote")

    def _promote(
        self, store, chapter: Chapter, shadow: dict[str, Any]
    ) -> None:
        super()._promote(store, chapter, shadow)
        # Accessing the book-local store activates only discovered candidates whose
        # evidence survived all gates into Formal text.
        self.terminology.terms


__all__ = ["AlignmentError", "DirectPipeline", "export_json"]
