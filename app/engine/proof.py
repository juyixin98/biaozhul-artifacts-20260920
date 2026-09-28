"""Reconstruct human-verifiable proof trees from canonical evidence.

For every derived tuple the semi-naive engine recorded the *first* firing
that produced it.  Walking those firings recursively yields a proof tree:

* leaves are EDB facts (ground axioms);
* internal nodes are rule applications, with one child per positive body
  tuple;
* negated literals are recorded as *absence* nodes, carrying the match
  pattern that was verified to have no counterpart in the relation.

The walk is guarded: an evidence chain that revisits a tuple would indicate
an engine bug (every firing consumes tuples available strictly before the
tuple was derived), and is reported as an ``uncertain`` conclusion rather
than silently looped.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..language.terms import render_tuple
from .fixpoint import FixpointResult
from .relations import Firing, PredKey, Tuple

MAX_PROOF_DEPTH = 200


@dataclass
class ProofNode:
    node_id: int
    pred: str
    arity: int
    tuple: Tuple
    kind: str  # "fact" | "rule" | "absence" | "uncertain"
    rule_index: int | None = None
    rule_text: str | None = None
    stratum: int | None = None
    children: list["ProofNode"] = field(default_factory=list)
    detail: str | None = None

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "atom": f"{self.pred}{render_tuple(self.tuple)}",
            "predicate": self.pred,
            "args": list(self.tuple),
            "kind": self.kind,
            "rule_index": self.rule_index,
            "rule": self.rule_text,
            "stratum": self.stratum,
            "detail": self.detail,
            "children": [c.to_dict() for c in self.children],
        }


class ProofBuilder:
    def __init__(
        self,
        result: FixpointResult,
        rule_texts: dict[int, str],
        stratum_of: dict[PredKey, int] | None = None,
    ) -> None:
        self.result = result
        self.rule_texts = rule_texts
        self.stratum_of = stratum_of or {}

    def build(self, goal: PredKey, tup: Tuple) -> ProofNode:
        self._next_id = 0
        return self._prove(goal, tup, frozenset(), depth=0)

    def _new_node(self, key: PredKey, tup: Tuple, kind: str) -> ProofNode:
        node = ProofNode(
            node_id=self._next_id,
            pred=key[0],
            arity=key[1],
            tuple=tup,
            kind=kind,
            stratum=self.stratum_of.get(key),
        )
        self._next_id += 1
        return node

    def result_stratum(self, key: PredKey) -> int | None:
        return self.stratum_of.get(key)

    def _prove(
        self,
        key: PredKey,
        tup: Tuple,
        active: frozenset[tuple[PredKey, Tuple]],
        depth: int,
    ) -> ProofNode:
        here = (key, tup)
        if here in active or depth > MAX_PROOF_DEPTH:
            node = self._new_node(key, tup, "uncertain")
            node.detail = "evidence chain revisited a tuple or exceeded depth; cannot certify"
            return node

        if here in self.result.base_facts:
            node = self._new_node(key, tup, "fact")
            node.detail = "EDB base fact"
            return node

        firing: Firing | None = self.result.evidence.get(here)
        if firing is None:
            node = self._new_node(key, tup, "uncertain")
            node.detail = "tuple present but no recorded derivation"
            return node

        node = self._new_node(key, tup, "rule")
        node.rule_index = firing.rule_index
        node.rule_text = self.rule_texts.get(firing.rule_index)
        next_active = active | {here}
        for child_key, child_tup in firing.pos_inputs:
            node.children.append(self._prove(child_key, child_tup, next_active, depth + 1))
        for neg_key, pattern in firing.neg_checks:
            absence = self._new_node(neg_key, pattern, "absence")
            absence.detail = (
                f"negation check: no tuple of {neg_key[0]}/{neg_key[1]} "
                f"matches {render_tuple(pattern)} ('*' = existential position)"
            )
            node.children.append(absence)
        return node
