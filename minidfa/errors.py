"""领域错误类型。

每个错误携带稳定的 ``category`` 字符串，API 层据此映射 HTTP 状态码，
测试据此断言失败类别，而不是笼统地断言“抛了异常”。
"""

from __future__ import annotations

from typing import Any


class MinDfaError(Exception):
    """所有领域错误的基类。"""

    category = "INTERNAL"
    http_status = 500

    def __init__(self, message: str, *, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail: dict[str, Any] = detail or {}

    def to_payload(self) -> dict[str, Any]:
        return {"category": self.category, "message": self.message, "detail": self.detail}


# ---------------------------------------------------------------------------
# 语料规范层错误
# ---------------------------------------------------------------------------


class CorpusError(MinDfaError):
    category = "CORPUS_ERROR"
    http_status = 422


class UnsortedInputError(CorpusError):
    """严格模式下输入未按字典序升序排列。"""

    category = "UNSORTED_INPUT"

    def __init__(self, index: int, previous: str, current: str) -> None:
        super().__init__(
            f"input not sorted at index {index}: {previous!r} > {current!r}",
            detail={"index": index, "previous": previous, "current": current},
        )


class DuplicateWordError(CorpusError):
    """严格模式下输入包含重复词。"""

    category = "DUPLICATE_WORD"

    def __init__(self, index: int, word: str) -> None:
        super().__init__(
            f"duplicate word at index {index}: {word!r}",
            detail={"index": index, "word": word},
        )


class InvalidWordError(CorpusError):
    """词本身非法（非字符串，或含无法持久化的字符）。"""

    category = "INVALID_WORD"


# ---------------------------------------------------------------------------
# 存储 / 查询层错误
# ---------------------------------------------------------------------------


class AutomatonNotFoundError(MinDfaError):
    category = "AUTOMATON_NOT_FOUND"
    http_status = 404

    def __init__(self, name: str) -> None:
        super().__init__(f"automaton not found: {name!r}", detail={"name": name})


class CorruptStoreError(MinDfaError):
    """持久化数据引用校验失败的基类。"""

    category = "CORRUPT_STORE"
    http_status = 500


class DanglingReferenceError(CorruptStoreError):
    """转移指向不存在的状态，或转移源状态不存在。"""

    category = "DANGLING_REFERENCE"


class CycleDetectedError(CorruptStoreError):
    """状态图存在环，违反无环约束。"""

    category = "CYCLE_DETECTED"


class UnreachableStateError(CorruptStoreError):
    """存在从初始状态不可达的状态（悬空状态）。"""

    category = "UNREACHABLE_STATE"


class InvalidSymbolError(CorruptStoreError):
    """转移符号不是单字符。"""

    category = "INVALID_SYMBOL"
