"""STRIPS transition semantics.

Fixed rules (do not change without a contract version bump):

1. Applicability is judged against a *single* predecessor state.
2. Positive preconditions hold iff every atom is in the state; negative
   preconditions hold iff every atom is absent (closed world).
3. The successor is computed in one shot from that predecessor::

       s' = (s - delete) | add

   Deletes and adds are never applied as sequential mutations of an
   evolving state; add/delete sets of a ground action are disjoint
   (enforced at grounding time), so add-wins vs delete-wins cannot differ.
"""

from __future__ import annotations

from .errors import PRECONDITION_FAILED, StateConflictError
from .model import GroundAction, State


def applicable(state: State, action: GroundAction) -> bool:
    return action.pre_pos <= state and not (action.pre_neg & state)


def unsatisfied_preconditions(
    state: State, action: GroundAction
) -> tuple[list[str], list[str]]:
    missing_pos = sorted(action.pre_pos - state)
    present_neg = sorted(action.pre_neg & state)
    return _texts(missing_pos), _texts(present_neg)


def apply(state: State, action: GroundAction) -> State:
    """Return the successor state; raise on a precondition conflict.

    Raises:
        StateConflictError: the action is not applicable in ``state``.
    """
    if not applicable(state, action):
        missing_pos, present_neg = unsatisfied_preconditions(state, action)
        raise StateConflictError(
            f"action {action.signature} is not applicable",
            code=PRECONDITION_FAILED,
            details=[
                {"action": action.signature,
                 "missing_positive": missing_pos,
                 "present_negative": present_neg},
            ],
        )
    return frozenset((set(state) - set(action.del_effects)) | set(action.add_effects))


def goal_satisfied(
    state: State, goal_pos: frozenset, goal_neg: frozenset
) -> bool:
    return goal_pos <= state and not (goal_neg & state)


def _texts(atoms) -> list[str]:
    from .model import atom_text

    return [atom_text(a) for a in atoms]
