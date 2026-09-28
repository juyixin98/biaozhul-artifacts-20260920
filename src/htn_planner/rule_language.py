"""Rule language: load domain/problem YAML/JSON and bind parameters.

The textual rule language is a small, explicitly documented subset:

    task          := bare symbol
    atom          := symbol | "?" variable
    condition     := literal                       (fact must hold)
                   | ["not", literal...]
                   | ["avail", RESOURCE, amount]
                   | ["bound", RESOURCE, amount]
    effect        := mapping with add/remove/reserve/release
    method        := name, task, parameters, guard, order, subtasks
    subtask       := {id, task:[task, *args], after:[ids]}

Arguments may mix constants and variables ("?x").  When a method is selected
its declared parameters are bound positionally from the caller's arguments;
free variables inside the method body are then substituted.  A body that
references an unbound variable is rejected at binding time rather than silently
planned around.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .models import (
    Condition,
    Domain,
    Effect,
    Method,
    Primitive,
    Problem,
    Resource,
    Subtask,
)

VARIABLE_PREFIX = "?"


class RuleLanguageError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
def _read_raw(path: str | Path) -> dict[str, Any]:
    text = Path(path).read_text(encoding="utf-8")
    if str(path).endswith(".json"):
        import json

        data = json.loads(text)
    else:
        data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise RuleLanguageError(f"{path}: top-level document must be a mapping")
    return data


def _parse_condition(raw: list[Any]) -> Condition:
    if not isinstance(raw, list) or not raw:
        raise RuleLanguageError(f"condition must be a non-empty list, got {raw!r}")
    head = raw[0]
    if head == "not":
        if len(raw) < 2:
            raise RuleLanguageError("'not' needs a literal")
        return Condition(kind="not", name=str(raw[1]), args=[str(a) for a in raw[2:]])
    if head == "avail":
        _, resource, amount = _exact3(raw, "avail")
        return Condition(kind="avail", name=str(resource), amount=int(amount))
    if head == "bound":
        _, resource, amount = _exact3(raw, "bound")
        return Condition(kind="bound", name=str(resource), amount=int(amount))
    if head == "bind":
        # ["bind", "?target", "predicate", *pattern-args]
        if len(raw) < 4:
            raise RuleLanguageError(
                "'bind' needs [bind, target-var, predicate, *pattern]"
            )
        target = str(raw[1])
        if not target.startswith(VARIABLE_PREFIX):
            raise RuleLanguageError(f"'bind' target must be a variable, got {target!r}")
        return Condition(
            kind="bind",
            name=str(raw[2]),
            args=[str(a) for a in raw[3:]],
            bind_target=target,
        )
    return Condition(kind="fact", name=str(head), args=[str(a) for a in raw[1:]])


def _exact3(raw: list[Any], tag: str) -> tuple[Any, Any, Any]:
    if len(raw) != 3:
        raise RuleLanguageError(f"{tag!r} condition needs [tag, resource, amount]")
    return raw[0], raw[1], raw[2]


def _parse_effect(raw: dict[str, Any] | None) -> Effect:
    raw = raw or {}
    reserve = []
    for item in raw.get("reserve", []) or []:
        if isinstance(item, str):
            reserve.append({"resource": item, "amount": 1})
        else:
            reserve.append(
                {"resource": str(item["resource"]), "amount": int(item["amount"])}
            )
    release = []
    for item in raw.get("release", []) or []:
        if isinstance(item, str):
            release.append([item, 1])
        else:
            release.append([str(item["resource"]), int(item["amount"])])
    return Effect(
        add=[[str(x) for x in row] for row in raw.get("add", []) or []],
        remove=[[str(x) for x in row] for row in raw.get("remove", []) or []],
        reserve=reserve,
        release=release,
    )


def _parse_primitive(name: str, raw: dict[str, Any]) -> Primitive:
    return Primitive(
        name=name,
        parameters=[str(p) for p in raw.get("parameters", [])],
        precondition=[_parse_condition(c) for c in raw.get("precondition", []) or []],
        effect=_parse_effect(raw.get("effect")),
        duration=int(raw.get("duration", 1)),
    )


def _parse_subtask(raw: dict[str, Any]) -> Subtask:
    task_ref = raw["task"]
    if isinstance(task_ref, str):
        task, args = task_ref, []
    else:
        task, args = str(task_ref[0]), [str(a) for a in task_ref[1:]]
    return Subtask(
        id=str(raw["id"]),
        task=task,
        args=args,
        after=[str(a) for a in raw.get("after", []) or []],
    )


def _parse_method(raw: dict[str, Any]) -> Method:
    return Method(
        name=str(raw["name"]),
        task=str(raw["task"]),
        parameters=[str(p) for p in raw.get("parameters", [])],
        guard=[_parse_condition(c) for c in raw.get("guard", []) or []],
        subtasks=[_parse_subtask(s) for s in raw["subtasks"]],
        order=str(raw.get("order", "sequential")),
    )


def load_domain(path: str | Path) -> Domain:
    raw = _read_raw(path)
    try:
        primitives = {
            name: _parse_primitive(name, body or {})
            for name, body in (raw.get("primitives") or {}).items()
        }
        methods = [_parse_method(m) for m in raw.get("methods", []) or []]
        resources = {
            name: Resource(name=name, capacity=int(body["capacity"]))
            for name, body in (raw.get("resources") or {}).items()
        }
        domain = Domain(
            name=str(raw["name"]),
            version=str(raw.get("version", "1.0.0")),
            primitives=primitives,
            methods=methods,
            resources=resources,
        )
    except (KeyError, TypeError) as exc:
        raise RuleLanguageError(f"{path}: malformed domain: {exc}") from exc
    _cross_validate(domain)
    return domain


def _cross_validate(domain: Domain) -> None:
    method_names: set[str] = set()
    for m in domain.methods:
        if m.name in method_names:
            raise RuleLanguageError(f"duplicate method name {m.name!r}")
        method_names.add(m.name)
        if len(m.parameters) != len(set(m.parameters)):
            raise RuleLanguageError(f"method {m.name!r}: duplicate parameters")
        if m.order == "sequential":
            for st in m.subtasks:
                if st.after:
                    raise RuleLanguageError(
                        f"method {m.name!r}: sequential subtask {st.id!r} may not "
                        "declare 'after'"
                    )


def load_problem(path: str | Path) -> Problem:
    raw = _read_raw(path)
    try:
        return Problem(
            name=str(raw["name"]),
            domain=str(raw["domain"]),
            initial_facts=[[str(x) for x in row] for row in raw.get("initial_facts", [])],
            initial_resources={
                str(k): int(v) for k, v in (raw.get("initial_resources") or {}).items()
            },
            goal_task=str(raw["goal_task"]),
            goal_args=[str(a) for a in raw.get("goal_args", [])],
            max_depth=int(raw.get("max_depth", 8)),
            max_expansions=int(raw.get("max_expansions", 256)),
        )
    except (KeyError, TypeError) as exc:
        raise RuleLanguageError(f"{path}: malformed problem: {exc}") from exc


# ---------------------------------------------------------------------------
# Parameter binding
# ---------------------------------------------------------------------------
def _subst_token(token: str, binding: dict[str, str]) -> str:
    if token.startswith(VARIABLE_PREFIX):
        if token not in binding:
            raise RuleLanguageError(
                f"unbound variable {token!r}; bound vars: {sorted(binding)}"
            )
        return binding[token]
    return token


def substitute_token(token: str, binding: dict[str, str]) -> str:
    """Public token substitution for guard evaluation in the kernel."""
    return _subst_token(token, binding)


def substitute_condition(cond: Condition, binding: dict[str, str]) -> Condition:
    """Ground a single (non-bind) condition against an explicit binding."""
    return _bind_condition(cond, binding)


def _bind_condition(cond: Condition, binding: dict[str, str]) -> Condition:
    return Condition(
        kind=cond.kind,
        # Resource names in avail/bound may also be variables.
        name=_subst_token(cond.name, binding),
        args=[_subst_token(a, binding) for a in cond.args],
        amount=cond.amount,
    )


def bind_primitive(
    prim: Primitive, call_args: list[str]
) -> Primitive:
    if len(call_args) != len(prim.parameters):
        raise RuleLanguageError(
            f"primitive {prim.name!r} arity mismatch: declared "
            f"{len(prim.parameters)}, called with {len(call_args)}"
        )
    binding = dict(zip(prim.parameters, call_args))
    effect = prim.effect
    return Primitive(
        name=prim.name,
        parameters=[],
        precondition=[_bind_condition(c, binding) for c in prim.precondition],
        effect=Effect(
            add=[[_subst_token(t, binding) for t in row] for row in effect.add],
            remove=[[_subst_token(t, binding) for t in row] for row in effect.remove],
            reserve=[
                {
                    "resource": _subst_token(r["resource"], binding),
                    "amount": r["amount"],
                }
                for r in effect.reserve
            ],
            release=[[_subst_token(r[0], binding), r[1]] for r in effect.release],
        ),
        duration=prim.duration,
    )


def bind_method(method: Method, call_args: list[str],
                extra_binding: dict[str, str] | None = None) -> Method:
    """Return a ground copy of ``method`` with parameters substituted.

    Constants and already-ground args pass through; every variable appearing in
    guard or subtask arguments must resolve against the parameter binding (plus
    any auxiliary variables derived from ``bind`` guards).
    """
    if len(call_args) != len(method.parameters):
        raise RuleLanguageError(
            f"method {method.name!r} arity mismatch on task {method.task!r}: "
            f"declared {len(method.parameters)}, called with {len(call_args)}"
        )
    binding = dict(zip(method.parameters, call_args))
    if extra_binding:
        binding.update(extra_binding)
    ground_subtasks: list[Subtask] = []
    for st in method.subtasks:
        ground_subtasks.append(
            Subtask(
                id=st.id,
                task=st.task,
                args=[_subst_token(a, binding) for a in st.args],
                after=list(st.after),
            )
        )
    return Method(
        name=method.name,
        task=method.task,
        parameters=[],
        guard=[_bind_condition(c, binding) for c in method.guard],
        subtasks=ground_subtasks,
        order=method.order,
    )
