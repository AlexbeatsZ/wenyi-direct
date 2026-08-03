"""Minimal terminology groups, term lifecycle, lookup, and enforcement."""

from __future__ import annotations

import os
from collections import Counter
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

TermMode = Literal["hard", "preferred"]
TermStatus = Literal["active", "candidate", "rejected"]
Pronoun = Literal["他", "她", "它", "neutral"]


class TranslationGroup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_anchor: str
    target_anchor: str

    @model_validator(mode="after")
    def validate_group(self) -> "TranslationGroup":
        self.source_anchor = self.source_anchor.strip()
        self.target_anchor = self.target_anchor.strip()
        if not self.source_anchor or not self.target_anchor:
            raise ValueError("group anchors must be non-empty")
        return self


class TermRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str
    target: str
    group_id: str | None = None
    mode: TermMode = "hard"
    status: TermStatus = "active"
    valid_from: int | None = Field(default=None, ge=0)
    valid_to: int | None = Field(default=None, ge=0)
    pronoun: Pronoun | None = None

    @model_validator(mode="after")
    def validate_rule(self) -> "TermRule":
        self.source = self.source.strip()
        self.target = self.target.strip()
        if not self.source or not self.target:
            raise ValueError("term source and target must be non-empty")
        if (
            self.valid_from is not None
            and self.valid_to is not None
            and self.valid_from > self.valid_to
        ):
            raise ValueError("term valid_from cannot be greater than valid_to")
        return self

    def applies_to_chapter(self, chapter: int) -> bool:
        return (
            self.status == "active"
            and (self.valid_from is None or chapter >= self.valid_from)
            and (self.valid_to is None or chapter <= self.valid_to)
        )


class TerminologyDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    groups: dict[str, TranslationGroup] = Field(default_factory=dict)
    terms: list[TermRule] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_relations(self) -> "TerminologyDocument":
        for term in self.terms:
            if not term.group_id:
                continue
            group = self.groups.get(term.group_id)
            if group is None:
                raise ValueError(
                    f"term {term.source!r} references unknown group {term.group_id!r}"
                )
            if group.source_anchor not in term.source:
                raise ValueError(
                    f"term {term.source!r} does not contain group source_anchor "
                    f"{group.source_anchor!r}"
                )
            if group.target_anchor not in term.target:
                raise ValueError(
                    f"term target {term.target!r} does not contain group target_anchor "
                    f"{group.target_anchor!r}"
                )
        for index, left in enumerate(self.terms):
            if left.status != "active":
                continue
            for right in self.terms[index + 1 :]:
                if right.status != "active" or left.source != right.source:
                    continue
                if _ranges_overlap(left, right):
                    raise ValueError(
                        f"active rules for {left.source!r} have overlapping chapter ranges"
                    )
        return self


def _ranges_overlap(left: TermRule, right: TermRule) -> bool:
    left_start = left.valid_from if left.valid_from is not None else 0
    right_start = right.valid_from if right.valid_from is not None else 0
    left_end = left.valid_to if left.valid_to is not None else float("inf")
    right_end = right.valid_to if right.valid_to is not None else float("inf")
    return max(left_start, right_start) <= min(left_end, right_end)


def _normalise_legacy(raw: dict[str, Any]) -> dict[str, Any]:
    """Accept the project's first hard-term YAML without retaining obsolete fields."""
    normalised = {
        "groups": raw.get("groups", {}) or {},
        "terms": [],
    }
    for original in raw.get("terms", []) or []:
        term = dict(original)
        if "group" in term and "group_id" not in term:
            term["group_id"] = term.pop("group")
        if "from_chapter" in term and "valid_from" not in term:
            term["valid_from"] = term.pop("from_chapter")
        if "to_chapter" in term and "valid_to" not in term:
            term["valid_to"] = term.pop("to_chapter")
        confirmed = term.pop("confirmed", None)
        if "status" not in term and confirmed is False:
            term["status"] = "candidate"
        term.pop("note", None)
        normalised["terms"].append(term)
    return normalised


