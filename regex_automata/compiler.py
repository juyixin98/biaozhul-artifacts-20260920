"""Pattern compiler: source -> tokens -> AST -> Thompson NFA."""
from __future__ import annotations

from dataclasses import dataclass

from . import ast
from .lexer import lex
from .matcher import Simulator
from .nfa import NFA, build_nfa
from .parser import Parser


@dataclass
class CompiledPattern:
    source: str
    tree: ast.Node
    nfa: NFA
    assertions: dict[int, list[str]]

    def simulator(self) -> Simulator:
        return Simulator(self.nfa, self.assertions)

    def search(self, text: str):
        return self.simulator().search(text)

    def fullmatch(self, text: str):
        return self.simulator().fullmatch(text)

    def prefix(self, text: str):
        return self.simulator().prefix(text)

    def to_dict(self) -> dict:
        return {
            "pattern": self.source,
            "nfa": self.nfa.to_dict(),
            "assertions": {
                str(state): kinds for state, kinds in sorted(self.assertions.items())
            },
        }


def compile_pattern(source: str) -> CompiledPattern:
    """Compile a pattern string; raises :class:`RegexError` subclasses."""
    tokens = lex(source)
    tree = Parser(source, tokens).parse()
    nfa, assertions = build_nfa(tree)
    return CompiledPattern(source, tree, nfa, assertions)
