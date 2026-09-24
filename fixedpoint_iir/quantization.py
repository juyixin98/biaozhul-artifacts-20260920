"""Q 格式定点量化：定标、舍入与饱和语义。

Q 格式记法 Qm.n：m 为整数位（含符号位），n 为小数位，总字长 word_bits = m + n。
例如 Q2.14 表示 16 位字长、14 位小数，可表示范围约 [-2, 2)。

所有量化路径统一经过本模块，保证“先缩放 -> 舍入 -> 饱和/回绕”的顺序唯一。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

# 支持的舍入模式
ROUND_TRUNC = "trunc"        # 向零截断
ROUND_HALF_UP = "round"      # 最近舍入，平局远离零（round-half-away-from-zero）
ROUND_CONVERGENT = "convergent"  # 最近舍入，平局取偶（banker's rounding）

# 支持的溢出模式
OVERFLOW_SATURATE = "saturate"  # 饱和到可表示边界
OVERFLOW_WRAP = "wrap"          # 二进制补码回绕

_ROUND_MODES = {ROUND_TRUNC, ROUND_HALF_UP, ROUND_CONVERGENT}
_OVERFLOW_MODES = {OVERFLOW_SATURATE, OVERFLOW_WRAP}


@dataclass(frozen=True)
class QuantSpec:
    """定点格式描述。

    Attributes:
        word_bits: 总字长（含符号位），如 16。
        frac_bits: 小数位数 n，如 14 表示 Q(word_bits-n).n。
        rounding: 舍入模式，见 ROUND_* 常量。
        overflow: 溢出模式，见 OVERFLOW_* 常量。
    """

    word_bits: int = 16
    frac_bits: int = 14
    rounding: str = ROUND_HALF_UP
    overflow: str = OVERFLOW_SATURATE

    def __post_init__(self) -> None:
        if self.word_bits < 2:
            raise ValueError("word_bits 必须 >= 2（至少 1 符号位 + 1 数据位）")
        if not 0 <= self.frac_bits < self.word_bits:
            raise ValueError("frac_bits 必须满足 0 <= frac_bits < word_bits")
        if self.rounding not in _ROUND_MODES:
            raise ValueError(f"未知舍入模式: {self.rounding}")
        if self.overflow not in _OVERFLOW_MODES:
            raise ValueError(f"未知溢出模式: {self.overflow}")

    @property
    def int_bits(self) -> int:
        """整数位数（含符号位）。"""
        return self.word_bits - self.frac_bits

    @property
    def qmin(self) -> int:
        """可表示的最小整数码。"""
        return -(1 << (self.word_bits - 1))

    @property
    def qmax(self) -> int:
        """可表示的最大整数码。"""
        return (1 << (self.word_bits - 1)) - 1

    @property
    def min_value(self) -> float:
        """可表示的最小实数值。"""
        return self.qmin / (1 << self.frac_bits)

    @property
    def max_value(self) -> float:
        """可表示的最大实数值。"""
        return self.qmax / (1 << self.frac_bits)

    @property
    def resolution(self) -> float:
        """一个 LSB 对应的实数间隔。"""
        return 1.0 / (1 << self.frac_bits)

    def describe(self) -> str:
        return (
            f"Q{self.int_bits}.{self.frac_bits} ({self.word_bits}bit, "
            f"range [{self.min_value}, {self.max_value}], "
            f"lsb={self.resolution}, {self.rounding}/{self.overflow})"
        )


def _round_to_int(scaled: np.ndarray, mode: str) -> np.ndarray:
    """把已缩放的浮点数组按指定模式舍入为整数（float64 承载，值域为整数）。"""
    if mode == ROUND_TRUNC:
        return np.trunc(scaled)
    if mode == ROUND_HALF_UP:
        # 平局远离零：sign(x) * floor(|x| + 0.5)
        return np.sign(scaled) * np.floor(np.abs(scaled) + 0.5)
    if mode == ROUND_CONVERGENT:
        return np.rint(scaled)  # NumPy rint 即平局取偶
    raise ValueError(f"未知舍入模式: {mode}")


def _apply_overflow(code: np.ndarray, spec: QuantSpec) -> tuple[np.ndarray, int]:
    """对整数码应用溢出处理，返回 (处理后码, 发生溢出的样本数)。"""
    if spec.overflow == OVERFLOW_SATURATE:
        clipped = np.clip(code, spec.qmin, spec.qmax)
        n_over = int(np.count_nonzero(clipped != code))
        return clipped, n_over
    if spec.overflow == OVERFLOW_WRAP:
        period = 1 << spec.word_bits
        wrapped = ((code - spec.qmin) % period) + spec.qmin
        n_over = int(np.count_nonzero(wrapped != code))
        return wrapped, n_over
    raise ValueError(f"未知溢出模式: {spec.overflow}")


def quantize_array(values: np.ndarray, spec: QuantSpec) -> tuple[np.ndarray, int]:
    """把实数数组量化为定点整数码。

    Returns:
        (codes, n_overflow): int64 整数码数组与溢出样本数。
    """
    arr = np.asarray(values, dtype=np.float64)
    scaled = arr * (1 << spec.frac_bits)
    rounded = _round_to_int(scaled, spec.rounding)
    codes, n_over = _apply_overflow(rounded, spec)
    return codes.astype(np.int64), n_over


def quantize_scalar(value: float, spec: QuantSpec) -> tuple[int, bool]:
    """量化单个实数，返回 (整数码, 是否发生溢出)。"""
    codes, n_over = quantize_array(np.array([value]), spec)
    return int(codes[0]), n_over > 0


def dequantize_array(codes: np.ndarray, spec: QuantSpec) -> np.ndarray:
    """把定点整数码还原为实数值。"""
    return np.asarray(codes, dtype=np.float64) / (1 << spec.frac_bits)


def round_accumulator_to_spec(acc: np.ndarray, shift: int, spec: QuantSpec) -> tuple[np.ndarray, int]:
    """把宽累加器值右移 shift 位后落入 spec 字长（含舍入与溢出）。

    定点乘累加的结果位宽大于状态字长，写回状态前必须经此函数，
    等价于硬件上的“累加器 -> 舍入 -> 饱和 -> 存回”。

    Args:
        acc: int64 累加器值数组。
        shift: 右移位数（= 乘数的小数位之和 - 目标小数位）。
        spec: 目标定点格式。

    Returns:
        (codes, n_overflow)
    """
    arr = np.asarray(acc, dtype=np.int64)
    if shift < 0:
        raise ValueError("shift 必须 >= 0")
    if shift == 0:
        rounded = arr.astype(np.float64)
    elif spec.rounding == ROUND_TRUNC:
        # 算术右移向 -inf 取整；向零截断需对含非零小数的负数补偿 +1
        shifted = arr >> shift
        nonzero_frac = (arr & ((1 << shift) - 1)) != 0
        rounded = shifted + ((arr < 0) & nonzero_frac)
        rounded = rounded.astype(np.float64)
    else:
        # 在浮点域做 2^-shift 缩放后复用统一舍入，保证语义一致
        rounded = _round_to_int(arr.astype(np.float64) / (1 << shift), spec.rounding)
    codes, n_over = _apply_overflow(rounded, spec)
    return codes.astype(np.int64), n_over
