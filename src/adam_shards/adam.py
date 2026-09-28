"""Adam optimizer and training state.

State is keyed by stable parameter name. For every parameter the first moment
``m``, second moment ``v`` and per-parameter ``step`` live in one record, so the
three can never be split or paired with the wrong parameter during
resharding.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import numpy as np


@dataclass(frozen=True)
class AdamConfig:
    lr: float = 0.001
    beta1: float = 0.9
    beta2: float = 0.999
    eps: float = 1e-8

    def validate(self) -> None:
        if self.lr <= 0:
            raise ValueError("lr must be positive")
        if not 0.0 <= self.beta1 < 1.0:
            raise ValueError("beta1 must lie in [0, 1)")
        if not 0.0 <= self.beta2 < 1.0:
            raise ValueError("beta2 must lie in [0, 1)")
        if self.eps <= 0:
            raise ValueError("eps must be positive")


@dataclass(frozen=True)
class MomentRecord:
    """The optimizer triple that must stay aligned with one parameter."""

    m: np.ndarray
    v: np.ndarray
    step: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "m", np.ascontiguousarray(self.m, dtype=np.float64))
        object.__setattr__(self, "v", np.ascontiguousarray(self.v, dtype=np.float64))
        if self.m.shape != self.v.shape:
            raise ValueError("m and v shape differ")
        if int(self.step) < 0:
            raise ValueError("step must be non-negative")
        object.__setattr__(self, "step", int(self.step))


class OptimState:
    """Name-keyed, shape-checked collection of :class:`MomentRecord`."""

    def __init__(self, shapes: dict[str, tuple[int, ...]]):
        self._records: dict[str, MomentRecord] = {
            name: MomentRecord(np.zeros(shape), np.zeros(shape), 0)
            for name, shape in shapes.items()
        }

    @property
    def names(self) -> list[str]:
        return sorted(self._records)

    def __contains__(self, name: str) -> bool:
        return name in self._records

    def get(self, name: str) -> MomentRecord:
        return self._records[name]

    def set(self, name: str, m: np.ndarray, v: np.ndarray, step: int) -> None:
        if name not in self._records:
            raise KeyError(f"unknown parameter {name!r}")
        record = MomentRecord(m, v, step)
        if record.m.shape != self._records[name].m.shape:
            raise ValueError(f"shape mismatch setting state for {name!r}")
        self._records[name] = record

    def global_step(self) -> int:
        steps = {r.step for r in self._records.values()}
        if len(steps) != 1:
            raise ValueError(f"per-parameter steps disagree: {sorted(steps)}")
        return next(iter(steps))

    def snapshot(self) -> dict[str, MomentRecord]:
        return {
            name: MomentRecord(r.m.copy(), r.v.copy(), r.step)
            for name, r in self._records.items()
        }

    def load_snapshot(self, snap: dict[str, MomentRecord]) -> None:
        if set(snap) != set(self._records):
            missing = sorted(set(self._records) - set(snap))
            unknown = sorted(set(snap) - set(self._records))
            raise KeyError(f"state snapshot mismatch missing={missing} unknown={unknown}")
        for name, rec in snap.items():
            self.set(name, rec.m, rec.v, rec.step)

    def select(self, names: Iterable[str]) -> dict[str, MomentRecord]:
        return {n: self._records[n] for n in names}


class Adam:
    """Bias-corrected Adam. Mutates model params in place; returns nothing."""

    def __init__(self, config: AdamConfig) -> None:
        config.validate()
        self.config = config

    def step(
        self,
        params: dict[str, np.ndarray],
        state: OptimState,
        grads: dict[str, np.ndarray],
    ) -> None:
        cfg = self.config
        if set(params) != set(grads) or set(params) != set(state.names):
            raise KeyError("params / grads / state name sets differ")
        for name in state.names:
            p, g = params[name], grads[name]
            if p.shape != g.shape:
                raise ValueError(f"gradient shape mismatch for {name!r}")
            rec = state.get(name)
            m = cfg.beta1 * rec.m + (1.0 - cfg.beta1) * g
            v = cfg.beta2 * rec.v + (1.0 - cfg.beta2) * (g * g)
            t = rec.step + 1
            m_hat = m / (1.0 - cfg.beta1**t)
            v_hat = v / (1.0 - cfg.beta2**t)
            params[name] = p - cfg.lr * m_hat / (np.sqrt(v_hat) + cfg.eps)
            state.set(name, m, v, t)
