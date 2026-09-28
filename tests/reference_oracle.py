"""Independent dense reference oracle (test-only, deliberately separate).

This is NOT built on the code under test. It is a from-scratch, small-vocabulary
dense implementation written in plain Python (dict aggregation, explicit
per-element loops) using a different mechanism than the NumPy sparse core:

* aggregation  : Python ``dict`` accumulation in input order
* clipping     : explicit closed-form scales computed by hand
* optimiser    : dense nested-list momentum SGD

Its only shared inputs with the system under test are the *initial table
values* (captured from the freshly seeded state) and the scalar hyper
parameters. Every subsequent number is computed independently. Tests then
assert the sparse NumPy implementation and this dense oracle agree after the
same sequence of batches. Because the oracle is dense, "untouched rows" are
naturally held by simply never visiting them, which gives an independent
guarantee on the untouched-row rule.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional, Sequence


@dataclass
class DenseOracleResult:
    global_step_before: int
    global_step_after: int
    active: List[int] = field(default_factory=list)
    zero_skipped: List[int] = field(default_factory=list)
    global_norm: float = 0.0
    scales: List[float] = field(default_factory=list)


class DenseReferenceOracle:
    """Dense momentum-SGD with explicit clipping and zero-row rules."""

    def __init__(
        self,
        initial_weights: Sequence[Sequence[float]],
        *,
        lr: float,
        momentum: float = 0.0,
        weight_decay: float = 0.0,
        clip_mode: str = "none",  # "none" | "global" | "row"
        max_norm: Optional[float] = None,
    ) -> None:
        if clip_mode not in ("none", "global", "row"):
            raise ValueError(f"unknown clip_mode {clip_mode}")
        if clip_mode != "none" and (max_norm is None or max_norm <= 0):
            raise ValueError("clipping needs a positive max_norm")

        self.n = len(initial_weights)
        self.dim = len(initial_weights[0]) if self.n else 0
        # Independent dense copies.
        self.w: List[List[float]] = [list(map(float, row)) for row in initial_weights]
        self.v: List[List[float]] = [[0.0] * self.dim for _ in range(self.n)]
        self.row_steps: List[int] = [0] * self.n
        self.global_step = 0

        self.lr = float(lr)
        self.momentum = float(momentum)
        self.weight_decay = float(weight_decay)
        self.clip_mode = clip_mode
        self.max_norm = float(max_norm) if max_norm is not None else None
        # momentum == 0 means *plain* SGD, whose buffer is conceptually always
        # zero (mirrors the production rule that returns a zero buffer).
        self.plain_sgd = self.momentum == 0.0

    # ------------------------------------------------------------- one batch

    def run_batch(
        self, indices: Sequence[int], values: Sequence[Sequence[float]]
    ) -> DenseOracleResult:
        before = self.global_step
        result = DenseOracleResult(global_step_before=before, global_step_after=before)

        if len(indices) == 0:
            # Explicit empty-batch no-op: no counters move.
            return result

        # --- independent validation of index range (dense) ------------------
        for idx in indices:
            if idx < 0 or idx >= self.n:
                raise IndexError(f"oracle: index {idx} out of range [0,{self.n})")
        if len(indices) != len(values):
            raise ValueError("oracle: index/value length mismatch")

        # --- duplicate aggregation with a plain dict ------------------------
        agg: dict[int, List[float]] = {}
        for idx, row in zip(indices, values):
            acc = agg.get(idx)
            if acc is None:
                acc = [0.0] * self.dim
                agg[idx] = acc
            for j in range(self.dim):
                acc[j] += float(row[j])

        keys = sorted(agg.keys())

        # --- norms, computed independently from the NumPy path --------------
        sq_total = 0.0
        row_norms: List[float] = []
        for k in keys:
            s = 0.0
            for x in agg[k]:
                s += x * x
            rn = math.sqrt(s)
            row_norms.append(rn)
            sq_total += s
        global_norm = math.sqrt(sq_total)
        result.global_norm = global_norm

        # --- clipping: exactly one mode, explicit formula -------------------
        if self.clip_mode == "none":
            scales = [1.0] * len(keys)
        elif self.clip_mode == "global":
            scales = [
                1.0 if global_norm <= self.max_norm else self.max_norm / global_norm
            ] * len(keys)
        else:  # row
            scales = []
            for rn in row_norms:
                scales.append(1.0 if rn <= 0.0 or rn <= self.max_norm else self.max_norm / rn)
        result.scales = scales

        # --- optimiser step on non-zero rows only ---------------------------
        for pos, k in enumerate(keys):
            scale = scales[pos]
            g = [agg[k][j] * scale for j in range(self.dim)]
            is_zero = all(x == 0.0 for x in g)
            if is_zero:
                # Zero aggregated gradient: NO step, momentum/counter untouched.
                result.zero_skipped.append(k)
                continue

            result.active.append(k)
            wk = self.w[k]
            vk = self.v[k]
            for j in range(self.dim):
                gj = g[j] + self.weight_decay * wk[j]
                if self.plain_sgd:
                    # No momentum history is retained; step uses g directly.
                    vk[j] = 0.0
                    wk[j] = wk[j] - self.lr * gj
                else:
                    vk[j] = self.momentum * vk[j] + gj
                    wk[j] = wk[j] - self.lr * vk[j]
            self.row_steps[k] += 1

        # A non-empty batch always advances the round counter by exactly one.
        self.global_step += 1
        result.global_step_after = self.global_step
        return result

    # ----------------------------------------------------------------- views

    def weights_matrix(self) -> List[List[float]]:
        return [row[:] for row in self.w]

    def momentum_matrix(self) -> List[List[float]]:
        return [row[:] for row in self.v]
