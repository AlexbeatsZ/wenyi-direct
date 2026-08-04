"""Convert audit symptoms into precise write regions with separate read halos."""

from __future__ import annotations

from .types import RepairRegion


class RepairPlanner:
    def __init__(self, context_segments: int = 2) -> None:
        self.context_segments = max(0, context_segments)

    def plan(self, issues: list[dict], segment_count: int) -> list[RepairRegion]:
        if not issues or segment_count <= 0:
            return []
        raw_regions: list[RepairRegion] = []
        for issue in issues:
            symptom_start = int(issue.get("start", issue.get("segment", 0)))
            symptom_end = int(issue.get("end", symptom_start))
            cause_start = int(issue.get("cause_start", symptom_start))
            cause_end = int(issue.get("cause_end", symptom_end))
            start = max(0, min(symptom_start, cause_start))
            end = min(segment_count - 1, max(symptom_end, cause_end))
            raw_regions.append(RepairRegion(start, end, (issue,)))
        raw_regions.sort(key=lambda region: (region.start, region.end))
        merged: list[RepairRegion] = []
        for region in raw_regions:
            if not merged or region.start > merged[-1].end + 1:
                merged.append(region)
                continue
            previous = merged[-1]
            merged[-1] = RepairRegion(
                previous.start,
                max(previous.end, region.end),
                previous.issues + region.issues,
            )
        return merged

    def read_bounds(self, region: RepairRegion, segment_count: int) -> tuple[int, int]:
        """Expand context for reading without silently widening writable scope."""
        if segment_count <= 0:
            raise ValueError("segment_count must be positive")
        return (
            max(0, region.start - self.context_segments),
            min(segment_count - 1, region.end + self.context_segments),
        )
