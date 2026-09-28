"""全局配置与默认值。

内存以"浮点元素个数"为统一计量单位（避免依赖真实 GPU/分配器），
因此默认 dtype 为 float64，每元素 8 字节；:func:`to_bytes` 可换算为字节。
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_DTYPE = "float64"
ELEMENT_BYTES = {"float32": 4, "float64": 8}


@dataclass(frozen=True)
class Config:
    """服务级配置（不可变）。

    Attributes
    ----------
    dtype:
        所有张量使用的浮点类型。
    grad_tol:
        梯度一致性校验的绝对容差。
    finite_diff_eps:
        数值验证中有限差分的扰动量。
    default_budget_elements:
        API 未显式给出预算时的默认内存预算（元素数）。
    journal_dir:
        测试/运行日志的输出目录（相对路径或绝对路径）。
    """

    dtype: str = DEFAULT_DTYPE
    grad_tol: float = 1e-6
    finite_diff_eps: float = 1e-5
    default_budget_elements: int = 1_000_000
    journal_dir: str = "logs"

    def element_bytes(self) -> int:
        if self.dtype not in ELEMENT_BYTES:
            from .errors import GraphValidationError

            raise GraphValidationError(
                f"unsupported dtype: {self.dtype!r}",
                dtype=self.dtype,
                supported=sorted(ELEMENT_BYTES),
            )
        return ELEMENT_BYTES[self.dtype]


DEFAULT_CONFIG = Config()


def to_bytes(elements: float, dtype: str = DEFAULT_DTYPE) -> int:
    """把元素数换算为字节数。"""
    if dtype not in ELEMENT_BYTES:
        raise KeyError(dtype)
    return int(elements) * ELEMENT_BYTES[dtype]
