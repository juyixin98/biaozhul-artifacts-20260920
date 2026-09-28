"""Query evaluation against a materialized program.

A query is a single atom (possibly with variables).  Matching runs over the
in-memory materialization produced by the fixpoint engine — the engine is
the only place where recursion happens; the query layer is a plain join
against finished relations plus proof-tree expansion.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List

from ..engine.derivations import EvidenceNode, build_proof_tree
from ..engine.fixpoint import Materialization
from ..engine.relational import match_terms
from ..language.ast import Atom, Variable
from ..language.errors import QueryError


@dataclass(frozen=True)
class Answer:
    bindings: Dict[str, Any]
    row: tuple
    proof: EvidenceNode

    def to_dict(self, include_proof: bool) -> Dict[str, Any]:
        return {
            "bindings": self.bindings,
            "proof": self.proof.to_dict() if include_proof else None,
        }


def answer_query(
    mat: Materialization,
    goal: Atom,
    *,
    include_proofs: bool = True,
    max_answers: int = 1000,
) -> List[Answer]:
    if goal.predicate not in mat.compiled.predicates:
        raise QueryError(
            f"unknown predicate {goal.predicate!r}: it appears in neither "
            f"facts nor rules",
            details={"predicate": goal.predicate},
        )
    expected = mat.compiled.predicates[goal.predicate].arity
    if goal.arity != expected:
        raise QueryError(
            f"query {goal.canonical()} has arity {goal.arity} but "
            f"{goal.predicate} is declared with arity {expected}",
            details={"predicate": goal.predicate, "expected_arity": expected,
                     "found_arity": goal.arity},
        )

    relation = mat.relation(goal.predicate)
    rule_texts = {cr.rule_id: cr.text for cr in mat.compiled.rules}
    answers: List[Answer] = []
    for row in sorted(relation, key=lambda r: tuple(str(v) for v in r)):
        subst = match_terms(goal.args, row)
        if subst is None:
            continue
        bindings = {name: value for name, value in sorted(subst.items())}
        proof = build_proof_tree(
            goal.predicate, row, mat.derivations, rule_texts
        )
        answers.append(Answer(bindings=bindings, row=row, proof=proof))
        if len(answers) >= max_answers:
            break
    return answers


def goal_variables(goal: Atom) -> List[str]:
    seen: List[str] = []
    for term in goal.args:
        if isinstance(term, Variable) and term.name not in seen:
            seen.append(term.name)
    return seen
