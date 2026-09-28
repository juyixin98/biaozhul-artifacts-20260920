"""Compile-time analysis: arity, variable safety and stratification.

The compiler turns a parsed :class:`~datalog_service.language.ast.Program`
into a :class:`CompiledProgram` without evaluating anything.

Checks performed (errors carry stable codes used by the API and tests):

* ``ARITY_MISMATCH``      - one predicate used with different arities.
* ``UNSAFE_VARIABLE``     - a head or negated variable is not positively
                            range-restricted by a positive body literal.
* ``NEGATION_CYCLE``      - the predicate dependency graph has a cycle that
                            traverses a negative edge (stratified negation
                            is impossible).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Dict, FrozenSet, List, Set, Tuple

from .ast import Atom, Program, Rule, Variable
from .errors import CompileError, Issue


@dataclass(frozen=True)
class CompiledRule:
    rule_id: str
    rule_hash: str
    text: str
    head: Atom
    positive: Tuple[Atom, ...]
    negative: Tuple[Atom, ...]
    source_index: int

    @property
    def head_predicate(self) -> str:
        return self.head.predicate


@dataclass(frozen=True)
class PredicateInfo:
    name: str
    arity: int
    defined_by_facts: bool
    defined_by_rules: bool
    stratum: int


@dataclass(frozen=True)
class CompiledProgram:
    rules: Tuple[CompiledRule, ...]
    predicates: Dict[str, PredicateInfo]
    # Ordered evaluation groups; rules[s] are the rules whose head lives on
    # stratum s (stratum 0 holds all purely positive/EDB-driven rules).
    strata: Tuple[Tuple[CompiledRule, ...], ...]
    rule_version: str
    normalized_text: str
    positive_edges: FrozenSet[Tuple[str, str]]
    negative_edges: FrozenSet[Tuple[str, str]]


def _rule_hash(rule: Rule) -> str:
    return sha256(rule.canonical().encode("utf-8")).hexdigest()[:16]


def _variables(atom: Atom) -> Set[str]:
    return {t.name for t in atom.args if isinstance(t, Variable)}


def compile_program(program: Program) -> CompiledProgram:
    issues: List[Issue] = []

    signatures = _collect_signatures(program, issues)
    rules = _compile_rules(program.rules)
    for cr in rules:
        _check_rule_safety(cr, issues)

    pos_edges, neg_edges = _build_dependency_edges(rules)
    strata = _stratify(signatures, pos_edges, neg_edges, rules, issues)

    if issues:
        raise CompileError(issues)

    predicate_info = _predicate_table(
        signatures, strata,
        fact_predicates={f.predicate for f in program.facts},
        head_predicates={r.head_predicate for r in rules},
    )
    ordered_strata = _group_strata(rules, strata)
    rule_version, normalized_text = _version_text(program)

    return CompiledProgram(
        rules=tuple(rules),
        predicates=predicate_info,
        strata=ordered_strata,
        rule_version=rule_version,
        normalized_text=normalized_text,
        positive_edges=frozenset(pos_edges),
        negative_edges=frozenset(neg_edges),
    )


def _compile_rules(rules: Tuple[Rule, ...]) -> List[CompiledRule]:
    return [
        CompiledRule(
            rule_id=f"r{idx + 1}",
            rule_hash=_rule_hash(rule),
            text=rule.canonical(),
            head=rule.head,
            positive=tuple(lit.atom for lit in rule.body if not lit.negated),
            negative=tuple(lit.atom for lit in rule.body if lit.negated),
            source_index=idx,
        )
        for idx, rule in enumerate(rules)
    ]


def _predicate_table(
    signatures: Dict[str, int],
    strata: Dict[str, int],
    fact_predicates: Set[str],
    head_predicates: Set[str],
) -> Dict[str, PredicateInfo]:
    return {
        name: PredicateInfo(
            name=name,
            arity=arity,
            defined_by_facts=name in fact_predicates,
            defined_by_rules=name in head_predicates,
            stratum=strata[name],
        )
        for name, arity in signatures.items()
    }


def _group_strata(
    rules: List[CompiledRule], strata: Dict[str, int]
) -> Tuple[Tuple[CompiledRule, ...], ...]:
    grouped: Dict[int, List[CompiledRule]] = defaultdict(list)
    for cr in rules:
        grouped[strata[cr.head_predicate]].append(cr)
    return tuple(
        tuple(sorted(grouped[s], key=lambda r: r.source_index))
        for s in sorted(grouped)
    )


def _version_text(program: Program) -> Tuple[str, str]:
    # Rule version hashes rules alone; fact content is versioned separately
    # by the engine (fact_set_version), so swapping facts does not change
    # the rule version.
    rules_canonical = "\n".join(sorted({r.canonical() for r in program.rules}))
    rule_version = sha256(rules_canonical.encode("utf-8")).hexdigest()[:16]
    normalized_text = "\n".join(
        sorted({f.canonical() for f in program.facts})
        + [r.canonical() for r in sorted(program.rules, key=lambda r: r.canonical())]
    )
    return rule_version, normalized_text

def _collect_signatures(program: Program, issues: List[Issue]) -> Dict[str, int]:
    seen: Dict[str, int] = {}

    def record(predicate: str, arity: int, where: str) -> None:
        if predicate in seen and seen[predicate] != arity:
            issues.append(
                Issue(
                    "ARITY_MISMATCH",
                    f"predicate {predicate}/{seen[predicate]} used again with "
                    f"arity {arity} in {where}",
                    {"predicate": predicate, "expected_arity": seen[predicate],
                     "found_arity": arity},
                )
            )
        else:
            seen[predicate] = arity

    for fact in program.facts:
        record(fact.predicate, fact.arity, f"fact {fact.canonical()}")
    for rule in program.rules:
        record(rule.head.predicate, rule.head.arity, f"rule head {rule.head.canonical()}")
        for lit in rule.body:
            record(lit.predicate, lit.arity, f"rule {rule.head.canonical()}")
    return seen


def _check_rule_safety(cr: CompiledRule, issues: List[Issue]) -> None:
    positive_vars: Set[str] = set()
    for atom in cr.positive:
        positive_vars |= _variables(atom)

    head_vars = _variables(cr.head)
    missing_head = head_vars - positive_vars
    for var in sorted(missing_head):
        issues.append(
            Issue(
                "UNSAFE_VARIABLE",
                f"rule {cr.rule_id} ({cr.text}): head variable {var} is not "
                f"bound by any positive body literal",
                {"rule_id": cr.rule_id, "variable": var, "position": "head"},
            )
        )

    for atom in cr.negative:
        for var in sorted(_variables(atom) - positive_vars):
            issues.append(
                Issue(
                    "UNSAFE_VARIABLE",
                    f"rule {cr.rule_id} ({cr.text}): variable {var} in negated "
                    f"literal {atom.canonical()} is not bound by a positive "
                    f"body literal",
                    {"rule_id": cr.rule_id, "variable": var, "position": "negation",
                     "literal": atom.canonical()},
                )
            )


def _build_dependency_edges(
    rules: List[CompiledRule],
) -> Tuple[Set[Tuple[str, str]], Set[Tuple[str, str]]]:
    positive: Set[Tuple[str, str]] = set()
    negative: Set[Tuple[str, str]] = set()
    for cr in rules:
        for atom in cr.positive:
            positive.add((cr.head_predicate, atom.predicate))
        for atom in cr.negative:
            negative.add((cr.head_predicate, atom.predicate))
    return positive, negative


def _stratify(
    signatures: Dict[str, int],
    pos_edges: Set[Tuple[str, str]],
    neg_edges: Set[Tuple[str, str]],
    rules: List[CompiledRule],
    issues: List[Issue],
) -> Dict[str, int]:
    """Assign strata; SCCs containing a negative edge are rejected."""
    nodes = set(signatures)
    adj: Dict[str, Set[str]] = defaultdict(set)
    for p, q in pos_edges | neg_edges:
        adj[p].add(q)

    sccs = _strongly_connected_components(nodes, adj)
    component_of = _component_map(sccs)
    _report_negation_cycles(sccs, component_of, neg_edges, adj, issues)
    comp_strata = _assign_component_strata(
        sccs, component_of, pos_edges, neg_edges
    )
    return {node: comp_strata[component_of[node]] for node in nodes}


def _component_map(sccs: List[Set[str]]) -> Dict[str, int]:
    return {
        node: index
        for index, component in enumerate(sccs)
        for node in component
    }


def _report_negation_cycles(
    sccs: List[Set[str]],
    component_of: Dict[str, int],
    neg_edges: Set[Tuple[str, str]],
    adj: Dict[str, Set[str]],
    issues: List[Issue],
) -> None:
    # One issue per SCC containing a negative edge; details list every edge.
    bad_components: Dict[int, List[Tuple[str, str]]] = defaultdict(list)
    for p, q in neg_edges:
        if component_of[p] == component_of[q]:
            bad_components[component_of[p]].append((p, q))
    for edges in bad_components.values():
        p, q = sorted(edges)[0]
        cycle = _describe_cycle(p, q, adj)
        issues.append(
            Issue(
                "NEGATION_CYCLE",
                f"negation on {q} participates in a dependency cycle "
                f"({' -> '.join(cycle)}); stratified negation is impossible",
                {"edge": [p, q], "cycle": cycle,
                 "negative_edges": [list(e) for e in edges]},
            )
        )


def _assign_component_strata(
    sccs: List[Set[str]],
    component_of: Dict[str, int],
    pos_edges: Set[Tuple[str, str]],
    neg_edges: Set[Tuple[str, str]],
) -> Dict[int, int]:
    """Longest weighted path over the SCC DAG.

    Positive edge weight 0, negative edge weight 1: stratum(cp) >=
    stratum(cq) + weight.  Kahn order, dependencies first (no recursion).
    """
    dag_edges = _component_dag_edges(component_of, pos_edges, neg_edges)
    dependents: Dict[int, Set[int]] = defaultdict(set)
    pending: Dict[int, int] = {c: 0 for c in range(len(sccs))}
    for cp, deps in dag_edges.items():
        pending[cp] = len({cq for cq, _ in deps})
        for cq, _ in deps:
            dependents[cq].add(cp)

    ready = [c for c, remaining in pending.items() if remaining == 0]
    comp_strata: Dict[int, int] = {}
    while ready:
        cq = ready.pop()
        comp_strata[cq] = max(
            (comp_strata[c] + weight for c, weight in dag_edges.get(cq, ())),
            default=0,
        )
        for cp in dependents.get(cq, ()):
            pending[cp] -= 1
            if pending[cp] == 0:
                ready.append(cp)

    if len(comp_strata) != len(sccs):  # pragma: no cover - SCC DAG is acyclic
        raise RuntimeError("internal error: SCC dependency graph is cyclic")
    return comp_strata


def _component_dag_edges(
    component_of: Dict[str, int],
    pos_edges: Set[Tuple[str, str]],
    neg_edges: Set[Tuple[str, str]],
) -> Dict[int, Set[Tuple[int, int]]]:
    dag_edges: Dict[int, Set[Tuple[int, int]]] = defaultdict(set)
    for p, q in pos_edges:
        cp, cq = component_of[p], component_of[q]
        if cp != cq:
            dag_edges[cp].add((cq, 0))
    for p, q in neg_edges:
        cp, cq = component_of[p], component_of[q]
        if cp != cq:
            dag_edges[cp].add((cq, 1))
    return dag_edges

def _strongly_connected_components(
    nodes: Set[str], adj: Dict[str, Set[str]]
) -> List[Set[str]]:
    """Iterative Tarjan (avoids recursion limits on large predicate graphs)."""
    index_counter = 0
    indices: Dict[str, int] = {}
    lowlink: Dict[str, int] = {}
    on_stack: Set[str] = set()
    stack: List[str] = []
    result: List[Set[str]] = []

    for root in sorted(nodes):
        if root in indices:
            continue
        work: List[Tuple[str, int]] = [(root, 0)]
        while work:
            v, child_i = work[-1]
            if child_i == 0:
                indices[v] = index_counter
                lowlink[v] = index_counter
                index_counter += 1
                stack.append(v)
                on_stack.add(v)
            neighbours = sorted(adj.get(v, ()))
            if child_i < len(neighbours):
                w = neighbours[child_i]
                work[-1] = (v, child_i + 1)
                if w not in indices:
                    work.append((w, 0))
                elif w in on_stack:
                    lowlink[v] = min(lowlink[v], indices[w])
            else:
                if lowlink[v] == indices[v]:
                    component: Set[str] = set()
                    while True:
                        w = stack.pop()
                        on_stack.discard(w)
                        component.add(w)
                        if w == v:
                            break
                    result.append(component)
                work.pop()
                if work:
                    parent = work[-1][0]
                    lowlink[parent] = min(lowlink[parent], lowlink[v])
    return result


def _describe_cycle(p: str, q: str, adj: Dict[str, Set[str]]) -> List[str]:
    """Find a path q -> ... -> p to display the offending cycle."""
    queue: List[Tuple[str, ...]] = [(q,)]
    seen = {q}
    while queue:
        path = queue.pop(0)
        node = path[-1]
        for nxt in sorted(adj.get(node, ())):
            if nxt == p:
                return [p] + list(path) + [p]
            if nxt not in seen:
                seen.add(nxt)
                queue.append(path + (nxt,))
    return [p, q, p]
