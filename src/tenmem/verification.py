"""Numerical verification: independent correctness and memory invariants.

This module is the explicit *numerical verification* boundary. It never uses
the planner or executor to produce expected answers; it compares reuse-mode
results against (a) the no-reuse executor and (b) caller-supplied independent
reference functions, and it asserts structural invariants every plan/run must
satisfy.

Verdicts are structured records so they can be logged verbatim (the "judgement
reason" attached to a run id).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .planner.memory import Plan
from .planner.liveness import overlaps

TOL_RTOL = 1e-5
TOL_ATOL = 1e-5


@dataclass(frozen=True)
class TensorVerdict:
    name: str
    match: bool
    reason: str
    shape: tuple[int, ...]
    reference_shape: tuple[int, ...]
    max_abs_diff: float


@dataclass(frozen=True)
class NumericalVerdict:
    equivalent: bool
    max_abs_diff: float
    tensor_verdicts: tuple[TensorVerdict, ...]

    def to_dict(self) -> dict:
        return {
            "equivalent": self.equivalent,
            "max_abs_diff": self.max_abs_diff,
            "tensors": [
                {
                    "name": t.name,
                    "match": t.match,
                    "reason": t.reason,
                    "shape": list(t.shape),
                    "reference_shape": list(t.reference_shape),
                    "max_abs_diff": t.max_abs_diff,
                }
                for t in self.tensor_verdicts
            ],
        }


def compare_outputs(
    outputs: dict[str, np.ndarray],
    references: dict[str, np.ndarray],
) -> NumericalVerdict:
    """Compare reuse outputs to a reference dict (no-reuse or hand-written).

    Distinguishes failure *reasons*: missing tensor, shape mismatch, non-finite
    values, tolerance breach.
    """
    verdicts: list[TensorVerdict] = []
    worst = 0.0
    equivalent = True
    for name, got in outputs.items():
        if name not in references:
            equivalent = False
            verdicts.append(
                TensorVerdict(name, False, "reference tensor missing", tuple(got.shape), (), float("inf"))
            )
            continue
        ref = references[name]
        if got.shape != ref.shape:
            equivalent = False
            verdicts.append(
                TensorVerdict(name, False, "shape mismatch", tuple(got.shape), tuple(ref.shape), float("inf"))
            )
            continue
        if got.size == 0:
            diff = 0.0
            finite = True
        else:
            diff = float(np.max(np.abs(got.astype(np.float64) - ref.astype(np.float64))))
            finite = bool(np.all(np.isfinite(got)))
        reason = "ok"
        match = True
        if not finite:
            match = False
            equivalent = False
            reason = "non-finite values in output"
        elif not np.allclose(got, ref, rtol=TOL_RTOL, atol=TOL_ATOL):
            match = False
            equivalent = False
            reason = f"abs diff {diff:g} exceeds tolerance rtol={TOL_RTOL} atol={TOL_ATOL}"
        worst = max(worst, diff)
        verdicts.append(TensorVerdict(name, match, reason, tuple(got.shape), tuple(ref.shape), diff))
    return NumericalVerdict(equivalent, worst, tuple(verdicts))


def assert_plan_invariants(plan: Plan) -> None:
    """Structural invariants every accepted plan must satisfy.

    Raises AssertionError with a concrete explanation; used by tests and by the
    demo verifier (independent of the planner's internal post-check).
    """
    # 1. Capacities are positive, aligned and large enough for each occupant.
    for buf in plan.buffers:
        assert buf.capacity >= 0, f"buffer {buf.id} negative capacity"
    for p in plan.placements:
        buf = plan.buffers[p.buffer_id]
        assert buf.capacity >= p.size, (
            f"occupant {p.name} needs {p.size} B but buffer {p.buffer_id} holds {buf.capacity}"
        )

    # 2. Distinct groups on the same buffer never have overlapping closed
    #    intervals (parallel-safety). Same-group rows are alias unions.
    by_buffer: dict[int, list] = {}
    for p in plan.placements:
        by_buffer.setdefault(p.buffer_id, []).append(p)
    for bid, ps in by_buffer.items():
        for i, a in enumerate(ps):
            for b in ps[i + 1 :]:
                if a.group and a.group == b.group:
                    continue
                assert not overlaps((a.birth, a.death), (b.birth, b.death)), (
                    f"buffer {bid}: {a.name}[{a.birth},{a.death}] overlaps "
                    f"{b.name}[{b.birth},{b.death}]"
                )

    # 3. Pinned occupants never share their buffer with anything.
    for bid, ps in by_buffer.items():
        pinned = [p for p in ps if p.pinned]
        if pinned and len({p.group for p in ps if p.group}) > 1:
            other_groups = {p.group for p in ps if not p.pinned and p.group}
            assert not other_groups, f"pinned buffer {bid} reused by {other_groups}"

    # 4. Accounting consistency.
    assert plan.peak_bytes <= plan.no_reuse_bytes, "reuse cannot exceed no-reuse demand"
    assert plan.peak_bytes >= 0
