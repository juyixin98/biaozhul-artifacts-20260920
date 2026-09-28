"""Independent finite-model enumerator (the *oracle*).

This module is an independent reference implementation used to cross-check the
forward-chaining kernel.  Independence guarantees:

* it does **not** import the rule/kernel packages of this project;
* it does not build flat production rules and does not run fixpoint loops;
* it reads the shared *syntactic* AST only to flatten each class expression into
  a frozenset of named-class conjuncts (``_flatten`` is purely syntactic), then
  applies the raw set-theoretic semantics by brute force.

Why one-element checks suffice here
-----------------------------------
The restricted fragment has no object/datatype properties, no cardinality, no
``oneOf``, no equality/different-individuals and no existential restrictions.
Every axiom is a Boolean, universally-quantified constraint on the *type label*
of a single domain element, and every named class may have an empty extension.
Therefore:

* class satisfiability is witnessed or refuted on a domain of size 1;
* individuals do not interact, so ontology consistency reduces to an
  independent valid-label search per individual.

All 2**|classes|| labels are enumerated (small synthetic fixtures only).
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations

from ..lang import ast

__all__ = [
    "NormalizedOntology",
    "OracleVerdict",
    "normalize",
    "enumerate_ontology",
]


# --------------------------------------------------------------------------- #
# Purely syntactic normalization (no entailment reasoning whatsoever)
# --------------------------------------------------------------------------- #
def _flatten(expr: ast.ClassExpr) -> frozenset[str]:
    """Atomic named classes an element must carry to satisfy ``expr``."""
    if isinstance(expr, ast.ClassName):
        return frozenset({expr.name})
    out: set[str] = set()
    for op in expr.operands:
        out |= _flatten(op)
    return frozenset(out)


@dataclass(frozen=True, slots=True)
class NormalizedOntology:
    classes: tuple[str, ...]
    # Each constraint over one element's label L (a frozenset[str]):
    #   ("sub",      sub:frozenset, sup:frozenset)
    #   ("equiv",    tuple[frozenset, ...])
    #   ("disjoint", tuple[frozenset, ...])
    tbox: tuple[tuple, ...]
    # individual -> required class conjuncts (from its ClassAssertions)
    abox: tuple[tuple[str, frozenset[str]], ...]


def normalize(axioms: list[ast.Axiom]) -> NormalizedOntology:
    tbox: list[tuple] = []
    abox: dict[str, set[str]] = {}
    classes: set[str] = set()

    def touch(expr: ast.ClassExpr) -> frozenset[str]:
        conj = _flatten(expr)
        classes.update(conj)
        return conj

    for axiom in axioms:
        if isinstance(axiom, ast.SubClassOf):
            tbox.append(("sub", touch(axiom.sub), touch(axiom.sup)))
        elif isinstance(axiom, ast.EquivalentClasses):
            ops = tuple(touch(o) for o in axiom.operands)
            tbox.append(("equiv", ops))
        elif isinstance(axiom, ast.DisjointClasses):
            ops = tuple(touch(o) for o in axiom.operands)
            tbox.append(("disjoint", ops))
        elif isinstance(axiom, ast.ClassAssertion):
            conj = touch(axiom.cls)
            abox.setdefault(axiom.individual, set()).update(conj)
        else:  # pragma: no cover - exhaustiveness
            raise TypeError(f"unexpected axiom node {type(axiom)!r}")

    return NormalizedOntology(
        classes=tuple(sorted(classes)),
        tbox=tuple(tbox),
        abox=tuple(sorted((name, frozenset(req)) for name, req in abox.items())),
    )


# --------------------------------------------------------------------------- #
# Raw semantics, label by label
# --------------------------------------------------------------------------- #
def _satisfies(label: frozenset[str], conjuncts: frozenset[str]) -> bool:
    return conjuncts <= label


def _label_is_tbox_valid(label: frozenset[str], tbox: tuple[tuple, ...]) -> bool:
    for cons in tbox:
        kind = cons[0]
        if kind == "sub":
            _, sub, sup = cons
            if _satisfies(label, sub) and not _satisfies(label, sup):
                return False
        elif kind == "equiv":
            ops = cons[1]
            flags = [_satisfies(label, op) for op in ops]
            if any(flag != flags[0] for flag in flags[1:]):
                return False
        elif kind == "disjoint":
            ops = cons[1]
            for a, b in combinations(ops, 2):
                if _satisfies(label, a) and _satisfies(label, b):
                    return False
    return True


def _all_labels(classes: tuple[str, ...]):
    """Yield every subset of the class universe as a frozenset."""
    yield frozenset()
    for size in range(1, len(classes) + 1):
        for chosen in combinations(classes, size):
            yield frozenset(chosen)


@dataclass(frozen=True, slots=True)
class IndividualOracle:
    individual: str
    satisfiable: bool
    # Labels consistent with TBox + this individual's assertions.
    valid_labels: tuple[frozenset[str], ...]
    # Classes present in EVERY valid label (cautiously entailed instance types).
    entailed_types: frozenset[str]
    required: frozenset[str]


@dataclass(frozen=True, slots=True)
class ClassOracle:
    cls: str
    satisfiable: bool
    # One witness label (non-empty extension) when satisfiable.
    witness: frozenset[str] | None


@dataclass(frozen=True, slots=True)
class OracleVerdict:
    classes: tuple[ClassOracle, ...]
    individuals: tuple[IndividualOracle, ...]
    ontology_consistent: bool
    # Subsumption C ⊑ D over named classes: pair in every valid label scenario.
    subsumptions: tuple[tuple[str, str], ...]
    labels_explored: int
    fragment_note: str


def _all_tbox_valid_labels(onto: NormalizedOntology) -> list[frozenset[str]]:
    return [
        label
        for label in _all_labels(onto.classes)
        if _label_is_tbox_valid(label, onto.tbox)
    ]


def enumerate_ontology(onto: NormalizedOntology) -> OracleVerdict:
    valid_labels = _all_tbox_valid_labels(onto)
    explored = 2 ** len(onto.classes)

    # ---- class satisfiability: valid label containing the class ------------
    class_results: list[ClassOracle] = []
    for cls in onto.classes:
        witness = next(
            (label for label in valid_labels if cls in label),
            None,
        )
        class_results.append(
            ClassOracle(cls=cls, satisfiable=witness is not None, witness=witness)
        )

    # ---- individuals: valid labels extending their required conjuncts ------
    individual_results: list[IndividualOracle] = []
    ontology_consistent = True
    for individual, required in onto.abox:
        fitting = [
            label for label in valid_labels if _satisfies(label, required)
        ]
        if not fitting:
            ontology_consistent = False
            entailed: frozenset[str] = frozenset(onto.classes)  # ex falso
        else:
            entailed = frozenset.intersection(*fitting) if fitting else frozenset()
        individual_results.append(
            IndividualOracle(
                individual=individual,
                satisfiable=bool(fitting),
                valid_labels=tuple(sorted(fitting, key=lambda s: (len(s), sorted(s)))),
                entailed_types=entailed,
                required=required,
            )
        )

    # ---- named-class subsumptions (independent entailed hierarchy) ---------
    subs: list[tuple[str, str]] = []
    for c in onto.classes:
        labels_with_c = [label for label in valid_labels if c in label]
        if not labels_with_c:
            # Unsatisfiable class entails every named class (ex falso); the
            # kernel reports it via unsatisfiability instead, so cross-checks
            # compare subsumptions only among satisfiable classes. Keep the raw
            # relation out of the comparison list to avoid a notational clash.
            continue
        for d in onto.classes:
            if c != d and all(d in label for label in labels_with_c):
                subs.append((c, d))

    return OracleVerdict(
        classes=tuple(class_results),
        individuals=tuple(individual_results),
        ontology_consistent=ontology_consistent,
        subsumptions=tuple(subs),
        labels_explored=explored,
        fragment_note=(
            "unary Boolean constraints; no properties/cardinality/equality; "
            "domain size 1 is complete per element"
        ),
    )
