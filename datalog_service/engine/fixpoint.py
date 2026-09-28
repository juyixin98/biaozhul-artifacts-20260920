"""Semi-naive fixpoint evaluation over stratified rules.

For each stratum the engine maintains a *master* relation per predicate and
a per-predicate *delta* holding only tuples newly derived in the previous
round.  A rule fires in ``len(recursive_literals)`` variants: in variant *i*
literal *i* is matched against its delta instead of master, so every join
contains at least one tuple new in the previous round and no firing is ever
repeated with an all-old body.  Rules without same-stratum positive
literals (base cases) fire once in round 0.

Negated literals always refer to strictly lower strata (guaranteed by the
compiler) and are evaluated as set difference against their finished
relation.

The engine is purely functional: call :func:`evaluate` with a compiled
program plus EDB facts and receive an immutable :class:`Materialization`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..language.ast import Atom, Variable
from ..language.compiler import CompiledProgram, CompiledRule
from .derivations import BodyBinding, Derivation
from .relational import Row, Relation, match_terms

# A provider maps predicate -> current concrete relation.
RelationStore = Dict[str, Relation]

EMPTY: Relation = frozenset()


@dataclass(frozen=True)
class TraceStep:
    stratum: int
    round_no: int
    rule_id: str
    rule_text: str
    variant: str  # "base" or "delta:<predicate>"
    delta_input_size: int
    produced_rows: int
    new_tuples: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "stratum": self.stratum,
            "round": self.round_no,
            "rule_id": self.rule_id,
            "rule": self.rule_text,
            "variant": self.variant,
            "delta_input_size": self.delta_input_size,
            "produced_rows": self.produced_rows,
            "new_tuples": self.new_tuples,
        }


@dataclass(frozen=True)
class StratumStats:
    stratum: int
    rounds: int
    derived: Dict[str, int]


@dataclass(frozen=True)
class Materialization:
    compiled: CompiledProgram
    relations: RelationStore
    derivations: Dict[Tuple[str, Row], Derivation]
    fact_set_version: str
    materialization_id: str
    strata_rounds: Tuple[int, ...]
    trace: Tuple[TraceStep, ...]
    stats: Tuple[StratumStats, ...]
    fact_rows: Tuple[Tuple[str, Row], ...]
    evaluated_at: float

    def relation(self, predicate: str) -> Relation:
        return self.relations.get(predicate, EMPTY)


def fact_set_version(facts: Sequence[Atom]) -> str:
    canonical = "\n".join(sorted(f.canonical() for f in facts))
    return sha256(canonical.encode("utf-8")).hexdigest()[:16]


def evaluate(compiled: CompiledProgram, facts: Sequence[Atom]) -> Materialization:
    master, derivations, fact_rows = _seed_facts(compiled, facts)

    trace: List[TraceStep] = []
    strata_rounds: List[int] = []
    strata_stats: List[StratumStats] = []
    for stratum_no, stratum_rules in enumerate(compiled.strata):
        rounds, stats = _evaluate_stratum(
            compiled, stratum_no, stratum_rules, master, derivations, trace
        )
        strata_rounds.append(rounds)
        strata_stats.append(stats)

    fv = fact_set_version(facts)
    materialization_id = sha256(
        (fv + ":" + compiled.rule_version).encode("utf-8")
    ).hexdigest()[:16]
    return Materialization(
        compiled=compiled,
        relations=master,
        derivations=derivations,
        fact_set_version=fv,
        materialization_id=materialization_id,
        strata_rounds=tuple(strata_rounds),
        trace=tuple(trace),
        stats=tuple(strata_stats),
        fact_rows=tuple(fact_rows),
        evaluated_at=time.time(),
    )


def _seed_facts(
    compiled: CompiledProgram, facts: Sequence[Atom]
) -> Tuple[RelationStore, Dict[Tuple[str, Row], Derivation],
           List[Tuple[str, Row]]]:
    """Load EDB facts into frozen relations with fact derivations."""
    growing: Dict[str, set] = {}
    derivations: Dict[Tuple[str, Row], Derivation] = {}
    fact_rows = [
        (f.predicate, tuple(t.value for t in f.args))  # type: ignore[union-attr]
        for f in facts
    ]
    for predicate, row in fact_rows:
        growing.setdefault(predicate, set()).add(row)
        info = compiled.predicates.get(predicate)
        derivations.setdefault(
            (predicate, row),
            Derivation(
                rule_id=Derivation.FACT_RULE,
                head=(predicate, row),
                bindings=(),
                round_no=0,
                stratum=info.stratum if info is not None else 0,
            ),
        )
    return {p: frozenset(rows) for p, rows in growing.items()}, derivations, fact_rows


def _evaluate_stratum(
    compiled: CompiledProgram,
    stratum_no: int,
    stratum_rules: Tuple[CompiledRule, ...],
    master: RelationStore,
    derivations: Dict[Tuple[str, Row], Derivation],
    trace: List[TraceStep],
) -> Tuple[int, StratumStats]:
    """Run round 0 plus semi-naive delta rounds for one stratum."""
    group_heads = {cr.head_predicate for cr in stratum_rules}
    before_counts = {p: len(master.get(p, EMPTY)) for p in group_heads}

    # Round 0 is a full firing against the pre-round snapshot; all
    # consequences are absorbed together.
    deltas = _run_round(
        stratum_no, 0, stratum_rules, group_heads, master,
        derivations, deltas_by_pred=None, trace=trace,
    )

    # The final confirming round (which produces nothing) is executed but
    # not counted, so the reported round count covers productive rounds.
    round_no = 0
    while any(deltas.values()):
        next_deltas = _run_round(
            stratum_no, round_no + 1, stratum_rules, group_heads, master,
            derivations, deltas_by_pred=deltas, trace=trace,
        )
        if not any(next_deltas.values()):
            break
        round_no += 1
        deltas = next_deltas

    derived = {
        p: len(master.get(p, EMPTY)) - before_counts[p]
        for p in group_heads
        if len(master.get(p, EMPTY)) > before_counts[p]
    }
    return round_no, StratumStats(stratum_no, round_no, derived)


# --------------------------------------------------------------------------- #
# Rule firing
# --------------------------------------------------------------------------- #

def _row_key(row: Row):
    return tuple((type(v).__name__, v) for v in row)


def _run_round(
    stratum_no: int,
    round_no: int,
    stratum_rules: Tuple[CompiledRule, ...],
    group_heads: set,
    master: RelationStore,
    derivations: Dict[Tuple[str, Row], Derivation],
    deltas_by_pred: Optional[Dict[str, set]],
    trace: List[TraceStep],
) -> Dict[str, set]:
    """Execute one full (round 0) or semi-naive delta round.

    Every rule variant reads the same *snapshot* of master; derived tuples
    are absorbed only after all variants are evaluated, so a tuple can only
    depend on tuples present before the round.  Returns the per-predicate
    set of tuples newly absorbed in this round.
    """
    snapshot: RelationStore = dict(master)
    firings = _plan_firings(stratum_rules, group_heads, snapshot, deltas_by_pred)
    return _absorb_firings(
        firings, snapshot, master, derivations, stratum_no, round_no, trace
    )


def _plan_firings(
    stratum_rules: Tuple[CompiledRule, ...],
    group_heads: set,
    snapshot: RelationStore,
    deltas_by_pred: Optional[Dict[str, set]],
) -> List[Tuple[CompiledRule, str, int,
                List[Tuple[Row, Tuple[BodyBinding, ...]]]]]:
    """Evaluate every variant for the round against the shared snapshot."""
    firings: List[Tuple[CompiledRule, str, int,
                        List[Tuple[Row, Tuple[BodyBinding, ...]]]]] = []
    for cr in stratum_rules:
        if deltas_by_pred is None:
            firings.append((cr, "full", 0, _ordered_fire(cr, snapshot, None, EMPTY)))
            continue
        for idx in _recursive_positions(cr, group_heads):
            predicate = cr.positive[idx].predicate
            delta_rel = frozenset(deltas_by_pred.get(predicate, set()))
            if delta_rel:
                firings.append(
                    (cr, f"delta:{predicate}", len(delta_rel),
                     _ordered_fire(cr, snapshot, idx, delta_rel))
                )
    return firings


def _ordered_fire(
    cr: CompiledRule,
    snapshot: RelationStore,
    pinned_index: Optional[int],
    pinned_rows: Relation,
) -> List[Tuple[Row, Tuple[BodyBinding, ...]]]:
    return sorted(
        _fire(cr, snapshot, pinned_index=pinned_index, pinned_rows=pinned_rows),
        key=lambda item: _row_key(item[0]),
    )


def _absorb_firings(
    firings: List[Tuple[CompiledRule, str, int,
                        List[Tuple[Row, Tuple[BodyBinding, ...]]]]],
    snapshot: RelationStore,
    master: RelationStore,
    derivations: Dict[Tuple[str, Row], Derivation],
    stratum_no: int,
    round_no: int,
    trace: List[TraceStep],
) -> Dict[str, set]:
    """Insert freshly produced rows; first producer owns the derivation."""
    new_deltas: Dict[str, set] = {}
    for cr, variant, delta_size, produced in firings:
        new_count = 0
        for row, bindings in produced:
            if row in snapshot.get(cr.head_predicate, EMPTY):
                continue
            if row in master.get(cr.head_predicate, EMPTY):
                continue  # produced earlier within this same round
            master[cr.head_predicate] = (
                master.get(cr.head_predicate, EMPTY) | {row}
            )
            derivations[(cr.head_predicate, row)] = Derivation(
                rule_id=cr.rule_id,
                head=(cr.head_predicate, row),
                bindings=bindings,
                round_no=round_no,
                stratum=stratum_no,
            )
            new_deltas.setdefault(cr.head_predicate, set()).add(row)
            new_count += 1
        trace.append(
            TraceStep(stratum_no, round_no, cr.rule_id, cr.text, variant,
                      delta_size, len(produced), new_count)
        )
    return new_deltas
    return new_deltas


def _recursive_positions(
    cr: CompiledRule, group_heads: set
) -> List[int]:
    """Positive literal indices whose predicate grows inside this stratum."""
    return [i for i, a in enumerate(cr.positive) if a.predicate in group_heads]


def _fire(
    cr: CompiledRule,
    master: RelationStore,
    pinned_index: Optional[int],
    pinned_rows: Relation,
) -> List[Tuple[Row, Tuple[BodyBinding, ...]]]:
    """Evaluate one rule variant, returning (head_row, body bindings) pairs.

    When ``pinned_index`` is given, that positive literal is matched only
    against ``pinned_rows`` (a delta); every other literal uses master.
    """
    ordered = _join_order(cr, master, pinned_index)
    results: List[Tuple[Row, Tuple[BodyBinding, ...]]] = []
    seen_rows: set = set()

    def join(pos_i: int, subst: Dict[str, object],
             bindings: Dict[int, BodyBinding]) -> None:
        if pos_i == len(ordered):
            completed = _complete_firing(cr, subst, bindings, master)
            if completed is not None and completed[0] not in seen_rows:
                seen_rows.add(completed[0])
                results.append(completed)
            return
        original_index, atom = ordered[pos_i]
        relation = (
            pinned_rows if original_index == pinned_index
            else master.get(atom.predicate, EMPTY)
        )
        for row in relation:
            extension = match_terms(atom.args, row)
            if extension is None:
                continue
            merged = _merge(subst, extension)
            if merged is None:
                continue
            bindings[original_index] = BodyBinding(atom.predicate, row, False)
            join(pos_i + 1, merged, bindings)
            del bindings[original_index]

    join(0, {}, {})
    return results


def _join_order(
    cr: CompiledRule, master: RelationStore, pinned_index: Optional[int]
) -> List[Tuple[int, Atom]]:
    """Pinned literal first, then remaining literals smallest-relation-first."""
    pos = list(enumerate(cr.positive))
    if pinned_index is None:
        pinned, rest = [], pos
    else:
        pinned = [p for p in pos if p[0] == pinned_index]
        rest = [p for p in pos if p[0] != pinned_index]
    rest.sort(key=lambda p: len(master.get(p[1].predicate, EMPTY)))
    return pinned + rest


def _complete_firing(
    cr: CompiledRule,
    subst: Dict[str, object],
    bindings: Dict[int, BodyBinding],
    master: RelationStore,
) -> Optional[Tuple[Row, Tuple[BodyBinding, ...]]]:
    """Check negations and project the head once all positives match."""
    if not _negations_pass(cr, subst, master):
        return None
    head_row = _concrete_row(cr.head, subst)
    ordered_bindings = tuple(bindings[i] for i in range(len(cr.positive)))
    neg_bindings = tuple(
        BodyBinding(a.predicate, _concrete_row(a, subst), True)
        for a in cr.negative
    )
    return head_row, ordered_bindings + neg_bindings


def _concrete_row(atom: Atom, subst: Dict[str, object]) -> Row:
    return tuple(
        subst[t.name] if isinstance(t, Variable) else t.value
        for t in atom.args
    )


def _merge(base: Dict[str, object], extension: Dict[str, object]) -> Optional[Dict[str, object]]:
    for key, value in extension.items():
        if key in base and base[key] != value:
            return None
    merged = dict(base)
    merged.update(extension)
    return merged


def _negations_pass(
    cr: CompiledRule, subst: Dict[str, object], master: RelationStore
) -> bool:
    """True iff no negated literal has a matching tuple.

    Range restriction (checked at compile time) guarantees every variable
    in a negated literal is already bound by positive literals, so matching
    a negated atom amounts to checking whether its one concrete row exists.
    """
    for atom in cr.negative:
        row = _concrete_row(atom, subst)
        if row in master.get(atom.predicate, EMPTY):
            return False
    return True
