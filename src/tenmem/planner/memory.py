"""Memory reuse planner.

Algorithm (greedy interval allocation with free-pool reuse)
-----------------------------------------------------------
1. Liveness analysis produces *live-range groups* (alias unions) with inclusive
   wave intervals ``[birth, death]`` and worst-case aligned capacities, plus
   per-node workspace intervals.
2. Events are processed in wave order. An occupant whose interval ends at wave
   ``w`` returns to the free pool only when wave ``w+1`` begins: a consumer at
   wave ``w`` and an independent producer at wave ``w`` may run concurrently,
   so closed intervals that merely touch still conflict.
3. At each birth the planner first takes an existing free buffer with
   sufficient capacity (best fit — smallest fit wins, reserving bigger buffers
   for later demand), otherwise creates a new one. Buffers holding pinned
   graph outputs never return to the pool.
4. Workspace is live exactly on the node's wave; two nodes in the same wave
   (parallel branches) therefore always receive distinct workspace buffers,
   while a later wave may reuse them.
5. Peak is the maximum sum of capacities of all buffers live during a wave;
   alignment padding and workspace are included. When peak exceeds
   ``max_bytes`` planning fails with ``resource_exhausted`` — capacity is
   decided at plan time, never discovered as an OOM mid-execution.

Every generated plan is then re-checked independently
(:func:`_verify_no_conflicts`) before it is returned.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..errors import PlanningError, ResourceExhaustedError
from ..graph import Graph, Schedule, validate_and_schedule
from ..tensor import DEFAULT_ALIGNMENT, Buffer, align_up as align_up_
from .liveness import LiveGroup, WorkspaceInterval, analyze, overlaps


@dataclass(frozen=True)
class Placement:
    kind: str                  # "tensor" | "workspace"
    name: str                  # tensor name, or "ws:<node id>"
    buffer_id: int
    birth: int
    death: int
    size: int
    pinned: bool = False
    group: str = ""            # alias-union key; same-group rows share by design


@dataclass(frozen=True)
class Plan:
    graph_name: str
    buffers: tuple[Buffer, ...]
    placements: tuple[Placement, ...]
    peak_bytes: int
    no_reuse_bytes: int
    alignment: int
    max_bytes: int | None
    waves: tuple[tuple[str, ...], ...]
    conflicts_checked: bool = True

    def buffer_id_for(self, name: str) -> int:
        for p in self.placements:
            if p.name == name:
                return p.buffer_id
        raise KeyError(name)

    def to_dict(self) -> dict:
        return {
            "graph_name": self.graph_name,
            "peak_bytes": self.peak_bytes,
            "no_reuse_bytes": self.no_reuse_bytes,
            "reuse_saved_bytes": self.no_reuse_bytes - self.peak_bytes,
            "alignment": self.alignment,
            "max_bytes": self.max_bytes,
            "buffers": [b.to_dict() for b in self.buffers],
            "placements": [
                {
                    "kind": p.kind,
                    "name": p.name,
                    "buffer_id": p.buffer_id,
                    "birth": p.birth,
                    "death": p.death if p.death < 10**8 else "pinned",
                    "size": p.size,
                    "pinned": p.pinned,
                }
                for p in self.placements
            ],
            "waves": [list(w) for w in self.waves],
            "conflicts_checked": self.conflicts_checked,
        }


@dataclass
class _Unit:
    birth: int
    death: int
    size: int
    kind: str                 # "tensor" | "workspace"
    name: str                 # tensor name or "ws:<node id>"
    pinned: bool = False
    members: tuple[str, ...] = ()  # tensor group members (owner metadata)
    group: str = ""


def plan_memory(
    graph: Graph,
    *,
    alignment: int = DEFAULT_ALIGNMENT,
    max_bytes: int | None = None,
) -> Plan:
    schedule = validate_and_schedule(graph)
    live = analyze(graph, schedule, alignment)

    units: list[_Unit] = []
    for g in live.groups:
        units.append(
            _Unit(
                birth=g.birth,
                death=g.death,
                size=g.bytes_needed,
                kind="tensor",
                name=g.members[0],
                pinned=g.pinned,
                members=tuple(g.members),
                group=f"g{g.gid}",
            )
        )
    for ws in live.workspaces:
        units.append(
            _Unit(
                birth=ws.wave,
                death=ws.wave,
                size=ws.size,
                kind="workspace",
                name=f"ws:{ws.node_id}",
            )
        )

    # No-reuse baseline: one fresh allocation per *tensor* (alias unions and
    # last-consumer handoffs are reuse optimizations and do not apply here)
    # plus one per workspace. Everything is retained, so total demand is the
    # simple sum. Matches the executor's execute_no_reuse reference.
    all_specs = {s.name: s for s in (*graph.inputs, *graph.constants)}
    for node in graph.nodes:
        for out in node.outputs:
            all_specs[out.name] = out
    no_reuse_bytes = sum(
        align_up_(s.max_bytes, alignment) for s in all_specs.values()
    ) + sum(u.size for u in units if u.kind == "workspace")

    # Larger and pinned units first inside a wave (hard-to-fit early).
    units.sort(key=lambda u: (u.birth, 0 if u.pinned else 1, -u.size, u.name))

    buffers: list[Buffer] = []
    owners: dict[int, list[str]] = {}
    placements: list[Placement] = []
    free_pool: list[int] = []             # buffer ids
    live_until: dict[int, int] = {}       # buffer id -> death wave

    def new_buffer(capacity: int, workspace: bool) -> int:
        bid = len(buffers)
        buffers.append(
            Buffer(id=bid, capacity=capacity, alignment=alignment, workspace=workspace)
        )
        owners[bid] = []
        return bid

    # Iterate only over real waves; pinned occupants simply never get reaped.
    wave_count = len(schedule.waves)
    cap = lambda bid: buffers[bid].capacity
    peak = 0

    for wave in range(wave_count):
        # Reap only occupants whose interval ended *strictly* before this wave.
        for bid in [b for b, d in live_until.items() if d < wave]:
            free_pool.append(bid)
            del live_until[bid]

        for u in units:
            if u.birth != wave:
                continue
            bid = None
            if not u.pinned:
                fits = sorted(
                    (b for b in free_pool if cap(b) >= u.size),
                    key=lambda b: (cap(b), b),
                )
                if fits:
                    bid = fits[0]
                    free_pool.remove(bid)
            if bid is None:
                bid = new_buffer(u.size, u.kind == "workspace")
            live_until[bid] = u.death

            if u.kind == "tensor":
                owners[bid].extend(u.members)
                # One placement per tensor name so the executor can resolve
                # every alias member to the shared buffer.
                for member in u.members:
                    placements.append(
                        Placement(
                            kind="tensor",
                            name=member,
                            buffer_id=bid,
                            birth=u.birth,
                            death=u.death,
                            size=u.size,
                            pinned=u.pinned,
                            group=u.group,
                        )
                    )
            else:
                owners[bid].append(u.name)
                placements.append(
                    Placement(
                        kind="workspace",
                        name=u.name,
                        buffer_id=bid,
                        birth=u.birth,
                        death=u.death,
                        size=u.size,
                    )
                )

        # Peak after all births; deaths inside this wave free only afterwards.
        peak = max(peak, sum(cap(b) for b in live_until))

    final_buffers = tuple(
        Buffer(
            id=b.id,
            capacity=b.capacity,
            alignment=b.alignment,
            owners=tuple(dict.fromkeys(owners[b.id])),
            workspace=b.workspace,
        )
        for b in buffers
    )
    plan = Plan(
        graph_name=graph.name,
        buffers=final_buffers,
        placements=tuple(placements),
        peak_bytes=peak,
        no_reuse_bytes=no_reuse_bytes,
        alignment=alignment,
        max_bytes=max_bytes,
        waves=schedule.waves,
    )
    _verify_no_conflicts(plan)
    if max_bytes is not None and peak > max_bytes:
        raise ResourceExhaustedError(
            f"peak demand {peak} B exceeds budget {max_bytes} B",
            details={"peak_bytes": peak, "max_bytes": max_bytes, "graph": graph.name},
        )
    return plan


def _verify_no_conflicts(plan: Plan) -> None:
    """Independent post-check: occupancies sharing a buffer must have disjoint
    closed intervals. Raises PlanningError on the first conflict."""
    by_buffer: dict[int, list[Placement]] = {}
    for p in plan.placements:
        by_buffer.setdefault(p.buffer_id, []).append(p)
    for bid, ps in by_buffer.items():
        ps.sort(key=lambda p: (p.birth, p.death, p.name))
        for a, b in zip(ps, ps[1:]):
            if a.group and a.group == b.group:
                continue  # alias union shares one buffer by construction
            if overlaps((a.birth, a.death), (b.birth, b.death)):
                raise PlanningError(
                    "buffer conflict detected by post-plan verification",
                    details={"buffer": bid, "a": a.name, "b": b.name},
                )
