"""Static memory planning: liveness, slice-view aliasing, buffer reuse.

Model
-----
* Every **root tensor** (input / add / matmul result) owns a contiguous
  flat storage window. ``slice`` results are *views*: they own no storage
  and alias their transitive root's window for the root buffer's whole
  extended lifetime.
* Time is the topological (definition) order index. A buffer born at index
  ``b`` and last used at index ``l`` occupies storage at every execution
  point in the inclusive interval ``[b, l]``. At point ``l`` the last
  consumer reads the buffer while its own output is being written, so two
  buffers conflict iff ``b1 <= l2 and b2 <= l1``.
* Placement is a classic greedy best-fit: at each birth point, expired
  slots are reclaimed; the smallest free slot that fits is reused,
  otherwise the arena grows.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from .dag import DAG, SLICE

DTYPE_ITEMSIZE = 4  # float32


class AllocationError(ValueError):
    """Base class for planner failures."""


class BudgetExceeded(AllocationError):
    """Raised in strict mode when the planned arena exceeds the peak budget."""

    def __init__(self, arena_elements: int, budget_elements: int):
        super().__init__(
            f"peak budget exceeded: arena needs {arena_elements} elements "
            f"({arena_elements * DTYPE_ITEMSIZE} bytes), "
            f"budget {budget_elements} elements "
            f"({budget_elements * DTYPE_ITEMSIZE} bytes)"
        )
        self.arena_elements = arena_elements
        self.budget_elements = budget_elements


def num_elements(shape: tuple[int, ...]) -> int:
    return math.prod(shape) if shape else 1


def c_strides(shape: tuple[int, ...]) -> tuple[int, ...]:
    """Row-major (C-order) strides in *elements*; scalar returns ()."""
    if not shape:
        return ()
    strides = [1] * len(shape)
    for k in range(len(shape) - 2, -1, -1):
        strides[k] = strides[k + 1] * shape[k + 1]
    return tuple(strides)


@dataclass(frozen=True)
class Lifetime:
    birth: int       # topo index at which the tensor/buffer becomes available
    last_use: int    # topo index of its last read (L for graph outputs)


@dataclass(frozen=True)
class Buffer:
    """One root tensor's placement in the arena (views share it)."""

    id: int
    root: str
    size: int            # elements the root tensor occupies
    capacity: int        # slot size (>= size; reuse slots may be larger)
    offset: int          # arena offset in elements
    birth: int
    last_use: int
    members: tuple[str, ...]   # root + every slice view aliasing it

    @property
    def window(self) -> tuple[int, int]:
        return self.offset, self.offset + self.size


@dataclass(frozen=True)
class ViewInfo:
    """Storage metadata for a slice view inside its root's window."""

    name: str
    root: str
    rel_offset: int                  # first element offset, relative to root
    shape: tuple[int, ...]
    strides: tuple[int, ...]         # C-order strides inherited from root
    min_offset: int                  # relative flat offset of first element
    end_offset: int                  # one past the last touched element

    def absolute_footprint_bounds(self, root_offset: int) -> tuple[int, int]:
        return root_offset + self.min_offset, root_offset + self.end_offset


@dataclass
class MemoryPlan:
    dag: DAG
    buffers: dict[int, Buffer]
    buffer_of: dict[str, int]            # every tensor name -> buffer id
    views: dict[str, ViewInfo]
    arena_elements: int
    theoretical_peak_elements: int       # max live-size sum at any point
    last_use: dict[str, int]
    lifetimes: dict[str, Lifetime]
    live_buffers_at: list[list[int]]     # index = topo point
    events: list[dict]                   # per-point allocation timeline
    dtype_itemsize: int = DTYPE_ITEMSIZE

    # -- convenience accessors -------------------------------------------------

    def buffer_for(self, name: str) -> Buffer:
        return self.buffers[self.buffer_of[name]]

    @property
    def arena_bytes(self) -> int:
        return self.arena_elements * self.dtype_itemsize

    @property
    def theoretical_peak_bytes(self) -> int:
        return self.theoretical_peak_elements * self.dtype_itemsize

    def is_view(self, name: str) -> bool:
        return name in self.views


def _compute_last_use(dag: DAG) -> dict[str, int]:
    """Last topo index at which each tensor is read; outputs live to the end."""
    last_point = len(dag.order) - 1
    index = {name: i for i, name in enumerate(dag.order)}
    last_use: dict[str, int] = {}
    for name in dag.order:
        consumers = dag.consumers[name]
        lu = max((index[c] for c, _ in consumers), default=index[name])
        if dag.nodes[name].is_output:
            lu = last_point
        last_use[name] = lu
    return last_use


def _resolve_roots(dag: DAG) -> dict[str, str]:
    """Map every tensor to its storage root (following slice-view chains)."""
    root_of: dict[str, str] = {}
    for name in dag.order:
        node = dag.nodes[name]
        if node.op == SLICE:
            root_of[name] = root_of[node.inputs[0]]
        else:
            root_of[name] = name
    return root_of


