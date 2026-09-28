"""Independent *naive* fixpoint oracle, used only by the test-suite.

This is deliberately written in a different style from the production
engine in :mod:`datalog_service.engine.fixpoint`:

* it recomputes **every** rule over the **whole** relation on each round
  (no deltas, no pinning) -- textbook naive evaluation;
* it carries its own term-binding / join code instead of importing the
  engine's relational operators;
* it mutates local plain ``dict``/``set`` structures.

Sharing the compiler's stratum grouping is intentional and documented:
stratification is a separate compile-time concern (with its own tests);
what this oracle independently certifies is the **least-fixpoint closure**
the semi-naive engine computes.
"""

from __future__ import annotations

from typing import Any, Dict, List, Sequence, Set, Tuple

from datalog_service.language.ast import Atom, Constant, Variable
from datalog_service.language.compiler import CompiledProgram

NaiveRelations = Dict[str, Set[Tuple[Any, ...]]]


def _bind_terms(terms: Sequence[Any], values: Sequence[Any],
                env: Dict[str, Any]) -> Dict[str, Any] | None:
    """Extend ``env`` so that ``terms`` map onto concrete ``values``.

    Independent re-implementation of variable/constant matching.
    """
    if len(terms) != len(values):
        return None
    next_env = dict(env)
    for term, value in zip(terms, values):
        if isinstance(term, Variable):
            if term.name in next_env:
                if next_env[term.name] != value:
                    return None
            else:
                next_env[term.name] = value
        elif isinstance(term, Constant):
            if term.value != value:
                return None
        else:  # pragma: no cover - defensive
            return None
    return next_env


def _project(atom: Atom, env: Dict[str, Any]) -> Tuple[Any, ...]:
    out: List[Any] = []
    for term in atom.args:
        if isinstance(term, Variable):
            out.append(env[term.name])
        else:
            out.append(term.value)
    return tuple(out)


def _rule_consequences(cr: Any, rel: NaiveRelations) -> Set[Tuple[Any, ...]]:
    found: Set[Tuple[Any, ...]] = set()

    def satisfy(pos_idx: int, env: Dict[str, Any]) -> None:
        if pos_idx == len(cr.positive):
            # Negation: the fully-bound negated atom's row must be absent.
            for neg in cr.negative:
                row = _project(neg, env)
                if row in rel.get(neg.predicate, set()):
                    return
            found.add(_project(cr.head, env))
            return
        atom = cr.positive[pos_idx]
        for candidate in rel.get(atom.predicate, set()):
            extended = _bind_terms(atom.args, candidate, env)
            if extended is not None:
                satisfy(pos_idx + 1, extended)

    satisfy(0, {})
    return found


def naive_evaluate(compiled: CompiledProgram, facts: Sequence[Atom]) -> NaiveRelations:
    rel: NaiveRelations = {}
    for fact in facts:
        rel.setdefault(fact.predicate, set()).add(
            tuple(t.value for t in fact.args)  # type: ignore[union-attr]
        )

    for group in compiled.strata:
        head_predicates = {cr.head_predicate for cr in group}
        # Naive loop: full re-derivation until the relations stop growing.
        while True:
            snapshot = {p: set(rel.get(p, set())) for p in head_predicates}
            accumulated = {p: set(rel.get(p, set())) for p in head_predicates}
            for cr in group:
                accumulated[cr.head_predicate].update(_rule_consequences(cr, rel))
            if all(accumulated[p] == snapshot[p] for p in head_predicates):
                break
            for p in head_predicates:
                rel[p] = accumulated[p]
    return rel
