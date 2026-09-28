"""Lexical specification for typed brackets.

The spec declares which characters open/close typed bracket pairs, which
characters delimit string literals, and which character escapes the next one
inside a string. Content inside a string literal is NOT structure: the lexer
must not emit bracket tokens for it. An unterminated string swallows the rest
of the text (documented assumption; see README).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping


@dataclass(frozen=True)
class LexicalSpec:
    open_to_close: Mapping[str, str] = field(
        default_factory=lambda: MappingProxyType({"(": ")", "[": "]", "{": "}"})
    )
    quotes: frozenset[str] = frozenset({'"', "'"})
    escape: str = "\\"

    def __post_init__(self) -> None:
        for opener, closer in self.open_to_close.items():
            if len(opener) != 1 or len(closer) != 1:
                raise ValueError("bracket symbols must be single characters")
            if opener in self.quotes or closer in self.quotes:
                raise ValueError("bracket symbols must not overlap with quotes")
        if len(self.escape) != 1:
            raise ValueError("escape must be a single character")

    @property
    def close_to_open(self) -> Mapping[str, str]:
        return MappingProxyType({v: k for k, v in self.open_to_close.items()})

    @property
    def bracket_types(self) -> tuple[str, ...]:
        return tuple(self.open_to_close.keys())


DEFAULT_SPEC = LexicalSpec()
