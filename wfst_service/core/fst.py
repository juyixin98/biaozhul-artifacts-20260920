"""Immutable weighted finite-state transducer model.

The kernel is intentionally dumb about corpora: it only knows states,
arcs, final weights and plain value types.  Higher level concepts
(lexica, character rules) live in :mod:`wfst_service.corpus`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite
from typing import Mapping

from ..corpus.symbols import EPS
from .errors import TopologyError


@dataclass(frozen=True, slots=True)
class Arc:
    """One transition.

    ``ilabel`` / ``olabel`` are single characters or :data:`EPS` ("").
    Input epsilon and output epsilon are distinct tape positions: an arc
    may carry exactly one of them (``(EPS, "x")`` insertion,
    ``("x", EPS)`` deletion) or both (``(EPS, EPS)`` silent).
    """

    src: int
    dst: int
    ilabel: str
    olabel: str
    cost: float


@dataclass(frozen=True, slots=True)
class Fst:
    """Immutable WFST.

    States are dense integer ids ``0 .. num_states-1``.  ``finals`` maps
    accepting states to their final (terminal) cost.
    """

    name: str
    num_states: int
    start: int
    finals: Mapping[int, float]
    arcs: tuple[Arc, ...]
    _adj: Mapping[int, tuple[Arc, ...]] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.num_states <= 0:
            raise TopologyError(f"fst {self.name!r}: needs at least one state")
        if not (0 <= self.start < self.num_states):
            raise TopologyError(
                f"fst {self.name!r}: start state {self.start} out of range"
            )
        adj: dict[int, list[Arc]] = {s: [] for s in range(self.num_states)}
        for arc in self.arcs:
            if not (0 <= arc.src < self.num_states and 0 <= arc.dst < self.num_states):
                raise TopologyError(
                    f"fst {self.name!r}: arc {arc.src}->{arc.dst} out of range"
                )
            if not isfinite(arc.cost):
                raise TopologyError(
                    f"fst {self.name!r}: non-finite arc cost {arc.cost}"
                )
            adj[arc.src].append(arc)
        for s, w in self.finals.items():
            if not (0 <= s < self.num_states):
                raise TopologyError(
                    f"fst {self.name!r}: final state {s} out of range"
                )
            if not isfinite(w):
                raise TopologyError(
                    f"fst {self.name!r}: non-finite final cost {w}"
                )
        if not self.finals:
            # An empty final set denotes the empty language; the
            # specification layer forbids it in user documents, but
            # internal compositions of disjoint middle tapes produce it.
            pass
        frozen_adj = {s: tuple(outs) for s, outs in adj.items()}
        object.__setattr__(self, "_adj", frozen_adj)

    def outgoing(self, state: int) -> tuple[Arc, ...]:
        return self._adj[state]

    def is_final(self, state: int) -> bool:
        return state in self.finals

    def final_cost(self, state: int) -> float:
        return self.finals[state]

    @classmethod
    def create(
        cls,
        name: str,
        num_states: int,
        start: int,
        finals: Mapping[int, float],
        arcs: tuple[Arc, ...] | list[Arc],
    ) -> "Fst":
        return cls(name, num_states, start, dict(finals), tuple(arcs))

    @classmethod
    def acceptor(cls, text: str, name: str = "<acceptor>") -> "Fst":
        """Linear identity acceptor for exactly ``text`` (all costs zero)."""
        arcs = tuple(
            Arc(i, i + 1, ch, ch, 0.0) for i, ch in enumerate(text)
        )
        return cls(name, len(text) + 1, 0, {len(text): 0.0}, arcs)
