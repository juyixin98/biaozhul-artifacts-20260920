"""Chunked / streaming merge algorithms.

The point of this module is to show what *must* be carried between blocks
when a sequence is processed in pieces:

* naive summation has no state beyond the running total;
* Kahan summation must carry the compensation pair ``(s, c)`` - simply
  summing per-block totals discards ``c`` and silently degrades the result
  back towards naive accuracy;
* pairwise summation keeps every block's local total and merges them with
  a balanced tree instead of folding them left-to-right.

Two distributed Kahan variants are provided because they correspond to two
different deployment shapes:

* :func:`blocked_kahan_streaming` - one compensation thread continued across
  blocks (bit-identical to a monolithic Kahan pass);
* :func:`blocked_kahan_merged` - independent workers, each producing a
  normalised double-double ``(hi, lo)`` state, combined with an error-free
  TwoSum transform (Knuth/``Dekker``).  The merge keeps the low-order word
  of every shard; a plain ``total += shard_total`` throws it away.

:func:`naive_sharded_totals` is included deliberately as the *anti-pattern*
baseline: independent block totals folded with ordinary float adds.  It is
not labelled a compensated algorithm anywhere.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .kernels import SpecialAssessment, assess, resolve_special, signed_zero_for


# ---------------------------------------------------------------------------
# Error-free transforms for double-double arithmetic (Knuth 1969; Dekker 1971).
# a + b is represented exactly as the pair (hi, lo) with hi = fl(a+b).
# ---------------------------------------------------------------------------

def two_sum(a: float, b: float) -> tuple[float, float]:
    """Knuth's error-free sum: ``a + b == hi + lo`` exactly."""
    hi = a + b
    b_virtual = hi - a
    lo = (a - (hi - b_virtual)) + (b - b_virtual)
    return hi, lo


def fast_two_sum(a: float, b: float) -> tuple[float, float]:
    """Error-free sum requiring ``|a| >= |b|``."""
    hi = a + b
    lo = b - (hi - a)
    return hi, lo


def dd_add(x: tuple[float, float], y: tuple[float, float]) -> tuple[float, float]:
    """Add two double-double pairs, renormalised (Dekker, 6 flops + 2)."""
    hi, e = two_sum(x[0], y[0])
    e = e + x[1]
    e = e + y[1]
    return fast_two_sum(hi, e)


@dataclass
class KahanState:
    """Kahan accumulator.

    ``s`` is the running sum and ``c`` the lost-low-bits compensation.
    """

    s: float = 0.0
    c: float = 0.0

    def add_elements(self, block: np.ndarray) -> None:
        """Continue one Kahan pass over ``block`` using the current state."""
        s, c = self.s, self.c
        for x in block.tolist():
            y = x - c
            t = s + y
            c = (t - s) - y
            s = t
        self.s, self.c = s, c

    def value(self) -> float:
        return self.s + self.c

    def as_double_double(self) -> tuple[float, float]:
        """Normalise ``(s, c)`` into an exact double-double of ``s + c``."""
        return two_sum(self.s, self.c)

    def merge(self, other: "KahanState") -> None:
        """Merge an independent shard's state in double-double precision.

        Both states are normalised with an error-free transform and combined
        with :func:`dd_add`, so the other shard's compensation word is
        retained instead of being rounded away by a plain ``s += other.s``.
        """
        hi, lo = dd_add(self.as_double_double(), other.as_double_double())
        self.s, self.c = hi, lo


def _canonicalize(result: float, info: SpecialAssessment) -> float:
    if result == 0.0:
        return signed_zero_for(info)
    return result


def blocked_naive(values: np.ndarray, block_size: int) -> float:
    """Naive summation driven block-by-block with one running total.

    The running total is the only state, so blocking cannot (and does not)
    change the result - the assertion ``blocked_naive == naive`` holds
    bit-for-bit and is checked in the service invariants.
    """
    info = assess(values)
    special = resolve_special(info)
    if special is not None:
        return special
    total = 0.0
    for start in range(0, values.size, block_size):
        for x in values[start : start + block_size].tolist():
            total += x
    return _canonicalize(total, info)


def naive_sharded_totals(values: np.ndarray, block_size: int) -> float:
    """ANTI-PATTERN baseline: sum each block independently, add the totals.

    This is what "just add the local results" actually computes - no
    compensation state crosses the block boundary.  It is exposed so tests
    and reports can demonstrate the accuracy loss versus
    :func:`blocked_kahan_merged` on identical block boundaries.
    """
    info = assess(values)
    special = resolve_special(info)
    if special is not None:
        return special
    total = 0.0
    for start in range(0, values.size, block_size):
        local = 0.0
        for x in values[start : start + block_size].tolist():
            local += x
        total += local
    return _canonicalize(total, info)


def blocked_kahan_streaming(values: np.ndarray, block_size: int) -> float:
    """Kahan compensation threaded continuously through every block.

    The same :class:`KahanState` is updated block after block.  The result
    is bit-identical to a single monolithic Kahan pass regardless of
    ``block_size``; tests assert that invariant.
    """
    info = assess(values)
    special = resolve_special(info)
    if special is not None:
        return special
    state = KahanState()
    for start in range(0, values.size, block_size):
        state.add_elements(values[start : start + block_size])
    return _canonicalize(state.value(), info)


def blocked_kahan_merged(values: np.ndarray, block_size: int) -> float:
    """Independent per-block Kahan states merged as double-doubles.

    Models sharded workers: each block accumulates its own ``(s, c)``
    state independently, states are normalised via error-free TwoSum and
    combined with :func:`dd_add`.  The result meets the compensated-order
    error bound but need not be bit-identical to one monolithic pass -
    the difference itself is evidence that carrying state (not just local
    totals) is what provides the accuracy.
    """
    info = assess(values)
    special = resolve_special(info)
    if special is not None:
        return special
    accumulator = (0.0, 0.0)
    for start in range(0, values.size, block_size):
        local = KahanState()
        local.add_elements(values[start : start + block_size])
        accumulator = dd_add(accumulator, local.as_double_double())
    return _canonicalize(accumulator[0] + accumulator[1], info)


def _balanced_merge(parts: list[float]) -> float:
    """Merge a list of partial sums in a balanced binary tree."""
    while len(parts) > 1:
        nxt: list[float] = []
        for i in range(0, len(parts) - 1, 2):
            nxt.append(parts[i] + parts[i + 1])
        if len(parts) % 2:
            nxt.append(parts[-1])
        parts = nxt
    return parts[0] if parts else 0.0


def blocked_pairwise(values: np.ndarray, block_size: int) -> float:
    """Per-block sequential totals merged in a balanced tree.

    The block size is the tree's leaf width: blocks are summed in input
    order and their totals are combined pairwise, so rounding error grows
    like ``O(log2(n/block_size))`` merge levels rather than ``O(n)``.
    """
    info = assess(values)
    special = resolve_special(info)
    if special is not None:
        return special
    partials = [
        _sequential_block(values[start : start + block_size])
        for start in range(0, values.size, block_size)
    ]
    return _canonicalize(_balanced_merge(partials), info)


def _sequential_block(block: np.ndarray) -> float:
    total = 0.0
    for x in block.tolist():
        total += x
    return total
