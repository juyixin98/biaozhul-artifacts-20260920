"""Independent test oracle.

This module deliberately re-implements, from scratch, the simulation needed to
judge a returned plan:

* it parses the raw domain YAML itself (no import of the planner's parser or
  state module),
* it walks the retained tree and replays each leaf action in the returned
  execution order,
* it enforces preconditions, resource capacities, and applies effects with its
  own fact/resource bookkeeping.

Expected answers in the test suite therefore come from this independent
simulator and from hand-written literals -- never from the planner under test.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


class OracleFailure(AssertionError):
    pass


class IndependentOracle:
    def __init__(self, domain_path: str | Path, initial_facts, initial_resources):
        with open(domain_path, encoding="utf-8") as fh:
            self.raw: dict[str, Any] = yaml.safe_load(fh)
        self.facts: set[tuple[str, ...]] = {tuple(f) for f in initial_facts}
        self.resources: dict[str, list[int]] = {}
        for name, body in (self.raw.get("resources") or {}).items():
            self.resources[name] = [int(body["capacity"]),
                                    int((initial_resources or {}).get(name, 0))]
        self.max_held: dict[str, int] = {n: v[1] for n, v in self.resources.items()}
        self.executed: list[tuple[str, tuple[str, ...]]] = []

    def _binding(self, params: list[str], args: list[str]) -> dict[str, str]:
        if len(params) != len(args):
            raise OracleFailure(f"arity mismatch {params} vs {args}")
        return dict(zip(params, args))

    def _sub(self, token: Any, binding: dict[str, str]) -> str:
        token = str(token)
        return binding[token] if token.startswith("?") else token

    def _eval(self, cond: list[Any], binding: dict[str, str]) -> None:
        head = str(cond[0])
        if head == "not":
            key = tuple(self._sub(t, binding) for t in cond[1:])
            if key in self.facts:
                raise OracleFailure(f"negated literal holds: {key}")
            return
        if head == "avail":
            _, res, amount = cond
            cap, held = self.resources[str(res)]
            if cap - held < int(amount):
                raise OracleFailure(
                    f"resource {res} exhausted: need {amount}, free={cap - held}"
                )
            return
        if head == "bound":
            _, res, amount = cond
            cap, held = self.resources[str(res)]
            if cap - held >= int(amount):
                raise OracleFailure(f"resource {res} unexpectedly free")
            return
        key = tuple(self._sub(t, binding) for t in cond)
        if key not in self.facts:
            raise OracleFailure(f"missing precondition fact: {key}")

    def replay(self, nodes: dict, order: list[str]) -> None:
        for node_id in order:
            node = nodes[node_id]
            if node["kind"] != "primitive":
                raise OracleFailure(f"{node_id}: non-primitive in execution order")
            name = node["primitive"]
            args = node["args"]
            spec = self.raw["primitives"][name]
            binding = self._binding(spec.get("parameters", []), args)
            for cond in spec.get("precondition", []) or []:
                self._eval(cond, binding)
            self._apply(spec.get("effect") or {}, binding)
            self.executed.append((name, tuple(args)))

    def _apply(self, effect: dict, binding: dict[str, str]) -> None:
        for row in effect.get("remove", []) or []:
            self.facts.discard(tuple(self._sub(t, binding) for t in row))
        for row in effect.get("add", []) or []:
            self.facts.add(tuple(self._sub(t, binding) for t in row))
        for item in effect.get("reserve", []) or []:
            if isinstance(item, str):
                res, amount = item, 1
            else:
                res, amount = item["resource"], int(item["amount"])
            cap, held = self.resources[str(res)]
            if held + amount > cap:
                raise OracleFailure(f"reserve over capacity for {res}")
            self.resources[str(res)][1] = held + amount
            self.max_held[str(res)] = max(self.max_held[str(res)], held + amount)
        for item in effect.get("release", []) or []:
            if isinstance(item, str):
                res, amount = item, 1
            else:
                res, amount = item["resource"], int(item["amount"])
            cap, held = self.resources[str(res)]
            self.resources[str(res)][1] = max(0, held - amount)

    def action_names(self) -> list[str]:
        return [name for name, _ in self.executed]

    def grounded_actions(self) -> list[tuple[str, tuple[str, ...]]]:
        return list(self.executed)
