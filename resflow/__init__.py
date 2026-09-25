"""resflow: a small language toolchain for resource-release path analysis.

Public entry points:
    analyze_source(source, *, filename, loop_bound, steps): analyze source text.
    main: command line interface.
"""
from .analyzer import analyze_source
from .errors import ResflowError, LexError, ParseError, SemanticError, AnalysisError

__all__ = [
    "analyze_source",
    "ResflowError",
    "LexError",
    "ParseError",
    "SemanticError",
    "AnalysisError",
]

__version__ = "1.0.0"
