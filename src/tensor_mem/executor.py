"""Pool-backed graph executor with dynamic-shape replanning.

Execution model
---------------
* Every planned pool slot is one raw byte allocation; tensor bindings are
  aligned views over it (each reuse starts at the slot offset 0, so no record
  ever writes past a previous occupant's range).
* Before each run, *concrete* feed shapes are propagated through the graph and
  every record is capacity-checked. Any deficit replans the whole graph with
  the concrete shapes -- an undersized binding is never written out of range.
* Nodes inside a wave run on a thread pool behind a wave barrier; planner
  conflict-freedom makes their slot sets disjoint, so concurrent branches
  cannot share a buffer that is still in use.
* Graph outputs are returned as :class:`OutputHandle` objects. A handle pins
  its run's pool storage; resident bytes of earlier, not-yet-released runs are
  charged against later runs and the budget.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import numpy as np

from .errors import (
    BindCapacityError,
    ComputationError,
    InputValidationError,
    PlannerError,
    ResourceExhaustedError,
    StateConflictError,
)
from .graph import Graph
from .liveness import LivenessResult, analyze_liveness
from .ops import get_op
from .planner import Plan, check_capacity, plan_graph
from .runlog import RunLogger, new_run_id
from .tensor import (
    DEFAULT_ALIGNMENT,
    Shape,
    TensorMeta,
    TensorType,
)


# --------------------------------------------------------------------------- #
# Concrete shape propagation
# --------------------------------------------------------------------------- #


def propagate_concrete_metas(
    graph: Graph, feed_arrays: dict[str, np.ndarray]
) -> dict[str, TensorMeta]:
    """Infer every tensor's concrete TensorMeta from actual feed arrays.

    Rank and dtype must match the static graph contract; only dimension values
    are allowed to differ (the dynamic-shape case).
    """
    metas: dict[str, TensorMeta] = {}
    for name, static_meta in graph.feeds.items():
        if name not in feed_arrays:
            raise InputValidationError(
                "missing feed array", feed=name
            )
        arr = feed_arrays[name]
        if not isinstance(arr, np.ndarray):
            raise InputValidationError(
                "feed must be a numpy ndarray", feed=name
            )
        if arr.ndim != static_meta.tensor_type.rank:
            raise InputValidationError(
                "feed rank does not match graph declaration",
                feed=name,
                expected_rank=static_meta.tensor_type.rank,
                got_rank=int(arr.ndim),
            )
        if arr.dtype != static_meta.tensor_type.numpy_dtype:
            raise InputValidationError(
                "feed dtype does not match graph declaration",
                feed=name,
                expected=static_meta.tensor_type.dtype,
                got=str(arr.dtype),
            )
        shape = Shape(*(int(d) for d in arr.shape))
        metas[name] = TensorMeta(static_meta.tensor_type, shape)

    extra = sorted(set(feed_arrays) - set(graph.feeds))
    if extra:
        raise InputValidationError("unknown feed arrays supplied", feeds=extra)

    for nid in graph.order:
        node = graph.nodes[nid]
        in_metas = [metas[ref] for ref in node.inputs]
        types, shapes = node.spec.infer(
            [m.tensor_type for m in in_metas],
            [m.shape for m in in_metas],
            node.attrs_dict,
        )
        for out_name, t, s in zip(node.outputs, types, shapes):
            metas[out_name] = TensorMeta(t, s)
    return metas


# --------------------------------------------------------------------------- #
# Pool storage and output handles
# --------------------------------------------------------------------------- #


class _PoolStorage:
    """Per-slot raw allocations with wave-granularity acquire/release.

    A slot's backing bytes are allocated only for waves in which a record
    assigned to it is live, and released at a barrier when the slot is idle
    (re-allocated later if the slot is reused in a later wave). This makes the
    measured high-water mark equal to the planner's per-wave live capacity:
    reuse across disjoint intervals reduces real resident RAM, not just a
    notational count. Persistent (retained-output) slots are never released
    until every output handle is gone.
    """

    def __init__(self, plan: Plan) -> None:
        self.plan = plan
        self.raw: dict[str, np.ndarray] = {}
        # Cumulative bytes ever acquired: reuse across waves keeps this low;
        # no-reuse allocates a fresh slot for every record.
        self.total_acquired_bytes = 0

    def held_bytes(self) -> int:
        return sum(int(a.nbytes) for a in self.raw.values())

    def reconcile(self, needed: set[str]) -> int:
        """Release idle slots and allocate missing ones; returns held bytes."""
        for sid in list(self.raw):
            if sid not in needed:
                del self.raw[sid]
        for sid in needed:
            if sid not in self.raw:
                cap = self.plan.slots[sid].capacity
                self.raw[sid] = np.empty(cap, dtype=np.uint8)
                self.total_acquired_bytes += cap
        return self.held_bytes()

    def bind(self, slot_id: str, meta: TensorMeta, rid: str) -> np.ndarray:
        capacity = self.plan.slots[slot_id].capacity
        nbytes = meta.bytes()
        if nbytes > capacity:
            # Defensive last line of defence; capacity check + replan should
            # make this unreachable. Never write past the allocation.
            raise BindCapacityError(
                "binding exceeds slot capacity",
                record=rid, slot=slot_id,
                required_bytes=nbytes, capacity_bytes=capacity,
                shape=str(meta.shape),
            )
        view = self.raw[slot_id][:nbytes].view(meta.tensor_type.numpy_dtype)
        return view.reshape(meta.shape.as_tuple())


class OutputHandle:
    """Client-held handle to a graph output; releasing it frees the pin."""

    def __init__(
        self,
        name: str,
        run_id: str,
        array: np.ndarray,
        release_cb: "OutputHandleRelease",
    ) -> None:
        self._name = name
        self._run_id = run_id
        self._array = array
        self._release_cb = release_cb
        self._released = False

    @property
    def name(self) -> str:
        return self._name

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def released(self) -> bool:
        return self._released

    @property
    def array(self) -> np.ndarray:
        if self._released:
            raise StateConflictError(
                "output handle has already been released; the backing "
                "storage may have been reused",
                output=self._name, run=self._run_id,
            )
        return self._array

    def numpy(self) -> np.ndarray:
        return self.array

    def release(self) -> None:
        if self._released:
            raise StateConflictError(
                "output handle released twice",
                output=self._name, run=self._run_id,
            )
        self._released = True
        self._release_cb(self._run_id)

    def __repr__(self) -> str:
        state = "released" if self._released else f"shape={self._array.shape}"
        return f"<OutputHandle {self._name} run={self._run_id} {state}>"


OutputHandleRelease = Callable[[str], None]


@dataclass
class NodeTrace:
    node_id: str
    op: str
    wave: int
    input_shapes: list[tuple[int, ...]]
    output_shapes: list[tuple[int, ...]]
    slots: dict[str, str]


@dataclass
class ExecutionReport:
    run_id: str
    replanned: bool
    capacity_deficits: list[dict[str, int | str]]
    plan_peak_resident: int
    retained_before_bytes: int
    charged_peak_bytes: int
    budget: Optional[int]
    wave_resident: list[dict[str, Any]]
    node_traces: list[NodeTrace]
    output_shapes: dict[str, tuple[int, ...]]
    seq_events: int
    total_acquired_bytes: int = 0


# --------------------------------------------------------------------------- #
# Executor
# --------------------------------------------------------------------------- #


class Executor:
    def __init__(
        self,
        graph: Graph,
        budget: Optional[int] = None,
        alignment: int = DEFAULT_ALIGNMENT,
        external_names: frozenset[str] | None = None,
        logger: Optional[RunLogger] = None,
        max_workers: Optional[int] = None,
        allow_reuse: bool = True,
    ) -> None:
        self.graph = graph
        self.budget = budget
        self.alignment = alignment
        self.external_names = frozenset(external_names or ())
        self.allow_reuse = allow_reuse
        unknown = self.external_names - set(graph.feeds)
        if unknown:
            raise InputValidationError(
                "external names must be graph feeds", unknown=sorted(unknown)
            )
        self.logger = logger or RunLogger()
        self._owns_logger = logger is None
        # Initial static plan, using the graph's declared feed shapes.
        self.plan: Plan = plan_graph(
            graph, budget=budget, alignment=alignment,
            external_names=self.external_names,
            allow_reuse=allow_reuse,
        )
        self._lock = threading.RLock()
        self._max_workers = max_workers
        # run_id -> [outstanding handle count, pool storage, plan]
        self._pinned: dict[str, list[Any]] = {}

    # ------------------------------------------------------------------ #
    # Retained-output accounting
    # ------------------------------------------------------------------ #

    def retained_bytes(self) -> int:
        with self._lock:
            return sum(entry[2].persistent_bytes for entry in self._pinned.values())

    def _release_run(self, run_id: str) -> None:
        with self._lock:
            if run_id not in self._pinned:
                return
            entry = self._pinned[run_id]
            entry[0] -= 1
            if entry[0] <= 0:
                freed = entry[2].persistent_bytes
                del self._pinned[run_id]
                self.logger.event(
                    "outputs_released",
                    "all output handles released; pool storage unpinned",
                    run_id=run_id, freed_bytes=freed,
                    retained_bytes=self.retained_bytes(),
                )

    # ------------------------------------------------------------------ #
    # Execution
    # ------------------------------------------------------------------ #

    def execute(
        self,
        feed_arrays: dict[str, np.ndarray],
        run_id: Optional[str] = None,
    ) -> tuple[dict[str, OutputHandle], ExecutionReport]:
        """Run the graph once; returns output handles plus a trace report."""
        run_id = run_id or new_run_id("exec")
        with self._lock:
            return self._execute_locked(feed_arrays, run_id)

    def _execute_locked(
        self, feed_arrays: dict[str, np.ndarray], run_id: str
    ) -> tuple[dict[str, OutputHandle], ExecutionReport]:
        retained = self.retained_bytes()
        self.logger.event(
            "run_start", "execution requested",
            run_id=run_id,
            feed_shapes={
                k: list(arr.shape) for k, arr in sorted(feed_arrays.items())
            },
            budget=self.budget,
            retained_output_bytes=retained,
        )

        try:
            concrete = propagate_concrete_metas(self.graph, feed_arrays)
        except PlannerError as exc:
            self.logger.failure(exc.category, exc.message, run_id=run_id, **exc.details)
            raise

        # Capacity check against the current plan; deficits force a replan.
        deficits = check_capacity(self.plan, self.graph, concrete)
        replanned = False
        deficit_payload: list[dict[str, int | str]] = []
        if deficits:
            replanned = True
            deficit_payload = [
                {"record": rid, "required": req, "capacity": cap}
                for rid, req, cap in deficits
            ]
            old_peak = self.plan.peak_resident
            self.logger.event(
                "capacity_deficit",
                "concrete shapes exceed existing bindings; replanning "
                "(out-of-range reuse forbidden)",
                run_id=run_id, deficits=deficit_payload,
                old_peak_resident=old_peak,
            )
            try:
                new_plan = plan_graph(
                    self.graph,
                    budget=None,  # budget re-checked together with retained bytes
                    alignment=self.alignment,
                    override_metas=concrete,
                    external_names=self.external_names,
                    allow_reuse=self.allow_reuse,
                )
            except ResourceExhaustedError:
                raise
            self.plan = new_plan
            self.logger.event(
                "replan", "graph replanned for concrete shapes",
                run_id=run_id,
                old_peak_resident=old_peak,
                new_peak_resident=new_plan.peak_resident,
                slots=len(new_plan.slots),
            )

        charged_peak = self.plan.peak_resident + retained
        if self.budget is not None and charged_peak > self.budget:
            exc = ResourceExhaustedError(
                "charged peak (plan + not-yet-released outputs) exceeds budget",
                run_id=run_id,
                plan_peak_resident=self.plan.peak_resident,
                retained_output_bytes=retained,
                charged_peak_bytes=charged_peak,
                budget_bytes=self.budget,
                over_bytes=charged_peak - self.budget,
            )
            self.logger.failure(exc.category, exc.message, run_id=run_id,
                                **{k: v for k, v in exc.details.items()
                                   if k != "run_id"})
            raise exc

        live = analyze_liveness(
            self.graph, self.alignment, concrete, self.external_names
        )
        pool = _PoolStorage(self.plan)
        values: dict[str, np.ndarray] = {}
        traces: list[NodeTrace] = []

        wave_resident = self._run_waves(
            concrete, live, pool, values, traces, run_id, retained, feed_arrays
        )

        # Drop every non-output value so intermediate views stop keeping
        # released slots' byte buffers alive before the pool is trimmed.
        kept = {name: values[name] for name in self.graph.graph_outputs}
        values.clear()
        values.update(kept)

        # After the final barrier only retained-output storage must remain in
        # the pinned pool; everything else is returned.
        persistent_slots = {
            self.plan.assignment[f"val::{live.alias_root[name]}"]
            for name in self.graph.graph_outputs
            if name not in self.external_names
            and f"val::{live.alias_root[name]}" in self.plan.assignment
        }
        pool.reconcile(persistent_slots)
        assert pool.held_bytes() == self.plan.persistent_bytes, (
            f"retained pool {pool.held_bytes()} != planned "
            f"{self.plan.persistent_bytes}"
        )

        handles = self._make_handles(concrete, values, pool, run_id)
        output_shapes = {
            name: concrete[name].shape.as_tuple() for name in self.graph.graph_outputs
        }
        report = ExecutionReport(
            run_id=run_id,
            replanned=replanned,
            capacity_deficits=deficit_payload,
            plan_peak_resident=self.plan.peak_resident,
            retained_before_bytes=retained,
            charged_peak_bytes=charged_peak,
            budget=self.budget,
            wave_resident=wave_resident,
            node_traces=list(traces),
            output_shapes=output_shapes,
            seq_events=len(self.logger.events),
            total_acquired_bytes=pool.total_acquired_bytes,
        )
        self.logger.event(
            "run_complete", "execution finished",
            run_id=run_id,
            replanned=replanned,
            plan_peak_resident=self.plan.peak_resident,
            charged_peak_bytes=charged_peak,
            outputs={h.name: list(h.array.shape) for h in handles.values()},
        )
        return handles, report

    # ------------------------------------------------------------------ #

    def _record_of(self, name: str, live: LivenessResult):
        return self.plan.records[f"val::{live.alias_root[name]}"]

    def _bind_feeds(
        self,
        feed_arrays: dict[str, np.ndarray],
        concrete: dict[str, TensorMeta],
        live: LivenessResult,
        pool: _PoolStorage,
        values: dict[str, np.ndarray],
        run_id: str,
    ) -> None:
        for name, meta in self.graph.feeds.items():
            rid = f"val::{live.alias_root[name]}"
            arr = feed_arrays[name]
            if name in self.external_names:
                # State-owned storage: injected directly, never copied into
                # the reusable pool.
                if arr.shape != concrete[name].shape.as_tuple():
                    raise InputValidationError(
                        "external feed shape mismatch",
                        feed=name, shape=list(arr.shape),
                    )
                values[name] = arr
                continue
            slot_id = self.plan.assignment[rid]
            bound = pool.bind(slot_id, concrete[name], rid)
            bound[...] = arr
            values[name] = bound

    def _run_waves(
        self,
        concrete: dict[str, TensorMeta],
        live: LivenessResult,
        pool: _PoolStorage,
        values: dict[str, np.ndarray],
        traces: list[NodeTrace],
        run_id: str,
        retained: int,
        feed_arrays: dict[str, np.ndarray],
    ) -> list[dict[str, Any]]:
        wave_resident: list[dict[str, Any]] = []
        workers = self._max_workers or max(
            (len(w) for w in self.graph.waves), default=1
        )
        for w, node_ids in enumerate(self.graph.waves):
            # Barrier, step 1: drop references to tensors whose alias class
            # died in an earlier wave. Views keep their base alive, so every
            # member name of a dead class must be released here for the slot's
            # bytes to actually return before the slot is reconciled away.
            dead = [
                name
                for name, arr in list(values.items())
                if self._record_of(name, live).last_wave is not None
                and self._record_of(name, live).last_wave < w
            ]
            for name in dead:
                del values[name]

            # Barrier, step 2: reconcile physical pool to exactly the slots
            # live during this wave (frees dead occupants, acquires new ones).
            needed = set(self.plan.wave_stats[w].occupied_slots)
            held = pool.reconcile(needed)
            assert held == self.plan.wave_stats[w].live_capacity, (
                f"wave {w}: held {held} != planned "
                f"{self.plan.wave_stats[w].live_capacity}"
            )
            if w == 0:
                self._bind_feeds(feed_arrays, concrete, live, pool, values, run_id)

            self.logger.event(
                "wave_start", "entering wave",
                run_id=run_id, wave=w, nodes=list(node_ids),
                concurrency=len(node_ids), held_pool_bytes=held,
            )

            def run_node(nid: str) -> NodeTrace:
                return self._run_node(nid, w, concrete, live, pool, values, run_id)

            if len(node_ids) == 1 or workers == 1:
                wave_traces = [run_node(node_ids[0])]
            else:
                with ThreadPoolExecutor(
                    max_workers=min(workers, len(node_ids))
                ) as pool_exec:
                    wave_traces = list(pool_exec.map(run_node, node_ids))

            traces.extend(sorted(wave_traces, key=lambda t: t.node_id))
            stat = self.plan.wave_stats[w]
            wave_resident.append(
                {
                    "wave": w,
                    "pool_bytes": stat.live_capacity,
                    "external_bytes": stat.external_bytes,
                    "retained_output_bytes": retained,
                    "resident_bytes": stat.resident_bytes + retained,
                    "nodes": list(node_ids),
                }
            )
            self.logger.event(
                "wave_end", "wave barrier reached",
                run_id=run_id, wave=w,
                pool_bytes=stat.live_capacity,
                external_bytes=stat.external_bytes,
                resident_bytes=stat.resident_bytes + retained,
            )
        return wave_resident

    def _run_node(
        self,
        nid: str,
        wave: int,
        concrete: dict[str, TensorMeta],
        live: LivenessResult,
        pool: _PoolStorage,
        values: dict[str, np.ndarray],
        run_id: str,
    ) -> NodeTrace:
        node = self.graph.nodes[nid]
        inputs = [values[ref] for ref in node.inputs]

        out_buffers: list[np.ndarray] = []
        slot_map: dict[str, str] = {}
        for out_name, aliased_in in zip(node.outputs, node.spec.alias):
            rid = f"val::{live.alias_root[out_name]}"
            slot_map[out_name] = self.plan.assignment.get(rid, "<external|alias>")
            if aliased_in is None:
                slot_id = self.plan.assignment[rid]
                out_buffers.append(pool.bind(slot_id, concrete[out_name], rid))
            else:
                out_buffers.append(None)  # view op: kernel returns a view

        ws_rid = f"ws::{nid}"
        workspace = None
        if ws_rid in self.plan.assignment:
            ws_slot = self.plan.assignment[ws_rid]
            ws_record = self.plan.records[ws_rid]
            workspace = pool.raw[ws_slot][: ws_record.bytes_required]

        try:
            outputs = node.spec.kernel(inputs, node.attrs_dict, workspace, out_buffers)
        except (InputValidationError, ComputationError) as kernel_exc:
            self.logger.failure(
                kernel_exc.category, kernel_exc.message,
                run_id=run_id, node=nid, op=node.op,
                **kernel_exc.details,
            )
            raise
        except Exception as exc:  # numpy/attr failures become computation failures
            err = ComputationError(
                "op kernel raised",
                node=nid, op=node.op,
                error_type=type(exc).__name__, error=str(exc),
            )
            self.logger.failure(err.category, err.message, run_id=run_id, **err.details)
            raise err from exc

        def fail_computation(message: str, **details: object) -> ComputationError:
            err = ComputationError(
                message, node=nid, op=node.op, **details
            )
            self.logger.failure(
                err.category, err.message, run_id=run_id, **err.details
            )
            return err

        for out_name, out in zip(node.outputs, outputs):
            values[out_name] = out
            if out.dtype != concrete[out_name].tensor_type.numpy_dtype:
                raise fail_computation(
                    "kernel output dtype mismatch",
                    output=out_name,
                    expected=concrete[out_name].dtype, got=str(out.dtype),
                )
            if not np.all(np.isfinite(out)):
                raise fail_computation(
                    "kernel produced non-finite values",
                    output=out_name,
                    non_finite=int(out.size - np.count_nonzero(np.isfinite(out))),
                )

        trace = NodeTrace(
            node_id=nid, op=node.op, wave=wave,
            input_shapes=[list(a.shape) for a in inputs],
            output_shapes=[list(a.shape) for a in outputs],
            slots=dict(slot_map),
        )
        self.logger.event(
            "node_done", "node executed",
            run_id=run_id, wave=wave, node=nid, op=node.op,
            input_shapes=trace.input_shapes,
            output_shapes=trace.output_shapes,
            slots=trace.slots,
        )
        return trace

    def _make_handles(
        self,
        concrete: dict[str, TensorMeta],
        values: dict[str, np.ndarray],
        pool: _PoolStorage,
        run_id: str,
    ) -> dict[str, OutputHandle]:
        # Defensive copy of retained outputs is unnecessary: handles pin the
        # whole pool via self._pinned, and persistent records never share a
        # slot with a later record, so the views stay stable.
        handles: dict[str, OutputHandle] = {}
        for name in self.graph.graph_outputs:
            arr = values[name]
            handles[name] = OutputHandle(
                name=name, run_id=run_id, array=arr,
                release_cb=lambda rid: self._release_run(rid),
            )
        self._pinned[run_id] = [len(handles), pool, self.plan]
        self.logger.event(
            "outputs_retained",
            "output handles pin pool storage until client release",
            run_id=run_id,
            outputs=list(handles),
            pinned_bytes=self.plan.persistent_bytes,
            retained_bytes=self.retained_bytes(),
        )
        return handles

    def close(self) -> None:
        if self._owns_logger:
            self.logger.close()
