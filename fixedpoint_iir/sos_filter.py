"""二阶节（SOS）级联 IIR 滤波：定点实现与浮点参考路径。

定点路径采用直接 II 型转置（DF-II Transposed）逐节级联：

    y[n]  = b0*x[n] + s1[n-1]
    s1[n] = b1*x[n] - a1*y[n] + s2[n-1]
    s2[n] = b2*x[n] - a2*y[n]

每个 SOS 行为 [b0, b1, b2, a0, a1, a2]，要求 a0 == 1（内部会归一化并记录缩放）。

定点语义：
- 输入样本先量化到 state_spec（数据格式）。
- 系数量化到 coef_spec。
- 乘积在“无限宽”Python 整数累加器中累加（等价于硬件宽累加器，
  位宽 = coef.word_bits + state.word_bits + 2，不会溢出）。
- 每次写回状态 / 输出前，累加器右移 coef.frac_bits 位，
  按 state_spec 舍入并饱和/回绕。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .quantization import (
    QuantSpec,
    dequantize_array,
    quantize_array,
    quantize_scalar,
    round_accumulator_to_spec,
)


def normalize_sos(sos: np.ndarray) -> np.ndarray:
    """归一化 SOS 使每节 a0 == 1（分子分母同除 a0，传递函数不变）。"""
    sos = np.asarray(sos, dtype=np.float64)
    if sos.ndim != 2 or sos.shape[1] != 6:
        raise ValueError("sos 必须是形状 (n_sections, 6) 的数组")
    if np.any(sos[:, 3] == 0.0):
        raise ValueError("存在 a0 == 0 的二阶节，无法归一化")
    norm = sos.copy()
    norm[:, :3] /= sos[:, 3:4]
    norm[:, 3:] /= sos[:, 3:4]
    return norm


def sos_filter_float(sos: np.ndarray, x: np.ndarray) -> np.ndarray:
    """浮点参考路径：DF-II 转置直接形式，float64 全精度。"""
    norm = normalize_sos(sos)
    x = np.asarray(x, dtype=np.float64)
    y = np.empty_like(x)
    s1 = np.zeros(norm.shape[0])
    s2 = np.zeros(norm.shape[0])
    for n in range(x.shape[0]):
        v = x[n]
        for k in range(norm.shape[0]):
            b0, b1, b2, _, a1, a2 = norm[k]
            out = b0 * v + s1[k]
            s1[k] = b1 * v - a1 * out + s2[k]
            s2[k] = b2 * v - a2 * out
            v = out
        y[n] = v
    return y


@dataclass
class SectionStats:
    """单节定点运行统计。"""

    n_state_overflow: int = 0   # 状态写回时发生饱和/回绕的次数
    n_output_overflow: int = 0  # 输出写回时发生饱和/回绕的次数
    max_abs_state: float = 0.0  # 运行中状态的最大绝对值（实数域）


@dataclass
class FixedPointResult:
    """定点滤波结果。"""

    y: np.ndarray                       # 输出实数数组（已按 state_spec 反量化）
    y_codes: np.ndarray                 # 输出定点整数码
    x_codes: np.ndarray                 # 输入定点整数码
    section_stats: list = field(default_factory=list)  # list[SectionStats]
    n_input_overflow: int = 0           # 输入量化溢出次数
    coef_overflow_flags: list = field(default_factory=list)  # 每个系数是否饱和

    @property
    def total_overflows(self) -> int:
        total = self.n_input_overflow
        for s in self.section_stats:
            total += s.n_state_overflow + s.n_output_overflow
        return total


class FixedPointSOSFilter:
    """定点 SOS 级联 IIR 滤波器。

    Args:
        sos: 形状 (n_sections, 6) 的浮点系数，行为 [b0,b1,b2,a0,a1,a2]。
        coef_spec: 系数量化格式。
        state_spec: 数据 / 状态 / 输出量化格式。
    """

    def __init__(self, sos: np.ndarray, coef_spec: QuantSpec, state_spec: QuantSpec):
        self.coef_spec = coef_spec
        self.state_spec = state_spec
        norm = normalize_sos(sos)
        self.sos_float = norm
        # 量化系数并记录哪些系数发生了饱和（定标不合理的重要信号）
        self.sos_q = np.empty_like(norm)
        self.coef_overflow_flags: list[bool] = []
        for k in range(norm.shape[0]):
            row = []
            for j in range(6):
                code, ov = quantize_scalar(float(norm[k, j]), coef_spec)
                row.append(code)
                self.coef_overflow_flags.append(ov)
            self.sos_q[k] = dequantize_array(np.array(row), coef_spec)
        # 整数码形式的系数，用于整数乘累加
        self._b, _ = quantize_array(self.sos_q[:, :3].ravel(), coef_spec)
        self._b = self._b.reshape(-1, 3)
        self._a, _ = quantize_array(self.sos_q[:, 4:6].ravel(), coef_spec)
        self._a = self._a.reshape(-1, 2)
        self._s1 = np.zeros(norm.shape[0], dtype=np.int64)
        self._s2 = np.zeros(norm.shape[0], dtype=np.int64)

    @property
    def n_sections(self) -> int:
        return self.sos_q.shape[0]

    def reset(self) -> None:
        """清零内部状态。"""
        self._s1[:] = 0
        self._s2[:] = 0

    def _quantize_accum(self, acc: int) -> tuple[int, bool]:
        """累加器 -> 状态字长（右移 coef.frac_bits 位 + 舍入 + 溢出）。"""
        codes, n_over = round_accumulator_to_spec(
            np.array([acc], dtype=np.int64), self.coef_spec.frac_bits, self.state_spec
        )
        return int(codes[0]), n_over > 0

    def process(self, x: np.ndarray) -> FixedPointResult:
        """对输入实数数组做定点滤波，返回结果与溢出统计。"""
        self.reset()
        x_codes, n_in_over = quantize_array(np.asarray(x, dtype=np.float64), self.state_spec)
        n = x_codes.shape[0]
        y_codes = np.empty(n, dtype=np.int64)
        stats = [SectionStats() for _ in range(self.n_sections)]

        for i in range(n):
            v = int(x_codes[i])
            for k in range(self.n_sections):
                b0, b1, b2 = (int(c) for c in self._b[k])
                a1, a2 = (int(c) for c in self._a[k])
                # 累加器小数位 = coef.frac_bits + state.frac_bits
                acc_y = b0 * v + int(self._s1[k]) * (1 << self.coef_spec.frac_bits)
                y_sec, ov_y = self._quantize_accum(acc_y)
                acc_s1 = b1 * v - a1 * y_sec + int(self._s2[k]) * (1 << self.coef_spec.frac_bits)
                s1_new, ov_1 = self._quantize_accum(acc_s1)
                acc_s2 = b2 * v - a2 * y_sec
                s2_new, ov_2 = self._quantize_accum(acc_s2)
                self._s1[k] = s1_new
                self._s2[k] = s2_new
                st = stats[k]
                st.n_output_overflow += int(ov_y)
                st.n_state_overflow += int(ov_1) + int(ov_2)
                st.max_abs_state = max(
                    st.max_abs_state,
                    abs(s1_new) / (1 << self.state_spec.frac_bits),
                    abs(s2_new) / (1 << self.state_spec.frac_bits),
                )
                v = y_sec
            y_codes[i] = v

        return FixedPointResult(
            y=dequantize_array(y_codes, self.state_spec),
            y_codes=y_codes,
            x_codes=x_codes,
            section_stats=stats,
            n_input_overflow=int(n_in_over),
            coef_overflow_flags=list(self.coef_overflow_flags),
        )
