"""Abstract syntax tree for the restricted expression language.

Every node records its character span (``start``, ``end``) in the original
source so that parse and domain errors can point at a precise location.

Grammar (continuously differentiable, real-valued expressions)::

    expr    := term (('+' | '-') term)*
    term    := factor (('*' | '/') factor)*
    factor  := unary ('^' NUMBER)?      # exponent must be a numeric literal
    unary   := ('+' | '-') unary | primary
    primary := NUMBER | CONST | 'x' | FUNC '(' expr ')' | '(' expr ')'
    CONST   := 'pi' | 'e'
    FUNC    := 'sin' | 'cos' | 'tan' | 'exp' | 'log' | 'sqrt'

Design decisions (documented in README):

* Only the single variable ``x`` is supported.
* ``^`` is the only exponentiation operator; the right operand of ``^`` must
  be a numeric literal (variable exponents are rejected). ``exp`` covers
  base-``e`` variable exponents.
* ``abs`` is deliberately excluded: it is not continuously differentiable.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Node:
    # kw_only keeps subclass positional fields natural:
    # e.g. Num(text, start=..., end=...).
    start: int = field(kw_only=True)
    end: int = field(kw_only=True)


@dataclass(frozen=True)
class Num(Node):
    """Numeric literal, kept as its exact source decimal string."""

    text: str


@dataclass(frozen=True)
class Var(Node):
    name: str = "x"


@dataclass(frozen=True)
class Const(Node):
    name: str  # 'pi' | 'e'


@dataclass(frozen=True)
class Unary(Node):
    op: str  # '-' | '+'
    operand: Node


@dataclass(frozen=True)
class Binary(Node):
    op: str  # '+' | '-' | '*' | '/' | '^'
    left: Node
    right: Node


@dataclass(frozen=True)
class Call(Node):
    name: str  # sin | cos | tan | exp | log | sqrt
    arg: Node


@dataclass(frozen=True)
class Paren(Node):
    """Parenthesised sub-expression (semantically transparent)."""

    inner: Node


SUPPORTED_FUNCTIONS = frozenset({"sin", "cos", "tan", "exp", "log", "sqrt"})
SUPPORTED_CONSTANTS = frozenset({"pi", "e"})
VARIABLE_NAME = "x"
