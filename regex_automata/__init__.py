"""regex_automata — a small regex language tool-chain built on Thompson NFA.

Public API::

    from regex_automata import compile_pattern, RegexError

    pat = compile_pattern(r"(ab|a)*b")
    pat.search("aab")         # -> Match(...)
    pat.fullmatch("aab")
    pat.prefix("aabxyz")

The supported language is documented in ``docs/language.md``.
"""
from .compiler import CompiledPattern, compile_pattern
from .errors import LexError, ParseError, RegexError
from .matcher import Match, Simulator

__all__ = [
    "compile_pattern",
    "CompiledPattern",
    "RegexError",
    "LexError",
    "ParseError",
    "Match",
    "Simulator",
]

__version__ = "1.0.0"
