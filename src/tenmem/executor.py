"""Plan interpreter / executor.

The executor trusts the plan for *placement* but never for *capacity*: before
every write it checks the concrete runtime shape against the planned buffer
capacity. A dynamic shape that grew past the declared bound raises
``replanning_required`` with the offending tensor, actual shape and buffer
capacity — the run aborts and no undersized buffer is ever written.

Execution modes
---------------
* ``execute`` runs the planned graph: tensor views are slices of shared backing
  bytearrays exactly as :class:`Plan` places them. Nodes inside a wave can run
  in parallel threads; the planner's conflict-free guarantee means their buffer
  sets are disjoint.
* ``execute_no_reuse`` is the reference executor: every tensor and workspace
  gets a fresh allocation, nothing is reused. It exists to prove numerical
  equivalence of the two strategies and to report the no-reuse memory total.

Results are numerical NumPy arrays; tests additionally compare against
hand-written NumPy expressions so reference answers are not produced by the
system under test itself.
"""
from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np

from .errors import (
    ComputationError,
    GraphValidationError,
    ReplanningRequiredError,
    StateConflictError,
)
from .graph import Graph, validate_and_schedule
from .ops import get_op
from .planner.liveness import analyze
from .planner.memory import Plan
from .tensor import align_up

_DTYPES = {name: np.dtype(name) for name in ("float32", "float64", "int32", "int64")}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


@dataclass
class WaveTrace:
    wave: int
    nodes: list[str]
    born: list[str]
    died: list[str]
    live_buffers: list[int]
    live_capacity_bytes: int
    live_actual_bytes: int
    parallel: bool


@dataclass
class RunTrace:
    run_id: str
    graph_name: str
    started_at: str
    mode: str                        # "reuse" | "no_reuse"
    parallel: bool
    waves: list[WaveTrace] = field(default_factory=list)
    peak_capacity_bytes: int = 0
    peak_actual_bytes: int = 0
    output_shapes: dict[str, list] = field(default_factory=dict)
    pinned_bytes: int = 0
    status: str = "running"          # running | ok | failed
    failure: dict | None = None
    decisions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "graph_name": self.graph_name,
            "started_at": self.started_at,
            "mode": self.mode,
            "parallel": self.parallel,
            "peak_capacity_bytes": self.peak_capacity_bytes,
            "peak_actual_bytes": self.peak_actual_bytes,
            "pinned_bytes": self.pinned_bytes,
            "status": self.status,
            "failure": self.failure,
            "decisions": self.decisions,
            "output_shapes": self.output_shapes,
            "waves": [
                {
                    "wave": w.wave,
                    "nodes": w.nodes,
                    "born": w.born,
                    "died": w.died,
                    "live_buffers": w.live_buffers,
                    "live_capacity_bytes": w.live_capacity_bytes,
                    "live_actual_bytes": w.live_actual_bytes,
                    "parallel": w.parallel,
                }
                for w in self.waves
            ],
        }


