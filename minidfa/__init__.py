"""minidfa — 不可变有序词典的最小无环确定自动机（DAFSA）及前缀统计。"""

__version__ = "0.1.0"

from .automaton import MinimalDFA
from .builder import build_minimal_dfa
from .corpus import CorpusSpec, normalize_words, validate_sorted_unique
from .errors import (
    AutomatonNotFoundError,
    CorruptStoreError,
    CycleDetectedError,
    DanglingReferenceError,
    DuplicateWordError,
    InvalidSymbolError,
    InvalidWordError,
    MinDfaError,
    UnreachableStateError,
    UnsortedInputError,
)

__all__ = [
    "__version__",
    "MinimalDFA",
    "build_minimal_dfa",
    "CorpusSpec",
    "normalize_words",
    "validate_sorted_unique",
    "MinDfaError",
    "UnsortedInputError",
    "DuplicateWordError",
    "InvalidWordError",
    "InvalidSymbolError",
    "AutomatonNotFoundError",
    "CorruptStoreError",
    "CycleDetectedError",
    "DanglingReferenceError",
    "UnreachableStateError",
]
