# streaming-stft

纯后端离线信号处理服务：用 Python + NumPy 实现**流式短时傅里叶变换（STFT）与逆变换（iSTFT）**。
输入为合成信号或本地无头 PCM / `.npy` 数据，输出只有数值（JSON 指标）和文件（频谱、重建 PCM）。
**没有播放器、没有图形界面、没有网络服务。**

## 功能

- 加窗 STFT（`np.fft.rfft`）与**窗重叠权重归一**逆变换（WOLA）：
  `x̂[n] = Σₘ w[n−mH]·yₘ[n−mH] / Σₘ w²[n−mH]`
- 显式的帧参数与补边规则：窗长 `nfft`、步长 `hop`（要求 `1 ≤ hop ≤ nfft`）、
  窗类型（`hann` / `hamming` / `blackman` / `rect`，支持自定义数组）、
  居中补边 `center`（`reflect` / `edge` / `constant`）。
- **可重建性诊断**：累积平方窗权重，逐样本检查覆盖；不满足条件时给出
  `NOT RECONSTRUCTIBLE` 结论、零权重样本的具体位置和修复建议；CLI 返回码 `2`。
- **整段（bulk）与分块（streaming）两种计算路径共用同一确定性成帧网格**：
  任意块大小（含逐样本、1 样本尾块）产出的帧谱与整段**逐位相同**，重建信号也逐位相同。
- 有界内存流式分析 / 合成器（内部缓存约 `nfft + hop`）。
- 合成信号（多正弦、线性 chirp、高斯噪声、固定随机种子）与原始 PCM 读写
  （`float64/float32/int32/int16/int8/uint8`，缩放与削波明确）。

## 目录结构

```
src/streaming_stft/
  windows.py     窗函数（周期性 / 对称性两种）
  stft.py        成帧网格 frame_plan/build_grid、stft、窗权重诊断、istft(WOLA)
  streaming.py   STFTStreamer / ISTFTStreamer（有界内存、任意块大小）
  signals.py     确定性合成信号
  pcmio.py       无头 PCM / .npy 读写
  service.py     离线服务：整段 vs 分块对照、误差与诊断、写出数值文件
  cli.py         命令行：run / diagnose
examples/        JSON 请求样例与 PCM 生成脚本
tests/           pytest 自动化测试（unit / integration 标记）
```

## 安装

无需联网，依赖只有 NumPy（开发/测试另需 pytest、pytest-cov）：

```bash
# 直接以源码方式运行
export PYTHONPATH=src

# 或可编辑安装
pip install -e .
```

需要 Python ≥ 3.10。

## 数学约定（均为显式定义）

1. 帧起点在网格上位于 `n = 0, hop, 2·hop, …`；每帧先乘分析窗再做 `rfft`，
   频谱形状为 `(n_frames, nfft//2 + 1)`。
2. `center=True`（默认）时先在信号两侧各补 `nfft//2` 个样本
   （`reflect` 要求信号长度 > `nfft//2`；`edge` / `constant` 无此限制）。
3. 之后在右侧**零填充**，直到最后一帧窗能够到达信号末端。帧数取两者较大值：
   - 居中补齐后网格内容纳的帧数 `F₁ = ⌊(L−N)/H⌋ + 1`；
   - 覆盖信号末端所需帧数 `F₂ = ⌈(nfft//2 + n_samples − N)/H⌉ + 1`；
   - `F = max(F₁, F₂)`，总网格长 `(F−1)·H + N`。
4. 逆变换用**同一扇窗**做 WOLA 并除以累积平方窗权重；权重近零（≤ 1e-12）的样本
   不做除法（输出 0），同时在 `zero_weight` 中报告其索引。
5. 内部 COLA 恒定性（`Σₘ w²[n−mH]` 是否为常数）只在距两端各一个窗长的
   稳态内部区间判定；全信号覆盖性仍逐样本判定。

> 注意：周期性 Hann 窗在 `hop = nfft`（无重叠）时边缘样本严格零权重，
> 即使居中也不可重建——这是数学事实，服务会明确诊断而不是静默置零。
> 周期性 Hann 在 75% 重叠（`hop = nfft/4`）时内部平方窗权重恒定为 1.5。

## 命令行用法

