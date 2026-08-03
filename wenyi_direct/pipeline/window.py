"""Plan broad read scopes independently from narrow write scopes."""

from __future__ import annotations

from ..config import WindowConfig
from ..ingest.models import Chapter
from .types import TranslationWindow


class WindowPlanner:
    def __init__(self, config: WindowConfig) -> None:
        self.config = config

    def plan(self, chapter: Chapter) -> list[TranslationWindow]:
        indexes = [segment.index for segment in chapter.text_segments]
        if not indexes:
            return []
        lengths = {segment.index: len(segment.source) for segment in chapter.text_segments}
        total = sum(lengths.values())
        write_groups: list[list[int]] = []
        current: list[int] = []
        current_chars = 0
        for index in indexes:
            length = lengths[index]
            if current and current_chars + length > self.config.max_write_chars:
                write_groups.append(current)
                current = []
                current_chars = 0
            current.append(index)
            current_chars += length
        if current:
            write_groups.append(current)

        windows: list[TranslationWindow] = []
        for write in write_groups:
            if total <= self.config.max_read_chars:
                read = indexes
            else:
                read = self._halo(indexes, lengths, write)
            windows.append(TranslationWindow(tuple(read), tuple(write)))
        return windows

    def _halo(
        self, indexes: list[int], lengths: dict[int, int], write: list[int]
    ) -> list[int]:
        positions = {index: position for position, index in enumerate(indexes)}
        left = positions[write[0]]
        right = positions[write[-1]]
        selected_chars = sum(lengths[index] for index in write)
        before_chars = 0
        after_chars = 0
        while True:
            changed = False
            if left > 0:
                candidate = indexes[left - 1]
                length = lengths[candidate]
                if (
                    before_chars + length <= self.config.source_halo_chars
                    and selected_chars + length <= self.config.max_read_chars
                ):
                    left -= 1
                    before_chars += length
                    selected_chars += length
                    changed = True
            if right + 1 < len(indexes):
                candidate = indexes[right + 1]
                length = lengths[candidate]
                if (
                    after_chars + length <= self.config.source_halo_chars
                    and selected_chars + length <= self.config.max_read_chars
                ):
                    right += 1
                    after_chars += length
                    selected_chars += length
                    changed = True
            if not changed:
                break
        return indexes[left : right + 1]


def split_write_scope(window: TranslationWindow) -> tuple[TranslationWindow, TranslationWindow]:
    """Shrink only the output scope while preserving the same source evidence."""
    if len(window.write_indexes) < 2:
        raise ValueError("cannot split a one-segment write scope")
    middle = len(window.write_indexes) // 2
    return (
        TranslationWindow(window.read_indexes, window.write_indexes[:middle]),
        TranslationWindow(window.read_indexes, window.write_indexes[middle:]),
    )