def _view_info(dag: DAG, name: str) -> ViewInfo:
    """Flat-window metadata for a (possibly chained) slice view.

    Every hop is a per-axis ``start:stop`` range with step 1 on the full
    axis set, so ndim and C-order strides are inherited unchanged from the
    terminal root; only the origin shifts. Hop ranges are therefore summed
    into global per-axis ``[g_start, g_stop)`` intervals in root coords.
    """
    hops: list[str] = []
    cur = name
    while dag.nodes[cur].op == SLICE:
        hops.append(cur)
        cur = dag.nodes[cur].inputs[0]
    root = cur
    root_shape = dag.nodes[root].shape
    ndim = len(root_shape)
    strides = c_strides(root_shape)

    g_starts = [0] * ndim
    g_ends = list(root_shape)
    for hop in reversed(hops):  # root-most hop first
        spec = dag.nodes[hop].slice_spec
        assert spec is not None
        for axis in range(ndim):
            # hop's [start, stop) are indices within its immediate source
            # view, whose origin sits at g_starts in root coordinates.
            origin = g_starts[axis]
            g_starts[axis] = origin + spec.starts[axis]
            g_ends[axis] = origin + spec.stops[axis]

    first_offset = sum(g_starts[a] * strides[a] for a in range(ndim))
    node = dag.nodes[name]
    if any(g_ends[a] == g_starts[a] for a in range(ndim)):
        # Empty view (an empty slice on some axis) touches no storage:
        # represent its footprint as the empty interval at its origin.
        min_offset = end_offset = first_offset
    else:
        last_offset = sum((g_ends[a] - 1) * strides[a] for a in range(ndim))
        min_offset, end_offset = first_offset, last_offset + 1
    return ViewInfo(
        name=name,
        root=root,
        rel_offset=first_offset,
        shape=node.shape,
        strides=strides,
        min_offset=min_offset,
        end_offset=end_offset,
    )


def plan_memory(
    dag: DAG,
    peak_budget_elements: Optional[int] = None,
    enforce_budget: bool = False,
) -> MemoryPlan:
    """Run liveness analysis and best-fit buffer reuse for ``dag``."""
    index = {name: i for i, name in enumerate(dag.order)}
    last_point = len(dag.order) - 1
    last_use = _compute_last_use(dag)
    root_of = _resolve_roots(dag)

    # Coalesce each alias family into one buffer record.
    family_members: dict[str, list[str]] = {}
    for name, root in root_of.items():
        family_members.setdefault(root, []).append(name)

    # Birth / last-use of a family buffer.
    family_lu: dict[str, int] = {
        root: max(last_use[m] for m in members)
        for root, members in family_members.items()
    }

    # Greedy best-fit placement, walking execution points in topo order.
    @dataclass
    class _Slot:
        offset: int
        capacity: int
        holder: Optional[int]  # buffer id currently occupying it, or free

    slots: list[_Slot] = []
    arena_high = 0
    buffers: dict[int, Buffer] = {}
    buffer_of: dict[str, int] = {}
    next_buffer_id = 0
    events: list[dict] = []

    for point, name in enumerate(dag.order):
        node = dag.nodes[name]
        # Reclaim slots whose holder's inclusive lifetime ended before now.
        reclaimed: list[str] = []
        for slot in slots:
            if slot.holder is not None and buffers[slot.holder].last_use < point:
                reclaimed.append(buffers[slot.holder].root)
                slot.holder = None

        event = {
            "point": point,
            "name": name,
            "op": node.op,
            "allocated": None,
            "reused_slot": False,
            "reclaimed": reclaimed,
        }

        if node.op != SLICE:
            size = num_elements(node.shape)
            birth = point
            lu = family_lu[name]
            candidates = [
                s for s in slots if s.holder is None and s.capacity >= size
            ]
            if candidates:
                slot = min(candidates, key=lambda s: s.capacity)  # best fit
                reused = True
            else:
                slot = _Slot(
                    offset=arena_high, capacity=size, holder=None
                )
                arena_high += size
                slots.append(slot)
                reused = False
            bid = next_buffer_id
            next_buffer_id += 1
            buf = Buffer(
                id=bid,
                root=name,
                size=size,
                capacity=slot.capacity,
                offset=slot.offset,
                birth=birth,
                last_use=lu,
                members=tuple(family_members[name]),
            )
            buffers[bid] = buf
            slot.holder = bid
            for member in family_members[name]:
                buffer_of[member] = bid
            event["allocated"] = {
                "buffer": bid,
                "size": size,
                "offset": slot.offset,
                "last_use": lu,
            }
            event["reused_slot"] = reused
        else:
            # Views allocate nothing; sanity: mapping established by root.
            if name not in buffer_of:  # pragma: no cover - defensive
                raise AllocationError(
                    f"slice view {name!r} appeared before its root was placed"
                )
        events.append(event)

    arena_elements = arena_high

    # Per-point live-buffer sets and the placement-independent lower bound.
    # Sweep birth/death events: each buffer is added and removed once, so
    # cost is O(n + total live entries) rather than O(n^2).
    n_points = len(dag.order)
    born_at: dict[int, list[int]] = {}
    die_at: dict[int, list[int]] = {}
    for b in buffers.values():
        born_at.setdefault(b.birth, []).append(b.id)
        die_at.setdefault(b.last_use + 1, []).append(b.id)

    live_at: list[list[int]] = []
    active: set[int] = set()
    live_size = 0
    theoretical_peak = 0
    for point in range(n_points):
        for bid in die_at.get(point, []):
            active.remove(bid)
            live_size -= buffers[bid].size
        for bid in born_at.get(point, []):
            active.add(bid)
            live_size += buffers[bid].size
        live_at.append(sorted(active))
        theoretical_peak = max(theoretical_peak, live_size)

    # View metadata (offsets relative to each family root).
    views: dict[str, ViewInfo] = {}
    for name in dag.order:
        if dag.nodes[name].op == SLICE:
            views[name] = _view_info(dag, name)

    lifetimes = {
        name: Lifetime(birth=index[name], last_use=last_use[name])
        for name in dag.order
    }

    plan = MemoryPlan(
        dag=dag,
        buffers=buffers,
        buffer_of=buffer_of,
        views=views,
        arena_elements=arena_elements,
        theoretical_peak_elements=theoretical_peak,
        last_use=last_use,
        lifetimes=lifetimes,
        live_buffers_at=live_at,
        events=events,
    )
    _verify_plan(plan)

    if (
        enforce_budget
        and peak_budget_elements is not None
        and arena_elements > peak_budget_elements
    ):
        raise BudgetExceeded(arena_elements, peak_budget_elements)
    return plan


