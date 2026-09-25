# 双音频率识别（DTMF 风格）离线后端

纯后端 Python + NumPy 项目：基于 Goertzel 算法的双音频率检测库与双音编码识别服务。
输入为**合成信号或本地 PCM/WAV 文件**，输出仅为数值（JSON）与文件，不包含任何播放器或界面。

> **适用范围声明**：本项目仅面向合成音频场景（程序生成的双音信号 + 加性高斯白噪声），
> 未在真实电话线路上验证，**不宣称与真实线路全兼容**。

## 功能

- **Goertzel 频率检测库**（`dtmf/goertzel.py`）：标量递归形式 + 向量化批量形式，
  数学上等价于任意频率点的单点 DFT；
- **双音检测器**（`dtmf/detector.py`）：分帧分析（40ms 帧 / 20ms 帧移）+ 多重门限判决：
  - 能量门限（噪声门限，`energy_threshold`）；
  - 行/列组内主次功率比（`group_ratio_db`）；
  - twist 检查，即高低音**幅度比**上限（`twist_db`）；
  - 双音功率 / 残余功率信噪比门限（`min_tone_snr`）；
  - **持续时长**门限（`min_tone_ms`），过短音段拒识；
  - 频偏搜索（`freq_search_pct`）：在标称频率 ±2% 内取最大功率，容忍合成信号频偏；
- **合成信号发生器**（`dtmf/synth.py`）：按键序列、音长/间隔、幅度、twist、
  频率偏移、目标 SNR 噪声注入，全部参数化且可设随机种子复现；
- **本地 PCM/WAV 读写**（`dtmf/pcmio.py`）：16bit 单声道；
- **stdio JSON 服务**（`dtmf/service.py`）与 CLI（`dtmf/cli.py`）。

键位为标准 4×4：行频 697/770/852/941 Hz，列频 1209/1336/1477/1633 Hz，
按键 `0-9 * # A-D`。

## 安装与运行

```bash
pip install -r requirements.txt   # 仅 numpy（测试需 pytest）
```

### CLI

```bash
# 合成：生成 "159#" 双音序列，SNR=20dB
python -m dtmf.cli synth --keys "159#" --out sample.pcm --snr-db 20 --seed 5
# -> {"ok": true, "out_file": "sample.pcm", "num_samples": 3200, "sample_rate": 8000}

# 解码：输出 JSON 数值结果
python -m dtmf.cli decode --file sample.pcm --sample-rate 8000
# -> {"digits": "159#", "events": [{"key":"1","start_ms":20.0,"end_ms":80.0,...}, ...],
#     "rejected_frames": 11, "total_frames": 19, "short_segments": 0}
```

### stdio JSON 服务

每行一个 JSON 请求，每行一个 JSON 响应；单个请求出错不影响后续请求。

```bash
python -m dtmf.service < examples/requests.jsonl
```

请求样例（见 `examples/requests.jsonl`）：

```json
{"id": 1, "action": "decode_synth", "keys": "159#", "synth": {"snr_db": 20, "seed": 11}}
{"id": 2, "action": "synthesize", "keys": "0755", "out_file": "out.pcm", "config": {"snr_db": 25, "seed": 3}}
{"id": 3, "action": "decode", "pcm_file": "out.pcm", "sample_rate": 8000}
```

三个动作：`synthesize`（合成并写文件）、`decode`（解码本地 PCM/WAV）、
`decode_synth`（合成后立即解码，便于自测，返回 `matched` 字段）。

## 自动化测试

```bash
python -m pytest tests/ -q
```

覆盖：Goertzel 正确性（递归与向量化一致性、频点选择性、幅度还原）、
合成器时序与噪声注入、检测器（干净序列、逐键、静音/纯噪声/过短音/过大 twist/
低幅度拒识、零间隔相邻切换、含噪解码）、PCM/WAV 往返、服务协议。

## 验收

```bash
python scripts/run_acceptance.py
```

扫描频偏、信噪比与相邻音切换，输出混淆矩阵（`results/confusion_matrix.csv`）
与摘要（`results/acceptance_summary.md`）。

### 实测结果（2026-09-24，本机 Python 3.12.3 + NumPy 2.5.3）

检测器配置：`frame_ms=40, hop_ms=20, min_tone_ms=40, energy_threshold=500,
group_ratio_db=6, twist_db=8, min_tone_snr=1.5, freq_search_pct=2.0`

**频偏扫描（SNR=30dB，每点 16 键）**

| 频偏 | 准确率 | 拒识率 |
|---|---|---|
| +0.0% ~ +3.0%（每 0.5% 一档） | 100.0% | 0.0% |

注：引入 ±2% 频偏搜索前，+1.5% 即全部拒识（scalloping 损失被计入残余功率，
触发信噪比门限）；加入频偏搜索后该悬崖消除。

**SNR 扫描（无频偏，每点 16 键）**

| SNR | 准确率 | 拒识率 |
|---|---|---|
| 40 ~ 5 dB | 100.0% | 0.0% |
| 0 dB | 81.2% | 18.8% |

**相邻音切换（8 键随机序列 ×10，音长 60ms，SNR=30dB）**

| 音间间隔 | 序列正确率 |
|---|---|
| 0ms（不含相邻重复键） | 10/10 |
| 10ms / 20ms / 40ms | 10/10 |

**混淆矩阵（SNR=40/20/10dB × 16 键 × 3 次 = 144 次试验）**

正确 144（100.0%），拒识 0，误识 0；对角线外全零，明细见
`results/confusion_matrix.csv`。

### 已知限制与未通过项（如实记录）

1. **相邻相同键 + 零间隔不可分**：`'00'` 零间隔合成信号与单个 120ms 长音在波形上
   等价（仅边界处有相位重置），解码合并为单键 `'0'`。这是原理性限制，
   真实 DTMF 同样要求音间静音间隔；验收中 0ms 组因此使用不含相邻重复键的序列。
2. **SNR=0dB 时性能下降**：准确率降至 81.2%，拒识率 18.8%（未出现误识，
   失败形式为拒识而非错判）。
3. **未覆盖真实线路因素**：未建模线路滤波、回声、非线性失真、语音串扰
   （talk-off）等，不宣称真实电话线路兼容性。
4. 开发过程中曾有 2 个测试断言与物理行为不符（非整数周期泄漏的幅度估计偏差、
   过短音在帧级即被拒识而非进入音段统计），已修正测试以匹配实际行为。

## 目录结构

```
dtmf/
  goertzel.py   # Goertzel 频率检测库（递归 + 向量化）
  tones.py      # 频率表与键位映射
  detector.py   # 分帧检测、门限判决、时序合并
  synth.py      # 合成信号发生器
  pcmio.py      # PCM/WAV 读写
  service.py    # stdio JSON 服务
  cli.py        # 命令行入口
scripts/run_acceptance.py   # 验收脚本
tests/                      # pytest 测试（31 项）
examples/requests.jsonl     # 服务请求样例
results/                    # 验收输出（混淆矩阵 CSV + 摘要）
```
