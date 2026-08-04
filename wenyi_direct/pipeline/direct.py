"""Public pipeline with validated terminology-revision extensions."""

from __future__ import annotations

import json
from typing import Any

from ..ingest.models import Chapter
from ..prompts import (
    factual_audit_messages,
    fidelity_validation_messages,
    term_migration_repair_messages,
    term_revision_validation_messages,
)
from .direct_core import AlignmentError, DirectPipeline as _CoreDirectPipeline, export_json
from .term_migration import (
    AmbiguousTermUse,
    TermMigrationService,
    TermRevision,
)
from .types import RepairRegion, TranslationWindow, segment_id


class DirectPipeline(_CoreDirectPipeline):
    """Core chapter pipeline plus independently validated whole-rule term revision."""

    @staticmethod
    def _term_revision_key(revision: TermRevision) -> str:
        return json.dumps(revision.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)

    def _parse_term_revision_proposals(
        self,
        chapter: Chapter,
        audit_batches: dict[str, Any],
    ) -> list[tuple[TermRevision, tuple[int, ...], dict[str, Any]]]:
        proposals: dict[str, tuple[TermRevision, tuple[int, ...], dict[str, Any]]] = {}
        target_by_rule: dict[tuple[str, str, int | None, int | None], str] = {}
        id_map = {
            segment_id(chapter.index, segment): segment.index
            for segment in chapter.text_segments
        }
        for batch in audit_batches.values():
            if not isinstance(batch, dict):
                continue
            read_indexes = tuple(int(index) for index in batch.get("read_indexes", []))
            visible = set(read_indexes)
            raw_revisions = batch.get("term_revisions", [])
            if not isinstance(raw_revisions, list):
                continue
            for raw in raw_revisions:
                if not isinstance(raw, dict):
                    continue
                if str(raw.get("scope", "")) != "entire_existing_rule":
                    continue
                revision = TermRevision(
                    source=str(raw.get("source", "")),
                    old_target=str(raw.get("old_target", "")),
                    new_target=str(raw.get("new_target", "")),
                    valid_from=raw.get("valid_from"),
                    valid_to=raw.get("valid_to"),
                    reason=str(raw.get("reason", "")),
                )
                evidence_ids = raw.get("evidence_ids", [])
                if not isinstance(evidence_ids, list) or not evidence_ids:
                    raise AlignmentError("term revision must provide evidence_ids")
                evidence_indexes: list[int] = []
                for value in evidence_ids:
                    try:
                        index = self._resolve_audit_id(chapter, id_map, value)
                    except KeyError as error:
                        raise AlignmentError(
                            f"term revision contains unknown evidence ID {value!r}"
                        ) from error
                    if index not in visible:
                        raise AlignmentError(
                            "term revision evidence escaped outside the visible read scope"
                        )
                    if revision.source not in chapter.segments[index].source:
                        raise AlignmentError(
                            f"term revision evidence {value!r} does not contain "
                            f"source term {revision.source!r}"
                        )
                    evidence_indexes.append(index)
                self.terminology.find_active_rule(
                    revision.source,
                    revision.old_target,
                    valid_from=revision.valid_from,
                    valid_to=revision.valid_to,
                )
                address = (
                    revision.source,
                    revision.old_target,
                    revision.valid_from,
                    revision.valid_to,
                )
                previous_target = target_by_rule.get(address)
                if previous_target is not None and previous_target != revision.new_target:
                    raise AlignmentError(
                        "factual audit proposed conflicting replacements for one term rule"
                    )
                target_by_rule[address] = revision.new_target
                proposal = {
                    **raw,
                    "evidence_ids": [
                        segment_id(chapter.index, chapter.segments[index])
                        for index in evidence_indexes
                    ],
                }
                proposals.setdefault(
                    self._term_revision_key(revision),
                    (revision, read_indexes, proposal),
                )
        return list(proposals.values())

    def _resolve_term_migration_use(
        self,
        store,
        use: AmbiguousTermUse,
        revision: TermRevision,
    ) -> str:
        chapter = store.load_chapter(use.chapter)
        targets = {
            segment.index: segment.target or "" for segment in chapter.segments
        }
        shadow = store.load_shadow(use.chapter)
        if isinstance(shadow, dict):
            shadow_targets = shadow.get("targets", {})
            if isinstance(shadow_targets, dict):
                for index, target in shadow_targets.items():
                    if isinstance(target, str) and target.strip():
                        targets[int(index)] = target
        targets[use.segment] = use.current_target
        read_indexes = self._read_scope_for_range(
            chapter,
            use.segment,
            use.segment,
            targets,
        )
        revision_payload = revision.model_dump(mode="json")
        messages = term_migration_repair_messages(
            chapter,
            revision_payload,
            read_indexes,
            use.segment,
            targets,
        )
        input_ref = store.save_translation_input(
            {
                "stage": "term_migration_repair",
                "messages": messages,
                "chapter": use.chapter,
                "segment": use.segment,
            }
        )
        response = self.clients["repair"].complete_json(
            messages,
            tier=self.config.pipeline.repair_tier,
            stage="term_migration_repair",
        )
        changes = self._parse_translations(chapter, (use.segment,), response)
        proposed = dict(targets)
        proposed.update(changes)
        read_source = "\n".join(
            chapter.segments[index].source for index in read_indexes
        )
        validation_messages = fidelity_validation_messages(
            chapter,
            proposed,
            read_indexes,
            (use.segment,),
            self._knowledge_for(store, chapter, read_source),
        )
        validation_ref = store.save_translation_input(
            {
                "stage": "term_migration_fidelity",
                "messages": validation_messages,
                "chapter": use.chapter,
                "segment": use.segment,
            }
        )
        validation = self.clients["validation"].complete_json(
            validation_messages,
            tier=self.config.pipeline.validation_tier,
            stage="term_migration_fidelity",
        )
        valid = isinstance(validation, dict) and validation.get("valid") is True
        issues = list(validation.get("issues", [])) if isinstance(validation, dict) else []
        store.record_audit(
            use.chapter,
            "term_migration_fidelity",
            {
                "input_ref": validation_ref,
                "repair_input_ref": input_ref,
                "segment": use.segment,
                "valid": valid,
                "issues": issues,
                "revision": revision_payload,
            },
        )
        if not valid:
            raise RuntimeError(
                f"term migration repair failed fidelity validation for chapter "
                f"{use.chapter} segment {use.segment}: {issues}"
            )
        return changes[use.segment]

    def _process_term_revisions(
        self,
        store,
        chapter: Chapter,
        shadow: dict[str, Any],
        state: dict[str, Any],
        audit_batches: dict[str, Any],
    ) -> list[TermRevision]:
        results = state.setdefault("term_revision_results", {})
        approved: list[TermRevision] = []
        for revision, read_indexes, proposal in self._parse_term_revision_proposals(
            chapter, audit_batches
        ):
            key = self._term_revision_key(revision)
            stored = results.get(key)
            if isinstance(stored, dict) and stored.get("status") in {
                "applied",
                "rejected",
            }:
                if stored.get("status") == "applied":
                    approved.append(revision)
                continue
            try:
                self.terminology.find_active_rule(
                    revision.source,
                    revision.old_target,
                    valid_from=revision.valid_from,
                    valid_to=revision.valid_to,
                )
            except ValueError:
                try:
                    self.terminology.find_active_rule(
                        revision.source,
                        revision.new_target,
                        valid_from=revision.valid_from,
                        valid_to=revision.valid_to,
                    )
                except ValueError:
                    raise
                results[key] = {
                    "status": "applied",
                    "recovered": True,
                    "proposal": proposal,
                }
                self._save_shadow(store, chapter.index, shadow)
                approved.append(revision)
                continue
            targets = self._targets(shadow)
            source = "\n".join(
                chapter.segments[index].source for index in read_indexes
            )
            messages = term_revision_validation_messages(
                chapter,
                proposal,
                read_indexes,
                targets,
                self._knowledge_for(store, chapter, source),
            )
            input_ref = store.save_translation_input(
                {"stage": "term_revision_validation", "messages": messages}
            )
            response = self.clients["validation"].complete_json(
                messages,
                tier=self.config.pipeline.validation_tier,
                stage="term_revision_validation",
            )
            is_approved = (
                isinstance(response, dict) and response.get("approved") is True
            )
            reason = str(response.get("reason", "")) if isinstance(response, dict) else ""
            store.record_audit(
                chapter.index,
                "term_revision_validation",
                {
                    "input_ref": input_ref,
                    "proposal": proposal,
                    "approved": is_approved,
                    "reason": reason,
                },
            )
            if not is_approved:
                results[key] = {
                    "status": "rejected",
                    "proposal": proposal,
                    "reason": reason,
                }
                self._save_shadow(store, chapter.index, shadow)
                continue
            resolution_cache: dict[tuple[int, int, str], str] = {}

            def resolver(use: AmbiguousTermUse, accepted: TermRevision) -> str:
                cache_key = (use.chapter, use.segment, use.current_target)
                if cache_key not in resolution_cache:
                    resolution_cache[cache_key] = self._resolve_term_migration_use(
                        store, use, accepted
                    )
                return resolution_cache[cache_key]

            migration = TermMigrationService(store, self.terminology).revise(
                revision,
                resolver=resolver,
            )
            refreshed = store.load_shadow(chapter.index)
            if isinstance(refreshed, dict):
                shadow.clear()
                shadow.update(refreshed)
                state = shadow.setdefault("factual_state", {})
                results = state.setdefault("term_revision_results", results)
            results[key] = {
                "status": "applied",
                "proposal": proposal,
                "reason": reason,
                "migration": migration.model_dump(mode="json"),
            }
            self._save_shadow(store, chapter.index, shadow)
            approved.append(revision)
        return approved

    @staticmethod
    def _issue_covered_by_revision(
        chapter: Chapter,
        issue: dict[str, Any],
        revisions: list[TermRevision],
    ) -> bool:
        if issue.get("type") != "term":
            return False
        start = int(issue.get("start", 0))
        end = int(issue.get("end", start))
        indexes = range(min(start, end), max(start, end) + 1)
        return any(
            revision.source in chapter.segments[index].source
            for revision in revisions
            for index in indexes
            if 0 <= index < len(chapter.segments)
        )

    def _factual_stage(
        self, store, chapter: Chapter, shadow: dict[str, Any]
    ) -> None:
        targets = self._targets(shadow)
        state = shadow.setdefault("factual_state", {})
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
        issues: list[dict] = []
        for batch_index, item in enumerate(plan):
            key = str(batch_index)
            if key in audit_batches:
                batch = audit_batches[key]
                issues.extend(list(batch.get("issues", [])))
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
        approved_revisions = self._process_term_revisions(
            store,
            chapter,
            shadow,
            state,
            audit_batches,
        )
        targets = self._targets(shadow)
        if approved_revisions:
            issues = [
                issue
                for issue in issues
                if not self._issue_covered_by_revision(
                    chapter, issue, approved_revisions
                )
            ]
        issues.extend(
            self._terminology_issues(
                chapter,
                targets,
                tuple(segment.index for segment in chapter.text_segments),
            )
        )
        state = shadow.setdefault("factual_state", state)
        stored_regions = state.get("repair_regions")
        if not isinstance(stored_regions, list):
            stored_regions = [
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
            state["repair_regions"] = stored_regions
            self._save_shadow(store, chapter.index, shadow)
        completed = set(state.setdefault("completed_repair_regions", []))
        for item in stored_regions:
            region_id = str(item["id"])
            if region_id in completed:
                continue
            region = RepairRegion(
                int(item["start"]),
                int(item["end"]),
                tuple(item.get("issues", [])),
            )
            targets = self._repair_and_validate(
                store, chapter, targets, region, "factual_repair"
            )
            self._save_targets(shadow, targets)
            completed.add(region_id)
            state["completed_repair_regions"] = sorted(completed)
            self._save_shadow(store, chapter.index, shadow)
        snapshots = shadow.setdefault("stage_snapshots", {})
        snapshots["factual"] = dict(shadow["targets"])
        shadow.pop("chinese_state", None)
        shadow["phase"] = (
            "chinese_audit"
            if self.config.pipeline.chinese_reader_audit
            else "promote"
        )
        self._save_shadow(store, chapter.index, shadow)
        store.set_chapter_fields(chapter.index, phase=shadow["phase"])


__all__ = ["AlignmentError", "DirectPipeline", "export_json"]