def _live_overlap_violations(
    plan: MemoryPlan,
) -> list[tuple[Buffer, Buffer]]:
    """Return every pair of buffers whose windows overlap while both live.

    Linearithmic rather than pairwise: fresh slots are appended at disjoint
    arena ranges, so two buffers can share storage only when they held the
    *same* slot at different times. We therefore (a) check distinct slots
    are disjoint, and (b) within each slot (buffers sharing an offset) check
    that successive holders' lifetimes are strictly separated.
    """
    buffers = list(plan.buffers.values())

    # (a) slots themselves must tile the arena without overlap.
    slot_bases = sorted({(b.offset, b.capacity) for b in buffers})
    for (off1, cap1), (off2, _) in zip(slot_bases, slot_bases[1:]):
        if off1 + cap1 > off2:
            same = [b for b in buffers if b.offset in (off1, off2)]
            return [(same[0], same[1])]

    # (b) reuses of one slot must not have overlapping lifetimes. Zero-size
    # buffers own empty windows [o, o) and may legitimately share an offset
    # (allocating one does not advance arena_high), so they can never touch
    # another buffer: require both holders to be non-empty to conflict.
    violations: list[tuple[Buffer, Buffer]] = []
    by_offset: dict[int, list[Buffer]] = {}
    for b in buffers:
        by_offset.setdefault(b.offset, []).append(b)
    for holders in by_offset.values():
        holders.sort(key=lambda b: b.birth)
        for prev, cur in zip(holders, holders[1:]):
            if (
                prev.size > 0
                and cur.size > 0
                and cur.birth <= prev.last_use
            ):
                violations.append((prev, cur))
    return violations


def _verify_plan(plan: MemoryPlan) -> None:
    """Assert structural safety: no overlap of simultaneously live storage.

    1. Buffers whose lifetimes overlap occupy disjoint windows. This covers
       roots *and* their whole alias families, because a view extends its
       root buffer's last-use to the latest read of any member.
    2. Every slice view's touched flat region is contained in its root's
       contiguous window (so views can never reach another buffer's slot).
    """
    for a, b in _live_overlap_violations(plan):
        raise AllocationError(
            f"overlapping live storage: buffer {a.id} (root {a.root!r}, "
            f"life [{a.birth},{a.last_use}], window "
            f"[{a.offset},{a.offset + a.size}) and buffer {b.id} "
            f"(root {b.root!r}, life [{b.birth},{b.last_use}], window "
            f"[{b.offset},{b.offset + b.size})"
        )

    for view in plan.views.values():
        root_buf = plan.buffer_for(view.root)
        lo, hi = view.absolute_footprint_bounds(root_buf.offset)
        if lo < root_buf.offset or hi > root_buf.offset + root_buf.size:
            raise AllocationError(
                f"slice view {view.name!r} footprint [{lo},{hi}) escapes root "
                f"{view.root!r} window "
                f"[{root_buf.offset},{root_buf.offset + root_buf.size})"
            )


def overlapping_live_pairs(plan: MemoryPlan) -> list[tuple[str, str]]:
    """Public check returning any violating live pair (empty list == safe)."""
    return [(a.root, b.root) for a, b in _live_overlap_violations(plan)]
