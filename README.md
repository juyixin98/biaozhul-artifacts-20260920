# dtmf-service — 双音频率识别（纯后端）

离线双音多频（DTMF 式）信号识别服务。输入**合成信号**或**本地 PCM 文件**，
输出 JSON 数值结果（识别出的按键序列、每个按键的时间区间、拒识原因）。
**不包含**播放器、界面或任何前端；**仅面向合成音频场景**，不宣称对真实
电话线路全兼容。

## 原理

- **Goertzel 算法**（`dtmf_service/goertzel.py`）：对 4 个低频（697/770/852/941 Hz）
  与 4 个高频（1209/1336/1477/1633 Hz）做单频功率估计，复杂度 O(N)，
  采用广义（精确频率）形式，无 DFT 取整误差。
- **帧级判决**（`dtmf_service/detector.py`）：40 ms 帧 / 20 ms 步进，每帧检查：
  1. 噪声门限：帧 RMS ≥ `min_rms`（默认 0.01，满幅 1.0）；
  2. 主导性：组内最强频率功率 ≥ 次强 × `dominance_ratio`（默认 2，抗宽带干扰）；
  3. 能量占比：双音能量 / 帧总能量 ≥ `min_tone_energy_ratio`（默认 0.5，抗噪）；
  4. 幅度比（twist）：高/低频功率比 ∈ [-8, +4] dB。
- **序列解码**：持续时长 ≥ `min_tone_ms`（默认 40 ms）才计为一次按键；
  同一按键的两次按下需间隔 ≥ `min_gap_ms`（默认 30 ms）静音，否则合并。
  未通过任何检查的信号段记入 `rejections` 并给出原因，绝不静默丢弃。

## 安装

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## 使用

### 命令行

```bash
# 合成信号识别（指定按键序列、信噪比、频偏）
.venv/bin/python -m dtmf_service.cli --synthesize "159#" --snr 20 --offset 1.0

# 本地 PCM/WAV 文件识别
.venv/bin/python -m dtmf_service.cli --input audio.wav            # WAV 自动读采样率
.venv/bin/python -m dtmf_service.cli --input audio.pcm --rate 8000 # 裸 PCM 需指定采样率

# JSON 请求文件（样例见 examples/request_sample.json）
.venv/bin/python -m dtmf_service.cli --request examples/request_sample.json
```

输出为 JSON：`digits`（按键串）、`events`（每键的起止毫秒、双音频率、twist）、
`rejections`（拒识区间与原因）。

### Python API

```python
from dtmf_service.service import detect_samples, detect_file, handle_request

result = detect_file("audio.wav")                    # 本地 PCM 文件
result = handle_request({"synthesize": {"keys": "159#", "snr_db": 20.0}})
print(result["digits"])  # "159#"
```

### 请求样例

`examples/request_sample.json`（合成输入）与 `examples/request_pcm.json`
（PCM 文件输入）展示了完整请求格式；`detector` 字段可覆盖
`DetectorConfig` 中的任意门限。

## 测试与验收

```bash
.venv/bin/python -m pytest                 # 单元测试
.venv/bin/python -m experiments.acceptance # 验收扫描，结果写入 results/
```

验收内容（全部为合成信号）：

1. **频偏扫描**：0% ~ 4% 频率误差下的识别率；
2. **信噪比扫描**：干净信号 ~ -5 dB 加性白噪声下的识别率；
3. **相邻音切换**：不同按键背靠背、间隔 100~10 ms 的整串识别率，
   以及同一按键重按在不同间隔下的合并/区分行为；
4. **混淆矩阵**：16 键 × 4 次试验的识别/误判/拒识分布；
5. **拒识情况**：静音、纯白噪声、单音、过短突发、极端 twist。

完整结果见 `results/acceptance.md` / `results/acceptance.json`（脚本生成，
如实记录）。本次实际运行（2026-09-24，numpy 2.5.3 / pytest 9.1.1）摘要：

- 单元测试 **41 项全部通过**（`python -m pytest`，0 失败）；
- 频偏：≤1.0% 识别率 100%，1.5% 时 84.4%，≥2.0% 全部拒识（0%）——
  即本实现对频偏的容忍约为 ±1.5%，超出即拒识而非误判；
- 信噪比：干净 ~ 0 dB 识别率 100%，-5 dB 全部拒识；
- 相邻音切换：12 键连发在 100~10 ms 间隔下整串识别率均 100%；
  同键重按在间隔 ≥40 ms 时稳定区分为两次，≤10 ms 时合并为一次，
  20~30 ms 为边界区（与 `min_gap_ms=30` 设计一致）；
- 混淆矩阵：64 次试验全部命中对角线，无误判、无拒识；
- 拒识：静音、白噪声、单音、20 ms 短突发、+20 dB 极端 twist 均正确拒识，
  拒识原因分别为 silence / no_dominant_tone_pair / twist_out_of_range /
  tone_too_short / twist_out_of_range。

## 项目结构

```
dtmf_service/
  goertzel.py    # Goertzel 单频功率估计
  synth.py       # 合成信号生成（测试/验收输入）
  detector.py    # 帧级判决 + 序列解码
  pcm.py         # 16-bit PCM/WAV 读写
  service.py     # 服务入口（数组 / 文件 / 请求字典）
  cli.py         # 命令行接口
tests/           # 单元测试（pytest）
experiments/     # 验收扫描脚本
examples/        # 请求样例
results/         # 验收输出（运行后生成）
```

## 限制（如实声明）

- 仅验证**合成音频**：理想正弦双音 + 加性白高斯噪声；未在真实电话线路、
  编解码压缩（μ-law/G.729 等）、回声、丢包场景下验证，不宣称全兼容。
- 仅支持 16-bit 单声道 PCM/WAV 输入。
- 默认参数针对 8 kHz 采样率调优；其他采样率可用但门限可能需调整。
