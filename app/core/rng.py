"""Deterministic random state used by stochastic ops (dropout).

The recomputation contract requires that a node re-executed during the
backward pass draws *exactly* the same mask it drew during the forward pass.
We avoid relying on global numpy RNG state: every stochastic draw is made
through a :class:`RandomStream` seeded per graph, and the full state of that
stream is captured into the forward trace and restored before a replay.

Two replay strategies are supported and both are checked by the tests:

* ``strategy="snapshot"`` -- the stream's bit state is snapshotted before the
  first forward and restored before every replay segment (true RNG state
  replay, independent of op counts).
* ``strategy="counter"``  -- each stochastic node is keyed by its node id and
  draws from a per-key ``np.random.Generator`` derived from the master seed,
  so replay of node ``i`` always yields node ``i``'s mask regardless of how
  many nodes ran in between.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

import numpy as np

from .errors import StateConflictError

MASTER_SEED_MIN = 0
MASTER_SEED_MAX = 2**32 - 1


class RandomStream:
    """Owns the RNG for one graph execution and side-effect counters."""

    def __init__(self, master_seed: int, strategy: str = "snapshot") -> None:
        if not isinstance(master_seed, int) or isinstance(master_seed, bool):
            raise TypeError("master_seed must be an int")
        if not MASTER_SEED_MIN <= master_seed <= MASTER_SEED_MAX:
            raise ValueError(
                f"master_seed must be in [{MASTER_SEED_MIN}, {MASTER_SEED_MAX}]"
            )
        if strategy not in ("snapshot", "counter"):
            raise ValueError(f"unknown rng strategy: {strategy!r}")
        self.master_seed = master_seed
        self.strategy = strategy
        self._ss = np.random.RandomState(master_seed)
        self._keyed: Dict[str, np.random.Generator] = {}
        # External side effects are *not* replayed; this counter proves it.
        self.external_emit_count = 0

    # -- state capture / restore ---------------------------------------

    def snapshot(self) -> object:
        if self.strategy == "snapshot":
            return ("ss", self._ss.get_state())
        return ("counter", dict(self._keyed_state()))

    def _keyed_state(self) -> Dict[str, object]:
        return {k: g.bit_generator.state for k, g in self._keyed.items()}

    def restore(self, state: object) -> None:
        kind, payload = state  # type: ignore[misc]
        if self.strategy == "snapshot":
            if kind != "ss":
                raise StateConflictError(
                    "rng state kind does not match stream strategy",
                    code="E_RNG_STATE_KIND",
                    context={"expected": "ss", "got": kind},
                )
            self._ss.set_state(payload)
        else:
            if kind != "counter":
                raise StateConflictError(
                    "rng state kind does not match stream strategy",
                    code="E_RNG_STATE_KIND",
                    context={"expected": "counter", "got": kind},
                )
            self._keyed = {}
            for key, bit_state in payload.items():  # type: ignore[union-attr]
                bg = np.random.default_rng(0).bit_generator
                bg.state = bit_state
                self._keyed[key] = np.random.Generator(bg)

    # -- draws -----------------------------------------------------------

    def dropout_mask(self, node_id: str, shape: tuple, p: float, *,
                     replay: bool = False) -> np.ndarray:
        """Bernoulli keep-mask, scaled by ``1/(1-p)`` (inverted dropout).

        ``replay=True`` under the counter strategy recreates the node's
        generator from its fixed seed, so the first draw reproduces the
        forward mask regardless of generator state.
        """

        keep = 1.0 - p
        if self.strategy == "snapshot":
            r = self._ss
            mask = (r.random_sample(shape) < keep).astype(np.float64) / keep
        else:
            if replay:
                self._keyed.pop(node_id, None)
            gen = self._keyed.get(node_id)
            if gen is None:
                gen = np.random.default_rng(
                    np.random.SeedSequence(
                        [self.master_seed, _hash_node_id(node_id)]
                    )
                )
                self._keyed[node_id] = gen
            mask = (gen.random(shape) < keep).astype(np.float64) / keep
        return mask

    def draw_dummy(self, shape: tuple, p: float) -> None:
        """Advance a snapshot stream exactly as one dropout draw would.

        Used to skip past stochastic draws that happened before the node
        currently being replayed. Only valid for the snapshot strategy.
        """

        if self.strategy != "snapshot":
            raise StateConflictError(
                "draw_dummy is only valid for the snapshot strategy",
                code="E_RNG_DUMMY_STRATEGY",
            )
        self._ss.random_sample(shape)

    # -- side effects -----------------------------------------------------

    def note_external_emit(self) -> None:
        """Account a non-idempotent external side effect (e.g. a log push)."""

        self.external_emit_count += 1


def _hash_node_id(node_id: str) -> int:
    # Stable across processes (do not use Python's salted hash).
    import hashlib

    return int.from_bytes(hashlib.sha256(node_id.encode()).digest()[:4], "big")


@dataclass(frozen=True)
class ReplayDecision:
    """Result of deciding whether an op's side effect may run during replay."""

    may_emit: bool
    reason: str
