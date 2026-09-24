"""系数量化后的稳定性风险检测。

检测内容：
1. 系数是否超出量化格式可表示范围（会被饱和，等价于改换了滤波器）。
2. 量化后每个二阶节的极点位置：|pole| >= 1 判定不稳定，
   1 - margin <= |pole| < 1 判定为“临界稳定风险”（量化/极限环敏感）。
3. 量化前后极点最大位移，用于衡量灵敏度。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .quantization import QuantSpec, dequantize_array, quantize_array
from .sos_filter import normalize_sos

# 极点模长安全裕度默认值：|pole| >= 1 - margin 即给出风险提示
DEFAULT_POLE_MARGIN = 1e-2


@dataclass
class SectionPoleInfo:
    """单节极点信息。"""

    section: int
    poles: list          # 量化后极点（复数）
    max_radius: float
    ref_max_radius: float  # 量化前（浮点参考）极点模长
    pole_shift: float      # 量化前后极点最大位移


@dataclass
class StabilityReport:
    """稳定性检测报告。"""

    stable: bool                       # 量化后所有极点严格在单位圆内
    has_risk: bool                     # 是否存在任何风险项
    sections: list = field(default_factory=list)   # list[SectionPoleInfo]
    coef_out_of_range: list = field(default_factory=list)  # [(section, coef_idx, value)]
    warnings: list = field(default_factory=list)   # list[str]

    def to_dict(self) -> dict:
        return {
            "stable": self.stable,
            "has_risk": self.has_risk,
            "coef_out_of_range": [
                {"section": s, "coef_index": j, "value": v}
                for s, j, v in self.coef_out_of_range
            ],
            "sections": [
                {
                    "section": p.section,
                    "poles": [[z.real, z.imag] for z in p.poles],
                    "max_radius": p.max_radius,
                    "ref_max_radius": p.ref_max_radius,
                    "pole_shift": p.pole_shift,
                }
                for p in self.sections
            ],
            "warnings": list(self.warnings),
        }


def sos_poles(sos: np.ndarray) -> list[np.ndarray]:
    """计算每个二阶节的极点（分母 1 + a1 z^-1 + a2 z^-2 的根）。"""
    norm = normalize_sos(sos)
    poles = []
    for k in range(norm.shape[0]):
        _, _, _, _, a1, a2 = norm[k]
        # 分母多项式 z^2 + a1 z + a2 的根
        poles.append(np.roots([1.0, a1, a2]))
    return poles


def quantize_sos_checked(sos: np.ndarray, coef_spec: QuantSpec) -> tuple[np.ndarray, list]:
    """量化 SOS 系数，返回 (量化后 SOS, 越界系数列表 [(section, idx, 原值)])。"""
    norm = normalize_sos(sos)
    codes, _ = quantize_array(norm.ravel(), coef_spec)
    sos_q = dequantize_array(codes, coef_spec).reshape(norm.shape)
    out_of_range = []
    for idx, value in np.ndenumerate(norm):
        if value < coef_spec.min_value or value > coef_spec.max_value:
            out_of_range.append((int(idx[0]), int(idx[1]), float(value)))
    return sos_q, out_of_range


def check_sos_stability(
    sos: np.ndarray,
    coef_spec: QuantSpec,
    pole_margin: float = DEFAULT_POLE_MARGIN,
) -> StabilityReport:
    """对“量化后”的 SOS 做稳定性与风险检测。"""
    sos_q, out_of_range = quantize_sos_checked(sos, coef_spec)
    ref_poles = sos_poles(sos)
    q_poles = sos_poles(sos_q)

    report = StabilityReport(stable=True, has_risk=False)
    report.coef_out_of_range = out_of_range

    for k, (qp, rp) in enumerate(zip(q_poles, ref_poles)):
        max_r = float(np.max(np.abs(qp)))
        ref_max_r = float(np.max(np.abs(rp)))
        # 极点集合最大位移（两节均为二阶，直接对齐比较）
        shift = float(np.max(np.abs(np.sort_complex(qp) - np.sort_complex(rp))))
        report.sections.append(
            SectionPoleInfo(
                section=k,
                poles=[complex(z) for z in qp],
                max_radius=max_r,
                ref_max_radius=ref_max_r,
                pole_shift=shift,
            )
        )
        if max_r >= 1.0:
            report.stable = False
            report.warnings.append(
                f"第 {k} 节量化后极点模长 {max_r:.6f} >= 1，滤波器不稳定"
            )
        elif max_r >= 1.0 - pole_margin:
            report.warnings.append(
                f"第 {k} 节量化后极点模长 {max_r:.6f} 距单位圆小于 {pole_margin}，"
                "存在临界稳定 / 极限环风险"
            )
        if ref_max_r < 1.0 <= max_r:
            report.warnings.append(
                f"第 {k} 节浮点参考稳定但量化后失稳（系数量化导致）"
            )

    for s, j, v in out_of_range:
        report.warnings.append(
            f"第 {s} 节系数索引 {j} 的值 {v:.6g} 超出 {coef_spec.describe()} "
            "可表示范围，量化时被饱和截断"
        )

    report.has_risk = (not report.stable) or bool(report.warnings)
    return report
