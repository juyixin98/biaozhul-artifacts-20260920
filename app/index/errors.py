"""持久化与引用完整性错误类别。"""

from dataclasses import dataclass


class IndexError(RuntimeError):
    """索引层错误基类。"""

    error_code = "index_error"
    http_status = 500


class IndexIntegrityError(IndexError):
    """持久化引用完整性校验失败（环、悬空状态、计数不一致等）。

    完整性失败绝不作为成功返回；调用方必须让请求失败并暴露违规类别。
    """

    error_code = "index_integrity_violation"
    http_status = 500

    def __init__(self, violations: "list[IntegrityViolation]") -> None:
        self.violations = violations
        detail = "; ".join(f"{v.kind}: {v.detail}" for v in violations)
        super().__init__(f"索引完整性校验未通过（{len(violations)} 项）: {detail}")


@dataclass(frozen=True)
class IntegrityViolation:
    """单项完整性违规。

    :param kind: 固定违规类别标识（机器可读）。
    :param detail: 判定依据的人类可读描述。
    """

    kind: str
    detail: str


# 违规类别常量（测试按这些具体类别断言）。
VIOLATION_ROOT_MISSING = "root_missing"
VIOLATION_DUPLICATE_STATE = "duplicate_state"
VIOLATION_DANGLING_EDGE = "dangling_edge"
VIOLATION_UNREACHABLE_STATE = "unreachable_state"
VIOLATION_CYCLE = "cycle"
VIOLATION_NONDETERMINISTIC = "nondeterministic_transition"
VIOLATION_BAD_FINAL_FLAG = "bad_final_flag"
VIOLATION_DUPLICATE_SYMBOL = "duplicate_symbol"
VIOLATION_COUNT_MISMATCH = "stored_word_count_mismatch"
VIOLATION_COUNT_MISSING = "word_count_missing"
VIOLATION_META_MISSING = "metadata_missing"
