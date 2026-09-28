"""语料规范层：词表合法性、排序/去重策略与输入指纹。

排序规则固定为 Python 字符串字典序（按 Unicode 码位逐字符比较）。
空词 "" 是合法词且字典序最小，必须排在首位（见 config.ALLOW_EMPTY_WORD）。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Sequence

from .errors import DuplicateWordError, InvalidWordError, UnsortedInputError


class InputMode(str, Enum):
    """新增输入的处理策略。"""

    STRICT = "strict"  # 必须已按字典序升序且无重复，否则明确拒绝
    NORMALIZE = "normalize"  # 先排序并去重，再构建


@dataclass(frozen=True)
class CorpusSpec:
    """校验通过的语料及其身份指纹。"""

    words: tuple[str, ...]  # 已保证：字典序升序、无重复
    fingerprint: str  # sha256，前 16 位，用于日志关联输入

    @property
    def size(self) -> int:
        return len(self.words)


def fingerprint_words(words: Sequence[str]) -> str:
    """对规范化后的词表计算稳定指纹，用于把日志/产物关联到具体输入。"""
    h = hashlib.sha256()
    h.update(str(len(words)).encode("ascii"))
    for w in words:
        # 以 NUL 分隔避免拼接歧义；NUL 已被 validate 拒绝
        h.update(b"\x00")
        h.update(w.encode("utf-8"))
    return h.hexdigest()[:16]


def _check_word(index: int, word: object) -> str:
    if not isinstance(word, str):
        raise InvalidWordError(
            f"word at index {index} is not a string: {type(word).__name__}",
            detail={"index": index, "type": type(word).__name__},
        )
    if "\x00" in word:
        raise InvalidWordError(
            f"word at index {index} contains NUL character",
            detail={"index": index},
        )
    return word


def validate_sorted_unique(words: Iterable[str]) -> tuple[str, ...]:
    """严格校验：字典序升序且无重复。违反时抛出带位置的明确错误。

    空词 "" 合法，且因字典序最小只能出现在首位。
    """
    out: list[str] = []
    previous: str | None = None
    for index, raw in enumerate(words):
        word = _check_word(index, raw)
        if previous is not None:
            if word == previous:
                raise DuplicateWordError(index, word)
            if word < previous:
                raise UnsortedInputError(index, previous, word)
        out.append(word)
        previous = word
    return tuple(out)


def normalize_words(words: Iterable[str]) -> tuple[str, ...]:
    """规范化模式：先校验词法，再排序并去重。"""
    checked = [_check_word(i, w) for i, w in enumerate(words)]
    return tuple(sorted(set(checked)))


def prepare_corpus(words: Iterable[str], mode: InputMode) -> CorpusSpec:
    """按策略得到规范语料。strict 拒绝乱序/重复；normalize 先排序去重。"""
    if mode is InputMode.STRICT:
        normalized = validate_sorted_unique(words)
    elif mode is InputMode.NORMALIZE:
        normalized = normalize_words(words)
    else:  # pragma: no cover - 防御未知枚举
        raise ValueError(f"unknown input mode: {mode!r}")
    return CorpusSpec(words=normalized, fingerprint=fingerprint_words(normalized))
