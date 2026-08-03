"""Human-confirmed hard terms and visibility-bounded past facts."""

from __future__ import annotations

from pathlib import Path

import yaml


class KnowledgeBase:
    def __init__(self, data: dict | None = None) -> None:
        data = data or {}
        self.terms = list(data.get("terms", []) or [])
        self.facts = list(data.get("facts", []) or [])

    @classmethod
    def load(cls, path: str | None) -> "KnowledgeBase":
        if not path:
            return cls()
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return cls(raw)

    def visible(self, chapter: int, read_source: str) -> dict[str, list[dict]]:
        terms = []
        for term in self.terms:
            if int(term.get("from_chapter", 0)) > chapter:
                continue
            source = str(term.get("source", ""))
            target = str(term.get("target", ""))
            if source and target and source in read_source:
                terms.append({"source": source, "target": target, "note": term.get("note")})
        facts = [
            fact
            for fact in self.facts
            if int(fact.get("from_chapter", 0)) <= chapter
        ]
        return {"hard_terms": terms, "past_confirmed_facts": facts}
