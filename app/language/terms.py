"""Term- and literal-level data structures for the Datalog dialect.

A program is a set of *rules* (Horn clauses) and *facts* (ground atoms).

Supported terms
---------------
* constants:    ``foo``, ``12``, ``"a b"``        -> :class:`Const`
* variables:    ``X``, ``Who`` (capitalised)      -> :class:`Var`

Literals
--------
* positive atom:  ``parent(ann, bob)``
* negated atom:   ``NOT parent(X, Y)``
* interpreted comparisons:  ``X = a``, ``X != Y``, ``X < 5`` ...

The language is *range restricted*: every variable that appears in a rule
head, in a negated literal, or in a comparison must also appear in some
positive (extensional/intensional) body literal.  This is the safety
condition enforced at compile time in :mod:`app.language.compiler`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Union

# ---------------------------------------------------------------------------
# Terms
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Const:
    """A ground constant.  Stored as ``str``; numeric-looking tokens keep
    their textual form so fact-set identity is independent of any numeric
    coercion (the comparison builtins interpret them when both sides look
    like numbers)."""

    value: str

    def render(self) -> str:
        return _quote_if_needed(self.value)


@dataclass(frozen=True)
class Var:
    """A logical variable.  ``name`` is stored without the leading marker."""

    name: str

    def render(self) -> str:
        return self.name


Term = Union[Const, Var]


@dataclass(frozen=True)
class Atom:
    """A positive relational literal ``pred(t1, ..., tn)``."""

    pred: str
    args: tuple[Term, ...]

    @property
    def arity(self) -> int:
        return len(self.args)

    def variables(self) -> frozenset[str]:
        return frozenset(a.name for a in self.args if isinstance(a, Var))

    def render(self) -> str:
        return f"{self.pred}({', '.join(t.render() for t in self.args)})"


@dataclass(frozen=True)
class NegAtom:
    """A negated relational literal ``NOT pred(...)`` (stratified)."""

    atom: Atom

    @property
    def pred(self) -> str:
        return self.atom.pred

    @property
    def args(self) -> tuple[Term, ...]:
        return self.atom.args

    def variables(self) -> frozenset[str]:
        return self.atom.variables()

    def render(self) -> str:
        return f"NOT {self.atom.render()}"


@dataclass(frozen=True)
class Comparison:
    """An interpreted comparison ``left op right``.

    ``op`` is one of ``=``, ``!=``, ``<``, ``<=``, ``>``, ``>=``.
    """

    op: str
    left: Term
    right: Term

    _OPS = frozenset({"=", "!=", "<", "<=", ">", ">="})

    def variables(self) -> frozenset[str]:
        out: set[str] = set()
        for t in (self.left, self.right):
            if isinstance(t, Var):
                out.add(t.name)
        return frozenset(out)

    def render(self) -> str:
        return f"{self.left.render()} {self.op} {self.right.render()}"


Literal = Union[Atom, NegAtom, Comparison]


@dataclass(frozen=True)
class Rule:
    """A Horn clause ``head :- body``."""

    head: Atom
    body: tuple[Literal, ...]

    def render(self) -> str:
        return f"{self.head.render()} :- {', '.join(l.render() for l in self.body)}."


@dataclass(frozen=True)
class Program:
    rules: tuple[Rule, ...]
    facts: tuple[Atom, ...]


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------


def _quote_if_needed(value: str) -> str:
    if value == "":
        return '""'
    if value[0].isupper() or value[0] == "_":
        # Would be lexed as a variable; quote to round-trip.
        return '"' + value.replace('"', '\\"') + '"'
    if any(c in value for c in ' \t(),.'):
        return '"' + value.replace('"', '\\"') + '"'
    return value


def render_tuple(values: tuple[str, ...]) -> str:
    """Render a ground tuple (constant strings) in the same convention."""

    return f"({', '.join(_quote_if_needed(v) for v in values)})"