class Session:
    """A single planned execution with explicit output lifecycle.

    States: ``initialized -> executed -> (released)*``. Re-running a session
    while a pinned output is still held is a ``state_conflict``.
    """

    def __init__(
        self,
        graph: Graph,
        plan: Plan,
        *,
        parallel: bool = False,
        run_id: str | None = None,
    ) -> None:
        self.graph = graph
        self.plan = plan
        self.schedule = validate_and_schedule(graph)
        self.live = analyze(graph, self.schedule, plan.alignment)
        self.parallel = parallel
        self.run_id = run_id or f"run-{uuid.uuid4().hex[:12]}"
        self.trace = RunTrace(
            run_id=self.run_id,
            graph_name=graph.name,
            started_at=now_iso(),
            mode="reuse",
            parallel=parallel,
        )
        self._state = "initialized"
        self._backing: dict[int, bytearray] = {}
        self._arrays: dict[str, np.ndarray] = {}
        self._specs = {s.name: s for s in (*graph.inputs, *graph.constants)}
        for node in graph.nodes:
            for o in node.outputs:
                self._specs[o.name] = o
        self._runtime_shapes: dict[str, tuple[int, ...]] = {}
        self._outputs: dict[str, np.ndarray] = {}
        self._released: set[str] = set()

    # -- lifecycle ----------------------------------------------------------------

    @property
    def state(self) -> str:
        return self._state

    def pinned_bytes(self) -> int:
        return sum(
            self.plan.buffers[self.plan.buffer_id_for(n)].capacity
            for n in self.graph.output_names
            if n not in self._released
        )

    def release_output(self, name: str) -> None:
        if name not in self.graph.output_names:
            raise StateConflictError(
                f"{name!r} is not a graph output", details={"name": name}
            )
        if name in self._released:
            raise StateConflictError(
                f"output {name!r} already released", details={"name": name}
            )
        if self._state == "initialized":
            raise StateConflictError(
                f"cannot release {name!r} before execution", details={"name": name}
            )
        self._released.add(name)
        self._outputs.pop(name, None)
        self.trace.decisions.append(f"release:{name}")

    def output(self, name: str) -> np.ndarray:
        if name not in self.graph.output_names:
            raise StateConflictError(f"{name!r} is not a graph output", details={"name": name})
        if name in self._released:
            raise StateConflictError(
                f"output {name!r} was released and is no longer available",
                details={"name": name},
            )
        return self._outputs[name]

    # -- internals ----------------------------------------------------------------

    def _buffer(self, bid: int) -> bytearray:
        if bid not in self._backing:
            self._backing[bid] = bytearray(self.plan.buffers[bid].capacity)
        return self._backing[bid]

    def _view(self, name: str, shape: tuple[int, ...]) -> np.ndarray:
        spec = self._specs[name]
        bid = self.plan.buffer_id_for(name)
        capacity = self.plan.buffers[bid].capacity
        needed = spec.bytes_for(shape)
        if needed > capacity:
            raise ReplanningRequiredError(
                f"runtime shape {shape} of tensor {name!r} needs {needed} B but "
                f"buffer {bid} holds {capacity} B; replan with the new shape bound",
                details={
                    "tensor": name,
                    "actual_shape": list(shape),
                    "declared_max_shape": list(spec.max_shape),
                    "needed_bytes": needed,
                    "buffer_id": bid,
                    "buffer_capacity": capacity,
                },
            )
        buf = self._buffer(bid)
        dtype = _DTYPES[spec.dtype]
        view = np.frombuffer(buf, dtype=dtype, count=spec.num_elements(shape)).reshape(shape)
        return view

    def _bind_feed(self, name: str, value: np.ndarray) -> None:
        spec = self._specs[name]
        arr = np.asarray(value)
        if arr.dtype != _DTYPES[spec.dtype]:
            raise GraphValidationError(
                f"feed {name!r}: dtype {arr.dtype} != declared {spec.dtype}",
                details={"tensor": name, "actual": str(arr.dtype), "expected": spec.dtype},
            )
        shape = tuple(int(x) for x in arr.shape)
        if len(shape) != len(spec.bounds):
            raise GraphValidationError(
                f"feed {name!r}: rank {len(shape)} != declared {len(spec.bounds)}",
                details={"tensor": name, "actual_rank": len(shape), "expected_rank": len(spec.bounds)},
            )
        for axis, (actual, bound) in enumerate(zip(shape, spec.bounds)):
            if actual > bound.hi or actual < bound.lo:
                raise ReplanningRequiredError(
                    f"feed {name!r}: shape {shape} exceeds declared bound; replan required",
                    details={
                        "tensor": name,
                        "actual_shape": list(shape),
                        "bound_hi": list(spec.max_shape),
                        "axis": axis,
                    },
                )
        view = self._view(name, shape)
        view[...] = arr
        self._arrays[name] = view
        self._runtime_shapes[name] = shape

    def _run_node(self, node) -> None:
        op = get_op(node.op)
        inputs = [self._arrays[i] for i in node.inputs]
        # Positional scalar inputs (int64 0-d) drive dynamic shape inference.
        scalar_values = tuple(
            int(np.asarray(self._arrays[i]).item())
            if self._specs[i].dtype == "int64" and self._arrays[i].ndim == 0
            else None
            for i in node.inputs
        )
        cfg = dict(node.config)
        cfg["runtime_scalar_values"] = scalar_values
        out_shapes = op.shape_fn([a.shape for a in inputs], cfg, node.outputs)
        outputs = []
        for spec, shape in zip(node.outputs, out_shapes):
            shape = tuple(int(x) for x in shape)
            try:
                spec.check_runtime_shape(shape)
            except ValueError as exc:
                raise ReplanningRequiredError(
                    f"inferred shape {shape} of {spec.name!r} violates declared "
                    f"bound {spec.min_shape}..{spec.max_shape}; replan required",
                    details={
                        "tensor": spec.name,
                        "actual_shape": list(shape),
                        "bound_lo": list(spec.min_shape),
                        "bound_hi": list(spec.max_shape),
                    },
                ) from exc
            outputs.append(self._view(spec.name, shape))
            self._arrays[spec.name] = outputs[-1]
            self._runtime_shapes[spec.name] = shape
        workspaces = None
        if op.workspace_fn is not None:
            ws_name = f"ws:{node.id}"
            bid = self.plan.buffer_id_for(ws_name)
            capacity = self.plan.buffers[bid].capacity
            dtype = _DTYPES[node.outputs[0].dtype]
            needed = op.workspace_fn(
                [a.shape for a in inputs], cfg, dtype.itemsize
            )
            if needed > capacity:  # defensive: bounds should already prevent it
                raise ReplanningRequiredError(
                    f"workspace for node {node.id!r} grew past planned capacity",
                    details={"node": node.id, "needed": needed, "capacity": capacity},
                )
            workspaces = [np.frombuffer(self._buffer(bid), dtype=dtype, count=capacity // dtype.itemsize)]
        try:
            op.fn(inputs, outputs, workspaces, cfg)
        except ReplanningRequiredError:
            raise
        except Exception as exc:  # kernel numerical/runtime failure
            raise ComputationError(
                f"kernel {node.op!r} failed in node {node.id!r}: {exc}",
                details={"node": node.id, "op": node.op, "error": type(exc).__name__},
            ) from exc

    def run(self, feeds: dict[str, np.ndarray], constants: dict[str, np.ndarray] | None = None):
        if self._state == "executed" and self.graph.output_names and not all(
            n in self._released for n in self.graph.output_names
        ):
            held = [n for n in self.graph.output_names if n not in self._released]
            raise StateConflictError(
                "session still holds pinned graph outputs; release them before re-run",
                details={"held_outputs": held},
            )
        # Reset transient state for a fresh run.
        self._backing.clear()
        self._arrays.clear()
        self._runtime_shapes.clear()
        self._outputs.clear()
        self._released.clear()
        self.trace = RunTrace(
            run_id=self.run_id, graph_name=self.graph.name,
            started_at=now_iso(), mode="reuse", parallel=self.parallel,
        )
        self._state = "executed"

        try:
            constants = constants or {}
            expected = {s.name for s in self.graph.inputs}
            missing = expected - set(feeds)
            extra = set(feeds) - expected
            if missing:
                raise GraphValidationError(
                    f"missing feeds: {sorted(missing)}", details={"missing": sorted(missing)}
                )
            if extra:
                raise GraphValidationError(
                    f"unexpected feeds: {sorted(extra)}", details={"extra": sorted(extra)}
            )
            for s in self.graph.inputs:
                self._bind_feed(s.name, feeds[s.name])
            const_specs = {s.name: s for s in self.graph.constants}
            for name, value in constants.items():
                if name not in const_specs:
                    raise GraphValidationError(f"unknown constant {name!r}", details={"name": name})
                self._bind_feed(name, value)
            for name, spec in const_specs.items():
                if name not in constants:
                    raise GraphValidationError(f"missing constant {name!r}", details={"name": name})

            node_by_id = {n.id: n for n in self.graph.nodes}
            for wave_idx, wave_nodes in enumerate(self.schedule.waves):
                born, died = self._wave_transitions(wave_idx, wave_nodes)
                if self.parallel and len(wave_nodes) > 1:
                    with ThreadPoolExecutor(max_workers=len(wave_nodes)) as pool:
                        list(pool.map(lambda nid: self._run_node(node_by_id[nid]), wave_nodes))
                else:
                    for nid in wave_nodes:
                        self._run_node(node_by_id[nid])
                self._record_wave(wave_idx, wave_nodes, born, died)

            for name in self.graph.output_names:
                # Hold the *view* into the planned backing buffer, not a copy:
                # a pinned output keeps that buffer alive until release.
                self._outputs[name] = self._arrays[name]
                self.trace.output_shapes[name] = list(self._outputs[name].shape)
            self.trace.peak_capacity_bytes = self.plan.peak_bytes
            self.trace.peak_actual_bytes = max(
                (w.live_actual_bytes for w in self.trace.waves), default=0
            )
            self.trace.pinned_bytes = self.pinned_bytes()
            self.trace.status = "ok"
            return self
        except Exception as exc:
            self.trace.status = "failed"
            cat = getattr(exc, "category", "computation_failed")
            self.trace.failure = {
                "category": cat,
                "message": str(exc),
                "details": getattr(exc, "details", {}),
            }
            raise

    def _wave_transitions(self, wave_idx: int, wave_nodes):
        born = [
            spec.name
            for nid in wave_nodes
            for spec in node_by_id_of(self.graph, nid).outputs
        ]
        died = [
            name
            for name, (_, d) in self.live.tensor_interval.items()
            if d == wave_idx
        ]
        return sorted(born), sorted(died)

    def _record_wave(self, wave_idx, wave_nodes, born, died) -> None:
        # Capacity accounting is per *buffer*; union/alias members share one.
        live_bufs: set[int] = set()
        actual_per_buffer: dict[int, int] = {}
        for name, (b, d) in self.live.tensor_interval.items():
            if b <= wave_idx <= d:
                bid = self.plan.buffer_id_for(name)
                live_bufs.add(bid)
                shape = self._runtime_shapes.get(name)
                if shape is not None:
                    actual_per_buffer[bid] = max(
                        actual_per_buffer.get(bid, 0), self._specs[name].bytes_for(shape)
                    )
        for nid in wave_nodes:
            node = node_by_id_of(self.graph, nid)
            op = get_op(node.op)
            if op.workspace_fn is None:
                continue
            ws_bid = self.plan.buffer_id_for(f"ws:{nid}")
            live_bufs.add(ws_bid)
            ws_bytes = op.workspace_fn(
                [self._arrays[i].shape for i in node.inputs], node.config,
                _DTYPES[node.outputs[0].dtype].itemsize,
            )
            actual_per_buffer[ws_bid] = max(actual_per_buffer.get(ws_bid, 0), ws_bytes)
        cap_bytes = sum(self.plan.buffers[b].capacity for b in live_bufs)
        actual_bytes = sum(actual_per_buffer.values())
        self.trace.waves.append(
            WaveTrace(
                wave=wave_idx,
                nodes=list(wave_nodes),
                born=born,
                died=died,
                live_buffers=sorted(live_bufs),
                live_capacity_bytes=cap_bytes,
                live_actual_bytes=actual_bytes,
                parallel=self.parallel and len(wave_nodes) > 1,
            )
        )


def node_by_id_of(graph: Graph, node_id: str):
    return next(n for n in graph.nodes if n.id == node_id)


# ------------------------------------------------------------------------------
# No-reuse reference executor: fresh allocation per tensor/workspace, no sharing.
# ------------------------------------------------------------------------------

def execute_no_reuse(
    graph: Graph,
    feeds: dict[str, np.ndarray],
    constants: dict[str, np.ndarray] | None = None,
    *,
    alignment: int = 64,
) -> tuple[dict[str, np.ndarray], RunTrace]:
    """Reference execution with zero buffer reuse.

    Returns the output arrays and a trace whose ``peak_capacity_bytes`` equals
    the planner's ``no_reuse_bytes``: one aligned allocation per tensor and per
    workspace, nothing freed, nothing shared.
    """
    schedule = validate_and_schedule(graph)
    trace = RunTrace(
        run_id=f"run-{uuid.uuid4().hex[:12]}",
        graph_name=graph.name,
        started_at=now_iso(),
        mode="no_reuse",
        parallel=False,
    )
    specs = {s.name: s for s in (*graph.inputs, *graph.constants)}
    for node in graph.nodes:
        for o in node.outputs:
            specs[o.name] = o
    arrays: dict[str, np.ndarray] = {}
    total = 0
    constants = constants or {}
    try:
        for s in graph.inputs:
            arr = np.asarray(feeds[s.name]).astype(_DTYPES[s.dtype], copy=True)
            arrays[s.name] = arr
            total += align_up(arr.nbytes, alignment)
        for s in graph.constants:
            arr = np.asarray(constants[s.name]).astype(_DTYPES[s.dtype], copy=True)
            arrays[s.name] = arr
            total += align_up(arr.nbytes, alignment)
        node_by_id = {n.id: n for n in graph.nodes}
        for wave_idx, wave_nodes in enumerate(schedule.waves):
            for nid in wave_nodes:
                node = node_by_id[nid]
                op = get_op(node.op)
                inputs = [arrays[i] for i in node.inputs]
                scalar_values = tuple(
                    int(np.asarray(arrays[i]).item())
                    if specs[i].dtype == "int64" and arrays[i].ndim == 0
                    else None
                    for i in node.inputs
                )
                cfg = dict(node.config)
                cfg["runtime_scalar_values"] = scalar_values
                out_shapes = op.shape_fn([a.shape for a in inputs], cfg, node.outputs)
                outputs = []
                for spec, shape in zip(node.outputs, out_shapes):
                    arr = np.empty(shape, dtype=_DTYPES[spec.dtype])
                    arrays[spec.name] = arr
                    outputs.append(arr)
                    total += align_up(arr.nbytes, alignment)
                workspaces = None
                if op.workspace_fn is not None:
                    raw = op.workspace_fn(
                        [a.shape for a in inputs], cfg,
                        _DTYPES[node.outputs[0].dtype].itemsize,
                    )
                    elem = int(np.ceil(raw / _DTYPES[node.outputs[0].dtype].itemsize))
                    workspaces = [np.empty(elem, dtype=_DTYPES[node.outputs[0].dtype])]
                    total += align_up(raw, alignment)
                try:
                    op.fn(inputs, outputs, workspaces, cfg)
                except Exception as exc:
                    raise ComputationError(
                        f"[no_reuse] kernel {node.op!r} failed in {node.id!r}: {exc}",
                        details={"node": node.id, "op": node.op, "error": type(exc).__name__},
                    ) from exc
            trace.waves.append(WaveTrace(wave_idx, list(wave_nodes), [], [], [], total, total, False))
        result = {name: arrays[name].copy() for name in graph.output_names}
        trace.peak_capacity_bytes = total
        trace.peak_actual_bytes = total
        trace.status = "ok"
        trace.output_shapes = {k: list(v.shape) for k, v in result.items()}
        return result, trace
    except Exception as exc:
        trace.status = "failed"
        trace.failure = {
            "category": getattr(exc, "category", "computation_failed"),
            "message": str(exc),
            "details": getattr(exc, "details", {}),
        }
        raise


def execute(
    graph: Graph,
    plan: Plan,
    feeds: dict[str, np.ndarray],
    *,
    constants: dict[str, np.ndarray] | None = None,
    parallel: bool = False,
    run_id: str | None = None,
) -> Session:
    """Convenience wrapper: create a session, run it, return it (outputs pinned)."""
    session = Session(graph, plan, parallel=parallel, run_id=run_id)
    return session.run(feeds, constants)
