"""Thompson NFA construction over range-labelled transitions.

Transitions carry inclusive code-point ranges so the whole Unicode alphabet
can be represented without per-character edges. ``accepts`` maps an accepting
state to the set of rule indices that accept there (a combined NFA for a whole
ruleset shares one start state).
"""

from __future__ import annotations

from ..errors import resource_exhausted
from .ast_nodes import Alt, Char, Concat, Epsilon, Node, Opt, Plus, Repeat, Star


class NFA:
    def __init__(self, state_limit: int) -> None:
        self._state_limit = state_limit
        self.eps: list[list[int]] = []
        self.ranges: list[list[tuple[int, int, int]]] = []
        self.accepts: dict[int, frozenset[int]] = {}

    @property
    def state_count(self) -> int:
        return len(self.eps)

    def new_state(self) -> int:
        if len(self.eps) >= self._state_limit:
            raise resource_exhausted(
                f"NFA state limit {self._state_limit} exceeded while expanding "
                "the pattern (nested repeats can explode the automaton)"
            )
        self.eps.append([])
        self.ranges.append([])
        return len(self.eps) - 1

    def add_eps(self, src: int, dst: int) -> None:
        self.eps[src].append(dst)

    def add_range(self, src: int, lo: int, hi: int, dst: int) -> None:
        self.ranges[src].append((lo, hi, dst))

    def build(self, node: Node) -> tuple[int, int]:
        """Build a fragment for ``node``; return its (start, end) states."""
        if isinstance(node, Char):
            start, end = self.new_state(), self.new_state()
            for lo, hi in node.ranges:
                self.add_range(start, lo, hi, end)
            return start, end
        if isinstance(node, Epsilon):
            start, end = self.new_state(), self.new_state()
            self.add_eps(start, end)
            return start, end
        if isinstance(node, Concat):
            first_start: int | None = None
            prev_end = -1
            for part in node.parts:
                start, end = self.build(part)
                if first_start is None:
                    first_start = start
                else:
                    self.add_eps(prev_end, start)
                prev_end = end
            assert first_start is not None
            return first_start, prev_end
        if isinstance(node, Alt):
            start, end = self.new_state(), self.new_state()
            for option in node.options:
                opt_start, opt_end = self.build(option)
                self.add_eps(start, opt_start)
                self.add_eps(opt_end, end)
            return start, end
        if isinstance(node, Star):
            start, end = self.new_state(), self.new_state()
            child_start, child_end = self.build(node.node)
            self.add_eps(start, child_start)
            self.add_eps(start, end)
            self.add_eps(child_end, child_start)
            self.add_eps(child_end, end)
            return start, end
        if isinstance(node, Plus):
            start, end = self.new_state(), self.new_state()
            child_start, child_end = self.build(node.node)
            self.add_eps(start, child_start)
            self.add_eps(child_end, child_start)
            self.add_eps(child_end, end)
            return start, end
        if isinstance(node, Opt):
            start, end = self.new_state(), self.new_state()
            child_start, child_end = self.build(node.node)
            self.add_eps(start, child_start)
            self.add_eps(start, end)
            self.add_eps(child_end, end)
            return start, end
        if isinstance(node, Repeat):
            return self._build_repeat(node)
        raise TypeError(f"unknown AST node: {node!r}")

    def _build_repeat(self, node: Repeat) -> tuple[int, int]:
        start = self.new_state()
        cursor = start
        for _ in range(node.min):
            child_start, child_end = self.build(node.node)
            self.add_eps(cursor, child_start)
            cursor = child_end
        end = self.new_state()
        if node.max is None:
            child_start, child_end = self.build(node.node)
            self.add_eps(cursor, child_start)
            self.add_eps(cursor, end)
            self.add_eps(child_end, child_start)
            self.add_eps(child_end, end)
            return start, end
        for _ in range(node.max - node.min):
            child_start, child_end = self.build(node.node)
            self.add_eps(cursor, child_start)
            self.add_eps(cursor, end)  # skip the remaining optional copies
            cursor = child_end
        self.add_eps(cursor, end)
        return start, end