### 1) 处理一个 JSON 请求（合成信号）

```bash
PYTHONPATH=src python -m streaming_stft.cli run --request examples/request_synth.json
```

输出：stdout 打印指标 JSON，并在 `output.dir` 写出：

| 文件 | 内容 |
|---|---|
| `spectrogram.npy` | 复数频谱，`np.load` 直接读取，shape `(F, N//2+1)` |
| `reconstructed.npy` | WOLA 重建信号（float64） |
| `reconstructed.pcm` | 按 `output.dtype` 量化的重建 PCM |
| `input.pcm` | 量化后的输入 PCM |
| `metrics.json` | 误差、帧计数、权重与零权重诊断、整段/分块对照 |

### 2) 处理本地 PCM

先生成样例 PCM（或使用自己的无头单声道文件）：

```bash
PYTHONPATH=src python examples/generate_sample_pcm.py
PYTHONPATH=src python -m streaming_stft.cli run --request examples/request_pcm.json
```

### 3) 不依赖输入信号，单独诊断窗/步长配置

```bash
PYTHONPATH=src python -m streaming_stft.cli diagnose --nfft 256 --hop 128 --window hann      # 退出码 0
PYTHONPATH=src python -m streaming_stft.cli diagnose --nfft 64  --hop 64  --window hann --no-center  # 退出码 2
```

退出码约定：`0` 可重建；`2` 存在零权重点（不可重建）；`1` 参数/文件错误。

### 请求 JSON 结构

```json
{
  "stft": {"nfft": 256, "hop": 128, "window": "hann",
           "center": true, "pad_mode": "reflect", "periodic_window": true},
  "input": {
    "kind": "synthetic",
    "signal": {"duration_s": 0.25, "sample_rate": 8000,
               "frequencies": [440.0, 880.0], "amplitudes": [0.6, 0.3],
               "noise_std": 0.01, "chirp_to": null, "seed": 42}
  },
  "streaming": {"chunk_sizes": [137, 363, 1, 499]},
  "output": {"dir": "out/synth", "dtype": "float64"}
}
```

文件输入把 `input` 换成：

```json
{"kind": "file", "path": "examples/sample_f32.pcm", "dtype": "float32"}
```

`chunk_sizes` 是循环使用的分块调度（按块序号取模），用于覆盖任意非均匀块与尾块。

## 作为库使用

```python
import numpy as np
from streaming_stft import STFTConfig, stft, istft, STFTStreamer, ISTFTStreamer

cfg = STFTConfig(nfft=256, hop=128, window="hann", pad_mode="reflect")
x = np.random.default_rng(0).standard_normal(2000)

# 整段
y = stft(x, cfg)
x_hat = istft(y)
print(x_hat.covered, np.max(np.abs(x_hat.signal - x)))   # True  ~1e-15

# 流式（任意块大小）
an, sy = STFTStreamer(cfg), ISTFTStreamer(cfg)
parts = []
for i in range(0, len(x), 137):
    frames = an.push(x[i:i + 137])
    if frames.shape[0]:
        parts.append(sy.push_frames(frames))
tail = an.flush()
if tail.shape[0]:
    parts.append(sy.push_frames(tail))
parts.append(sy.flush(len(x)))
x_stream = np.concatenate(parts)
assert np.array_equal(x_stream, x_hat.signal)             # 逐位相等
```

## 测试

```bash
python -m pytest                                     # 全部测试
python -m pytest -m unit                             # 仅单元测试
python -m pytest --cov=streaming_stft --cov-report=term-missing
```

测试覆盖：窗函数、成帧网格与帧数公式、整段重建（多种窗/步长/补边）、
短输入（0、1、127、128、129、…）、尾块、逐样本流式、
**零窗权重位置**（无重叠 Hann、`center=False` 等）、PCM 读写量化与削波、
端到端服务与 CLI（含不可重建退出码）。

## 设计边界

- 仅单通道、一维实信号；不做重采样、不做多声道。
- 离线处理：`service.py` 一次性读取输入并对照两种路径；`streaming.py`
  提供有界内存的在线原语供真实分块数据源使用。
- 输出不做反量化美化：PCM 写回按 dtype 缩放并削波，重建误差以 float64 内部结果为准。
