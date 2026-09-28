"""Semi-naive fixpoint evaluation over stratified programs.

Per stratum (in dependency order) the stratum's rules are run to a
fixpoint:

* bootstrap pass: every positive literal scans the full relation.  At this
  point recursive predicates are still empty, so only base derivations
  appear;
* delta passes: one variant per *recursive* positive body literal, scanned
  against the previous iteration's delta relation while the others scan the
  full relation.  Only tuples whose derivation consumes a newly derived
  fact can be new — this is the semi-naive rewrite that avoids repeating
  work.

The output is a :class:`FixpointResult` carrying the final relations plus,
for every derived tuple, one canonical :class:`Firing` as verifiable
evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..language.compiler import CompiledProgram
from ..language.terms import Atom, Rule
from .relations import Database, Firing, Tuple, fire_rule

PredKey = tuple[str, int]


@dataclass
class StratumTrace:
    level: int
    predicates: tuple[PredKey, ...]
    iterations: int
    produced: int
    steps: list[str] = field(default_factory=list)


@dataclass
class FixpointResult:
    db: Database
    # (pred, tuple) -> first/canonical firing that derived it.
    evidence: dict[tuple[PredKey, Tuple], Firing]
    # Ground tuples given as EDB facts (axioms; leaves of any proof tree).
    base_facts: frozenset[tuple[PredKey, Tuple]]
    traces: tuple[StratumTrace, ...]


def evaluate(compiled: CompiledProgram) -> FixpointResult:
    db = Database()
    evidence: dict[tuple[PredKey, Tuple], Firing] = {}
    base: set[tuple[PredKey, Tuple]] = set()

    for fact in compiled.program.facts:
        key = (fact.pred, fact.arity)
        tup = tuple(a.value for a in fact.args)  # type: ignore[union-attr]
        db.ensure(key).add(tup)
        base.add((key, tup))

    traces: list[StratumTrace] = []

    for level, rule_indices in enumerate(compiled.rules_by_stratum):
        if not rule_indices:
            traces.append(StratumTrace(level=level, predicates=compiled.strata[level], iterations=0, produced=0))
            continue
        rules = [compiled.program.rules[i] for i in rule_indices]
        stratum_preds = set(compiled.strata[level])
        trace = StratumTrace(
            level=level,
            predicates=compiled.strata[level],
            iterations=0,
            produced=0,
        )
        _evaluate_stratum(
            rules=rules,
            rule_indices=rule_indices,
            stratum_preds=stratum_preds,
            db=db,
            evidence=evidence,
            trace=trace,
        )
        traces.append(trace)

    return FixpointResult(
        db=db,
        evidence=evidence,
        base_facts=frozenset(base),
        traces=tuple(traces),
    )


def _recursive_literal_indices(rule: Rule, stratum_preds: set[PredKey]) -> list[int]:
    out: list[int] = []
    for i, lit in enumerate(rule.body):
        if isinstance(lit, Atom) and (lit.pred, lit.arity) in stratum_preds:
            out.append(i)
    return out


def _firing_key(firing: Firing) -> tuple:
    return (
        firing.rule_index,
        firing.head,
        firing.pos_inputs,
        firing.neg_checks,
    )


def _evaluate_stratum(
    rules: list[Rule],
    rule_indices: tuple[int, ...],
    stratum_preds: set[PredKey],
    db: Database,
    evidence: dict[tuple[PredKey, Tuple], Firing],
    trace: StratumTrace,
) -> None:
    for p in stratum_preds:
        db.rels.setdefault(p, set())

    iteration = 0
    total_produced = 0

    while True:
        iteration += 1
        candidates: dict[tuple[PredKey, Tuple], Firing] = {}

        for rule, rule_index in zip(rules, rule_indices):
            head_key = (rule.head.pred, rule.head.arity)
            if iteration == 1:
                variants: list[int | None] = [None]
            else:
                variants = list(_recursive_literal_indices(rule, stratum_preds))
                if not variants:
                    # Non-recursive rule in a later iteration cannot produce
                    # anything new: all its inputs were already available.
                    continue

            for designated in variants:
                for firing in fire_rule(rule, rule_index, db, designated_index=designated):
                    target = (head_key, firing.head)
                    existing = candidates.get(target)
                    if existing is None or _firing_key(firing) < _firing_key(existing):
                        candidates[target] = firing

        new_tuples: dict[PredKey, set[Tuple]] = {}
        for (key, tup), firing in candidates.items():
            if tup in db.rels[key]:
                continue
            new_tuples.setdefault(key, set()).add(tup)
            prior = evidence.get((key, tup))
            if prior is None or _firing_key(firing) < _firing_key(prior):
                evidence[(key, tup)] = firing

        if not new_tuples:
            trace.steps.append(f"iter {iteration}: no new tuples, fixpoint reached")
            break

        produced_this = 0
        db.deltas = {}
        for key, tuples in new_tuples.items():
            db.rels[key].update(tuples)
            db.deltas[key] = set(tuples)
            produced_this += len(tuples)
        total_produced += produced_this
        trace.steps.append(
            f"iter {iteration}: +{produced_this} tuples across "
            f"{len(new_tuples)} predicate(s)"
        )

    trace.iterations = iteration
    trace.produced = total_produced
