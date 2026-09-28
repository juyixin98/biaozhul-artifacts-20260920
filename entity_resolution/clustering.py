"""Mining core: constraint validation + constrained clustering.

Hard rules
----------
1. Pairwise similarity is **not transitive**. Candidate edges are soft weights
   fed to a global objective; nothing is merged just because a path exists.
2. ``must-link`` / ``cannot-link`` are hard. They are checked for contradiction
   *before* anything runs (a cannot-link whose endpoints are already forced
   together by a must-link chain, or by the same locked cluster, is rejected).
3. Locked clusters are atomic blocks.

Solving
-------
For small instances the solver enumerates **every** restricted-growth partition
consistent with the constraints and returns the objective optimum (ties broken
by a canonical encoding) - this is the reference-grade path the test-suite
cross-checks with an independently written oracle. When the Bell-number budget
is exceeded the solver either raises :class:`ResourceExhaustedError`
(``mode="exact"``) or falls back to a deterministic agglomerative correlation
clustering heuristic (``mode="auto"``). The heuristic still optimizes the same
global objective under the same hard constraints; it never does threshold
connected components.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from .errors import (
    ConstraintConflictError,
    InvalidRequestError,
    ResourceExhaustedError,
)

# A pair is always normalized lexicographically so (a,b) == (b,a).
Pair = tuple[str, str]
Block = frozenset[str]


def canon_pair(a: str, b: str) -> Pair:
    if a == b:
        raise InvalidRequestError(
            "a constraint cannot reference a record against itself",
            {"pair": (a, b), "reason": "self_link"},
        )
    return (a, b) if a < b else (b, a)


# ---------------------------------------------------------------------------
# Union-find (must-link closure)
# ---------------------------------------------------------------------------


class _DSU:
    def __init__(self, members: Iterable[str]) -> None:
        self.parent = {m: m for m in members}

    def find(self, x: str) -> str:
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            # Lexicographic root keeps structure deterministic.
            if ra > rb:
                ra, rb = rb, ra
            self.parent[rb] = ra


@dataclass(frozen=True)
class ConstraintSet:
    must: frozenset[Pair]
    cannot: frozenset[Pair]
    records: frozenset[str]


@dataclass(frozen=True)
class SolveResult:
    clusters: list[frozenset[str]]
    must: frozenset[Pair]
    cannot: frozenset[Pair]
    method: str                 # "exact" | "greedy"
    partitions_evaluated: int
    optimal: bool


def validate_constraints(
    records: Sequence[str],
    must: Iterable[Pair] = (),
    cannot: Iterable[Pair] = (),
    locked_blocks: Sequence[Block] = (),
) -> ConstraintSet:
    """Validate and normalize a constraint problem.

    Raises :class:`ConstraintConflictError` with ``details.pair`` and
    ``details.reason`` for the first contradiction, distinguishing:

    * unknown record,
    * self link,
    * duplicate contradictory kind,
    * cannot-link whose endpoints are joined by must-link closure,
    * cannot-link inside one locked block,
    * must-link spanning two different locked blocks.
    """
    known = set(records)
    must_set: set[Pair] = set()
    cannot_set: set[Pair] = set()

    def check_endpoints(pair: Pair) -> Pair:
        p = canon_pair(*pair)
        for endpoint in p:
            if endpoint not in known:
                raise InvalidRequestError(
                    "constraint references unknown record",
                    {"pair": p, "reason": "unknown_record", "record": endpoint},
                )
        return p

    for pair in must:
        p = check_endpoints(pair)
        if p in cannot_set:
            raise ConstraintConflictError(
                "pair declared both must-link and cannot-link",
                {"pair": p, "reason": "direct_contradiction"},
            )
        must_set.add(p)
    for pair in cannot:
        p = check_endpoints(pair)
        if p in must_set:
            raise ConstraintConflictError(
                "pair declared both cannot-link and must-link",
                {"pair": p, "reason": "direct_contradiction"},
            )
        cannot_set.add(p)

    # Locked blocks behave as pre-existing must-link cliques.
    block_of: dict[str, int] = {}
    for idx, block in enumerate(locked_blocks):
        frozen = frozenset(block)
        for member in frozen:
            if member not in known:
                raise InvalidRequestError(
                    "locked cluster contains unknown record",
                    {"reason": "unknown_record", "record": member},
                )
            if member in block_of:
                raise ConstraintConflictError(
                    "record belongs to two locked clusters",
                    {"reason": "record_double_locked", "record": member},
                )
            block_of[member] = idx

    dsu = _DSU(known)
    for block in locked_blocks:
        members = list(block)
        for other in members[1:]:
            dsu.union(members[0], other)
    for a, b in must_set:
        dsu.union(a, b)

    for a, b in cannot_set:
        if dsu.find(a) == dsu.find(b):
            reason = (
                "inside_locked_cluster"
                if a in block_of
                and b in block_of
                and block_of[a] == block_of[b]
                else "must_link_closure"
            )
            raise ConstraintConflictError(
                "cannot-link endpoints are forced together",
                {
                    "pair": (a, b),
                    "reason": reason,
                    "must_component": sorted(
                        x for x in known if dsu.find(x) == dsu.find(a)
                    ),
                },
            )

    for a, b in must_set:
        if a in block_of and b in block_of and block_of[a] != block_of[b]:
            raise ConstraintConflictError(
                "must-link joins two separately locked clusters",
                {
                    "pair": (a, b),
                    "reason": "cross_locked_must",
                    "blocks": [block_of[a], block_of[b]],
                },
            )

    return ConstraintSet(
        must=frozenset(must_set),
        cannot=frozenset(cannot_set),
        records=frozenset(known),
    )


def initial_blocks(
    records: Sequence[str],
    constraints: ConstraintSet,
    locked_blocks: Sequence[Block] = (),
) -> list[frozenset[str]]:
    """Collapse must-links and locked clusters into atomic blocks.

    Free records start as singleton blocks. Must-links union blocks. A
    must-link touching a locked block pulls the other endpoint into that block;
    contradiction with a second locked block was already rejected.
    """
    dsu = _DSU(records)
    for block in locked_blocks:
        members = list(block)
        for other in members[1:]:
            dsu.union(members[0], other)
    for a, b in constraints.must:
        dsu.union(a, b)

    groups: dict[str, set[str]] = {}
    for record in records:
        groups.setdefault(dsu.find(record), set()).add(record)
    # Deterministic block order by smallest member id.
    return [frozenset(parts) for parts in sorted(groups.values(), key=min)]


def _bell_number(n: int, stop_above: int) -> int:
    """Bell number via the Stirling recurrence, short-circuiting a cap."""
    if n == 0:
        return 1
    row = [1]  # S(n, k) for current n, starting at n=1 -> [1]
    for size in range(2, n + 1):
        nxt = [0] * size
        nxt[0] = row[0]
        for k in range(1, size):
            left = row[k - 1]
            up = row[k] if k < len(row) else 0
            nxt[k] = left + (k + 1) * up
        row = nxt
        if sum(row) > stop_above:
            return stop_above + 1
    return sum(row)


@dataclass
class SolverConfig:
    mode: str = "auto"            # "auto" | "exact"
    max_exact_partitions: int = 50_000
    # Weights at/below this between *different* blocks are ignored by greedy.
    greedy_min_gain: float = 1e-9


def _block_pair_index(
    blocks: Sequence[Block],
) -> tuple[dict[str, int], list[list[set[str]]]]:
    """Return member->block map and, per block pair, the cannot pairs seen."""
    idx = {member: i for i, block in enumerate(blocks) for member in block}
    cannot_between: list[list[set[Pair]]] = [
        [set() for _ in blocks] for _ in blocks
    ]
    return idx, cannot_between


def _block_weight_matrix(
    blocks: Sequence[Block],
    weights: dict[Pair, float],
    cannot: frozenset[Pair],
) -> tuple[list[list[float]], list[list[int]], list[list[set[Pair]]], dict[str, int]]:
    """Aggregate pair weights, pair counts and cannot-links onto block pairs.

    The correlation-clustering cost for a pair with similarity ``w`` is
    ``1-w`` when co-clustered and ``w`` when separated. The merge gain is
    therefore ``w - (1-w) = 2w-1`` per pair, which needs both the summed
    weight *and* the number of record pairs between two clusters.
    """
    member_block, cannot_between = _block_pair_index(blocks)
    n = len(blocks)
    w = [[0.0] * n for _ in range(n)]
    count = [[0] * n for _ in range(n)]
    for (a, b), value in weights.items():
        i, j = member_block[a], member_block[b]
        if i == j:
            continue
        if i > j:
            i, j = j, i
        w[i][j] += value
        count[i][j] += 1
    for a, b in cannot:
        i, j = member_block[a], member_block[b]
        if i == j:
            # Should have been rejected by validation; defensive guard.
            raise ConstraintConflictError(
                "cannot-link collapsed into a single must/locked block",
                {"pair": (a, b), "reason": "post_validation_contradiction"},
            )
        if i > j:
            i, j = j, i
        cannot_between[i][j].add((a, b))
    return w, count, cannot_between, member_block


def _objective(
    assignment: Sequence[int],
    blocks: Sequence[Block],
    weights: dict[Pair, float],
    cannot: frozenset[Pair],
) -> float:
    """Correlation-clustering cost for a block->label assignment.

    within-cluster cost  = sum (1 - w);  across-cluster cost = sum w.
    Cannot pairs sharing a label are +inf.
    """
    member_block, _ = _block_pair_index(blocks)
    cost = 0.0
    for (a, b), value in weights.items():
        same = assignment[member_block[a]] == assignment[member_block[b]]
        cost += (1.0 - value) if same else value
    for a, b in cannot:
        if assignment[member_block[a]] == assignment[member_block[b]]:
            return float("inf")
    return cost


def _enumerate_best(
    blocks: Sequence[Block],
    weights: dict[Pair, float],
    cannot: frozenset[Pair],
    budget: int,
) -> tuple[list[frozenset[str]], int]:
    """Enumerate restricted-growth labelings, pruned by cannot-links.

    Returns the optimal clusters and the number of valid partitions evaluated.
    Raises :class:`ResourceExhaustedError` if the Bell bound exceeds ``budget``.
    """
    n = len(blocks)
    if _bell_number(n, budget) > budget:
        raise ResourceExhaustedError(
            "exact partition enumeration exceeds the partition budget",
            {"blocks": n, "budget": budget, "bell_bound": _bell_number(n, budget)},
        )

    # cannot as block-index pairs
    block_of = {m: i for i, b in enumerate(blocks) for m in b}
    forbidden: set[Pair] = set()
    for a, b in cannot:
        i, j = sorted((block_of[a], block_of[b]))
        forbidden.add((i, j))

    labels = [0] * n
    best_cost = float("inf")
    best_labels: tuple[int, ...] = ()
    evaluated = 0

    def neighbors_differ_ok(pos: int, label: int) -> bool:
        for k in range(pos):
            pair = (k, pos) if k < pos else (pos, k)
            if pair in forbidden and labels[k] == label:
                return False
        return True

    def recurse(pos: int, max_label: int) -> None:
        nonlocal best_cost, best_labels, evaluated
        if pos == n:
            cost = _objective(labels, blocks, weights, cannot)
            evaluated += 1
            if cost < best_cost or (
                cost == best_cost and tuple(labels) < best_labels
            ):
                best_cost = cost
                best_labels = tuple(labels)
            return
        for label in range(max_label + 2):  # 0..max_label+1 (new group allowed)
            if not neighbors_differ_ok(pos, label):
                continue
            labels[pos] = label
            recurse(pos + 1, max(max_label, label))

    recurse(0, -1)

    if not best_labels:
        # validate_constraints guarantees feasibility; this is a defensive net.
        raise ConstraintConflictError(
            "no feasible partition exists under the given constraints",
            {"reason": "unsat_constraints"},
        )

    grouped: dict[int, set[str]] = {}
    for block_index, label in enumerate(best_labels):
        grouped.setdefault(label, set()).update(blocks[block_index])
    return [frozenset(parts) for parts in sorted(grouped.values(), key=min)], evaluated


def _greedy_solve(
    blocks: Sequence[Block],
    weights: dict[Pair, float],
    cannot: frozenset[Pair],
    min_gain: float,
) -> list[frozenset[str]]:
    """Deterministic agglomerative minimization of the correlation objective.

    Merging clusters i,j changes cost by ``sum_pairs (w - (1-w))`` over all
    record pairs spanning them, i.e. ``2 * sumW - |Ci|*|Cj|`` (unscored pairs
    count as w=0). A merge happens only while this is strictly positive and no
    cannot-link crosses the two clusters. Ties resolve to the smallest
    (i, j). This is a global heuristic, never a threshold union-find: weak
    bridges are refused because the pair-count penalty exceeds their weight.
    """
    clusters: list[set[str]] = [set(b) for b in blocks]
    _, _, cannot_between, _ = _block_weight_matrix(blocks, weights, cannot)
    n = len(clusters)
    sizes = [len(c) for c in clusters]
    active = list(range(n))

    # Summed cross-cluster pair weights and crossing cannot-pair sets.
    member_block = {m: i for i, b in enumerate(blocks) for m in b}
    cw = [[0.0] * n for _ in range(n)]
    cc = [[set() for _ in range(n)] for _ in range(n)]
    for (a, b), value in weights.items():
        i, j = member_block[a], member_block[b]
        if i == j:
            continue
        lo, hi = (i, j) if i < j else (j, i)
        cw[lo][hi] += value
    for a, b in cannot:
        i, j = member_block[a], member_block[b]
        lo, hi = (i, j) if i < j else (j, i)
        cc[lo][hi].add((a, b))

    while True:
        # Maximum-gain merge; ties resolve to smallest (lo, hi).
        best: tuple[float, int, int] | None = None
        for ii in range(len(active)):
            for jj in range(ii + 1, len(active)):
                i, j = active[ii], active[jj]
                lo, hi = (i, j) if i < j else (j, i)
                if cc[lo][hi]:
                    continue  # cannot-link forbids merge
                gain = 2.0 * cw[lo][hi] - sizes[lo] * sizes[hi]
                if gain <= min_gain:
                    continue
                cand = (-gain, lo, hi)
                if best is None or cand < best:
                    best = cand
        if best is None:
            break
        _, lo, hi = best
        # Merge hi into lo.
        clusters[lo].update(clusters[hi])
        clusters[hi].clear()
        sizes[lo] += sizes[hi]
        sizes[hi] = 0
        for k in active:
            if k in (lo, hi):
                continue
            a, b = (lo, k) if lo < k else (k, lo)
            x, y = (hi, k) if hi < k else (k, hi)
            cw[a][b] += cw[x][y]
            cc[a][b] |= cc[x][y]
            cw[x][y] = 0.0
            cc[x][y].clear()
        active.remove(hi)

    return [frozenset(c) for c in clusters if c]


def solve(
    records: Sequence[str],
    weights: dict[Pair, float],
    must: Iterable[Pair] = (),
    cannot: Iterable[Pair] = (),
    locked_blocks: Sequence[Block] = (),
    config: SolverConfig | None = None,
) -> SolveResult:
    """Solve a constrained correlation-clustering problem.

    Parameters
    ----------
    records:
        All record ids in the corpus.
    weights:
        Soft evidence ``pair -> w in [0, 1]`` (similarity). Pairs absent are
        treated as carrying no evidence. Must/cannot need not appear here.
    must / cannot:
        Hard constraints (validated first).
    locked_blocks:
        Atomic record groups (human-confirmed clusters).
    """
    cfg = config or SolverConfig()
    constraints = validate_constraints(records, must, cannot, locked_blocks)
    blocks = initial_blocks(records, constraints, locked_blocks)

    # Two *separately* locked clusters can never be merged by soft evidence.
    # Locking each one asserts they are distinct entities, so add implicit
    # cannot-links across every member pair belonging to different locks.
    # validate_constraints already rejected any explicit must-link that would
    # contradict this.
    locked_list = [frozenset(b) for b in locked_blocks]
    enforced_cannot = set(constraints.cannot)
    for i in range(len(locked_list)):
        for j in range(i + 1, len(locked_list)):
            for a in locked_list[i]:
                for b in locked_list[j]:
                    enforced_cannot.add(canon_pair(a, b))
    enforced_cannot = frozenset(enforced_cannot)

    # Weights internal to a block are irrelevant; matrix handles aggregation.
    if cfg.mode == "exact" or (
        cfg.mode == "auto" and _bell_number(len(blocks), cfg.max_exact_partitions)
        <= cfg.max_exact_partitions
    ):
        clusters, evaluated = _enumerate_best(
            blocks, weights, enforced_cannot, cfg.max_exact_partitions
        )
        return SolveResult(
            clusters=clusters,
            must=constraints.must,
            cannot=constraints.cannot,
            method="exact",
            partitions_evaluated=evaluated,
            optimal=True,
        )

    clusters = _greedy_solve(
        blocks, weights, enforced_cannot, cfg.greedy_min_gain
    )
    return SolveResult(
        clusters=clusters,
        must=constraints.must,
        cannot=constraints.cannot,
        method="greedy",
        partitions_evaluated=0,
        optimal=False,
    )
