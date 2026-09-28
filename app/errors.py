"""失败类别与业务异常。

所有可预期的失败都归入明确的类别，接口与日志据此给出可解释的结论，
而不是笼统的 500。
"""
from __future__ import annotations

from enum import Enum


class ErrorCategory(str, Enum):
    VALIDATION_ERROR = "VALIDATION_ERROR"          # 请求参数不合法
    RANGE_INVERSION = "RANGE_INVERSION"            # 排序键意义下 lower > upper
    INDEX_VERSION_CONFLICT = "INDEX_VERSION_CONFLICT"  # 规则已升级但索引未重建
    STALE_CURSOR = "STALE_CURSOR"                  # 游标来自旧索引版本
    INDEX_NOT_BUILT = "INDEX_NOT_BUILT"            # 索引尚未构建
    ENTRY_NOT_FOUND = "ENTRY_NOT_FOUND"            # 指定条目不存在
    CORPUS_ERROR = "CORPUS_ERROR"                  # 语料不合法
    INTERNAL = "INTERNAL"                          # 未预期错误


class ServiceError(Exception):
    """携带失败类别与 HTTP 状态码的业务异常。"""

    def __init__(
        self,
        category: ErrorCategory,
        message: str,
        *,
        detail: dict | None = None,
        http_status: int = 400,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.message = message
        self.detail = detail or {}
        self.http_status = http_status


def validation_error(message: str, **detail) -> ServiceError:
    return ServiceError(ErrorCategory.VALIDATION_ERROR, message, detail=detail)


def range_inversion(message: str, **detail) -> ServiceError:
    return ServiceError(
        ErrorCategory.RANGE_INVERSION, message, detail=detail, http_status=422
    )


def index_version_conflict(message: str, **detail) -> ServiceError:
    return ServiceError(
        ErrorCategory.INDEX_VERSION_CONFLICT, message, detail=detail, http_status=409
    )


def stale_cursor(message: str, **detail) -> ServiceError:
    return ServiceError(
        ErrorCategory.STALE_CURSOR, message, detail=detail, http_status=409
    )


def index_not_built(message: str, **detail) -> ServiceError:
    return ServiceError(
        ErrorCategory.INDEX_NOT_BUILT, message, detail=detail, http_status=409
    )


def entry_not_found(message: str, **detail) -> ServiceError:
    return ServiceError(
        ErrorCategory.ENTRY_NOT_FOUND, message, detail=detail, http_status=404
    )


def corpus_error(message: str, **detail) -> ServiceError:
    return ServiceError(ErrorCategory.CORPUS_ERROR, message, detail=detail)
