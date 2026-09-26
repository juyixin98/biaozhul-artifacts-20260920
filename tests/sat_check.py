"""Independent verification of SAT solver JSON responses.

This module is written from scratch and shares no code with the C++ backend.
It implements:
  * CNF normalization (duplicate literal collapse, tautology detection)
  * direct model checking against the ORIGINAL clauses
  * full DPLL search-tree replay for UNSAT proofs

A SAT response is valid only when the model satisfies every original clause.
An UNSAT response is valid only when the recorded tree, replayed step by step,
explores both polarities at every decision, records only genuinely forced
propagations, and ends every branch in a clause that is truly falsified.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class ValidationError(Exception):
    reason: str

    def __str__(self) -> str:
        return self.reason


def normalize_clause(clause: list[int]) -> Optional[list[int]]:
    """Returns sorted unique literals, or None if the clause is a tautology."""
    literals = set(clause)
    for lit in literals:
        if -lit in literals:
            return None
    return sorted(literals)


def normalize_cnf(num_vars: int, clauses: list[list[int]]):
    normalized = []
    tautology_indices = []
    for i, clause in enumerate(clauses):
        c = normalize_clause(clause)
        if c is None:
            tautology_indices.append(i)
        else:
            normalized.append(c)
    for clause in clauses:
        for lit in clause:
            if lit == 0 or abs(lit) > num_vars:
                raise ValidationError(f"literal {lit} out of range for num_vars={num_vars}")
    return num_vars, normalized, tautology_indices


def literal_value(lit: int, assignment: list[int]) -> int:
    v = assignment[abs(lit)]
    if v == 0:
        return 0
    return v if lit > 0 else -v


def clause_state(clause: list[int], assignment: list[int]) -> tuple[bool, int, int]:
    """Returns (satisfied, unassigned_count, last_unassigned_literal)."""
    unassigned = 0
    forced = 0
    for lit in clause:
        val = literal_value(lit, assignment)
        if val == 1:
            return True, 0, 0
        if val == 0:
            unassigned += 1
            forced = lit
    return False, unassigned, forced


def check_model(clauses: list[list[int]], model: list[int]) -> Optional[int]:
    """Returns index of first falsified clause, or None if all satisfied."""
    for i, clause in enumerate(clauses):
        satisfied, _, _ = clause_state(clause, model)
        if not satisfied:
            return i
    return None


def _replay(node_id, nodes_by_id, clauses, assignment, seen):
    """Recursively replay a proof node.

    Returns 1 if the subtree establishes SAT, 0 for UNSAT.
    Raises ValidationError on any inconsistency.
    """
    if node_id not in nodes_by_id:
        raise ValidationError(f"child references missing node {node_id}")
    if node_id in seen:
        raise ValidationError(f"node {node_id} reached twice (not a tree)")
    seen.add(node_id)
    node = nodes_by_id[node_id]
    a = assignment  # mutated locally via list copy at branching points

    for step in node.get("propagations", []):
        lit = step["literal"]
        reason = step["reason_clause"]
        if not (0 <= reason < len(clauses)):
            raise ValidationError(f"node {node_id}: invalid reason clause {reason}")
        var = abs(lit)
        if a[var] != 0:
            raise ValidationError(f"node {node_id}: variable {var} assigned twice")
        clause = clauses[reason]
        satisfied, unassigned, forced = clause_state(clause, a)
        if satisfied:
            raise ValidationError(f"node {node_id}: reason clause {reason} already satisfied")
        if unassigned != 1:
            raise ValidationError(
                f"node {node_id}: reason clause {reason} has {unassigned} unassigned literals")
        if forced != lit:
            raise ValidationError(f"node {node_id}: forced literal mismatch")
        a[var] = 1 if lit > 0 else -1

    kind = node["kind"]

    if kind == "conflict":
        cc = node.get("conflict_clause")
        if cc is None or not (0 <= cc < len(clauses)):
            raise ValidationError(f"node {node_id}: invalid conflict clause")
        for lit in clauses[cc]:
            if literal_value(lit, a) != -1:
                raise ValidationError(f"node {node_id}: conflict clause {cc} not falsified")
        if node.get("positive_child") is not None or node.get("negative_child") is not None:
            raise ValidationError(f"node {node_id}: conflict leaf has children")
        return 0

    if kind == "sat":
        if any(v == 0 for v in a[1:]):
            raise ValidationError(f"node {node_id}: SAT leaf leaves variables unassigned")
        bad = check_model(clauses, a)
        if bad is not None:
            raise ValidationError(f"node {node_id}: SAT leaf falsifies clause {bad}")
        recorded = node.get("assignment")
        if recorded is not None and recorded != a[1:]:
            raise ValidationError(f"node {node_id}: recorded assignment disagrees with replay")
        return 1

    if kind != "branch":
        raise ValidationError(f"node {node_id}: unknown kind {kind!r}")

    decision = node.get("decision_var")
    if decision is None or a[decision] != 0:
        raise ValidationError(f"node {node_id}: invalid decision variable")
    for i, clause in enumerate(clauses):
        satisfied, unassigned, _ = clause_state(clause, a)
        if not satisfied and unassigned == 0:
            raise ValidationError(
                f"node {node_id}: clause {i} falsified but no conflict recorded")
        if not satisfied and unassigned == 1:
            raise ValidationError(f"node {node_id}: unit clause {i} not propagated before branch")

    pos = node.get("positive_child")
    if pos is None:
        raise ValidationError(f"node {node_id}: missing positive child")
    a_pos = list(a)
    a_pos[decision] = 1
    if _replay(pos, nodes_by_id, clauses, a_pos, seen) == 1:
        return 1

    neg = node.get("negative_child")
    if neg is None:
        raise ValidationError(
            f"node {node_id}: positive branch infeasible but negative branch missing")
    a_neg = list(a)
    a_neg[decision] = -1
    if _replay(neg, nodes_by_id, clauses, a_neg, seen) == 1:
        return 1
    return 0


def validate_response(doc: dict, original_num_vars: int, original_clauses: list[list[int]]):
    """Validate a /solve response against the ORIGINAL request CNF.

    Raises ValidationError if invalid. Returns "sat" or "unsat".
    """
    status = doc.get("status")
    if status not in ("sat", "unsat"):
        raise ValidationError(f"cannot validate status {status!r} (limit reached?)")

    n, normalized, taut_indices = normalize_cnf(original_num_vars, original_clauses)

    # The response must report the same normalization we independently derive.
    if doc.get("num_vars") != n:
        raise ValidationError("num_vars disagrees with independent normalization")
    if doc.get("normalized_clauses") != normalized:
        raise ValidationError("normalized_clauses disagree with independent normalization")
    if doc.get("tautology_clause_indices") != taut_indices:
        raise ValidationError("tautology indices disagree with independent normalization")

    proof = doc.get("proof") or {}
    nodes = proof.get("nodes")
    root = proof.get("root")
    if not isinstance(nodes, list) or not isinstance(root, int):
        raise ValidationError("proof must contain integer root and node array")
    nodes_by_id = {}
    for node in nodes:
        if not isinstance(node, dict) or not isinstance(node.get("id"), int):
            raise ValidationError("proof node without integer id")
        if node["id"] in nodes_by_id:
            raise ValidationError(f"duplicate node id {node['id']}")
        nodes_by_id[node["id"]] = node
    if root not in nodes_by_id:
        raise ValidationError("root id not present in nodes")

    verdict = _replay(root, nodes_by_id, normalized, [0] * (n + 1), set())
    if len(nodes_by_id) != len(nodes):
        raise ValidationError("duplicate node ids")
    reached = set()
    # _replay records reachable nodes in its `seen` via closure side effect;
    # re-walk to collect them explicitly.
    def walk(nid):
        if nid in reached:
            return
        reached.add(nid)
        nd = nodes_by_id[nid]
        if nd["kind"] == "branch":
            if nd.get("positive_child") is not None:
                walk(nd["positive_child"])
            if nd.get("negative_child") is not None:
                walk(nd["negative_child"])
    walk(root)
    if reached != set(nodes_by_id):
        raise ValidationError("proof contains nodes unreachable from root")

    if status == "sat":
        if verdict != 1:
            raise ValidationError("response claims sat but replayed tree proves unsat")
        model_entries = doc.get("model")
        if not isinstance(model_entries, list) or len(model_entries) != n:
            raise ValidationError("sat response missing complete model")
        model = [0]
        for i, entry in enumerate(model_entries, start=1):
            if entry.get("variable") != i or not isinstance(entry.get("value"), bool):
                raise ValidationError("malformed model entry")
            model.append(1 if entry["value"] else -1)
        bad = check_model(normalized, model)
        if bad is not None:
            raise ValidationError(f"model falsifies normalized clause {bad}")
        # And against the original clauses directly (tautologies always true).
        for i, clause in enumerate(original_clauses):
            if any(literal_value(lit, model) == 1 for lit in set(clause)):
                continue
            if i in taut_indices:
                continue
            raise ValidationError(f"model falsifies original clause {i}")
        return "sat"

    if verdict != 0:
        raise ValidationError("response claims unsat but replayed tree finds sat")
    if doc.get("model") is not None:
        raise ValidationError("unsat response unexpectedly carries a model")
    return "unsat"
