"""Overlap and unreachability diagnostics over the combined DFA.

Both diagnostics are computed with a single breadth-first traversal of the
combined DFA (the determinised union of all rule NFAs):

- **Overlap witness.** The DFA state reached by a string ``w`` accepts exactly
  the set of rules whose patterns match ``w``. The first time (in BFS order)
  a state accepts both rule ``i`` and rule ``j`` we therefore hold the
  shortest common string — the shortest overlap witness. Transitions are
  visited in ascending character order, so among shortest witnesses the
  lexicographically smallest one is found, making the output deterministic.

- **Unreachable rules.** A rule wins the token for input ``w`` (followed by
  end-of-input) iff it is in the accept set of the state reached by ``w`` and
  no rule with a better ``(priority, declaration index)`` is. A rule that
  wins in no reachable DFA state can never be selected by the lexer and is
  reported as unreachable.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from itertools import combinations
from typing import Any

from .dfa import DFA


@dataclass(frozen=True)
class RuleMeta:
    index: int
    name: str
    pattern: str
    priority: int


@dataclass(frozen=True)
class Overlap:
    rule_a: str
    rule_b: str
    witness: str


@dataclass(frozen=True)
class Diagnostics:
    overlaps: tuple[Overlap, ...]
    unreachable: tuple[str, ...]
    unreachable_reasons: dict[str, str]
    win_witness: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "overlaps": [
                {"rule_a": o.rule_a, "rule_b": o.rule_b, "witness": o.witness}
                for o in self.overlaps
            ],
            "unreachable": list(self.unreachable),
            "unreachable_reasons": dict(self.unreachable_reasons),
            "win_witness": dict(self.win_witness),
        }


def _rank(rules: tuple[RuleMeta, ...], rule_index: int) -> tuple[int, int]:
    return (rules[rule_index].priority, rule_index)


def _reconstruct(parent: dict[int, tuple[int, int] | None], state: int) -> str:
    chars: list[str] = []
    while parent[state] is not None:
        prev, lo = parent[state]  # type: ignore[misc]
        chars.append(chr(lo))
        state = prev
    chars.reverse()
    return "".join(chars)


def compute_diagnostics(dfa: DFA, rules: tuple[RuleMeta, ...]) -> Diagnostics:
    parent: dict[int, tuple[int, int] | None] = {dfa.start: None}
    queue: deque[int] = deque([dfa.start])
    pair_witness: dict[tuple[int, int], str] = {}
    win_state: dict[int, int] = {}

    while queue:
        state = queue.popleft()
        accepts = dfa.accepts[state]
        if accepts:
            winner = min(accepts, key=lambda idx: _rank(rules, idx))
            if winner not in win_state:
                win_state[winner] = state
            for i, j in combinations(sorted(accepts), 2):
                if (i, j) not in pair_witness:
                    pair_witness[(i, j)] = _reconstruct(parent, state)
        for lo, _hi, target in dfa.transitions[state]:
            if target not in parent:
                parent[target] = (state, lo)
                queue.append(target)

    overlaps = tuple(
        Overlap(rules[i].name, rules[j].name, witness)
        for (i, j), witness in sorted(pair_witness.items())
    )
    unreachable: list[str] = []
    reasons: dict[str, str] = {}
    for rule in rules:
        if rule.index not in win_state:
            unreachable.append(rule.name)
            reasons[rule.name] = (
                f"rule {rule.name!r} can never be selected: every string it "
                "matches is also matched by a rule with higher or equal "
                "priority (longest match first, then explicit priority), so "
                "it wins in no reachable automaton state"
            )
    win_witness = {
        rules[idx].name: _reconstruct(parent, state)
        for idx, state in sorted(win_state.items())
    }
    return Diagnostics(
        overlaps=overlaps,
        unreachable=tuple(unreachable),
        unreachable_reasons=reasons,
        win_witness=win_witness,
    )
