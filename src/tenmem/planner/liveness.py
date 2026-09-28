"""Liveness intervals, alias unions and workspace intervals.

The unit of allocation is a *live-range group*: tensors connected by alias
edges are unioned (they must occupy one buffer for the hull of their joint
lifetime), then every remaining tensor gets its own group.

Interval semantics (wave coordinates)
-------------------------------------
* tensor produced by a node at wave ``w`` is born at ``w``.
* graph inputs/constants are born at wave ``0``.
* a tensor dies after the *greatest* consumer wave; a tensor used by two
  parallel branches therefore stays alive until the later branch finishes.
* graph outputs are pinned: death = ``PINNED_DEATH`` until the client releases
  them. Intervals are inclusive ``[birth, death]``.
* a node's workspace is live on ``[w, w]`` only.

Two groups are *compatible* (may share a buffer) iff their closed intervals do
not not overlap. Equality of endpoint counts as overlap: a buffer produced at
wave ``w`` cannot share with a buffer consumed at wave ``w`` by a node running
in parallel.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..graph import PINNED_DEATH, Graph, Schedule, produced_by
from ..ops import get_op
from ..tensor import align_up


@dataclass
class LiveGroup:
    gid: int
    members: list[str]
    birth: int
    death: int
    bytes_needed: int          # max aligned owner size
    alignment: int
    pinned: bool = False       # graph output: survives after the run
    workspace: bool = False    # scratch group (members are synthetic names)

    @property
    def interval(self) -> tuple[int, int]:
        return self.birth, self.death


class DSU:
    def __init__(self, names: list[str]) -> None:
        self.parent = {n: n for n in names}

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
            self.parent[rb] = ra


@dataclass
class WorkspaceInterval:
    node_id: str
    wave: int
    size: int
    alignment: int


@dataclass
class Liveness:
    groups: list[LiveGroup]
    tensor_group: dict[str, int]            # tensor name -> gid
    workspaces: list[WorkspaceInterval]
    tensor_interval: dict[str, tuple[int, int]]
    pinned_tensors: list[str]
    waves: tuple[tuple[str, ...], ...]

    def group_of(self, tensor: str) -> LiveGroup:
        return self.groups[self.tensor_group[tensor]]


def _all_specs(graph: Graph) -> dict[str, object]:
    specs: dict[str, object] = {s.name: s for s in (*graph.inputs, *graph.constants)}
    for node in graph.nodes:
        for out in node.outputs:
            specs[out.name] = out
    return specs


def analyze(graph: Graph, schedule: Schedule, alignment: int) -> Liveness:
    specs = _all_specs(graph)
    names = list(specs)

    # Consumers per tensor: (node id, wave, input index).
    consumers: dict[str, list[tuple[str, int, int]]] = {n: [] for n in names}
    for node in graph.nodes:
        w = schedule.node_wave[node.id]
        for idx, inp in enumerate(node.inputs):
            consumers[inp].append((node.id, w, idx))
    last_use: dict[str, int] = {}
    for name, uses in consumers.items():
        if uses:
            last_use[name] = max(w for _, w, _ in uses)

    # Alias unions (explicit) plus automatic last-consumer handoff unions.
    dsu = DSU(names)
    for node in graph.nodes:
        for out_name, in_name in node.aliases:
            dsu.union(out_name, in_name)
    _add_handoff_unions(graph, schedule, consumers, last_use, dsu)

    sets: dict[str, list[str]] = {}
    for n in names:
        sets.setdefault(dsu.find(n), []).append(n)

    pinned = set(graph.output_names)
    groups: list[LiveGroup] = []
    tensor_group: dict[str, int] = {}
    tensor_interval: dict[str, tuple[int, int]] = {}

    for root, members in sets.items():
        births, deaths = [], []
        is_pinned = False
        for m in members:
            b = schedule.tensor_birth[m]
            if m in pinned:
                d = PINNED_DEATH
                is_pinned = True
            elif produced_by(graph, m) is None and m not in last_use:
                d = 0  # unused input/constant: only resident at feed wave
            else:
                d = last_use.get(m, b)
            births.append(b)
            deaths.append(d)
            tensor_interval[m] = (b, d)
        birth, death = min(births), max(deaths)
        capacity = 0
        align = alignment
        for m in members:
            spec = specs[m]
            capacity = max(capacity, align_up(spec.max_bytes, alignment))
        gid = len(groups)
        groups.append(
            LiveGroup(
                gid=gid,
                members=sorted(members),
                birth=birth,
                death=death,
                bytes_needed=capacity,
                alignment=align,
                pinned=is_pinned,
            )
        )
        for m in members:
            tensor_group[m] = gid

    # Workspace intervals, sized from upper-bound shapes.
    workspaces: list[WorkspaceInterval] = []
    for node in graph.nodes:
        op = get_op(node.op)
        if op.workspace_fn is None:
            continue
        in_shapes = []
        for inp in node.inputs:
            spec = specs[inp]
            in_shapes.append(spec.max_shape)
        itemsize = node.outputs[0].itemsize if node.outputs else 4
        raw = int(op.workspace_fn(in_shapes, node.config, itemsize))
        size = align_up(raw, alignment)
        workspaces.append(
            WorkspaceInterval(node_id=node.id, wave=schedule.node_wave[node.id], size=size, alignment=alignment)
        )

    return Liveness(
        groups=groups,
        tensor_group=tensor_group,
        workspaces=workspaces,
        tensor_interval=tensor_interval,
        pinned_tensors=sorted(pinned),
        waves=schedule.waves,
    )


def overlaps(a: tuple[int, int], b: tuple[int, int]) -> bool:
    """Closed intervals: touching endpoints overlap (wave barriers)."""
    return a[0] <= b[1] and b[0] <= a[1]


def _add_handoff_unions(graph, schedule, consumers, last_use, dsu) -> None:
    """Union one last-consumer input with the node output on a safe handoff.

    When tensor ``t`` is consumed for the last time by node ``N`` at wave ``w``,
    the input buffer may pass directly to N's output *iff*:

    * ``t`` has exactly one consumer at its last-use wave (a sibling branch
      consuming ``t`` concurrently would race on the shared buffer);
    * the kernel declares that input position in-place safe;
    * neither ``t`` nor the output is a graph output (pinned buffers are never
      handed off — they must remain uniquely owned until client release);
    * input and output agree on dtype and upper-bound capacity.

    At most ONE input is handed off per node. Unioning two inputs with the same
    output would transitively merge the two inputs, which race when both are
    live concurrently (as in a join). One edge per node also makes the union
    graph a collection of chains — never a V-join — so transitive merging cannot
    alias two concurrently-live tensors.
    """
    pinned = set(graph.output_names)
    for node in graph.nodes:
        op = get_op(node.op)
        out_name = node.outputs[0].name
        if out_name in pinned:
            continue
        w = schedule.node_wave[node.id]
        out_spec = node.outputs[0]
        for idx, inp in enumerate(node.inputs):
            if idx not in op.inplace_safe or inp in pinned:
                continue
            uses = consumers.get(inp, [])
            if last_use.get(inp) != w:
                continue
            wave_users = [u for u in uses if u[1] == w]
            if len(wave_users) != 1:
                continue  # concurrent sibling consumers — must not alias
            in_spec = _spec_of(graph, None, inp)
            if in_spec.dtype != out_spec.dtype:
                continue
            if in_spec.max_bytes != out_spec.max_bytes:
                continue
            dsu.union(out_name, inp)
            break  # one handoff per node


def _spec_of(graph, node_by_id, name):
    for s in (*graph.inputs, *graph.constants):
        if s.name == name:
            return s
    producer = next(
        (n for n in graph.nodes if any(o.name == name for o in n.outputs)), None
    )
    return next(o for o in producer.outputs if o.name == name)
