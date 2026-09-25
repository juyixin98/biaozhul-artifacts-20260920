# dc-blocker — 音频分块去偏置（纯后端）

离线信号处理服务：对持续分块输入的音频流做 **DC 偏置估计与去除**。
输入为合成信号或本地 PCM 数据，输出仅为数值与文件 —— 无播放器、无界面。

## 模型：明确的一阶高通

不使用整段均值（那会泄漏未来样本、且无法流式分块）。采用标准一阶
DC-blocking 高通滤波器，逐样本因果递推：

```
y[n] = x[n] - x[n-1] + R · y[n-1] ,   R = exp(-2π · fc / fs)
```

* `fc`：-3 dB 截止频率（Hz），`fs`：采样率（Hz）。
* **直流增益严格为 0** → 恒定偏置的稳态误差为 0。
* 阶跃响应按 `R^n` 衰减，时间常数 `τ = -1/ln(R)` 个样本
  （`fc << fs` 时 ≈ `fs/(2π·fc)`）。
* **严格因果**：输出 `y[n]` 只依赖 `n` 及之前的输入，因此任意分块大小
  的处理结果与一次性处理**逐位一致**（分块不变性），不存在未来泄漏。

`DCBlocker` 支持：

* `process(block)` — 处理任意长度（≥0）的分块，状态跨块延续；
* `set_sample_rate(fs, preserve_state=True)` — 重设采样率，按同一截止
  频率重算极点 R（时间常数的秒数不变），默认保留状态；
* `set_cutoff(fc)` — 修改截止频率；
* `reset()` — 状态清零。

## 目录结构

```
src/dc_blocker/
  filter.py    # 一阶高通 DCBlocker（核心，仅依赖 NumPy）
  io.py        # 裸 PCM 读写（s16le/f32le/f64le）与合成信号
  cli.py       # 命令行：分块处理 PCM / 合成信号 → PCM + 指标 JSON
  service.py   # 纯 stdlib 的 JSON-over-HTTP 流式服务（会话制）
tests/         # pytest 验收测试（34 项）
examples/requests.sh  # HTTP 请求样例
```

## 安装与测试

```bash
pip install -r requirements.txt   # 仅需 numpy + pytest
python3 -m pytest                 # 运行全部验收测试
```

## 验收测试覆盖（tests/test_dc_blocker.py）

| 场景 | 验证内容 |
|---|---|
| 带偏置正弦 | 稳态误差：尾部均值 < 1e-3；正弦幅度保持（RMS ≈ A/√2，2% 内） |
| 偏置正弦 | 响应时间：1τ 处残差 ≈ bias·e⁻¹（5% 内）；1% 沉降时间后残差 < 1%·bias |
| 偏置跳变 | 跳变后重新收敛：跳变处瞬态 ≈ 跳变量，1τ 后 ≈ e⁻¹ 倍，沉降后 < 1% |
| 极短块 | 块长 1/2/3 正常工作，且与一次性处理逐位一致 |
| 分块不变性 | 块长 1/7/100/1024/4096/96001 的输出与一次性处理逐位相同 |
| 因果性 | 未来阶跃对之前的输出零影响（排除块均值式未来泄漏） |
| 采样率重设 | 时间常数（秒）不变、极点重算、Nyquist 校验 |
| 状态重置 | `reset()` 后行为等同新实例 |

## CLI 用法

```bash
# 合成带偏置正弦 → 分块去偏置 → 输出 PCM + 指标
PYTHONPATH=src python3 -m dc_blocker.cli \
    --synthetic biased-sine --sample-rate 48000 --seconds 2 \
    --freq 440 --amplitude 0.5 --bias 0.3 \
    --cutoff 5 --block-size 1024 \
    --output cleaned.s16 --metrics metrics.json --dump-input input.s16

# 处理本地 PCM 文件
PYTHONPATH=src python3 -m dc_blocker.cli \
    --input raw.s16 --encoding s16le --sample-rate 48000 \
    --cutoff 5 --block-size 1024 --output cleaned.s16 --metrics metrics.json
```

## HTTP 服务

```bash
PYTHONPATH=src python3 -c "from dc_blocker.service import serve; serve(port=8073)"
```

请求样例见 [examples/requests.sh](examples/requests.sh)。核心流程：

```
POST /sessions                      {"sample_rate": 48000, "cutoff_hz": 5}
POST /sessions/<id>/process         {"samples": [0.3, 0.3, ...]}   # 可分块连续调用
POST /sessions/<id>/reset           {}
POST /sessions/<id>/sample-rate     {"sample_rate": 16000, "preserve_state": true}
DELETE /sessions/<id>
```

每个会话持有一个独立的 `DCBlocker` 状态，跨请求流式延续。

## 实测记录（2026-09-25，本机 Python 3.12.3 / NumPy 2.5.3 / pytest 9.1.1）

**自动化测试**：`python3 -m pytest` → **34 passed in 2.43s**，无未通过项。

**CLI 实测**（合成 2 s、440 Hz、幅度 0.5、偏置 0.3 的正弦，fc=5 Hz，块 1024）：

```json
{
  "input_samples": 96000,
  "block_size": 1024,
  "pole_R": 0.9993457156679053,
  "time_constant_samples": 1527.8874536821443,
  "settling_samples_1pct": 7037,
  "input_mean": 0.30000000000000004,
  "output_tail_mean": -1.4111724205632533e-05,
  "output_tail_abs_mean": 0.31825619503149816
}
```

解读：输入均值 0.3（偏置）→ 稳态输出尾部均值 ≈ -1.4e-5（偏置去除，
残余 < 0.005%）；尾部绝对均值 0.318 ≈ 2A/π，正弦本身被保留。

**HTTP 服务实测**（恒定 0.3 输入，逐块处理）：

```
POST /sessions/<id>/process {"samples":[0.3,0.3,0.3,0.3,0.3]}
→ {"samples":[0.3, 0.2998037, 0.2996076, 0.2994115, 0.2992156], "count":5}
POST /sessions/<id>/sample-rate {"sample_rate":16000}
→ {"sample_rate":16000.0, "R":0.9980384309875864}
```

输出逐样本按 R 衰减，符合一阶高通模型。

## 已知限制

* 核心递推为逐样本 Python 循环，吞吐约数百万样本/秒量级；如需更高
  吞吐可换 `scipy.signal.lfilter` 分块带状态（`zi`）实现，模型不变。
* 一阶高通在 fc 附近有相位与幅度畸变；对低频成分敏感的场景应选更
  小的 `fc`（代价是响应时间变长）。
* 服务为单进程 stdlib 实现，面向离线/本机批处理，非生产级网关。
