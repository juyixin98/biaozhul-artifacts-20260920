"""Forward-chaining saturation kernel with proof trees.

The fragment compiled by :mod:`min_iowl.lang.compiler` is Horn, so a plain
least-fixed-point of the flat conjunctive rules is a *complete* decision
procedure for the supported consequences -- no hard-coded demos.

Two kinds of reasoning are deliberately kept separate:

* **TBox class satisfiability** -- a hypothetical individual is assumed to
  belong to class ``C`` (tagged ``HYPOTHESIS``, not a stored fact).  If the
  disjointness rules derive bottom from that assumption alone, ``C`` is
  *unsatisfiable*.  This says nothing about whether the ontology as a whole is
  inconsistent.
* **ABox ontology consistency** -- saturation runs over the *declared*
  individuals only.  Bottom derived for a real individual means the *ontology is
  inconsistent*.  An ontology with unsatisfiable classes but no instance of them
  stays consistent; the two states are reported independently.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from ..lang.compiler import BOTTOM, CompiledOntology, Fact, Rule

__all__ = [
    "BaseNode",
    "DerivedNode",
    "IndividualResult",
    "UnsatisfiableClass",
    "SaturationReport",
    "saturate",
    "class_hypothetical_supers",
]

HYPOTHESIS = "__hypothesis__"


@dataclass(frozen=True, slots=True)
class BaseNode:
    """Leaf of a proof tree: an asserted fact or a class-satisfiability hypothesis."""

    kind: Literal["fact", "hypothesis"]
    predicate: str
    source_axiom: str

    def sources(self) -> tuple[str, ...]:
        return (self.source_axiom,)


@dataclass(frozen=True, slots=True)
class DerivedNode:
    """Internal node: one rule firing with proofs for every body literal."""

    predicate: str
    rule_id: str
    rule_kind: str
    source_axiom: str
    children: tuple["ProofNode", ...]

    def sources(self) -> tuple[str, ...]:
        collected: list[str] = []
        seen: set[str] = set()
        for child in self.children:
            for src in child.sources():
                if src not in seen and src != HYPOTHESIS:
                    seen.add(src)
                    collected.append(src)
        if self.source_axiom not in seen and self.source_axiom != HYPOTHESIS:
            collected.append(self.source_axiom)
        return tuple(collected)


ProofNode = BaseNode | DerivedNode


@dataclass(frozen=True, slots=True)
class ConflictPath:
    """Why an individual ended up in two classes declared disjoint."""

    disjoint_axiom: str
    disjoint_pair: tuple[str, str]
    rule_id: str
    # One proof per class expression conjunct that jointly violated disjointness.
    literal_proofs: tuple[ProofNode, ...]

    @property
    def sources(self) -> tuple[str, ...]:
        collected: list[str] = []
        seen: set[str] = set()
        for node in self.literal_proofs:
            for src in node.sources():
                if src not in seen:
                    seen.add(src)
                    collected.append(src)
        collected.append(self.disjoint_axiom)
        return tuple(dict.fromkeys(collected))


@dataclass(frozen=True, slots=True)
class IndividualResult:
    individual: str
    types: frozenset[str]
    proofs: dict[str, ProofNode]
    conflict: ConflictPath | None


@dataclass(frozen=True, slots=True)
class UnsatisfiableClass:
    cls: str
    conflict: ConflictPath


@dataclass(frozen=True, slots=True)
class SaturationReport:
    individuals: tuple[IndividualResult, ...]
    unsatisfiable: tuple[UnsatisfiableClass, ...]
    inconsistent: bool
    # Shared TBox view: class -> its strictly-entailed super-classes.
    tbox_supers: dict[str, frozenset[str]]

    def individual(self, name: str) -> IndividualResult | None:
        for r in self.individuals:
            if r.individual == name:
                return r
        return None


@dataclass(slots=True)
class _Worklist:
    proofs: dict[str, ProofNode] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)
    conflict: ConflictPath | None = None

    def add(self, predicate: str, proof: ProofNode) -> bool:
        if predicate in self.proofs:
            return False
        self.proofs[predicate] = proof
        self.order.append(predicate)
        return True


def _fire(rule: Rule, known: dict[str, ProofNode]) -> tuple[ConflictPath | None, ProofNode | None, str | None]:
    """Try one rule. Returns (conflict, derived_proof, predicate)."""
    children = tuple(known[pred] for pred in rule.body if pred in known)
    if len(children) != len(rule.body):
        return None, None, None
    if rule.kind == "disjoint":
        path = ConflictPath(
            disjoint_axiom=rule.source_axiom,
            disjoint_pair=rule.disjoint_pair or ("?", "?"),
            rule_id=rule.rule_id,
            literal_proofs=children,
        )
        return path, None, None
    head = rule.heads[0]
    return None, DerivedNode(
        predicate=head,
        rule_id=rule.rule_id,
        rule_kind=rule.kind,
        source_axiom=rule.source_axiom,
        children=children,
    ), head


def _saturate_one(
    program: CompiledOntology,
    seed_facts: list[Fact],
    *,
    hypothesis: str | None = None,
) -> _Worklist:
    index = program.rule_index()
    work = _Worklist()
    agenda: list[str] = []

    for fact in seed_facts:
        if work.add(
            fact.predicate,
            BaseNode(kind="fact", predicate=fact.predicate, source_axiom=fact.source_axiom),
        ):
            agenda.append(fact.predicate)

    if hypothesis is not None:
        if work.add(
            hypothesis,
            BaseNode(kind="hypothesis", predicate=hypothesis, source_axiom=HYPOTHESIS),
        ):
            agenda.append(hypothesis)

    while agenda:
        gained = agenda.pop()
        for rule in index.get(gained, ()):
            # Only evaluate when *all* body literals are present.
            if any(pred not in work.proofs for pred in rule.body):
                continue
            conflict, proof, pred = _fire(rule, work.proofs)
            if conflict is not None:
                if work.conflict is None:
                    work.conflict = conflict
                continue
            if proof is not None and pred is not None and work.add(pred, proof):
                agenda.append(pred)
    return work


def saturate(program: CompiledOntology) -> SaturationReport:
    """Run both TBox satisfiability checks and ABox saturation."""
    # ---- ABox: one independent saturation per declared individual ----------
    by_individual: dict[str, list[Fact]] = {}
    for fact in program.facts:
        by_individual.setdefault(fact.individual, []).append(fact)

    individual_results: list[IndividualResult] = []
    inconsistent = False
    for individual, facts in by_individual.items():
        work = _saturate_one(program, facts)
        types = frozenset(work.proofs)
        individual_results.append(
            IndividualResult(
                individual=individual,
                types=types,
                proofs=dict(work.proofs),
                conflict=work.conflict,
            )
        )
        if work.conflict is not None:
            inconsistent = True

    # ---- TBox: each class assumed hypothetically, disjointness must not fire -
    unsat: list[UnsatisfiableClass] = []
    tbox_supers: dict[str, frozenset[str]] = {}
    for cls in sorted(program.declared_classes):
        if cls == BOTTOM:
            continue
        work = _saturate_one(program, [], hypothesis=cls)
        entailed = frozenset(p for p in work.proofs if p != cls)
        tbox_supers[cls] = entailed
        if work.conflict is not None:
            unsat.append(UnsatisfiableClass(cls=cls, conflict=work.conflict))

    return SaturationReport(
        individuals=tuple(
            sorted(individual_results, key=lambda r: r.individual)
        ),
        unsatisfiable=tuple(unsat),
        inconsistent=inconsistent,
        tbox_supers=tbox_supers,
    )


def class_hypothetical_supers(
    program: CompiledOntology, class_name: str
) -> tuple[frozenset[str], ConflictPath | None]:
    """Entailed super-classes of a hypothetical instance of ``class_name``."""
    work = _saturate_one(program, [], hypothesis=class_name)
    return frozenset(p for p in work.proofs if p != class_name), work.conflict
