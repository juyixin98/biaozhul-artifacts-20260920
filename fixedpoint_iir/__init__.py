"""定点 IIR 滤波离线信号处理包。

以二阶节（SOS）级联实现定点 IIR 滤波，提供：
- 明确的 Q 格式定标、饱和与舍入语义（quantization）
- 定点滤波器与浮点参考路径（sos_filter）
- 系数量化后的稳定性风险检测（stability）
- 合成信号与 PCM 读写（signals）
- 溢出 / 极限环 / 频响误差分析（analysis）
- 离线批处理服务入口（service）
"""

from .quantization import QuantSpec, quantize_array, quantize_scalar
from .sos_filter import FixedPointSOSFilter, sos_filter_float
from .stability import StabilityReport, check_sos_stability, quantize_sos_checked

__all__ = [
    "QuantSpec",
    "quantize_array",
    "quantize_scalar",
    "FixedPointSOSFilter",
    "sos_filter_float",
    "StabilityReport",
    "check_sos_stability",
    "quantize_sos_checked",
]

__version__ = "0.1.0"
