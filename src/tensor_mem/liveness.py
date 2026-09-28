"""Liveness intervals, alias classes and storage-conflict detection.

Execution is barrier-per-wave: a value occupies storage from before its
producer wave until every node in its last consumer wave has finished. Graph
outputs never die (the client holds an output handle). View ops
(reshape/transpose) *alias* an input: union-find merges them into one alias
class with one backing allocation whose lifetime is the union of the members.

Two records may share a pool slot only when their closed intervals are
strictly disjoint; anything else is an explicit, reason-tagged conflict.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .graph import Graph
from .tensor import DEFAULT_ALIGNMENT, TensorMeta, aligned_byte_size
from .errors import InputValidationError

PERSISTENT: Optional[int] = None  # last_wave sentinel: client-retained output


class _UnionFind:
    def __init__(self, items: list[str]) -> None:
        self.parent = {x: x for x in items}

    def find(self, x: str) -> str:
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != x:
            nxt = self.parent[x]
            self.parent[x] = root
            x = nxt
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            # deterministic root
            if ra < rb:
                self.parent[rb] = ra
            else:
                self.parent[ra] = rb


@dataclass(frozen=True)
class AllocRecord:
    """One physical allocation the planner must place: an alias class or a
    node workspace scratch buffer."""

    rid: str                      # unique record id, e.g. "val::t1", "ws::n3"
    kind: str                     # "value" | "workspace"
    bytes_required: int
    first_wave: int
    last_wave: Optional[int]      # PERSISTENT when retained by a client handle
    members: tuple[str, ...]      # tensor names merged by aliasing
    shape_repr: str
    dtype: str
    # External records (e.g. training parameters owned by TrainingState) are
    # not placed in the reusable pool: storage is supplied at execution time.
    # They still occupy RAM during covering waves and so still count at peak.
    external: bool = False

    @property
    def persistent(self) -> bool:
        return self.last_wave is PERSISTENT and self.kind == "value"

    def covers(self, wave: int) -> bool:
        if wave < self.first_wave:
            return False
        return self.last_wave is PERSISTENT or wave <= self.last_wave


@dataclass(frozen=True)
class Conflict:
    record_a: str
    record_b: str
    reason: str
    wave: int


@dataclass
class LivenessResult:
    records: list[AllocRecord]
    by_rid: dict[str, AllocRecord]
    alias_root: dict[str, str]
    wave_of_node: dict[str, int]
    waves: list[list[str]]
    # wave -> tensor names that are read/written during the wave (diagnostics)
    wave_live: dict[int, list[str]] = field(default_factory=dict)

    def conflict(self, a: AllocRecord, b: AllocRecord, graph: Graph) -> Conflict | None:
        return _classify_conflict(a, b, graph, self.wave_of_node)

    def all_pairwise_conflicts(self, graph: Graph) -> list[Conflict]:
        out: list[Conflict] = []
        for i, a in enumerate(self.records):
            for b in self.records[i + 1:]:
                c = _classify_conflict(a, b, graph, self.wave_of_node)
                if c is not None:
                    out.append(c)
        return out

    def validate_assignment(
        self, assignment: dict[str, str], graph: Graph
    ) -> list[Conflict]:
        """Check a rid -> slot-id map; every shared slot must be conflict-free."""
        slots: dict[str, list[str]] = {}
        for rid, slot in assignment.items():
            slots.setdefault(slot, []).append(rid)
        violations: list[Conflict] = []
        for rids in slots.values():
            for i, ra in enumerate(rids):
                for rb in rids[i + 1:]:
                    c = _classify_conflict(
                        self.by_rid[ra], self.by_rid[rb], graph, self.wave_of_node
                    )
                    if c is not None:
                        violations.append(c)
        return violations

    def live_bytes_per_wave(self) -> dict[int, int]:
        """Sum of required bytes of all records covering each wave."""
        out: dict[int, int] = {}
        for w in range(len(self.waves)):
            out[w] = sum(r.bytes_required for r in self.records if r.covers(w))
        return out


def analyze_liveness(
    graph: Graph,
    alignment: int = DEFAULT_ALIGNMENT,
    override_metas: dict[str, TensorMeta] | None = None,
    external_names: frozenset[str] | None = None,
) -> LivenessResult:
    """Derive allocation records (alias classes + workspaces) from the graph.

    ``override_metas`` replaces inferred metadata tensor-by-tensor; the executor
    uses it after propagating *concrete* feed shapes (dynamic shapes) without
    mutating the statically-planned graph. Alias/wave structure is identical;
    only sizes change.

    ``external_names`` are tensors whose storage is supplied from outside the
    reusable pool (training parameters owned by the state store). They are not
    placed in slots but their bytes still count toward the resident peak.
    """
    override_metas = override_metas or {}
    external_names = external_names or frozenset()
    for name in external_names:
        if name not in graph.producer:
            raise InputValidationError(
                "external tensor is not part of the graph", name=name
            )
        if graph.producer[name] is not None:
            raise InputValidationError(
                "only feeds can be external (state-owned) tensors", name=name
            )
    wave_of_node: dict[str, int] = {}
    for w, node_ids in enumerate(graph.waves):
        for nid in node_ids:
            wave_of_node[nid] = w

    all_names = list(graph.producer.keys())
    uf = _UnionFind(all_names)

    # Alias unions: view outputs share the storage of their aliased input.
    for nid in graph.order:
        node = graph.nodes[nid]
        for out_slot, in_slot in enumerate(node.spec.alias):
            if in_slot is not None:
                uf.union(node.outputs[out_slot], node.inputs[in_slot])

    # Birth wave per tensor name (feeds exist from wave 0).
    birth: dict[str, int] = {}
    for feed_name in graph.feeds:
        birth[feed_name] = 0
    for nid in graph.order:
        node = graph.nodes[nid]
        for out in node.outputs:
            birth[out] = wave_of_node[nid]

    # Last read wave per tensor name across all consumers.
    last_read: dict[str, int] = {}
    for nid in graph.order:
        node = graph.nodes[nid]
        w = wave_of_node[nid]
        for ref in node.inputs:
            last_read[ref] = max(last_read.get(ref, w), w)

    persistent_names = set(graph.graph_outputs)

    # Group tensor names by alias root.
    classes: dict[str, list[str]] = {}
    for name in all_names:
        classes.setdefault(uf.find(name), []).append(name)

    def meta_of(name: str) -> TensorMeta:
        if name in override_metas:
            return override_metas[name]
        producer = graph.producer[name]
        if producer is None:
            return graph.feeds[name]
        node = graph.nodes[producer]
        return node.out_meta[node.outputs.index(name)]

    records: list[AllocRecord] = []
    for root, members in sorted(classes.items()):
        member_set = set(members)
        is_external = bool(member_set & external_names)
        size = max(
            aligned_byte_size(meta_of(m).shape, meta_of(m).dtype, alignment)
            for m in members
        )
        first = min(birth[m] for m in members)
        reads = [last_read[m] for m in members if m in last_read]
        is_persistent = any(m in persistent_names for m in members)
        last = PERSISTENT if is_persistent else (max(reads) if reads else first)
        rep = meta_of(root)
        records.append(
            AllocRecord(
                rid=f"val::{root}",
                kind="value",
                bytes_required=size,
                first_wave=first,
                last_wave=last,
                members=tuple(sorted(members)),
                shape_repr=str(meta_of(root).shape),
                dtype=rep.dtype,
                external=is_external,
            )
        )

    # Workspace scratch is a per-node record alive exactly for its own wave.
    from .ops import op_workspace_bytes  # local import avoids a cycle

    for nid in graph.order:
        node = graph.nodes[nid]
        in_metas = [meta_of(ref) for ref in node.inputs]
        ws = op_workspace_bytes(
            node.spec,
            [m.tensor_type for m in in_metas],
            [m.shape for m in in_metas],
            node.attrs_dict,
            alignment,
        )
        if ws > 0:
            w = wave_of_node[nid]
            records.append(
                AllocRecord(
                    rid=f"ws::{nid}",
                    kind="workspace",
                    bytes_required=ws,
                    first_wave=w,
                    last_wave=w,
                    members=(nid,),
                    shape_repr=f"workspace:{ws}B",
                    dtype="byte",
                )
            )

    by_rid = {r.rid: r for r in records}

    wave_live: dict[int, list[str]] = {w: [] for w in range(len(graph.waves))}
    for r in records:
        for w in range(len(graph.waves)):
            if r.covers(w):
                wave_live[w].append(r.rid)

    return LivenessResult(
        records=records,
        by_rid=by_rid,
        alias_root={n: uf.find(n) for n in all_names},
        wave_of_node=wave_of_node,
        waves=graph.waves,
        wave_live=wave_live,
    )


def _intervals_overlap(a: AllocRecord, b: AllocRecord) -> bool:
    """Closed-wave overlap; persistent interval extends to +inf."""
    a_last = float("inf") if a.last_wave is PERSISTENT else a.last_wave
    b_last = float("inf") if b.last_wave is PERSISTENT else b.last_wave
    return a.first_wave <= b_last and b.first_wave <= a_last


def _classify_conflict(
    a: AllocRecord,
    b: AllocRecord,
    graph: Graph,
    wave_of_node: dict[str, int],
) -> Conflict | None:
    if a.rid == b.rid:
        return None
    if not _intervals_overlap(a, b):
        return None

    # Pin the reported wave to the earliest wave where they coexist.
    a_last = float("inf") if a.last_wave is PERSISTENT else a.last_wave
    b_last = float("inf") if b.last_wave is PERSISTENT else b.last_wave

    overlap_lo = max(a.first_wave, b.first_wave)
    overlap_hi_f = min(a_last, b_last)
    overlap_hi = overlap_lo if overlap_hi_f == float("inf") else int(overlap_hi_f)
    wave = overlap_lo  # earliest wave at which both coexist
    shared_waves = set(range(overlap_lo, overlap_hi + 1))

    # Both are node outputs born in the same wave from different nodes: the
    # canonical "parallel branches sharing a live buffer" case.
    if (
        a.kind == "value"
        and b.kind == "value"
        and a.first_wave == b.first_wave
    ):
        prod_a = graph.producer.get(a.members[0])
        prod_b = graph.producer.get(b.members[0])
        if (
            prod_a is not None
            and prod_b is not None
            and prod_a != prod_b
            and wave_of_node[prod_a] == wave_of_node[prod_b]
        ):
            return Conflict(a.rid, b.rid, "parallel_branch", wave)

    if any(
        len(graph.waves[w]) > 1 and w in shared_waves for w in range(len(graph.waves))
    ):
        return Conflict(a.rid, b.rid, "concurrent_wave", wave)

    if a.persistent or b.persistent:
        return Conflict(a.rid, b.rid, "retained_output_live", wave)

    return Conflict(a.rid, b.rid, "overlapping_lifetime", wave)
