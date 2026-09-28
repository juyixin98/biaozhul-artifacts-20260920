"""Compile-time analysis.

Responsibilities
----------------
1. **Arity checks** — a predicate name always carries the same arity.
2. **Variable safety (range restriction)** — every variable in a rule head,
   a negated literal, or a comparison must be bound by a *positive*
   relational body literal.  Anonymous variables (``_``) are existential
   and exempt, but a named variable occurring only under negation is
   rejected (it would flounder).
3. **Stratification** — strongly connected components are computed over the
   predicate dependency graph; an SCC that contains a negative edge means
   negation depends on a cycle and the program is rejected with
   :class:`NegationCycleError`.
4. **Version fingerprint** — SHA-256 over a canonical rendering of facts
   and rules, identifying the exact program version used in a derivation.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass

from .errors import ArityError, NegationCycleError, UnsafeVariableError
from .terms import Atom, Comparison, NegAtom, Program, Rule

PredKey = tuple[str, int]
WILDCARD_PREFIX = "_w"  # fresh names produced by the parser for '_'


@dataclass(frozen=True)
class CompiledProgram:
    program: Program
    strata: tuple[tuple[PredKey, ...], ...]
    stratum_of: dict[PredKey, int]
    rules_by_stratum: tuple[tuple[int, ...], ...]
    arities: dict[str, int]
    idb_preds: frozenset[PredKey]
    version: str


def compile_program(program: Program) -> CompiledProgram:
    arities = _check_arities(program)
    for rule in program.rules:
        _check_rule_safety(rule)
    return _stratify(program, arities)


# ---------------------------------------------------------------------------
# Arity
# ---------------------------------------------------------------------------


def _check_arities(program: Program) -> dict[str, int]:
    arities: dict[str, int] = {}

    def note(atom: Atom) -> None:
        seen = arities.get(atom.pred)
        if seen is None:
            arities[atom.pred] = atom.arity
        elif seen != atom.arity:
            raise ArityError(
                f"predicate {atom.pred}/{seen} reused with arity {atom.arity} "
                f"in {atom.render()}"
            )

    for fact in program.facts:
        note(fact)
    for rule in program.rules:
        note(rule.head)
        for lit in rule.body:
            if isinstance(lit, Atom):
                note(lit)
            elif isinstance(lit, NegAtom):
                note(lit.atom)
    return arities


# ---------------------------------------------------------------------------
# Safety
# ---------------------------------------------------------------------------


def _bound_variables(rule: Rule) -> frozenset[str]:
    out: set[str] = set()
    for lit in rule.body:
        if isinstance(lit, Atom):
            out.update(lit.variables())
    return frozenset(out)


def _is_wildcard(name: str) -> bool:
    return name.startswith(WILDCARD_PREFIX)


def _check_rule_safety(rule: Rule) -> None:
    bound = _bound_variables(rule)

    head_named = {v for v in rule.head.variables() if not _is_wildcard(v)}
    unbound_head = sorted(head_named - bound)
    if unbound_head:
        raise UnsafeVariableError(
            f"rule {rule.render()}: head variables {unbound_head} are not "
            "bound by any positive body literal"
        )
    head_wild = [v for v in rule.head.variables() if _is_wildcard(v)]
    if head_wild:
        raise UnsafeVariableError(
            f"rule {rule.render()}: anonymous variable '_' is not allowed "
            "in a rule head"
        )

    for lit in rule.body:
        if isinstance(lit, NegAtom):
            bad = sorted(v for v in lit.variables() - bound if not _is_wildcard(v))
            if bad:
                raise UnsafeVariableError(
                    f"rule {rule.render()}: variables {bad} occur only in "
                    f"negated literal {lit.render()} (would flounder)"
                )
        elif isinstance(lit, Comparison):
            bad = sorted(v for v in lit.variables() - bound if not _is_wildcard(v))
            if bad:
                raise UnsafeVariableError(
                    f"rule {rule.render()}: variables {bad} in comparison "
                    f"{lit.render()} are not bound by a positive body literal"
                )


# ---------------------------------------------------------------------------
# Dependency graph + stratification
# ---------------------------------------------------------------------------


def _body_preds(rule: Rule) -> list[tuple[PredKey, bool]]:
    """Return ``(predicate, is_negative)`` for each relational body literal."""

    out: list[tuple[PredKey, bool]] = []
    for lit in rule.body:
        if isinstance(lit, Atom):
            out.append(((lit.pred, lit.arity), False))
        elif isinstance(lit, NegAtom):
            out.append(((lit.atom.pred, lit.atom.arity), True))
    return out


def _tarjan_sccs(nodes: list[PredKey], edges: dict[PredKey, list[PredKey]]) -> list[list[PredKey]]:
    index = 0
    stack: list[PredKey] = []
    indices: dict[PredKey, int] = {}
    low: dict[PredKey, int] = {}
    on_stack: set[PredKey] = set()
    result: list[list[PredKey]] = []

    def strong(v: PredKey) -> None:
        nonlocal index
        indices[v] = low[v] = index
        index += 1
        stack.append(v)
        on_stack.add(v)
        for w in edges.get(v, ()):  # type: ignore[union-attr]
            if w not in indices:
                strong(w)
                low[v] = min(low[v], low[w])
            elif w in on_stack:
                low[v] = min(low[v], indices[w])
        if low[v] == indices[v]:
            comp: list[PredKey] = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                comp.append(w)
                if w == v:
                    break
            result.append(comp)

    for v in nodes:
        if v not in indices:
            strong(v)
    return result


def _stratify(program: Program, arities: dict[str, int]) -> CompiledProgram:
    # Every predicate that appears anywhere is a node; EDB-only predicates
    # have no outgoing edges and land in stratum 0.
    nodes: set[PredKey] = set()
    for name, arity in arities.items():
        nodes.add((name, arity))

    edges: dict[PredKey, list[PredKey]] = defaultdict(list)
    neg_edges: set[tuple[PredKey, PredKey]] = set()
    idb: set[PredKey] = set()
    rule_deps: dict[int, list[tuple[PredKey, bool]]] = {}

    for ri, rule in enumerate(program.rules):
        h = (rule.head.pred, rule.head.arity)
        idb.add(h)
        deps = _body_preds(rule)
        rule_deps[ri] = deps
        for dep, negative in deps:
            edges[dep].append(h)
            if negative:
                neg_edges.add((dep, h))

    node_list = sorted(nodes)
    sccs = _tarjan_sccs(node_list, edges)
    scc_of: dict[PredKey, int] = {}
    for si, comp in enumerate(sccs):
        for p in comp:
            scc_of[p] = si

    # A negative edge whose endpoints share an SCC => unstratifiable.
    for src, dst in sorted(neg_edges):
        if scc_of[src] == scc_of[dst]:
            raise NegationCycleError(
                f"negation cycle through {src[0]}/{src[1]} and "
                f"{dst[0]}/{dst[1]}: a negated dependency participates in a "
                "recursive cycle, so no stratified model exists"
            )

    # Stratum of each SCC: max over incoming edges (dep -> SCC member) of
    #   stratum(dep_scc) + (1 if edge negative else 0)
    # EDB SCCs (no rule heads) are stratum 0.
    scc_stratum: dict[int, int] = {}

    def scc_level(si: int) -> int:
        if si in scc_stratum:
            return scc_stratum[si]
        level = 0
        members = set(sccs[si])
        for dep, hs in edges.items():
            if scc_of[dep] == si:
                # Positive edge inside a recursive SCC: does not raise the
                # stratum level (and would recurse forever if followed).
                continue
            for h in hs:
                if h not in members:
                    continue
                edge_level = scc_level(scc_of[dep]) + (1 if (dep, h) in neg_edges else 0)
                level = max(level, edge_level)
        scc_stratum[si] = level
        return level

    for si in range(len(sccs)):
        scc_level(si)

    stratum_of = {p: scc_stratum[scc_of[p]] for p in node_list}

    # Group IDB predicates and rule indices by stratum.
    max_level = max(stratum_of.values(), default=0)
    pred_buckets: list[list[PredKey]] = [[] for _ in range(max_level + 1)]
    rule_buckets: list[list[int]] = [[] for _ in range(max_level + 1)]
    for p in node_list:
        if p in idb:
            pred_buckets[stratum_of[p]].append(p)
    for ri, rule in enumerate(program.rules):
        h = (rule.head.pred, rule.head.arity)
        rule_buckets[stratum_of[h]].append(ri)

    strata = tuple(tuple(sorted(b)) for b in pred_buckets)
    rules_by_stratum = tuple(tuple(b) for b in rule_buckets)

    version = _fingerprint(program)
    return CompiledProgram(
        program=program,
        strata=strata,
        stratum_of=stratum_of,
        rules_by_stratum=rules_by_stratum,
        arities=arities,
        idb_preds=frozenset(idb),
        version=version,
    )


# ---------------------------------------------------------------------------
# Version
# ---------------------------------------------------------------------------


def _fingerprint(program: Program) -> str:
    lines = ["FACTS"]
    lines.extend(sorted(a.render() for a in program.facts))
    lines.append("RULES")
    lines.extend(r.render() for r in program.rules)
    digest = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
    return f"sha256:{digest[:16]}"
