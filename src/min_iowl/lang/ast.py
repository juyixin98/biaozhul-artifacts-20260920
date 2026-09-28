"""Restricted OWL class-expression language (AST definitions).

Supported fragment on purpose:

* named classes
* ``ObjectIntersectionOf`` (arity >= 2, nested intersections flatten)
* axioms: ``SubClassOf`` / ``EquivalentClasses`` / ``DisjointClasses`` /
  ``ClassAssertion``

Anything else (union, complement, existential, cardinality, ...) has no AST
node here; the parser/builder reject it explicitly with ``UNSUPPORTED_CONSTRUCTOR``
instead of silently treating it as a plain label.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "ClassName",
    "ObjectIntersection",
    "ClassExpr",
    "SubClassOf",
    "EquivalentClasses",
    "DisjointClasses",
    "ClassAssertion",
    "Axiom",
]


@dataclass(frozen=True, slots=True)
class ClassName:
    """A named class / atomic predicate."""

    name: str


@dataclass(frozen=True, slots=True)
class ObjectIntersection:
    """ObjectIntersectionOf(CE1 ... CEn), n >= 2, flattened + de-duplicated."""

    operands: tuple["ClassExpr", ...]


ClassExpr = ClassName | ObjectIntersection


@dataclass(frozen=True, slots=True)
class SubClassOf:
    sub: ClassExpr
    sup: ClassExpr


@dataclass(frozen=True, slots=True)
class EquivalentClasses:
    operands: tuple[ClassExpr, ...]


@dataclass(frozen=True, slots=True)
class DisjointClasses:
    operands: tuple[ClassExpr, ...]


@dataclass(frozen=True, slots=True)
class ClassAssertion:
    individual: str
    cls: ClassExpr


Axiom = SubClassOf | EquivalentClasses | DisjointClasses | ClassAssertion
