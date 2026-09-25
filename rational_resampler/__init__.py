"""有理比重采样（rational resampling）纯后端信号处理包。

核心约定
--------
- 重采样比为 L/M（上采样 L 倍、下采样 M 倍），输出采样率 fs_out = fs_in * L / M。
- 抗混叠 FIR 为对称奇数长滤波器，群延迟 D = (N-1)/2 个上采样域采样点。
- 实现对齐方式为零相位对齐：输出 y[m] 对应输入时刻 t = m*M/L（输入采样点单位），
  群延迟由索引方式天然补偿；边界由填充（zero/edge/reflect）处理。
- 分块处理与整段处理结果逐样本一致（同一状态机驱动）。
"""

from .filter_design import design_anti_alias_fir, kaiser_beta, kaiser_length
from .core import PolyphaseResampler, StreamingResampler, resample, filter_info

__all__ = [
    "design_anti_alias_fir",
    "kaiser_beta",
    "kaiser_length",
    "PolyphaseResampler",
    "StreamingResampler",
    "resample",
    "filter_info",
]

__version__ = "0.1.0"
