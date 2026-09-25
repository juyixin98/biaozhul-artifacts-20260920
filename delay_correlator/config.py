"""配置对象与参数校验。

所有阈值集中在 :class:`EstimatorConfig`，既可用于库调用，也由 ``run`` 命令
从请求 JSON 解析构造。校验失败统一抛出 :class:`ValueError`，由 CLI 转成清晰的错误信息。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class EstimatorConfig:
    """延迟估计参数。

    延迟符号约定（贯穿整个项目，README 与测试均以此为准）：

        delay_samples > 0 表示 ref 的某个特征在 chan 中延后 delay_samples 个采样点出现
        （即 chan 比 ref 滞后/右移 delay_samples，ref 比 chan 提前）。
        delay_samples < 0 表示 chan 比 ref 超前（左移）。

    估计器通过在 ref 上平移对齐来实现：在滞后 ``lag`` 处比较 ``ref[k - lag]`` 与
    ``chan[k]``，因此正的最优 lag 正好等于 ``chan = shift(ref, +delay)`` 中的右移量，
    符号直观、无需取反。

    置信度判定（任一不满足则状态为 ``uncertain``）：

    * ``status == "confident"`` 要求窗口能量高于 ``min_rms_db``（相对满量程 1.0 的 dB）；
    * NCC 峰高不低于 ``min_peak``；
    * 次强候选峰（与主峰间隔至少 ``min_peak_distance`` 个采样点）的相对高度
      ``second_peak / peak`` 不超过 ``max_secondary_peak_ratio``，否则视为多峰模糊；
    * 峰落在搜索范围两端（``|lag| == max_lag``）视为边界不可信（``edge_hit``）。
    """

    max_lag: int = 512
    min_peak: float = 0.5
    max_secondary_peak_ratio: float = 0.85
    min_rms_db: float = -60.0
    min_peak_distance: int = 3
    window_size: int | None = None
    hop_size: int | None = None
    subtract_mean: bool = True
    interpolate: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.max_lag, (int, np.integer)) or self.max_lag <= 0:
            raise ValueError("max_lag 必须为正整数")
        if not 0.0 < self.min_peak <= 1.0 + 1e-9:
            raise ValueError("min_peak 必须落在 (0, 1]")
        if not 0.0 < self.max_secondary_peak_ratio <= 1.0 + 1e-9:
            raise ValueError("max_secondary_peak_ratio 必须落在 (0, 1]")
        if self.min_rms_db > 0.0:
            raise ValueError("min_rms_db 应为非正值（dBFS，满量程为 1.0）")
        if not isinstance(self.min_peak_distance, (int, np.integer)) or self.min_peak_distance < 1:
            raise ValueError("min_peak_distance 必须为 >=1 的整数")
        if self.window_size is not None:
            if not isinstance(self.window_size, (int, np.integer)) or self.window_size <= 0:
                raise ValueError("window_size 必须为正整数")
            if self.window_size <= 2 * self.max_lag:
                raise ValueError(
                    f"window_size ({self.window_size}) 必须大于 2*max_lag "
                    f"({2 * self.max_lag})，否则搜索范围没有有效重叠"
                )
        if self.hop_size is not None:
            if not isinstance(self.hop_size, (int, np.integer)) or self.hop_size <= 0:
                raise ValueError("hop_size 必须为正整数")
            if self.window_size is None:
                raise ValueError("指定 hop_size 时必须同时指定 window_size")

    @property
    def rms_floor(self) -> float:
        """``min_rms_db`` 对应的线性 RMS 门限。"""
        return float(10.0 ** (self.min_rms_db / 20.0))

    @classmethod
    def from_dict(cls, data: dict) -> "EstimatorConfig":
        """从请求 JSON 中识别 ``estimator`` 段；未知字段直接报错，防止静默拼写错误。"""
        if data is None:
            return cls()
        if not isinstance(data, dict):
            raise ValueError("estimator 配置必须是对象")
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        # 以下划线开头的键（如 "_comment"）视为注释，不参与解析。
        data = {k: v for k, v in data.items() if not k.startswith("_")}
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"estimator 含未知参数: {sorted(unknown)}")
        return cls(**data)
