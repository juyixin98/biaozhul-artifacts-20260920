"""Canonical state representation and deduplicated encoding.

A state is a frozen set of ground :class:`~strips_planner.models.Atom`. Search
deduplicates states by a compact integer bitmask produced by a
:class:`StateEncoder` with a fixed atom ordering, so two structurally equal
states reached on different paths always share one identity. The same encoder
also yields stable, replayable string encodings for evidence storage.
"""

from __future__ import annotations

from typing import Iterator

from strips_planner.models import Atom, GroundAction, Problem

State = frozenset[Atom]


class StateEncoder:
    """Bidirectional, deterministic mapping between atoms and bit positions."""

    def __init__(self, problem: Problem) -> None:
        atoms = sorted(
            {a for action in problem.ground_actions
             for group in (action.pre_pos, action.pre_neg, action.add, action.delete)
             for a in group}
            | set(problem.init)
            | set(problem.goal_pos)
            | set(problem.goal_neg),
            key=_atom_key,
        )
        self._index: dict[Atom, int] = {atom: i for i, atom in enumerate(atoms)}
        self._atoms: tuple[Atom, ...] = tuple(atoms)

    @property
    def size(self) -> int:
        return len(self._atoms)

    def encode(self, state: State) -> int:
        code = 0
        for atom in state:
            idx = self._index.get(atom)
            if idx is not None:
                code |= 1 << idx
        return code

    def decode(self, code: int) -> State:
        return frozenset(
            self._atoms[i] for i in range(len(self._atoms)) if code & (1 << i)
        )

    def render(self, state: State) -> list[str]:
        return [str(a) for a in sorted(state, key=_atom_key)]


def initial_state(problem: Problem) -> State:
    return frozenset(problem.init)


def goal_satisfied(problem: Problem, state: State) -> bool:
    return problem.goal_pos <= state and not (problem.goal_neg & state)


def missing_goal_atoms(problem: Problem, state: State) -> list[str]:
    missing = [f"+{a}" for a in sorted(problem.goal_pos - state, key=_atom_key)]
    present = [f"-{a}" for a in sorted(problem.goal_neg & state, key=_atom_key)]
    return missing + present


def applicable(action: GroundAction, state: State) -> bool:
    return action.pre_pos <= state and not (action.pre_neg & state)


def apply_action(action: GroundAction, state: State) -> State:
    """STRIPS transition.

    Both effects are decided from the *same* predecessor ``state`` and the
    conflict policy is fixed: delete first, then add. The language validator
    forbids an action from adding and deleting the same ground atom, so the
    union cannot surprise either way; add-wins is the declared tie-break.
    """
    return frozenset((set(state) - set(action.delete)) | set(action.add))


def successors(problem: Problem, state: State) -> Iterator[tuple[GroundAction, State, float]]:
    for action in problem.ground_actions:
        if applicable(action, state):
            yield action, apply_action(action, state), action.cost


def _atom_key(atom: Atom) -> tuple[str, tuple[str, ...]]:
    return atom.predicate, atom.args
