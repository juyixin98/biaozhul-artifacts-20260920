"""鲁棒核函数（IRLS 加权）。

记边的马氏距离平方 ``u = e^T Ω e``、``r = sqrt(u)``，鲁棒目标为
``0.5 * ρ(r)``。每次迭代按 ``w(r) = ρ'(r)/r`` 把信息矩阵缩放为
``w Ω``，从而抑制错误回环（IRLS）。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

KERNEL_TYPES = ("none", "huber", "cauchy", "tukey")


@dataclass(frozen=True)
class Kernel:
    """鲁棒核配置。

    Attributes:
        name: ``none`` / ``huber`` / ``cauchy`` / ``tukey`` 之一。
        delta: 核阈值（尺度参数），``none`` 时忽略。
    """

    name: str = "none"
    delta: float = 1.0

    def __post_init__(self) -> None:
        name = self.name.lower()
        if name not in KERNEL_TYPES:
            raise ValueError(
                f"未知鲁棒核类型 {self.name!r}，可选: {', '.join(KERNEL_TYPES)}"
            )
        if name != "none" and self.delta <= 0.0:
            raise ValueError("鲁棒核 delta 必须为正数")
        object.__setattr__(self, "name", name)


def kernel_weight(kernel: Kernel, u: float) -> float:
    """返回 IRLS 权重 ``w``（信息矩阵乘子）。"""
    r = float(np.sqrt(max(u, 0.0)))
    delta = kernel.delta
    if kernel.name == "none" or r <= 1e-12:
        return 1.0
    if kernel.name == "huber":
        return 1.0 if r <= delta else delta / r
    if kernel.name == "cauchy":
        return 1.0 / (1.0 + (r / delta) ** 2)
    # tukey (bisquare)：超过阈值的残差权重直接为 0
    if r >= delta:
        return 0.0
    ratio = 1.0 - (r / delta) ** 2
    return ratio * ratio


def robust_cost(kernel: Kernel, u: float) -> float:
    """单边鲁棒目标值 ``0.5 * ρ(r)``（``none`` 时即普通 0.5*u）。"""
    if kernel.name == "none":
        return 0.5 * u
    r = float(np.sqrt(max(u, 0.0)))
    delta = kernel.delta
    if kernel.name == "huber":
        if r <= delta:
            return 0.5 * r * r
        return delta * r - 0.5 * delta * delta
    if kernel.name == "cauchy":
        return 0.5 * delta * delta * np.log1p((r / delta) ** 2)
    # tukey
    if r >= delta:
        return delta * delta / 6.0
    ratio = 1.0 - (r / delta) ** 2
    return delta * delta / 6.0 * (1.0 - ratio**3)
