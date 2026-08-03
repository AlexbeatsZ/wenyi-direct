"""Reader for Wenyi Direct's stable JSON interchange format."""

from __future__ import annotations

import json
from pathlib import Path

from .models import Chapter, Document, Segment


def read_json_document(path: str, source_lang: str, target_lang: str) -> Document:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    chapters: list[Chapter] = []
    for chapter_index, chapter_raw in enumerate(raw.get("chapters", [])):
        segments: list[Segment] = []
        for segment_index, segment_raw in enumerate(chapter_raw.get("segments", [])):
            if isinstance(segment_raw, str):
                segment_raw = {"source": segment_raw}
            segments.append(
                Segment(
                    index=segment_index,
                    source=str(segment_raw.get("source", "")),
                    kind=str(segment_raw.get("kind", "text")),
                    anchor=segment_raw.get("anchor"),
                    resource_href=segment_raw.get("resource_href"),
                    cont=bool(segment_raw.get("cont", False)),
                    meta=dict(segment_raw.get("meta", {}) or {}),
                )
            )
        chapters.append(
            Chapter(
                index=chapter_index,
                title=str(chapter_raw.get("title", "")),
                segments=segments,
                href=chapter_raw.get("href"),
                meta=dict(chapter_raw.get("meta", {}) or {}),
            )
        )
    if not chapters:
        raise ValueError("JSON input must contain at least one chapter")
    return Document(
        title=str(raw.get("title") or Path(path).stem),
        source_lang=str(raw.get("source_lang") or source_lang),
        target_lang=str(raw.get("target_lang") or target_lang),
        fmt="json",
        source_path=str(Path(path).resolve()),
        chapters=chapters,
        meta=dict(raw.get("meta", {}) or {}),
    )
