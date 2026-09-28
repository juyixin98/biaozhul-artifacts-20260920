"""Grounding: replace schema parameters by problem objects.

A grounded problem bundles every applicable *instance* of every lifted
action. Instances whose add/delete sets intersect on the same concrete atom
are rejected as ``input_error`` (the fixed add/delete conflict rule,
enforced concretely in addition to the template-level check in validation).
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import permutations

from .errors import (
    EFFECT_ADD_DELETE_CONFLICT,
    GROUNDING_LIMIT,
    OBJECT_UNDECLARED,
    ResourceLimitError,
    ValidationError,
)
from .model import Atom, Domain, GroundAction, Problem, State
from .parser import is_variable

# Service-side safety valve; callers may pass a larger cap explicitly.
DEFAULT_GROUND_ACTIONS_LIMIT = 200_000


@dataclass(frozen=True)
class GroundProblem:
    domain: Domain
    problem: Problem
    actions: tuple[GroundAction, ...]
    initial: State
    goal_pos: frozenset[Atom]
    goal_neg: frozenset[Atom]


def ground(
    domain: Domain,
    problem: Problem,
    *,
    limit: int = DEFAULT_GROUND_ACTIONS_LIMIT,
) -> GroundProblem:
    objects = problem.objects
    if len(set(objects)) != len(objects):  # validation normally catches this
        raise ValidationError("problem.objects must be unique", code=OBJECT_UNDECLARED)

    grounded: list[GroundAction] = []
    seen_signatures: set[tuple[str, tuple[str, ...]]] = set()
    conflicts: list[str] = []

    for schema in domain.actions:
        n = len(schema.parameters)
        # Bound the combinatorial expansion before materialising permutations.
        est_copies = _factorial_bound(len(objects), n)
        if est_copies > limit and len(grounded) + est_copies > limit:
            raise ResourceLimitError(
                f"grounding action {schema.name!r} would produce up to "
                f"{est_copies} instances (limit {limit})",
                code=GROUNDING_LIMIT,
                details=[
                    {"action": schema.name, "estimated_instances": est_copies,
                     "limit": limit},
                ],
            )

        for binding in permutations(objects, n):
            instance = _instantiate(schema, binding)
            key = (instance.schema_name, instance.args)
            if key in seen_signatures:
                continue  # different bindings producing the same instance
            seen_signatures.add(key)

            if instance.add_effects & instance.del_effects:
                conflicts.append(instance.signature)
                continue
            grounded.append(instance)

            if len(grounded) > limit:
                raise ResourceLimitError(
                    f"grounding exceeded the limit of {limit} action instances",
                    code=GROUNDING_LIMIT,
                    details=[{"grounded": len(grounded), "limit": limit}],
                )

    if conflicts:
        raise ValidationError(
            f"{len(conflicts)} grounded action(s) add and delete the same "
            "atom, which the fixed conflict rule forbids",
            code=EFFECT_ADD_DELETE_CONFLICT,
            details=[{"action": sig} for sig in sorted(conflicts)],
        )

    return GroundProblem(
        domain=domain,
        problem=problem,
        actions=tuple(grounded),
        initial=problem.initial,
        goal_pos=problem.goal_pos,
        goal_neg=problem.goal_neg,
    )


def _instantiate(schema, binding: tuple[str, ...]) -> GroundAction:
    substitution = dict(zip(schema.parameters, binding))

    def substitute(atom: Atom) -> Atom:
        return tuple(_subst_token(t, substitution) for t in atom)

    args = tuple(substitution[p] for p in schema.parameters)
    return GroundAction(
        schema_name=schema.name,
        args=args,
        pre_pos=frozenset(substitute(a) for a in schema.pre_pos),
        pre_neg=frozenset(substitute(a) for a in schema.pre_neg),
        add_effects=frozenset(substitute(a) for a in schema.add_effects),
        del_effects=frozenset(substitute(a) for a in schema.del_effects),
        cost=schema.cost,
    )


def _subst_token(token: str, substitution: dict[str, str]) -> str:
    if is_variable(token):
        return substitution[token]
    return token  # schema-level constant


def _factorial_bound(n_objects: int, n_params: int) -> int:
    """n_objects permute n_params, guarded against huge intermediates."""
    if n_params > n_objects:
        return 0
    result = 1
    for step in range(n_params):
        result *= n_objects - step
    return result
