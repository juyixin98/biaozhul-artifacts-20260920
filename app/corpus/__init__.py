"""语料规范层：词项校验、有序性保证、错误类别。

本层只依赖标准库，不依赖挖掘内核，保证规范可以被独立测试。
"""

from app.corpus.errors import (
    CorpusError,
    EmptyWordError,
    FixtureNotFoundError,
    InvalidWordError,
    UnorderedCorpusError,
)
from app.corpus.spec import (
    CorpusSpec,
    Word,
    accept_empty_word,
    check_sorted,
    normalize_words,
)

__all__ = [
    "CorpusError",
    "EmptyWordError",
    "FixtureNotFoundError",
    "InvalidWordError",
    "UnorderedCorpusError",
    "CorpusSpec",
    "Word",
    "accept_empty_word",
    "check_sorted",
    "normalize_words",
]
