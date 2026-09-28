"""特殊值规则（固定，不随方法改变）。

规则一览（同时由 ``GET /v1/policies`` 对外公布）：

1. 空输入                -> 拒绝，类别 ``empty_input``。
2. 输入含 NaN            -> 拒绝，类别 ``non_finite_input``（NaN 使"和"无定义，
                            与其静默传播不如在边界显式拒绝）。
3. 同时含 +Inf 与 -Inf   -> 拒绝，类别 ``mixed_infinities``（Inf + -Inf = NaN，
                            属于不定式，边界拒绝而非返回 NaN）。
4. 仅含单一符号的 Inf    -> 结果为该符号的 Inf，不再进入有限求和路径。
5. 带符号零              -> 结果为 -0.0 当且仅当所有输入项都是 -0.0；
                            其余精确为零的结果一律为 +0.0（含补偿法 s+c 抵消到 0 的情形）。
6. 输入规模超过上限      -> 拒绝，类别 ``input_too_large``（由服务层执行）。

拒绝一律以 ``SummationRejected`` 抛出，携带失败类别与脱敏细节（计数，不含原始值）。
"""

from __future__ import annotations

import hashlib
import math
import struct
from dataclasses import dataclass
from enum import Enum
from typing import Sequence


class FailureCategory(str, Enum):
    EMPTY_INPUT = "empty_input"
    NON_FINITE_INPUT = "non_finite_input"
    MIXED_INFINITIES = "mixed_infinities"
    INPUT_TOO_LARGE = "input_too_large"


class SummationRejected(Exception):
    """输入违反固定规则时抛出；category 供 API 层映射为稳定的失败类别。"""

    def __init__(self, category: FailureCategory, message: str, detail: dict | None = None):
        super().__init__(message)
        self.category = category
        self.detail = detail or {}


@dataclass(frozen=True)
class InputProfile:
    """输入的脱敏画像：只含计数与量级，绝不含原始数值序列。"""

    count: int
    nan_count: int
    pos_inf_count: int
    neg_inf_count: int
    zero_count: int
    neg_zero_count: int
    max_abs_finite: float
    digest: str  # float64 小端字节流的 sha256（前 16 位），用于审计对齐而非还原数据

    def redacted(self) -> dict:
        return {
            "count": self.count,
            "nan_count": self.nan_count,
            "pos_inf_count": self.pos_inf_count,
            "neg_inf_count": self.neg_inf_count,
            "zero_count": self.zero_count,
            "neg_zero_count": self.neg_zero_count,
            "max_abs_finite": self.max_abs_finite,
            "digest": self.digest,
        }


def _digest(values: Sequence[float]) -> str:
    h = hashlib.sha256()
    h.update(struct.pack(f"<{len(values)}d", *values))
    return h.hexdigest()[:16]


def scan_input(values: Sequence[float]) -> InputProfile:
    """扫描输入并执行规则 1-3；返回脱敏画像。"""
    n = len(values)
    if n == 0:
        raise SummationRejected(FailureCategory.EMPTY_INPUT, "输入序列为空")

    nan_count = 0
    pos_inf = 0
    neg_inf = 0
    zero = 0
    neg_zero = 0
    max_abs = 0.0
    for v in values:
        if math.isnan(v):
            nan_count += 1
        elif math.isinf(v):
            if v > 0:
                pos_inf += 1
            else:
                neg_inf += 1
        else:
            if v == 0.0:
                zero += 1
                if math.copysign(1.0, v) < 0:
                    neg_zero += 1
            av = abs(v)
            if av > max_abs:
                max_abs = av

    profile = InputProfile(
        count=n,
        nan_count=nan_count,
        pos_inf_count=pos_inf,
        neg_inf_count=neg_inf,
        zero_count=zero,
        neg_zero_count=neg_zero,
        max_abs_finite=max_abs,
        digest=_digest(values),
    )

    if nan_count:
        raise SummationRejected(
            FailureCategory.NON_FINITE_INPUT,
            "输入包含 NaN",
            {"nan_count": nan_count, "digest": profile.digest},
        )
    if pos_inf and neg_inf:
        raise SummationRejected(
            FailureCategory.MIXED_INFINITIES,
            "输入同时包含 +Inf 与 -Inf（不定式）",
            {"pos_inf_count": pos_inf, "neg_inf_count": neg_inf, "digest": profile.digest},
        )
    return profile


def apply_special_rules(profile: InputProfile) -> float | None:
    """规则 4-5：返回非 None 表示结果已由特殊值规则决定，无需有限求和。"""
    if profile.pos_inf_count:
        return math.inf
    if profile.neg_inf_count:
        return -math.inf
    if profile.zero_count == profile.count:
        # 全零输入：仅当每一项都是 -0.0 时结果为 -0.0
        return -0.0 if profile.neg_zero_count == profile.count else 0.0
    return None


def normalize_zero(result: float, profile: InputProfile) -> float:
    """规则 5 的收尾：有限路径算出的零一律规范为 +0.0。

    （全零输入已在 apply_special_rules 处理，不会走到这里。）
    """
    if result == 0.0:
        return 0.0
    return result
