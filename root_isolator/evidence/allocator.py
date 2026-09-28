"""Half-open global allocation of approximate roots to exact intervals.

Both numeric witnesses (mpmath and float64) must obey the SAME membership rule
the exact kernel uses: intervals are ``(a, b]``, they are pairwise disjoint,
and adjacent intervals produced by bisection can share an endpoint ``m`` — in
which case a root placed exactly at ``m`` belongs to the LEFT interval.

Naively testing ``a <= root <= b`` per interval double-counts such a root. This
allocator assigns every approximate root to at most one interval globally,
after clustering roots that are indistinguishable at the witness precision
(float64 splits a repeated companion eigenvalue into two nearby values).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class Cluster(Generic[T]):
    center: T
    members: tuple[T, ...]


def cluster_roots(roots: list[T], zero_tol: T) -> list[Cluster[T]]:
    """Merge sorted roots that are within ``zero_tol`` of each other.

    ``zero_tol`` is an ABSOLUTE tolerance chosen by the caller relative to its
    working precision. Distinct but genuinely close roots stay separate as long
    as their gap exceeds the witness precision.
    """

    if not roots:
        return []
    ordered = sorted(roots)
    clusters: list[list[T]] = [[ordered[0]]]
    for root in ordered[1:]:
        if root - clusters[-1][-1] <= zero_tol:
            clusters[-1].append(root)
        else:
            clusters.append([root])

    def mean(values: list[T]) -> T:
        total = values[0]
        for value in values[1:]:
            total = total + value
        return total / len(values)

    return [Cluster(center=mean(members), members=tuple(members)) for members in clusters]


@dataclass(frozen=True)
class Allocation(Generic[T]):
    """Result of assigning clustered roots to ordered disjoint intervals."""

    owner_of_cluster: tuple[int, ...]   # interval index per cluster, or -1
    clusters_per_interval: tuple[tuple[int, ...], ...]
    unassigned: tuple[int, ...]         # cluster indices with no legitimate owner


def assign_half_open(
    left: list[T],
    right: list[T],
    centers: list[T],
    endpoint_tol: T,
) -> Allocation[T]:
    """Assign each cluster center to the unique interval that legitimately owns it.

    Intervals are ordered and pairwise disjoint. Membership is the strict
    half-open rule ``a < c <= b``. When numerical noise pushes a center just
    outside, it is snapped to a boundary it could belong to:

    * within ``endpoint_tol`` of a right endpoint ``b_i`` -> interval ``i``;
    * within ``endpoint_tol`` of a shared left endpoint ``a_i`` (with
      ``b_{i-1} == a_i``) -> interval ``i-1``, because a root exactly at that
      boundary belongs to the interval that closes there;
    * a center landing in a genuine gap or far outside is left unassigned.
    """

    n = len(left)
    owner: list[int] = [-1] * len(centers)

    for cluster_index, c in enumerate(centers):
        # 1. Strict half-open membership (unique because intervals are disjoint).
        strict = [i for i in range(n) if left[i] < c <= right[i]]
        if len(strict) == 1:
            owner[cluster_index] = strict[0]
            continue

        # 2. Snap to the closest right endpoint (root exactly at b).
        best_right = min(
            range(n),
            key=lambda i: abs(c - right[i]),
            default=-1,
        )
        # 3. Snap to a shared left endpoint (root exactly at the previous b).
        best_left = min(
            (i for i in range(n) if i > 0 and right[i - 1] == left[i]),
            key=lambda i: abs(c - left[i]),
            default=-1,
        )

        right_dist = abs(c - right[best_right]) if best_right >= 0 else None
        left_dist = abs(c - left[best_left]) if best_left >= 0 else None

        if right_dist is not None and right_dist <= endpoint_tol and (
            left_dist is None or right_dist <= left_dist
        ):
            owner[cluster_index] = best_right
        elif left_dist is not None and left_dist <= endpoint_tol:
            owner[cluster_index] = best_left - 1
        # else: unassigned (lies in a root-free gap or far outside)

    grouped: list[list[int]] = [[] for _ in range(n)]
    unassigned: list[int] = []
    for cluster_index, interval_index in enumerate(owner):
        if interval_index >= 0:
            grouped[interval_index].append(cluster_index)
        else:
            unassigned.append(cluster_index)
    return Allocation(
        owner_of_cluster=tuple(owner),
        clusters_per_interval=tuple(tuple(g) for g in grouped),
        unassigned=tuple(unassigned),
    )
