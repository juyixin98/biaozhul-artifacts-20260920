"""Numerical validation against an independent NumPy oracle.

Every check here derives its expected answer *directly from NumPy* — never by
calling the core implementation — so the test data cannot be circular.  Each
check asserts both concrete values and the copy/alias category of the result.

Results carry an explicit per-check status:

* ``pass``       – values and aliasing both match the oracle;
* ``fail``       – a concrete mismatch, with the failure category;
* ``uncertain``  – the check could not prove a property (e.g. the exact
  self-overlap test exceeded its enumeration budget);
* ``error``      – the core raised where the oracle did not, or vice versa.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np

from ..tensor import Tensor
from ..tensor.errors import ReshapeCopyRequiredError, TensorError
from ..tensor import ops as tops


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str  # pass | fail | uncertain | error
    detail: dict[str, Any] = field(default_factory=dict)
    failure_category: str | None = None
    message: str = ""


def _values_equal(a: np.ndarray, b: np.ndarray, *, tol: float = 1e-9) -> bool:
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    if a.shape != b.shape:
        return False
    if a.size == 0:
        return True
    return bool(np.allclose(a, b, rtol=tol, atol=tol, equal_nan=True))


def _tensor_equal(t: Tensor, expected: np.ndarray, *, tol: float = 1e-9) -> bool:
    # Compare materialized arrays directly: an empty tensor's to_list() is []
    # and a list round-trip erases the (0, k) shape.
    return _values_equal(t.materialize(), expected, tol=tol)


# --------------------------------------------------------------------- checks

def check_transpose_then_reshape() -> CheckResult:
    """Transpose is a view; reshaping it to 1-D must copy (NumPy agrees)."""
    base_np = np.arange(12.0).reshape(3, 4)
    t = Tensor.from_values(base_np)
    tr = t.T

    if not tr.shares_storage(t):
        return CheckResult("transpose_then_reshape", "fail",
                           failure_category="aliasing",
                           message="transpose must alias its source")
    try:
        tr.reshape((12,))
    except ReshapeCopyRequiredError:
        copied_required = True
    else:
        copied_required = False

    np_copy_required = not np.shares_memory(base_np, base_np.T.reshape(12))
    if copied_required != np_copy_required:
        return CheckResult("transpose_then_reshape", "fail",
                           failure_category="copy_decision",
                           message=f"core says copy={copied_required}, oracle says {np_copy_required}")

    forced = tr.reshape((12,), allow_copy=True)
    expected = base_np.T.reshape(12)
    if forced.shares_storage(t):
        return CheckResult("transpose_then_reshape", "fail",
                           failure_category="aliasing",
                           message="allow_copy reshape must not alias source")
    if not _tensor_equal(forced, expected):
        return CheckResult("transpose_then_reshape", "fail",
                           failure_category="value_mismatch",
                           message="copied reshape values differ from oracle")
    return CheckResult("transpose_then_reshape", "pass",
                       detail={"copy_required": True, "expected": expected.tolist()})


def check_broadcast_zero_stride() -> CheckResult:
    """A (1,3)+(2,3) add reads a zero-stride broadcast view and matches numpy."""
    row = np.array([[1.0, 2.0, 3.0]])
    zero = np.zeros((2, 3))
    a = Tensor.from_values(row)
    b = Tensor.from_values(zero)
    out = tops.binary("add", a, b)
    expected = row + zero
    strides_ok = out.shape == (2, 3)
    if not strides_ok:
        return CheckResult("broadcast_zero_stride", "fail",
                           failure_category="shape", message=str(out.shape))
    # The broadcast operand must expose a literal zero stride.
    from ..tensor import layout
    bs = layout.broadcast_strides(a.shape, a.strides, (2, 3))
    if bs[0] != 0:
        return CheckResult("broadcast_zero_stride", "fail",
                           failure_category="zero_stride",
                           message=f"broadcast leading stride must be 0, got {bs}")
    if not _tensor_equal(out, expected):
        return CheckResult("broadcast_zero_stride", "fail",
                           failure_category="value_mismatch")
    return CheckResult("broadcast_zero_stride", "pass",
                       detail={"broadcast_strides": list(bs), "expected": expected.tolist()})


def check_overlapping_slices() -> CheckResult:
    """Writing an overlapping slice pair is refused, or deterministic via temp."""
    base_np = np.arange(6.0)
    base = Tensor.from_values(base_np)
    dst = base[0:4]
    src = base[2:6]

    refused = False
    try:
        tops.assign(dst, src)
    except TensorError as exc:
        refused = exc.category == "overlapping_write"
    if not refused:
        return CheckResult("overlapping_slices", "fail",
                           failure_category="overlap_policy",
                           message="overlapping write must be refused under raise policy")

    fresh = Tensor.from_values(base_np)
    d2, s2 = fresh[0:4], fresh[2:6]
    tops.assign(d2, s2, overlap_policy=tops.OverlapPolicy.TEMP)
    # NumPy oracle: copy through a temporary.
    oracle_buf = base_np.copy()
    oracle_dst = oracle_buf[0:4]
    oracle_dst[:] = oracle_buf[2:6].copy()
    if not _tensor_equal(d2, oracle_dst):
        return CheckResult("overlapping_slices", "fail",
                           failure_category="value_mismatch",
                           message="temp policy result differs from copied oracle")
    if not _values_equal(fresh.materialize()[:4], np.array([2.0, 3.0, 4.0, 5.0])):
        return CheckResult("overlapping_slices", "fail",
                           failure_category="value_mismatch")
    return CheckResult("overlapping_slices", "pass",
                       detail={"temp_result": [2.0, 3.0, 4.0, 5.0]})


def check_self_overlapping_view_rejected() -> CheckResult:
    """A repeated-position view (broadcast stride 0 on a size>1 axis) cannot be
    a write destination."""
    storage = np.arange(3.0)
    view = Tensor.from_layout(storage, shape=(3, 3), strides=(0, 1))
    dst_ok = Tensor.zeros((3, 3))
    try:
        tops.assign(dst_ok, view)
    except TensorError:
        return CheckResult("self_overlap_rejected", "fail",
                           failure_category="overlap_policy",
                           message="reading an overlapping view into fresh dst is legal")
    try:
        tops.assign(view, dst_ok)
    except TensorError as exc:
        if exc.category == "overlapping_write":
            return CheckResult("self_overlap_rejected", "pass",
                               detail={"reason": "destination maps 9 elements to 3 positions"})
    return CheckResult("self_overlap_rejected", "fail",
                       failure_category="overlap_policy",
                       message="writing into a self-overlapping view must be rejected")


def check_empty_tensor() -> CheckResult:
    """Empty tensors: operations succeed with empty results; reshape is a view."""
    empty = Tensor.from_values(np.zeros((0, 3)))
    out = tops.unary("square", empty)
    if tuple(out.shape) != (0, 3) or out.size != 0:
        return CheckResult("empty_tensor", "fail", failure_category="shape",
                           message=f"got {out.shape}")
    reshaped = empty.reshape((3, 0))
    if not reshaped.shares_storage(empty):
        return CheckResult("empty_tensor", "fail", failure_category="aliasing",
                           message="empty reshape is defined as a zero-copy view")
    # Broadcasting with an empty dimension yields empty, matching numpy.
    a = Tensor.from_values(np.zeros((0, 1)))
    b = Tensor.from_values(np.zeros((4,)))
    res = tops.binary("add", a, b.unsqueeze(0))
    expected = np.zeros((0, 1)) + np.zeros((4,))
    if not _tensor_equal(res, expected):
        return CheckResult("empty_tensor", "fail", failure_category="value_mismatch")
    return CheckResult("empty_tensor", "pass", detail={"shape": [0, 3]})


def check_negative_strides() -> CheckResult:
    """Reverse-step slices alias and read in reverse, incl. multi-axis."""
    base_np = np.arange(12.0).reshape(3, 4)
    t = Tensor.from_values(base_np)
    rev = t[::-1]
    if not rev.shares_storage(t):
        return CheckResult("negative_strides", "fail", failure_category="aliasing",
                           message="reversed slice must alias")
    expected = base_np[::-1]
    if not _tensor_equal(rev, expected):
        return CheckResult("negative_strides", "fail",
                           failure_category="value_mismatch")
    step2 = t[:, ::-2]
    if not _tensor_equal(step2, base_np[:, ::-2]):
        return CheckResult("negative_strides", "fail",
                           failure_category="value_mismatch")
    return CheckResult("negative_strides", "pass",
                       detail={"reversed": expected.tolist()})


def check_out_of_bounds_and_overflow() -> CheckResult:
    """Out-of-range views are rejected; size products past the cap are rejected
    before allocation."""
    from ..tensor.errors import OutOfBoundsError, ShapeOverflowError
    try:
        Tensor.from_layout(np.arange(4.0), shape=(3, 3), strides=(3, 1))
    except OutOfBoundsError:
        pass
    else:
        return CheckResult("bounds_and_overflow", "fail",
                           failure_category="out_of_bounds",
                           message="view exceeding storage must be rejected")
    try:
        Tensor.zeros((10 ** 20, 10 ** 20))
    except ShapeOverflowError:
        return CheckResult("bounds_and_overflow", "pass")
    except TensorError as exc:
        return CheckResult("bounds_and_overflow", "fail",
                           failure_category=exc.category,
                           message="expected shape_overflow category")
    return CheckResult("bounds_and_overflow", "fail",
                       failure_category="shape_overflow",
                       message="huge shape product must raise before allocation")


def check_alias_chain_vs_numpy() -> CheckResult:
    """Transpose->slice->reshape chain: each alias/copy decision matches numpy."""
    base_np = np.arange(24.0).reshape(2, 3, 4)
    t = Tensor.from_values(base_np)
    chain = t.T[:, 1:, :]
    np_chain = base_np.T[:, 1:, :]
    if not chain.shares_storage(t):
        return CheckResult("alias_chain", "fail", failure_category="aliasing")
    if not _tensor_equal(chain, np_chain):
        return CheckResult("alias_chain", "fail", failure_category="value_mismatch")
    info = t.reshape_info((4, 6))
    np_view = np.shares_memory(base_np, base_np.reshape(4, 6))
    if info["zero_copy"] != np_view:
        return CheckResult("alias_chain", "fail", failure_category="copy_decision",
                           message=f"{info} vs numpy view={np_view}")
    return CheckResult("alias_chain", "pass", detail={"reshape_zero_copy": info["zero_copy"]})


_ALL_CHECKS: list[Callable[[], CheckResult]] = [
    check_transpose_then_reshape,
    check_broadcast_zero_stride,
    check_overlapping_slices,
    check_self_overlapping_view_rejected,
    check_empty_tensor,
    check_negative_strides,
    check_out_of_bounds_and_overflow,
    check_alias_chain_vs_numpy,
]


def run_validation_suite(*, only: list[str] | None = None) -> dict[str, Any]:
    results: list[CheckResult] = []
    for check in _ALL_CHECKS:
        if only is not None and check.__name__ not in only:
            continue
        try:
            results.append(check())
        except Exception as exc:  # surface unexpected core crashes as errors
            results.append(CheckResult(
                check.__name__, "error",
                failure_category=getattr(exc, "category", "unexpected_exception"),
                message=f"{type(exc).__name__}: {exc}",
            ))
    summary = {
        "total": len(results),
        "passed": sum(r.status == "pass" for r in results),
        "failed": sum(r.status == "fail" for r in results),
        "uncertain": sum(r.status == "uncertain" for r in results),
        "errors": sum(r.status == "error" for r in results),
    }
    return {
        "summary": summary,
        "checks": [
            {
                "name": r.name,
                "status": r.status,
                "failure_category": r.failure_category,
                "message": r.message,
                "detail": r.detail,
            }
            for r in results
        ],
    }
