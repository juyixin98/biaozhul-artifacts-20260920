"""Independent finite-model oracle.

This module is a **separate reference implementation** used to cross-check
the reasoning kernel. It deliberately shares *no code* with
``app.kernel`` -- the only shared piece is the language parser
(``app.language``), which is syntax, not inference.

Semantics (Tarski, ground over one individual at a time):

* every named class is a propositional variable = "this individual belongs
  to the class";
* every intersection expression is a fresh variable constrained by
  ``I  <->  O1 and O2 and ...`` (membership in an intersection is exactly
  conjunctive membership);
* subclass A ⊑ B, equivalence A ≡ B, disjoint A ⊥ B become CNF clauses that
  must hold for *every* individual;
* an instance assertion ``i : C`` is a unit clause on individual i only.

All satisfying assignments are enumerated by brute force. Fixtures and the
fuzz tests keep variable counts small (<= 12), so enumeration is exact and
fast -- no SAT library, no approximation, no sharing with the kernel.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass

from ..language import (
    ClassAssertion,
    ClassRef,
    Disjoint,
    Equivalent,
    Ontology,
    SubClass,
)

# A clause is a list of signed var indices: (index, is_positive).
Literal = tuple[int, bool]
Clause = list[Literal]


@dataclass(frozen=True)
class OracleVerdict:
    consistent: bool
    satisfiable_classes: frozenset[str]
    unsatisfiable_classes: frozenset[str]
    # unsatisfiable *all* expression variables (named classes + intersections),
    # keyed by canonical name (IRI for classes, and(...) for intersections)
    unsatisfiable_nodes: frozenset[str]
    # pairs (sub, super) entailed among named classes
    subsumptions: frozenset[tuple[str, str]]
    # frozenset of frozensets of names forced equivalent
    equivalence_classes: frozenset[frozenset[str]]
    # instance -> set of class names true in every model of that individual
    instance_entailed_types: dict[str, frozenset[str]]
    model_count: int
    variable_count: int


class _CompiledOntology:
    def __init__(self, onto: Ontology) -> None:
        self.var_index: dict[str, int] = {}
        self.clauses: list[Clause] = []
        self.assertions: dict[str, list[Clause]] = {}
        self.named: list[str] = []

        def intern(name: str, named: bool) -> int:
            if name not in self.var_index:
                self.var_index[name] = len(self.var_index)
                if named:
                    self.named.append(name)
            return self.var_index[name]

        compiled_exprs: set[str] = set()

        def compile_name(expr) -> str:
            """Compile an expression, returning its canonical variable name."""
            if isinstance(expr, ClassRef):
                intern(expr.iri, named=True)
                return expr.iri
            sub_names = sorted(compile_name(o) for o in expr.operands)
            key = "and(" + ",".join(sub_names) + ")"
            idx = intern(key, named=False)
            if key not in compiled_exprs:
                compiled_exprs.add(key)
                sub = [self.var_index[s] for s in sub_names]
                for s in sub:                      # I -> Oi
                    self.clauses.append([(idx, False), (s, True)])
                # O1 and ... and On -> I
                self.clauses.append(
                    [(s, False) for s in sub] + [(idx, True)]
                )
            return key

        def compile_expr(expr) -> int:
            return self.var_index[compile_name(expr)]

        for ax in onto.axioms:
            if isinstance(ax, SubClass):
                a, b = compile_expr(ax.sub), compile_expr(ax.super)
                self.clauses.append([(a, False), (b, True)])
            elif isinstance(ax, Equivalent):
                nodes = [compile_expr(e) for e in ax.operands]
                for x, y in itertools.combinations(nodes, 2):
                    self.clauses.append([(x, False), (y, True)])
                    self.clauses.append([(y, False), (x, True)])
            elif isinstance(ax, Disjoint):
                nodes = [compile_expr(e) for e in ax.operands]
                for x, y in itertools.combinations(nodes, 2):
                    self.clauses.append([(x, False), (y, False)])
            elif isinstance(ax, ClassAssertion):
                v = compile_expr(ax.cls)
                self.assertions.setdefault(ax.instance, []).append([(v, True)])

        self.names = sorted(self.var_index, key=lambda k: self.var_index[k])
        self.named = sorted(set(self.named))

    # ------------------------------------------------------------------
    def all_models(self, extra: list[Clause] | None = None):
        """Enumerate every satisfying truth assignment as a tuple[bool]."""
        clauses = self.clauses + (extra or [])
        n = len(self.var_index)
        for bits in itertools.product((False, True), repeat=n):
            if all(any(bits[i] == pos for i, pos in cl) for cl in clauses):
                yield bits


def evaluate(onto: Ontology, *, model_cap: int = 100_000) -> OracleVerdict:
    """Return the exact model-theoretic answers for ``onto``."""
    comp = _CompiledOntology(onto)
    n = len(comp.var_index)
    if 2 ** n > model_cap:
        raise ValueError(
            f"oracle enumeration would inspect 2^{n} assignments; "
            f"keep test ontologies small (cap={model_cap})"
        )

    class_models: list[tuple[bool, ...]] = list(comp.all_models())
    model_count = len(class_models)

    index = comp.var_index
    satisfiable: set[str] = set()
    for name in comp.named:
        v = index[name]
        if any(bits[v] for bits in class_models):
            satisfiable.add(name)
    unsat = frozenset(set(comp.named) - satisfiable)

    # Unsatisfiability of EVERY expression variable, including intersections.
    # (Named classes alone hide when an intersection expression is empty.)
    unsat_nodes = frozenset(
        name for name, v in index.items()
        if not any(bits[v] for bits in class_models)
    )

    # Subsumption A ⊑ B entailed iff no model has A=1, B=0.
    subs: set[tuple[str, str]] = set()
    for a_name, b_name in itertools.product(comp.named, repeat=2):
        if a_name == b_name:
            continue
        va, vb = index[a_name], index[b_name]
        if not any(bits[va] and not bits[vb] for bits in class_models):
            subs.add((a_name, b_name))

    # Equivalence classes = connected components of mutual subsumption.
    parent = {x: x for x in comp.named}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a_name, b_name in subs:
        if (b_name, a_name) in subs:
            parent[find(a_name)] = find(b_name)
    groups: dict[str, set[str]] = {}
    for x in comp.named:
        groups.setdefault(find(x), set()).add(x)
    equiv_classes = frozenset(
        frozenset(g) for g in groups.values() if len(g) > 1
    )

    # Instances: each named individual gets its own model set = class models
    # satisfying its assertion units. An instance with zero models witnesses
    # global inconsistency.
    consistent = model_count > 0
    inst_types: dict[str, frozenset[str]] = {}
    for inst, units in sorted(comp.assertions.items()):
        models = list(comp.all_models(units))
        if not models:
            consistent = False
        forced = frozenset(
            name for name in comp.named
            if all(bits[index[name]] for bits in models)
        ) if models else frozenset()
        inst_types[inst] = forced

    return OracleVerdict(
        consistent=consistent,
        satisfiable_classes=frozenset(satisfiable),
        unsatisfiable_classes=unsat,
        unsatisfiable_nodes=unsat_nodes,
        subsumptions=frozenset(subs),
        equivalence_classes=equiv_classes,
        instance_entailed_types=inst_types,
        model_count=model_count,
        variable_count=n,
    )