class TerminologyStore:
    def __init__(self, path: str | Path, document: TerminologyDocument | None = None) -> None:
        self.path = Path(path)
        self.document = document or TerminologyDocument()

    @classmethod
    def load(cls, path: str | Path) -> "TerminologyStore":
        target = Path(path)
        if not target.exists():
            return cls(target)
        raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
        return cls(target, TerminologyDocument.model_validate(_normalise_legacy(raw)))

    @property
    def groups(self) -> dict[str, TranslationGroup]:
        return self.document.groups

    @property
    def terms(self) -> list[TermRule]:
        return self.document.terms

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            yaml.safe_dump(
                self.document.model_dump(exclude_none=True),
                allow_unicode=True,
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        os.replace(temporary, self.path)

    def add_group(self, group_id: str, source_anchor: str, target_anchor: str) -> None:
        group_id = group_id.strip()
        if not group_id:
            raise ValueError("group_id must be non-empty")
        groups = dict(self.document.groups)
        groups[group_id] = TranslationGroup(
            source_anchor=source_anchor, target_anchor=target_anchor
        )
        self.document = TerminologyDocument(
            groups=groups, terms=self.terms
        )
        self.save()

    def add_term(self, term: TermRule) -> None:
        terms = [
            existing
            for existing in self.terms
            if not (
                existing.source == term.source
                and existing.valid_from == term.valid_from
                and existing.valid_to == term.valid_to
            )
        ]
        terms.append(term)
        self.document = TerminologyDocument(
            groups=self.groups, terms=terms
        )
        self.save()

    def set_status(
        self, source: str, status: TermStatus, *, target: str | None = None
    ) -> int:
        changed = 0
        terms: list[TermRule] = []
        for term in self.terms:
            matches = term.source == source and (target is None or term.target == target)
            if matches and term.status != status:
                term = term.model_copy(update={"status": status})
                changed += 1
            terms.append(term)
        if changed:
            self.document = TerminologyDocument(
                groups=self.groups, terms=terms
            )
            self.save()
        return changed

    def visible(self, chapter: int, read_source: str) -> dict[str, Any]:
        matches = self._selected_matches(chapter, read_source)
        hard: list[dict[str, Any]] = []
        preferred: list[dict[str, Any]] = []
        for term, _count in matches:
            item: dict[str, Any] = {
                "source": term.source,
                "target": term.target,
            }
            if term.group_id:
                group = self.groups[term.group_id]
                item["group"] = {
                    "source_anchor": group.source_anchor,
                    "target_anchor": group.target_anchor,
                }
            if term.pronoun:
                item["pronoun"] = term.pronoun
            (hard if term.mode == "hard" else preferred).append(item)
        return {
            "hard_terms": hard,
            "preferred_terms": preferred,
        }

    def hard_violations(
        self, chapter: int, source: str, target: str
    ) -> list[dict[str, Any]]:
        violations: list[dict[str, Any]] = []
        for term, count in self._selected_matches(chapter, source):
            if term.mode != "hard":
                continue
            actual = target.count(term.target)
            if actual < count:
                violations.append(
                    {
                        "source": term.source,
                        "required_target": term.target,
                        "required_count": count,
                        "actual_count": actual,
                    }
                )
        return violations

    def _selected_matches(self, chapter: int, text: str) -> list[tuple[TermRule, int]]:
        candidates: list[tuple[int, int, TermRule]] = []
        for term in self.terms:
            if not term.applies_to_chapter(chapter):
                continue
            start = text.find(term.source)
            while start >= 0:
                candidates.append((start, start + len(term.source), term))
                start = text.find(term.source, start + 1)
        candidates.sort(key=lambda item: (-(item[1] - item[0]), item[0]))
        occupied: list[tuple[int, int]] = []
        selected: list[TermRule] = []
        for start, end, term in candidates:
            if any(start < used_end and end > used_start for used_start, used_end in occupied):
                continue
            occupied.append((start, end))
            selected.append(term)
        counts = Counter(
            (term.source, term.target, term.group_id, term.mode, term.pronoun)
            for term in selected
        )
        first_by_key = {
            (term.source, term.target, term.group_id, term.mode, term.pronoun): term
            for term in selected
        }
        return [(first_by_key[key], count) for key, count in counts.items()]

    def add_discoveries(
        self,
        chapter: int,
        discoveries: list[dict[str, Any]],
        source_text: str,
        target_text: str,
    ) -> list[TermRule]:
        """Activate non-conflicting discoveries as soft hints; retain conflicts as candidates."""
        added: list[TermRule] = []
        terms = list(self.terms)
        for discovery in discoveries:
            source = str(discovery.get("source", "")).strip()
            target = str(discovery.get("target", "")).strip()
            if not source or not target or source not in source_text or target not in target_text:
                continue
            if any(term.source == source and term.target == target for term in terms):
                continue
            has_conflict = any(
                term.source == source
                and term.target != target
                and term.status == "active"
                and term.applies_to_chapter(chapter)
                for term in terms
            )
            rule = TermRule(
                source=source,
                target=target,
                mode="preferred",
                status="candidate" if has_conflict else "active",
                valid_from=chapter,
            )
            terms.append(rule)
            added.append(rule)
        if added:
            self.document = TerminologyDocument(
                groups=self.groups, terms=terms
            )
            self.save()
        return added
