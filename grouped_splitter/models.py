"""Result containers for the group-aware splitter.

All containers are immutable value objects that serialize cleanly to JSON.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class GroupReport:
    """One row per group: its canonical label, size and assigned split."""

    group_id: str
    label: str
    size: int
    split: str
    # Full class composition (a group may mix classes). ``label`` is the
    # dominant class and kept for convenience.
    class_counts: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class ClassDeviation:
    """Per-class distribution comparison against the global distribution."""

    label: str
    global_count: int
    global_proportion: float
    # Split name -> sample count / proportion of that class inside the split.
    split_counts: dict[str, int]
    split_proportions: dict[str, float]
    # max_s |split_proportion(s) - global_proportion|
    max_abs_deviation: float


@dataclass(frozen=True)
class SplitDiagnostics:
    """Measured deviations and structural reasons when targets cannot be met."""

    tolerance: float
    stratified_within_tolerance: bool
    counts_within_tolerance: bool
    max_class_proportion_deviation: float
    max_split_count_deviation: float
    # Human-readable, structural explanations for infeasibility.
    reasons: list[str] = field(default_factory=list)
    # Classes that have fewer groups than splits (rigorous necessary condition
    # for appearing in every split).
    rare_classes: list[str] = field(default_factory=list)
    # Groups whose atomic size alone is larger than the tolerance band.
    oversized_groups: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SplitResult:
    split_names: list[str]
    ratios: dict[str, float]
    seed: int
    n_samples: int
    n_groups: int
    split_sizes: dict[str, int]
    # Split name -> sample indices in the caller's input ordering.
    assignments: dict[str, list[int]]
    # Group id -> split name. Every group maps to exactly one split.
    group_assignments: dict[str, str]
    group_reports: list[GroupReport]
    class_deviations: list[ClassDeviation]
    diagnostics: SplitDiagnostics

    def groups_in_split(self, split: str) -> set[str]:
        return {
            gid
            for gid, assigned in self.group_assignments.items()
            if assigned == split
        }
