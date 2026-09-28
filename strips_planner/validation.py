"""Semantic validation of parsed domains and problems.

Static checks enforced here (all reported as ``input_error``):

* object names unique; action names unique; parameters unique;
* every variable in an action literal is a declared parameter;
* predicate arity consistent across all actions, init and goal;
* no action requires both ``p`` and ``~p``;
* no action both adds and deletes the same atom template
  (fixed delete/add conflict rule);
* init and goal are ground and use declared objects;
* goal is not self-contradictory.
"""

from __future__ import annotations

from .errors import (
    ACTION_NAME_DUPLICATE,
    EFFECT_ADD_DELETE_CONFLICT,
    GOAL_CONTRADICTION,
    INIT_NOT_POSITIVE,
    OBJECT_DUPLICATE,
    OBJECT_UNDECLARED,
    PARAM_DUPLICATE,
    PREDICATE_ARITY_CONFLICT,
    PRECONDITION_CONTRADICTION,
    UNBOUND_VARIABLE,
    IssueList,
)
from .model import Atom, Domain, Problem
from .parser import is_variable


def validate(domain: Domain, problem: Problem) -> None:
    """Run every static check; raise ``ValidationError`` listing all issues."""
    issues = IssueList()
    _check_objects(problem, issues)
    arity = _build_arity(domain, problem, issues)
    _check_actions(domain, issues)
    _check_init_and_goal(problem, arity, issues)
    issues.raise_if_any()


def _check_objects(problem: Problem, issues: IssueList) -> None:
    seen: set[str] = set()
    for obj in problem.objects:
        if obj in seen:
            issues.add(
                OBJECT_DUPLICATE,
                "problem.objects",
                f"object {obj!r} is declared more than once",
                value=obj,
            )
        seen.add(obj)


def _build_arity(domain: Domain, problem: Problem, issues: IssueList) -> dict[str, int]:
    arity: dict[str, int] = {}

    def record(atom: Atom, source: str) -> None:
        n = len(atom) - 1
        if atom[0] in arity and arity[atom[0]] != n:
            issues.add(
                PREDICATE_ARITY_CONFLICT,
                source,
                f"predicate {atom[0]!r} used with arity {arity[atom[0]]} and {n}",
                value=atom,
            )
        else:
            arity[atom[0]] = n

    for action in domain.actions:
        for bucket, label in (
            (action.pre_pos, "pre_pos"),
            (action.pre_neg, "pre_neg"),
            (action.add_effects, "add"),
            (action.del_effects, "del"),
        ):
            for atom in bucket:
                record(atom, f"action {action.name}.{label}")
    for atom in problem.initial:
        record(atom, "problem.init")
    for atom in (*problem.goal_pos, *problem.goal_neg):
        record(atom, "problem.goal")
    return arity


def _check_actions(domain: Domain, issues: IssueList) -> None:
    names: set[str] = set()
    for action in domain.actions:
        if action.name in names:
            issues.add(
                ACTION_NAME_DUPLICATE,
                "domain.actions",
                f"action {action.name!r} is declared more than once",
                value=action.name,
            )
        names.add(action.name)

        params: set[str] = set()
        for param in action.parameters:
            if param in params:
                issues.add(
                    PARAM_DUPLICATE,
                    f"action {action.name}.parameters",
                    f"parameter {param!r} repeats",
                    value=param,
                )
            params.add(param)

        _check_literal_args(action, params, issues)

        pos_templates = {_template(atom) for atom in action.pre_pos}
        neg_templates = {_template(atom) for atom in action.pre_neg}
        for sig in sorted(pos_templates & neg_templates):
            issues.add(
                PRECONDITION_CONTRADICTION,
                f"action {action.name}.preconditions",
                f"action requires both {sig} and ~{sig}",
            )

        add_templates = {_template(atom) for atom in action.add_effects}
        del_templates = {_template(atom) for atom in action.del_effects}
        for sig in sorted(add_templates & del_templates):
            issues.add(
                EFFECT_ADD_DELETE_CONFLICT,
                f"action {action.name}",
                f"action both adds and deletes {sig}; fixed rule forbids it",
            )


def _check_literal_args(action, params: set[str], issues: IssueList) -> None:
    for bucket, label in (
        (action.pre_pos, "pre_pos"),
        (action.pre_neg, "pre_neg"),
        (action.add_effects, "add"),
        (action.del_effects, "del"),
    ):
        for atom in bucket:
            for token in atom[1:]:
                if is_variable(token) and token not in params:
                    issues.add(
                        UNBOUND_VARIABLE,
                        f"action {action.name}.{label}",
                        f"variable {token!r} in {atom[0]} is not a parameter",
                        value=token,
                    )
                # Plain constants in schemas are fixed object references;
                # membership in problem.objects is checked at grounding time.


def _check_init_and_goal(
    problem: Problem,
    arity: dict[str, int],
    issues: IssueList,
) -> None:
    objects = set(problem.objects)

    def check_ground(atom: Atom, source: str, var_code: str) -> None:
        for token in atom[1:]:
            if is_variable(token):
                issues.add(
                    var_code,
                    source,
                    f"{source} atom must be ground, found variable {token!r}",
                    value=token,
                )
            elif token not in objects:
                issues.add(
                    OBJECT_UNDECLARED,
                    source,
                    f"object {token!r} is not declared in problem.objects",
                    value=token,
                )

    for atom in problem.initial:
        check_ground(atom, "problem.init", INIT_NOT_POSITIVE)
    for atom in (*problem.goal_pos, *problem.goal_neg):
        check_ground(atom, "problem.goal", UNBOUND_VARIABLE)

    pos = {_template(a) for a in problem.goal_pos}
    neg = {_template(a) for a in problem.goal_neg}
    for sig in sorted(pos & neg):
        issues.add(
            GOAL_CONTRADICTION,
            "problem.goal",
            f"goal requires both {sig} and ~{sig}",
        )


def _template(atom: Atom) -> str:
    return atom[0] if len(atom) == 1 else f"{atom[0]}({','.join(atom[1:])})"
