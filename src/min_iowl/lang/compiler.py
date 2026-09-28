"""Compile the restricted class-expression fragment into flat conjunctive rules.

The supported fragment is a strict subset of OWL that is *Horn*: every class
expression is a named class or a conjunction of named classes.  Consequently no
auxiliary predicates are needed --

* ``SubClassOf(C1..Ck, D1..Dm)`` flattens to rules
  ``C1 & ... & Ck  ->  Dj`` for every conclusion conjunct ``Dj``;
* ``EquivalentClasses(...)`` expands into the pairwise subclass closure;
* ``DisjointClasses(C, D)`` becomes a rule ``conj(C), conj(D) -> bottom``, one
  per unordered pair;
* ``ClassAssertion(x, C)`` becomes one base fact per conjunct of ``C``.

Every produced rule/fact carries the id of its originating axiom, so provenance
is never created by the reasoner -- it only propagates declared sources.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import ast
from .errors import ErrorCode, LangError

__all__ = ["Rule", "Fact", "CompiledOntology", "compile_ontology", "render_expr"]

BOTTOM = "__bottom__"


@dataclass(frozen=True, slots=True)
class Rule:
    rule_id: str
    body: tuple[str, ...]
    heads: tuple[str, ...]
    kind: str  # "sub" | "equiv" | "disjoint"
    source_axiom: str
    # For disjoint rules: the two (rendered) expressions declared disjoint.
    disjoint_pair: tuple[str, str] | None = None

    @property
    def is_bottom(self) -> bool:
        return self.kind == "disjoint"


@dataclass(frozen=True, slots=True)
class Fact:
    individual: str
    predicate: str
    source_axiom: str


@dataclass(frozen=True, slots=True)
class AxiomRecord:
    axiom_id: str
    kind: str
    text: str


@dataclass(frozen=True)
class CompiledOntology:
    rules: tuple[Rule, ...]
    facts: tuple[Fact, ...]
    axioms: tuple[AxiomRecord, ...]
    declared_classes: frozenset[str]
    individuals: frozenset[str]
    # class name -> ids of axioms in which the name appears (provenance anchors)
    class_sources: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def rule_index(self) -> dict[str, tuple[Rule, ...]]:
        """Predicate name -> rules that can fire when that predicate is gained."""
        index: dict[str, list[Rule]] = {}
        for rule in self.rules:
            for pred in rule.body:
                index.setdefault(pred, []).append(rule)
        return {k: tuple(v) for k, v in index.items()}


def _conjuncts(expr: ast.ClassExpr) -> tuple[str, ...]:
    """Atomic predicate names implied by satisfying ``expr`` (positive side)."""
    if isinstance(expr, ast.ClassName):
        return (expr.name,)
    names: list[str] = []
    for op in expr.operands:
        names.extend(_conjuncts(op))
    # de-dup preserving order
    seen: set[str] = set()
    out: list[str] = []
    for name in names:
        if name not in seen:
            seen.add(name)
            out.append(name)
    return tuple(out)


def render_expr(expr: ast.ClassExpr) -> str:
    if isinstance(expr, ast.ClassName):
        return expr.name
    return "ObjectIntersectionOf(" + " ".join(render_expr(o) for o in expr.operands) + ")"


def _iter_classes(expr: ast.ClassExpr) -> tuple[str, ...]:
    if isinstance(expr, ast.ClassName):
        return (expr.name,)
    out: list[str] = []
    for op in expr.operands:
        out.extend(_iter_classes(op))
    return tuple(out)


@dataclass(slots=True)
class _Builder:
    rules: list[Rule] = field(default_factory=list)
    axioms: list[AxiomRecord] = field(default_factory=list)
    facts: list[Fact] = field(default_factory=list)
    declared: set[str] = field(default_factory=set)
    individuals: set[str] = field(default_factory=set)
    class_sources: dict[str, list[str]] = field(default_factory=dict)
    _counter: int = 0

    def _rid(self) -> str:
        self._counter += 1
        return f"r{self._counter:04d}"

    def _anchor(self, names: tuple[str, ...], axiom_id: str) -> None:
        for name in names:
            self.declared.add(name)
            bucket = self.class_sources.setdefault(name, [])
            if axiom_id not in bucket:
                bucket.append(axiom_id)

    def _subclass_rules(
        self, sub: ast.ClassExpr, sup: ast.ClassExpr, kind: str, source: str
    ) -> None:
        body = _conjuncts(sub)
        for head in _conjuncts(sup):
            if (head,) == body and len(body) == 1:
                continue  # tautology A -> A; no rule needed
            self.rules.append(
                Rule(
                    rule_id=self._rid(),
                    body=body,
                    heads=(head,),
                    kind=kind,
                    source_axiom=source,
                )
            )

    def add_axiom(self, axiom_id: str, axiom: ast.Axiom) -> None:
        if isinstance(axiom, ast.SubClassOf):
            self._anchor(_iter_classes(axiom.sub) + _iter_classes(axiom.sup), axiom_id)
            self.axioms.append(AxiomRecord(axiom_id, "SubClassOf", f"SubClassOf({render_expr(axiom.sub)} {render_expr(axiom.sup)})"))
            self._subclass_rules(axiom.sub, axiom.sup, "sub", axiom_id)

        elif isinstance(axiom, ast.EquivalentClasses):
            names: tuple[str, ...] = ()
            for op in axiom.operands:
                names += _iter_classes(op)
            self._anchor(names, axiom_id)
            rendered = " ".join(render_expr(o) for o in axiom.operands)
            self.axioms.append(AxiomRecord(axiom_id, "EquivalentClasses", f"EquivalentClasses({rendered})"))
            # Pairwise closure in both directions.  Each direction is a genuine
            # subclass rule; equivalence never collapses declarations into one
            # anonymous node -- both source directions keep this axiom id.
            for i, left in enumerate(axiom.operands):
                for j, right in enumerate(axiom.operands):
                    if i != j:
                        self._subclass_rules(left, right, "equiv", axiom_id)

        elif isinstance(axiom, ast.DisjointClasses):
            names: tuple[str, ...] = ()
            for op in axiom.operands:
                names += _iter_classes(op)
            self._anchor(names, axiom_id)
            rendered = " ".join(render_expr(o) for o in axiom.operands)
            self.axioms.append(AxiomRecord(axiom_id, "DisjointClasses", f"DisjointClasses({rendered})"))
            for i in range(len(axiom.operands)):
                for j in range(i + 1, len(axiom.operands)):
                    left, right = axiom.operands[i], axiom.operands[j]
                    body = tuple(dict.fromkeys(_conjuncts(left) + _conjuncts(right)))
                    self.rules.append(
                        Rule(
                            rule_id=self._rid(),
                            body=body,
                            heads=(),
                            kind="disjoint",
                            source_axiom=axiom_id,
                            disjoint_pair=(render_expr(left), render_expr(right)),
                        )
                    )

        elif isinstance(axiom, ast.ClassAssertion):
            self._anchor(_iter_classes(axiom.cls), axiom_id)
            self.individuals.add(axiom.individual)
            text = f"ClassAssertion({render_expr(axiom.cls)} {axiom.individual})"
            self.axioms.append(AxiomRecord(axiom_id, "ClassAssertion", text))
            for pred in _conjuncts(axiom.cls):
                self.facts.append(
                    Fact(individual=axiom.individual, predicate=pred, source_axiom=axiom_id)
                )
        else:  # pragma: no cover - exhaustiveness guard
            raise LangError(ErrorCode.MALFORMED_EXPRESSION, f"cannot compile {axiom!r}")


def compile_ontology(items: list[tuple[str, ast.Axiom]]) -> CompiledOntology:
    """Compile ``(axiom_id, axiom)`` pairs into a saturated-ready program."""
    builder = _Builder()
    for axiom_id, axiom in items:
        builder.add_axiom(axiom_id, axiom)
    return CompiledOntology(
        rules=tuple(builder.rules),
        facts=tuple(builder.facts),
        axioms=tuple(builder.axioms),
        declared_classes=frozenset(builder.declared),
        individuals=frozenset(builder.individuals),
        class_sources={k: tuple(v) for k, v in builder.class_sources.items()},
    )
