"""Interval abstract interpretation backend (IntervalLang toolchain).

Pure-Python small-language front end (hand-written lexer / parser, no
compiler libraries), an integer IR, interval abstract interpretation with
widening + narrowing, and a concrete reference interpreter used for
differential validation.
"""

from .errors import IvlError, LexError, ParseError, AnalysisError, RuntimeErr
from .intervals import Interval
from .pipeline import analyze_source, AnalyzeResult
from .concrete import run_source

__all__ = [
    "IvlError",
    "LexError",
    "ParseError",
    "AnalysisError",
    "RuntimeErr",
    "Interval",
    "analyze_source",
    "run_source",
    "AnalyzeResult",
]
