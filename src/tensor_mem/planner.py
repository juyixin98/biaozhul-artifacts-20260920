"""Memory-reuse planner: pool placement, peak accounting, replanning.

Placement is best-fit with interval-based freeing: records are visited by
birth wave, and a slot is reusable only once *every* record previously placed
in it is dead before the new record's birth wave. Persistent (client-retained)
records pin their slot forever. Workspace scratch is placed like any record,
so alignment padding and scratch both show up in the peak.

The planner also proves its own output: every pair sharing a slot is checked
with the liveness conflict classifier.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .errors import ResourceExhaustedError
from .graph import Graph
from .liveness import (
    AllocRecord,
    Conflict,
    PERSISTENT,
    analyze_liveness,
)
from .tensor import DEFAULT_ALIGNMENT, TensorMeta


@dataclass(frozen=True)
class SlotInfo:
    slot_id: str
    capacity: int
    assigned: tuple[str, ...]  # rids in placement order


@dataclass(frozen=True)
class WaveStat:
    wave: int
    live_required: int     # sum of record requirements covering the wave
    live_capacity: int     # pool slot capacities occupied (real pool footprint)
    external_bytes: int    # externally-owned storage resident during the wave
    resident_bytes: int    # live_capacity + external_bytes (true RAM peak term)
    occupied_slots: tuple[str, ...]
    live_records: tuple[str, ...]


@dataclass
class Plan:
    graph_fingerprint: tuple[str, ...]  # ordered node ids the plan was built for
    alignment: int
    budget: Optional[int]
    assignment: dict[str, str]          # rid -> slot_id
    slots: dict[str, SlotInfo]
    records: dict[str, AllocRecord]
    peak_required: int                  # max over waves of sum(requirements incl. external)
    peak_capacity: int                  # max pool-only footprint (slot capacities)
    peak_resident: int                  # max over waves of pool + external bytes
    total_pool_bytes: int               # arena reservation: sum of all slot caps
    persistent_bytes: int               # retained pool outputs
    persistent_external_bytes: int      # retained external (state) storage
    no_reuse_total: int                 # naive: one allocation per pool record
    wave_stats: list[WaveStat]
    conflicts: list[Conflict]           # must be empty for a valid plan
    override_shapes: dict[str, tuple[int, ...]]
    external_names: tuple[str, ...]

    def slot_of(self, rid: str) -> str:
        return self.assignment[rid]

    def capacity_for(self, rid: str) -> int:
        return self.slots[self.assignment[rid]].capacity

    def assert_valid(self) -> None:
        if self.conflicts:
            c = self.conflicts[0]
            raise AssertionError(
                f"planner produced conflicting placement: {c.record_a} vs "
                f"{c.record_b} ({c.reason} @wave {c.wave})"
            )

    def summary(self) -> dict[str, object]:
        return {
            "slots": len(self.slots),
            "records": len(self.records),
            "peak_required_bytes": self.peak_required,
            "peak_pool_bytes": self.peak_capacity,
            "peak_resident_bytes": self.peak_resident,
            "persistent_pool_bytes": self.persistent_bytes,
            "persistent_external_bytes": self.persistent_external_bytes,
            "no_reuse_total_bytes": self.no_reuse_total,
            "budget": self.budget,
            "reuse_saved_bytes": self.no_reuse_total - self.peak_capacity,
            "external_names": list(self.external_names),
            "waves": [
                {
                    "wave": s.wave,
                    "live_required": s.live_required,
                    "live_pool": s.live_capacity,
                    "external": s.external_bytes,
                    "resident": s.resident_bytes,
                    "occupied_slots": list(s.occupied_slots),
                }
                for s in self.wave_stats
            ],
        }


def plan_graph(
    graph: Graph,
    budget: Optional[int] = None,
    alignment: int = DEFAULT_ALIGNMENT,
    override_metas: dict[str, TensorMeta] | None = None,
    external_names: frozenset[str] | None = None,
    allow_reuse: bool = True,
) -> Plan:
    """Compute a conflict-free reuse plan, enforcing ``budget`` on the peak."""
    liveness = analyze_liveness(graph, alignment, override_metas, external_names)
    records = sorted(
        liveness.records,
        key=lambda r: (r.first_wave, 0 if r.kind == "value" else 1, -r.bytes_required, r.rid),
    )

    # slot state: capacity, assigned rids, wave until which it is busy.
    # External records (state-owned parameter storage) are intentionally not
    # placed: their backing array is injected at execution time.
    slot_capacity: dict[str, int] = {}
    slot_busy_until: dict[str, int | float] = {}
    slot_assigned: dict[str, list[str]] = {}
    assignment: dict[str, str] = {}
    next_slot = 0

    for rec in records:
        if rec.external:
            continue
        # A slot is free iff its previous occupant dies strictly before this
        # record's birth wave. Persistent occupants keep busy_until == +inf.
        candidates = []
        if allow_reuse:
            candidates = [
                sid
                for sid, cap in slot_capacity.items()
                if slot_busy_until[sid] < rec.first_wave and cap >= rec.bytes_required
            ]
        if candidates:
            # best fit: smallest adequate capacity, deterministic id tiebreak
            candidates.sort(key=lambda sid: (slot_capacity[sid], sid))
            sid = candidates[0]
        else:
            sid = f"slot{next_slot}"
            next_slot += 1
            slot_capacity[sid] = rec.bytes_required
            slot_assigned[sid] = []
        assignment[rec.rid] = sid
        slot_assigned[sid].append(rec.rid)
        busy = float("inf") if rec.last_wave is PERSISTENT else rec.last_wave
        # A slot may hold several records over time; busy_until tracks the
        # latest expiry of whatever is currently assigned.
        slot_busy_until[sid] = max(slot_busy_until.get(sid, -1), busy)

    conflicts = liveness.validate_assignment(assignment, graph)

    slots = {
        sid: SlotInfo(
            slot_id=sid,
            capacity=slot_capacity[sid],
            assigned=tuple(slot_assigned[sid]),
        )
        for sid in sorted(slot_capacity)
    }

    # Per-wave footprint:
    #  * pool slots contribute full capacity when any assigned record is live;
    #  * external (state-owned) records contribute their bytes separately;
    #  * resident_bytes is the true RAM the wave needs.
    wave_stats: list[WaveStat] = []
    peak_required = 0
    peak_capacity = 0
    peak_resident = 0
    for w in range(len(graph.waves)):
        live_records = [r for r in liveness.records if r.covers(w)]
        pool_live = [r for r in live_records if not r.external]
        external_live = [r for r in live_records if r.external]
        occupied = {assignment[r.rid] for r in pool_live}
        live_required = sum(r.bytes_required for r in pool_live)
        live_capacity = sum(slot_capacity[sid] for sid in occupied)
        external_bytes = sum(r.bytes_required for r in external_live)
        resident = live_capacity + external_bytes
        peak_required = max(peak_required, live_required + external_bytes)
        peak_capacity = max(peak_capacity, live_capacity)
        peak_resident = max(peak_resident, resident)
        wave_stats.append(
            WaveStat(
                wave=w,
                live_required=live_required,
                live_capacity=live_capacity,
                external_bytes=external_bytes,
                resident_bytes=resident,
                occupied_slots=tuple(sorted(occupied)),
                live_records=tuple(sorted(r.rid for r in live_records)),
            )
        )

    persistent_bytes = sum(
        slot_capacity[assignment[r.rid]]
        for r in liveness.records
        if r.persistent and not r.external
    )
    persistent_external = sum(
        r.bytes_required for r in liveness.records if r.persistent and r.external
    )
    no_reuse_total = sum(
        r.bytes_required for r in liveness.records if not r.external
    )

    override_shapes = {
        name: m.shape.as_tuple() for name, m in (override_metas or {}).items()
    }

    plan = Plan(
        graph_fingerprint=tuple(graph.order),
        alignment=alignment,
        budget=budget,
        assignment=assignment,
        slots=slots,
        records={r.rid: r for r in liveness.records},
        peak_required=peak_required,
        peak_capacity=peak_capacity,
        peak_resident=peak_resident,
        total_pool_bytes=sum(slot_capacity.values()),
        persistent_bytes=persistent_bytes,
        persistent_external_bytes=persistent_external,
        no_reuse_total=no_reuse_total,
        wave_stats=wave_stats,
        conflicts=conflicts,
        override_shapes=override_shapes,
        external_names=tuple(sorted(external_names or ())),
    )
    plan.assert_valid()

    if budget is not None and peak_resident > budget:
        raise ResourceExhaustedError(
            "planned peak resident memory exceeds memory budget",
            peak_resident_bytes=peak_resident,
            peak_pool_bytes=peak_capacity,
            peak_external_bytes=peak_resident - peak_capacity,
            budget_bytes=budget,
            over_bytes=peak_resident - budget,
            wave_peak=max(wave_stats, key=lambda s: s.resident_bytes).wave,
        )
    return plan


def check_capacity(
    plan: Plan,
    graph: Graph,
    concrete_metas: dict[str, TensorMeta],
) -> list[tuple[str, int, int]]:
    """Compare concrete requirements with current slot capacities.

    Returns ``[(rid, required, capacity), ...]`` for every record whose
    concrete footprint exceeds its slot. An empty result means the existing
    plan safely covers the concrete shapes. A non-empty result is the
    capacity-deficit signal: those bindings must never be used out of range;
    the caller is expected to :func:`plan_graph` again with the same metas.
    """
    live = analyze_liveness(
        graph, plan.alignment, concrete_metas, frozenset(plan.external_names)
    )
    deficits: list[tuple[str, int, int]] = []
    for rec in live.records:
        if rec.external:
            continue  # state-owned storage is revalidated by TrainingState
        capacity = plan.slots[plan.assignment[rec.rid]].capacity
        if rec.bytes_required > capacity:
            deficits.append((rec.rid, rec.bytes_required, capacity))
    return deficits
