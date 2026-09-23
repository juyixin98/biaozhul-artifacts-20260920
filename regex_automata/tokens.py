"""Token definitions produced by :mod:`regex_automata.lexer`."""
from __future__ import annotations

from dataclasses import dataclass

from .locations import Span
from .predicates import CharPredicate

# Token kinds -----------------------------------------------------------------
T_CHAR = "CHAR"          # ordinary (possibly escaped) literal code point
T_DOT = "DOT"
T_CLASS = "CLASS"        # \d style shorthand or a full [...] class
T_ANCHOR = "ANCHOR"      # ^ $ \b \B
T_LPAREN = "LPAREN"
T_RPAREN = "RPAREN"
T_PIPE = "PIPE"
T_QUANT = "QUANT"        # * + ? {m,n}
T_EOF = "EOF"


@dataclass(frozen=True)
class Token:
    kind: str
    span: Span
    cp: int | None = None                       # T_CHAR
    predicate: CharPredicate | None = None     # T_CLASS
    anchor: str | None = None                  # T_ANCHOR: ^ $ b B
    minimum: int | None = None                 # T_QUANT
    maximum: int | None = None                 # T_QUANT; None = unbounded
