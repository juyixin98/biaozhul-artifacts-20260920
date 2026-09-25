"""PCM 容器校验的异常层级。

所有可预期的错误（格式不支持、文件截断、采样对齐错误等）都派生自
PcmError，CLI 捕获它后以退出码 2 结束，便于脚本区分"业务拒绝"与环境错误。
"""


class PcmError(Exception):
    """所有 PCM 容器/数据相关错误的基类。"""


class WavFormatError(PcmError):
    """WAV 内容不是本服务支持的 PCM 子集（格式拒绝）。"""


class WavTruncatedError(PcmError):
    """WAV 容器在 chunk 头或 chunk 体中间被截断。"""


class PcmAlignmentError(PcmError):
    """数据长度与 block align / 通道 / 位深不一致，采样无法对齐。"""
