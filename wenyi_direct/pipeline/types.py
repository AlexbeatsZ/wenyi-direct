"""Stable IDs and pipeline value objects."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from ..ingest.models import Chapter, Segment


def segment_id(chapter_index: int, segment: Segment) -> str:
    digest = hashlib.sha256(segment.source.encode("utf-8")).hexdigest()[:12]
    return f"ch{chapter_index}:s{segment.index}:{digest}"


def chapter_source_digest(chapter: Chapter) -> str:
    joined = "\0".join(segment.source for segment in chapter.segments)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class TranslationWindow:
    read_indexes: tuple[int, ...]
    write_indexes: tuple[int, ...]


@dataclass(frozen=True)
class RepairRegion:
    start: int
    end: int
    issues: tuple[dict, ...]

    @property
    def indexes(self) -> tuple[int, ...]:
        return tuple(range(self.start, self.end + 1))
