# 流式 FIR 状态管理(离线信号处理服务)

纯后端 Python + NumPy 项目:多通道流式 FIR 卷积,支持运行中切换滤波器(显式交叉渐变),输入为合成信号或本地 PCM 文件,输出仅为数值与文件。无播放器、无界面。

## 功能

- **多通道流式 FIR 卷积**:任意分块大小(含不能整除信号长度的分块),输出与整段 `np.convolve` 参考逐样本一致(误差 < 1e-10)。
- **运行中滤波器切换**:raised-cosine 交叉渐变,避免突跳;`fade_samples=0` 为硬切换(新滤波器仍用真实输入历史预置状态,不会从静音重启)。
- **切换中再切换**:渐变未结束时发起新切换,当前混合输出整体渐向新滤波器,行为有明确定义且有测试覆盖。
- **每通道状态隔离**:每个通道独立的滤波器链、状态与渐变调度;可按通道单独切换。
- **空块不改变状态**:零长度块返回空数组,所有内部状态保持不变。
- **通道数变化**:`set_num_channels` 重新配置通道数(状态重置,文档化行为)。
- **边界不连续检测**:`boundary_jumps` / `find_discontinuities` 工具,测试验证分块边界无不连续。

## 结构

```
fir_stream/
  core.py       # StreamingFIR(单通道车道/渐变引擎)、MultiChannelFIR(通道组)
  filters.py    # 滤波器设计:lowpass/highpass/moving_average/delay/identity/custom
  synth.py      # 合成测试信号(多正弦 + 噪声,种子可复现)
  pcm.py        # 裸 PCM 读写(s16le / f32le / f64le,交错)
  analysis.py   # 连续性/误差分析工具
  service.py    # 离线作业运行器(JSON 作业 -> PCM 输出 + JSON 报告)
  cli.py        # python -m fir_stream.cli job.json
tests/test_streaming_fir.py   # 23 项验收测试
examples/jobs/*.job.json      # 请求样例
examples/make_sample_pcm.py   # 生成 PCM 输入样例
```

## 核心设计

每个通道维护若干 **lane**(滤波器 + 独立输入历史状态 + 标量权重),通道输出为各 lane 输出的加权和。切换滤波器时:

1. 新 lane 用最近输入历史预置状态(卷积反映真实过去信号);
2. 新 lane 权重按 raised-cosine 从 0 渐到 1,旧 lane 整体按 (1 - ramp) 衰减;
3. 渐变结束后旧 lane 被丢弃,稳态开销为每块一次卷积。

渐变中再切换 = 再追加一个 lane,当前混合整体渐向新滤波器。

## 快速开始

```bash
# 运行测试(需要 numpy、pytest)
python3 -m pytest tests/ -v

# 运行合成信号示例作业(含切换中再切换、单通道硬切换)
python3 -m fir_stream.cli examples/jobs/synth_crossfade.job.json

# 运行 PCM 文件示例作业
python3 examples/make_sample_pcm.py
python3 -m fir_stream.cli examples/jobs/pcm_file.job.json
```

## 请求样例(作业 JSON)

见 `examples/jobs/`。字段说明:

| 字段 | 说明 |
|------|------|
| `sample_rate` | 采样率 (Hz) |
| `channels` | 通道数 |
| `block_size` | 流式分块大小(样本) |
| `input` | `{"type":"synth", ...}` 或 `{"type":"pcm","path","format"}` |
| `filter` | 初始滤波器:`lowpass`/`highpass`/`moving_average`/`delay`/`identity`/`custom` |
| `schedule` | 切换事件:`at_sample`(样本级精确)、`filter`、`fade_samples`、`channel`(null=全部) |
| `output` | 输出 PCM 路径/格式与报告路径 |

调度事件是样本级精确的:包含 `at_sample` 的块会被拆分,切换恰好发生在该样本。

## 库用法

```python
import numpy as np
from fir_stream import MultiChannelFIR, filters

engine = MultiChannelFIR(2, filters.lowpass(63, 1000.0, 48000.0))
y1 = engine.process(block1)                 # block: (2, N)
engine.switch(filters.highpass(63, 3000.0, 48000.0), fade_samples=512)
y2 = engine.process(block2)                 # 交叉渐变,无突跳
engine.switch(filters.moving_average(9), fade_samples=256, channel=0)  # 仅通道 0
```

## 测试与运行记录

实际命令与输出见 [RUN_LOG.md](RUN_LOG.md)。当前状态:**23 项测试全部通过,行覆盖率 89%**。
