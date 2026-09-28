"""Determinisation of range-labelled NFAs.

The subset construction partitions the alphabet into cells induced by the
outgoing range boundaries of each NFA state-set, so transitions stay
range-labelled and the alphabet size (all of Unicode) does not matter.
"""

from __future__ import annotations

from collections import deque

from ..errors import resource_exhausted
from .nfa import NFA


class DFA:
    """A deterministic automaton with sorted, disjoint range transitions."""

    def __init__(self) -> None:
        self.transitions: list[list[tuple[int, int, int]]] = []
        self.accepts: list[frozenset[int]] = []

    @property
    def start(self) -> int:
        return 0

    @property
    def state_count(self) -> int:
        return len(self.accepts)

    def add_state(self, accepts: frozenset[int]) -> int:
        self.transitions.append([])
        self.accepts.append(accepts)
        return len(self.accepts) - 1


def _eps_closure(nfa: NFA, states: frozenset[int]) -> frozenset[int]:
    seen = set(states)
    stack = list(states)
    while stack:
        src = stack.pop()
        for dst in nfa.eps[src]:
            if dst not in seen:
                seen.add(dst)
                stack.append(dst)
    return frozenset(seen)


def determinize(nfa: NFA, start_states: frozenset[int], state_limit: int) -> DFA:
    dfa = DFA()
    start_closure = _eps_closure(nfa, start_states)
    index_of: dict[frozenset[int], int] = {}

    def register(closure: frozenset[int]) -> int:
        if closure in index_of:
            return index_of[closure]
        if len(index_of) >= state_limit:
            raise resource_exhausted(
                f"DFA state limit {state_limit} exceeded during determinisation"
            )
        accepts: set[int] = set()
        for state in closure:
            accepts.update(nfa.accepts.get(state, ()))
        state_id = dfa.add_state(frozenset(accepts))
        index_of[closure] = state_id
        return state_id

    register(start_closure)
    queue: deque[frozenset[int]] = deque([start_closure])
    while queue:
        subset = queue.popleft()
        src_id = index_of[subset]
        # Collect outgoing ranges and their boundary points.
        boundaries: set[int] = set()
        edges: list[tuple[int, int, int]] = []
        for state in subset:
            for lo, hi, target in nfa.ranges[state]:
                edges.append((lo, hi, target))
                boundaries.add(lo)
                boundaries.add(hi + 1)
        if not edges:
            continue
        ordered = sorted(boundaries)
        # Merge adjacent cells that lead to the same target set.
        cur_lo = cur_hi = -1
        cur_targets: frozenset[int] | None = None
        for cell_lo, cell_hi in zip(ordered, ordered[1:]):
            lo, hi = cell_lo, cell_hi - 1
            targets = frozenset(
                target for (e_lo, e_hi, target) in edges if e_lo <= lo and e_hi >= lo
            )
            if not targets:
                continue
            if cur_targets == targets and cur_hi == lo - 1:
                cur_hi = hi
                continue
            if cur_targets is not None:
                _emit(
                    dfa, nfa, src_id, cur_lo, cur_hi, cur_targets,
                    index_of, queue, register,
                )
            cur_lo, cur_hi, cur_targets = lo, hi, targets
        if cur_targets is not None:
            _emit(
                dfa, nfa, src_id, cur_lo, cur_hi, cur_targets,
                index_of, queue, register,
            )
    return dfa


def _emit(
    dfa: DFA,
    nfa: NFA,
    src_id: int,
    lo: int,
    hi: int,
    targets: frozenset[int],
    index_of: dict[frozenset[int], int],
    queue: "deque[frozenset[int]]",
    register: "callable",
) -> None:
    closure = _eps_closure(nfa, targets)
    is_new = closure not in index_of
    target_id = register(closure)
    if is_new:
        queue.append(closure)
    dfa.transitions[src_id].append((lo, hi, target_id))


def step(dfa: DFA, state: int, codepoint: int) -> int | None:
    """Follow the transition labelled with ``codepoint`` or return None."""
    for lo, hi, target in dfa.transitions[state]:
        if codepoint < lo:
            return None  # rows are sorted by lo
        if codepoint <= hi:
            return target
    return None
