"""pcmval — 离线 PCM 容器校验与信号处理服务(纯后端,无界面/播放器)。

子模块:
- pcmval.pcm     整数 PCM 样本 <-> 浮点振幅 转换(16/24 bit)
- pcmval.wavio   WAV(RIFF/WAVE)PCM 子集读写与容器校验
- pcmval.synth   合成测试信号
- pcmval.service 离线请求处理(校验 / 合成 / 往返)
"""

from . import pcm, service, synth, wavio

__version__ = "0.1.0"
__all__ = ["pcm", "wavio", "synth", "service", "__version__"]
