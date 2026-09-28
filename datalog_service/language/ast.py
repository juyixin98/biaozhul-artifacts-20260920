"""Abstract syntax tree for the restricted Datalog dialect.

Terms are either :class:`Variable` or :class:`Constant`.  Constants carry
Python ``int`` (numeric literal) or ``str`` (identifier / quoted string)
values.  All AST nodes are frozen so programs can be shared freely between
the compiler, the evaluation engine and the reference oracle without any
risk of mutation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Tuple, Union

Term = Union["Variable", "Constant"]


@dataclass(frozen=True)
class Variable:
    name: str

    def canonical(self) -> str:
        return self.name


@dataclass(frozen=True)
class Constant:
    value: object  # int or str

    def canonical(self) -> str:
        if isinstance(self.value, bool):  # guard: bool is a subclass of int
            return f'"{int(self.value)}"'
        if isinstance(self.value, int):
            return str(self.value)
        # Symbols render bare (lexically they are [a-z][A-Za-z0-9_]*),
        # quoted strings render with JSON quoting, so symbol ``a`` and
        # string ``"a"`` never collapse into the same key.
        text = str(self.value)
        if text and (text[0].islower() or text[0] == "_") and text.replace("_", "a").isalnum():
            return text
        return _quote(text)


def _quote(text: str) -> str:
    out = ['"']
    for ch in text:
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def format_value(value: object) -> str:
    """Render a concrete Python value the way its source literal looks."""
    if isinstance(value, bool):
        return str(int(value))
    if isinstance(value, int):
        return str(value)
    text = str(value)
    if (
        text
        and (text[0].islower() or text[0] == "_")
        and text.replace("_", "a").isalnum()
    ):
        return text
    return _quote(text)


def format_goal(predicate: str, row: tuple, negated: bool = False) -> str:
    prefix = "not " if negated else ""
    if not row:
        return prefix + predicate
    return prefix + f"{predicate}({', '.join(format_value(v) for v in row)})"


@dataclass(frozen=True)
class Atom:
    predicate: str
    args: Tuple[Term, ...] = field(default_factory=tuple)

    @property
    def arity(self) -> int:
        return len(self.args)

    def canonical(self) -> str:
        if not self.args:
            return self.predicate
        return f"{self.predicate}({', '.join(a.canonical() for a in self.args)})"


@dataclass(frozen=True)
class Literal:
    atom: Atom
    negated: bool = False

    @property
    def predicate(self) -> str:
        return self.atom.predicate

    @property
    def arity(self) -> int:
        return self.atom.arity

    @property
    def args(self) -> Tuple[Term, ...]:
        return self.atom.args

    def canonical(self) -> str:
        prefix = "not " if self.negated else ""
        return prefix + self.atom.canonical()


@dataclass(frozen=True)
class Rule:
    head: Atom
    body: Tuple[Literal, ...]

    def canonical(self) -> str:
        return f"{self.head.canonical()} :- {', '.join(b.canonical() for b in self.body)}."


@dataclass(frozen=True)
class Program:
    facts: Tuple[Atom, ...]
    rules: Tuple[Rule, ...]
    source_text: str = ""
