"""Derivation records and proof trees.

Every derived tuple remembers the rule firing that first produced it
(rule id + the concrete body tuples used).  Expanding those records
recursively yields a human-verifiable proof tree down to ground facts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .relational import Row


@dataclass(frozen=True)
class BodyBinding:
    """One conjunct of a firing, bound to a concrete tuple."""

    predicate: str
    row: Row
    negated: bool


@dataclass(frozen=True)
class Derivation:
    rule_id: str  # EDB facts use the sentinel below
    head: Tuple[str, Row]
    bindings: Tuple[BodyBinding, ...]
    round_no: int
    stratum: int

    FACT_RULE = "<fact>"

    @property
    def is_fact(self) -> bool:
        return self.rule_id == self.FACT_RULE


@dataclass(frozen=True)
class EvidenceNode:
    predicate: str
    row: Row
    kind: str  # "fact" | "derived" | "absence"
    negated: bool = False
    rule_id: Optional[str] = None
    rule_text: Optional[str] = None
    stratum: Optional[int] = None
    round_no: Optional[int] = None
    children: Tuple["EvidenceNode", ...] = ()

    def to_dict(self) -> dict:
        from ..language.ast import format_goal

        # Iterative postorder serialization so very deep proofs do not hit
        # the Python recursion limit.  Shared DAG children are serialized
        # once.
        serialized: Dict[int, dict] = {}
        finalized: set = set()
        work: List[Tuple[EvidenceNode, bool]] = [(self, False)]
        while work:
            node, expanded = work.pop()
            if expanded:
                if id(node) in finalized:
                    continue
                finalized.add(id(node))
                serialized[id(node)] = {
                    "goal": format_goal(node.predicate, node.row, node.negated),
                    "kind": node.kind,
                    "rule_id": node.rule_id,
                    "rule_text": node.rule_text,
                    "stratum": node.stratum,
                    "round": node.round_no,
                    "children": [serialized[id(c)] for c in node.children],
                }
                continue
            work.append((node, True))
            for child in reversed(node.children):
                if id(child) not in finalized:
                    work.append((child, False))
        return serialized[id(self)]


def build_proof_tree(
    predicate: str,
    row: Row,
    derivations: Dict[Tuple[str, Row], Derivation],
    rule_texts: Dict[str, str],
    *,
    max_depth: int = 10000,
) -> EvidenceNode:
    """Expand first-recorded derivations into a tree.

    Termination argument: the engine records exactly one derivation per
    tuple — the firing that first inserted it.  First-derivation edges
    always point at tuples inserted *earlier*, so they form a DAG rooted at
    ground facts.  Expansion is therefore iterative (an explicit stack, no
    Python recursion limit on chain depth); ``max_depth`` is a safety valve
    whose tripping is surfaced as an explicit ``depth_truncated`` node.
    """
    root_key = (predicate, row)
    order, depths = _collect_evidence_nodes(root_key, derivations)
    return _assemble_evidence(
        root_key, order, depths, derivations, rule_texts, max_depth
    )


def _collect_evidence_nodes(
    root_key: Tuple[str, Row],
    derivations: Dict[Tuple[str, Row], Derivation],
) -> Tuple[List[Tuple[str, Row]], Dict[Tuple[str, Row], int]]:
    """Return (postorder, shortest depth) for all reachable keys.

    Postorder (children before parents) is produced by an explicit two-state
    DFS stack, so the shared-child/DAG case is handled correctly.
    """
    depths = _shortest_depths(root_key, derivations)
    postorder: List[Tuple[str, Row]] = []
    finalized = set()
    # Stack entries: (key, expanded).  Unvisited keys are pushed with
    # expanded=False; they are then re-pushed as finalized markers ahead of
    # their children, so they are emitted only after every child is emitted.
    work: List[Tuple[Tuple[str, Row], bool]] = [(root_key, False)]
    while work:
        key, expanded = work.pop()
        if expanded:
            if key not in finalized:
                finalized.add(key)
                postorder.append(key)
            continue
        if key in finalized:
            continue
        work.append((key, True))
        derivation = derivations.get(key)
        if derivation is not None and not derivation.is_fact:
            for binding in derivation.bindings:
                if not binding.negated:
                    work.append(((binding.predicate, binding.row), False))
    return postorder, depths


def _shortest_depths(
    root_key: Tuple[str, Row],
    derivations: Dict[Tuple[str, Row], Derivation],
) -> Dict[Tuple[str, Row], int]:
    from collections import deque

    depths = {root_key: 0}
    queue = deque([root_key])
    while queue:
        key = queue.popleft()
        derivation = derivations.get(key)
        if derivation is None or derivation.is_fact:
            continue
        for binding in derivation.bindings:
            if binding.negated:
                continue
            child = (binding.predicate, binding.row)
            if child not in depths:
                depths[child] = depths[key] + 1
                queue.append(child)
    return depths


def _assemble_evidence(
    root_key: Tuple[str, Row],
    order: List[Tuple[str, Row]],
    depths: Dict[Tuple[str, Row], int],
    derivations: Dict[Tuple[str, Row], Derivation],
    rule_texts: Dict[str, str],
    max_depth: int,
) -> EvidenceNode:
    """Build nodes bottom-up so every child exists before its parent."""
    nodes: Dict[Tuple[str, Row], EvidenceNode] = {}
    for key in order:  # postorder: children are already in ``nodes``
        pred, r = key
        derivation = derivations.get(key)
        if derivation is None:
            nodes[key] = EvidenceNode(predicate=pred, row=r, kind="unexplained")
        elif derivation.is_fact:
            nodes[key] = EvidenceNode(
                predicate=pred, row=r, kind="fact",
                stratum=derivation.stratum,
            )
        elif depths.get(key, 0) >= max_depth:
            nodes[key] = EvidenceNode(
                predicate=pred, row=r, kind="depth_truncated",
                rule_id=derivation.rule_id,
                rule_text=rule_texts.get(derivation.rule_id),
            )
        else:
            children = tuple(
                _child_node(binding, nodes) for binding in derivation.bindings
            )
            nodes[key] = EvidenceNode(
                predicate=pred, row=r, kind="derived",
                rule_id=derivation.rule_id,
                rule_text=rule_texts.get(derivation.rule_id),
                stratum=derivation.stratum,
                round_no=derivation.round_no,
                children=children,
            )
    return nodes[root_key]


def _child_node(
    binding: BodyBinding,
    nodes: Dict[Tuple[str, Row], EvidenceNode],
) -> EvidenceNode:
    if binding.negated:
        return EvidenceNode(
            predicate=binding.predicate, row=binding.row,
            kind="absence", negated=True,
        )
    # Positive children are always assembled before their parent; the
    # fallback only guards against a (structurally impossible) back-edge.
    return nodes.get(
        (binding.predicate, binding.row),
        EvidenceNode(
            predicate=binding.predicate, row=binding.row, kind="unexplained"
        ),
    )


def leaves(node: EvidenceNode) -> List[EvidenceNode]:
    """Leaf nodes in left-to-right DFS order (iterative, depth-safe)."""
    out: List[EvidenceNode] = []
    stack: List[EvidenceNode] = [node]
    while stack:
        current = stack.pop()
        if not current.children:
            out.append(current)
        else:
            stack.extend(reversed(current.children))
    return out
