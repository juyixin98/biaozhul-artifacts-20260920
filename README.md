# dcblocker — 音频分块去偏置（纯后端）

离线信号处理服务：对持续流音频做 DC 偏置估计与去除。输入合成信号或本地
PCM 原始数据，输出数值指标（JSON）与处理后的 PCM 文件。无播放器、无界面、
无网络服务。

## 原理：明确的一阶高通模型

差分方程（因果、只依赖历史样本，**不使用整段均值，无未来泄漏**）：

```
y[n] = x[n] - x[n-1] + R * y[n-1]
R    = exp(-2π · fc / fs)
```

- `fc`：截止频率（默认 5 Hz），`fs`：采样率
- 状态仅两个标量（`x[n-1]`、`y[n-1]`），跨块自动延续
- 递推按逐样本标量顺序计算，不引入改变浮点求和顺序的向量化，
  因此**任意分块下输出逐位一致**（分块不变性）
- 阶跃响应为 `A · Rⁿ`，1% 理论稳定时间 `t = ln(0.01) / ln(R) / fs`

## 目录结构

```
dcblocker/
  filter.py     # DCBlocker：process / reset / set_sample_rate / set_cutoff
  pcm.py        # int16/int32/float32/float64 PCM 读写（整型按满量程归一化）
  signals.py    # 合成信号：sine / dc_step / constant
  service.py    # 离线请求执行：读请求 → 分块处理 → 写文件 + 数值指标
  __main__.py   # CLI 入口
requests/       # 请求样例（JSON）
tests/          # pytest 自动化测试（26 项）
```

## 安装与运行

```bash
pip install -r requirements.txt   # 仅 numpy + pytest

# 库用法
python3 - <<'EOF'
from dcblocker import DCBlocker
b = DCBlocker(sample_rate=48000, cutoff_hz=5.0)
y1 = b.process([0.5, 0.6, 0.7])   # 任意长度块，含 1、2 的极短块
b.set_sample_rate(44100)          # 重设采样率（重算 R，默认保留状态）
b.reset()                         # 状态重置
EOF

# 服务用法：执行 JSON 请求，指标打印到 stdout，波形写入文件
python3 -m dcblocker requests/biased_sine.json
python3 -m dcblocker requests/biased_sine.json requests/bias_step.json requests/pcm_file.json
```

## 请求格式

```json
{
  "sample_rate": 48000,
  "cutoff_hz": 5.0,
  "block_size": 1024,
  "input":  {"type": "sine", "frequency": 440, "amplitude": 0.5,
             "dc_offset": 0.3, "duration_s": 2.0},
  "output_file": "out/cleaned.f32"
}
```

- `input` 二选一：
  - 合成信号：`sine`（frequency/amplitude/dc_offset/duration_s）、
    `dc_step`（initial_dc/final_dc/step_time_s/duration_s）、
    `constant`（value/duration_s）
  - 本地 PCM：`{"type": "pcm_file", "path": "in/raw.i16", "format": "int16"}`
- `output_file` 扩展名决定格式：`.i16` / `.i32` / `.f32` / `.f64`
- 路径相对当前工作目录；响应为 JSON：输入/输出统计、末尾 10% 稳态
  残差、系数 R、理论 1% 稳定时间等

## 测试与验收

```bash
python3 -m pytest tests/ -v
```

覆盖验收项：

| 验收项 | 测试 |
|---|---|
| 带偏置正弦：稳态误差 | `test_biased_sine_steady_state_error`（尾部均值 < 1e-3，幅度误差 < 1%） |
| 偏置跳变：响应时间 | `test_bias_step_response_time_matches_theory`（实测 1% 稳定点 vs 理论 ln(0.01)/ln(R)，误差 < 2%）、`test_bias_jump_midstream` |
| 极短块 | `test_extremely_short_blocks`（块长 1/2/3/5）、`test_empty_block` |
| 分块不变性 | `test_block_size_invariance`（块长 1…96000 与整段处理**逐位相等**） |
| 无未来泄漏 | `test_causality_no_future_leakage`（共享前缀输出一致）、`test_not_global_mean_subtraction` |
| 采样率重设 / 状态重置 | `test_set_sample_rate_*`、`test_reset_restores_initial_state` |

## 实际运行记录（2026-09-25，Python 3.12.3 / NumPy 2.5.3 / pytest 9.1.1）

```
$ python3 -m pytest tests/ -v
============================== 26 passed in 0.51s ==============================
```

```
$ python3 -m dcblocker requests/biased_sine.json requests/bias_step.json requests/pcm_file.json
```

| 请求 | 输入 DC | 稳态残差（末尾 10% 均值） | 理论 1% 稳定时间 |
|---|---|---|---|
| biased_sine（0.5·sin440Hz + 0.3） | 0.3 | 3.5e-17 | 0.147 s |
| bias_step（+0.2 → −0.4） | 跳变 ±0.6 | -1.6e-37 | 0.147 s |
| pcm_file（int16，220 Hz + 0.25） | 0.25 | 4.9e-16 | 0.092 s |

全部通过，无未通过项。

## 已知限制

- 单声道、逐样本标量递推：约 10⁵–10⁶ 样本/秒量级，分钟级音频可离线
  接受；若需更高吞吐可在保持逐样本语义的前提下改写为 C 扩展。
- 整型 PCM 写出做饱和量化，引入 ≤ 半个 LSB 的量化误差。
