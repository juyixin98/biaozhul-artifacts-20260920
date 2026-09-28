"""显式错误类别。

所有可预期的失败都携带稳定的 ``category`` 字符串，客户端与测试据此断言，
而不是依赖错误信息文本或 HTTP 状态码猜测。
"""
from __future__ import annotations


class ErrorCategory:
    # 鉴权 / 授权
    MISSING_CREDENTIALS = "MISSING_CREDENTIALS"
    INVALID_TOKEN = "INVALID_TOKEN"
    FORBIDDEN_ROLE = "FORBIDDEN_ROLE"

    # 请求与契约
    REQUEST_VALIDATION_FAILED = "REQUEST_VALIDATION_FAILED"
    INVALID_CONTRACT = "INVALID_CONTRACT"
    SEED_REQUIRED = "SEED_REQUIRED"
    INVALID_SEED = "INVALID_SEED"
    STUDY_ALREADY_EXISTS = "STUDY_ALREADY_EXISTS"
    UNKNOWN_STUDY = "UNKNOWN_STUDY"
    STUDY_FROZEN = "STUDY_FROZEN"
    SUBJECT_NOT_FOUND = "SUBJECT_NOT_FOUND"

    # 分层特征
    MISSING_STRATUM_FACTOR = "MISSING_STRATUM_FACTOR"
    UNEXPECTED_STRATUM_FACTOR = "UNEXPECTED_STRATUM_FACTOR"
    NON_DISCRETE_STRATUM_VALUE = "NON_DISCRETE_STRATUM_VALUE"
    EMPTY_STRATUM_VALUE = "EMPTY_STRATUM_VALUE"

    # 幂等 / 冻结身份
    FEATURES_CHANGED_AFTER_ALLOCATION = "FEATURES_CHANGED_AFTER_ALLOCATION"
    IDEMPOTENCY_KEY_REUSE_CONFLICT = "IDEMPOTENCY_KEY_REUSE_CONFLICT"
    IDEMPOTENCY_KEY_MISMATCH = "IDEMPOTENCY_KEY_MISMATCH"
    IDEMPOTENCY_KEY_MISSING_ON_REPLAY = "IDEMPOTENCY_KEY_MISSING_ON_REPLAY"

    # 存储冲突（并发）
    ALLOCATION_RACE = "ALLOCATION_RACE"

    # 尾组 / 入组
    STRATUM_SEALED = "STRATUM_SEALED"
    OPEN_TAIL_BLOCK = "OPEN_TAIL_BLOCK"
    ENROLLMENT_CLOSED = "ENROLLMENT_CLOSED"

    # 复现 / 恢复
    RECOVERY_SEED_MISSING = "RECOVERY_SEED_MISSING"
    REPLAY_MISMATCH = "REPLAY_MISMATCH"

    # 证据 / 诊断
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    STATISTICAL_INCONCLUSIVE = "STATISTICAL_INCONCLUSIVE"

    # 尾组
    NO_OPEN_BLOCK = "NO_OPEN_BLOCK"  # 仅内部使用，正常路径会自动开新区组


class AppError(Exception):
    """携带稳定类别的业务异常。"""

    def __init__(self, category: str, http_status: int, message: str, details=None):
        super().__init__(message)
        self.category = category
        self.http_status = http_status
        self.message = message
        self.details = details or {}

    def to_body(self, request_id: str | None = None) -> dict:
        return {
            "error": {
                "category": self.category,
                "message": self.message,
                "details": self.details,
                "request_id": request_id,
            }
        }
