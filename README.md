# 延迟估计相关器（Delay Estimation Correlator）

纯后端离线信号处理服务：对**双通道信号**做归一化互相关（NCC / ZNCC），
估计通道间延迟，支持分块窗口、最大延迟范围限制，并在**低能量**或**多峰**
等情况下输出“不确定”状态。只输出数值与文件，**不含播放器或任何界面**。

- 语言/依赖：Python 3.10+，仅依赖 NumPy（测试用 pytest）
- 输入：合成信号参数，或本地原始 PCM / 整数 PCM WAV 文件
- 输出：JSON 数值结果（stdout/文件），可选各窗口相关剖面（`.npz`）与
  逐窗口指标表（`.csv`）

---

## 1. 符号约定（务必先读）

**延迟 `lag` 定义为通道 B 相对通道 A 的延迟**：

```
B[n] = A[n − lag]
```

- `lag > 0`：**B 晚于 A**（A 先到达），相关峰在正滞后；
- `lag < 0`：**B 早于 A**，峰在负滞后。

内部互相关按 `r(k) = Σ a[n]·b[n+k]` 计算，因此 `B[n] = A[n−d]` 时峰在
`k = +d`。测试 `tests/test_correlator.py::test_sign_convention_*` 与
`tests/test_estimate.py::test_negative_integer_delays` 专门锁定该符号，
防止被取反。

## 2. 方法说明

1. **全滞后互相关**：FFT 计算线性（非循环）互相关，补零到 ≥ `2N−1`。
   实现见 `delay_correlator/correlator.py`，并有直接法逐项对拍测试。
2. **按重叠长度归一化**：每个滞后只统计实际重叠区间，分母为两通道重叠
   能量的几何平均，相关系数 ∈ `[−1, 1]`。
   - `ncc`：能量归一化；
   - `zncc`（默认）：每个重叠区间先减局部均值，抗直流偏移。
3. **最大延迟范围**：只搜索 `[−max_lag, +max_lag]`；峰贴边界时标记
   `boundary_peak`（真值可能在范围外）。
4. **分块窗口**：`window_ms/window_size_samples` 与 `hop_ms/hop_samples`
   控制分块；末尾不足整窗时按 `min_partial`（默认 0.5）比例决定取舍。
5. **峰置信度**：
   - `peak_coherence`：最强峰相关系数（主指标）；
   - `second_coherence` / `peak_ratio` / `prominence`：非极大抑制
     （半径 `ambiguity_guard`）后的次峰强度——多峰越接近，越不可信；
   - `fractional_lag`：三点抛物线亚样本插值（仅内部峰给出）。

### 状态判定

`status = "ok"`，或 `"uncertain"` + `uncertainty_reasons`：

| 原因 | 触发条件 | 默认阈值 |
|---|---|---|
| `low_energy` | 两通道合并 RMS（去均值）过低（静音） | `energy_db = −60` dBFS |
| `low_coherence` | 峰相关系数过低 | `min_coherence = 0.5` |
| `ambiguous_peaks` | 存在第二个相距 > `ambiguity_guard` 的强峰（周期信号） | 次峰/主峰 ≥ `0.9` 或峰差 ≤ `0.1` |
| `boundary_peak` | 最强峰位于搜索范围边界 | — |

多窗口聚合：所有 ok 窗口延迟中位数为最终值；窗口间跨度超过
`window_consistency_samples`（默认 1 样本）判 `inconsistent_windows`；
存在任何不确定窗口时附加 `some_windows_uncertain`；全部窗口不确定时
聚合状态为 uncertain 且不给出延迟。

## 3. 安装

```bash
python3 -m venv .venv && source .venv/bin/activate   # 可选
pip install -r requirements.txt
pip install pytest                                  # 仅跑测试需要
```

## 4. 使用

### 4.1 请求 JSON

合成信号示例（`examples/request_synthetic.json`，真值延迟 137 样本）：

```json
{
  "input": {
    "type": "synthetic",
    "signal_type": "noise",
    "n": 16000,
    "sample_rate": 16000,
    "delay": 137,
    "snr_db": 25,
    "seed": 2026,
    "save_wav": "synthetic_pair.wav"
  },
  "analysis": {
    "method": "zncc",
    "max_lag_ms": 20,
    "window_ms": 250,
    "hop_ms": 250,
    "min_coherence": 0.5,
    "ambiguity_guard_ms": 0.5,
    "energy_db": -60
  },
  "output": {
    "profile_npz": "profiles_synthetic.npz",
    "windows_csv": "windows_synthetic.csv"
  }
}
```

运行：

```bash
python -m delay_correlator run examples/request_synthetic.json \
    -o examples/result_synthetic.json
```

文件类输入（PCM/WAV）与输出路径均**相对于请求 JSON 所在目录**解析。

### 4.2 合成信号参数（`input.type = "synthetic"`）

| 字段 | 说明 | 默认 |
|---|---|---|
| `signal_type` | `noise` / `sine` / `chirp` / `uncorrelated` / `silence` | `noise` |
| `n` | 每通道样本数 | 8000 |
| `sample_rate` | 采样率 Hz | 8000 |
| `delay` | **真值整数延迟（样本，可负）**，B 相对 A | 0 |
| `freq` / `freq_end` | 正弦频率 / 扫频终止频率 | 440 / 4·freq |
| `amplitude` | 幅度 | 0.8 |
| `snr_db` | B 通道叠加噪声的信噪比（dB）；不加噪省略 | 无 |
| `seed` / `seed_b` | 随机种子（`uncorrelated` 用 `seed_b`） | 0 |
| `save_wav` / `save_wav_bits` | 可选：把生成信号写 16/8/32-bit WAV | 无 |
| `save_pcm` | 可选：`{"path": ..., "dtype": "s16"}` 写原始 PCM | 无 |

### 4.3 本地 PCM 输入（`input.type = "pcm"`）

```json
{
  "input": {
    "type": "pcm",
    "mode": "interleaved",
    "path": "stereo_pair.s16.pcm",
    "dtype": "s16",
    "sample_rate": 16000
  },
  "analysis": { "max_lag_ms": 20, "window_ms": 250 }
}
```

- `mode`：
  - `interleaved`：一个文件内 L,R,L,R… 交错（`channels=2`）；
  - `paired`：两个单声道文件 `path_a` / `path_b`（长度不等自动截短）；
  - `wav`：整数 PCM WAV（8/16/24/32-bit），采样率从文件头读取，可用
    `channel_a` / `channel_b`（默认 0 / 1）选声道；
- `dtype`：`u8` / `s16` / `s32` / `f32`（WAV 模式忽略）；
- `max_samples`：只读前若干帧（可选）。

样例 `stereo_pair.s16.pcm` 是已知延迟 +137 样本（@16 kHz）的 16-bit
交错立体声文件，可直接用 `examples/request_pcm.json` 复测。

### 4.4 analysis 字段

| 字段 | 说明 | 默认 |
|---|---|---|
| `method` | `zncc` / `ncc` | `zncc` |
| `max_lag_samples` 或 `max_lag_ms` | 最大搜索延迟 | 200 样本 |
| `window_size_samples` 或 `window_ms` | 窗口长度；省略=整段单窗口 | 整段 |
| `hop_samples` 或 `hop_ms` | 跳跃步长 | 等于窗长 |
| `min_partial` | 末尾零头保留比例 | 0.5 |
| `min_coherence` | 低相干阈值 | 0.5 |
| `ambiguity_guard_samples`/`_ms` | 多峰非极大抑制半径 | 3 样本 |
| `ambiguity_ratio` | 次峰/主峰模糊阈值 | 0.9 |
| `ambiguity_gap` | 峰间绝对差模糊阈值 | 0.1 |
| `energy_db` | 静音能量阈值 dBFS | −60 |
| `window_consistency_samples` | 跨窗口一致性容差 | 1 |

### 4.5 内置演示（无需请求文件）

```bash
python -m delay_correlator demo --case positive-delay   # +17 噪声对
python -m delay_correlator demo --case negative-delay   # -17，负号
python -m delay_correlator demo --case silence          # 静音 -> uncertain
python -m delay_correlator demo --case periodic         # 周期多峰 -> uncertain
```

### 4.6 作为库调用

```python
from delay_correlator.correlator import normalized_xcorr
from delay_correlator.estimate import estimate_window

prof = normalized_xcorr(a, b, max_lag=100, method="zncc")
est = estimate_window(a, b, max_lag=100, sample_rate=8000)
print(est.lag_samples, est.peak_coherence, est.status)
```

## 5. 输出结构

```jsonc
{
  "signal": { "source": "...", "sample_rate": 16000.0, "n_samples": 16000,
              "true_delay_samples": 137 },   // 仅合成输入带真值
  "config": { ... },
  "sign_convention": "lag > 0 表示通道 B 晚于 A（B[n] = A[n-lag]）",
  "n_windows": 4,
  "windows": [
    {
      "window_index": 0, "sample_start": 0, "sample_end": 4000,
      "lag_samples": 137, "lag_seconds": 0.0085625,
      "fractional_lag": 137.01,
      "peak_coherence": 0.999, "second_coherence": 0.21,
      "peak_ratio": 0.21, "prominence": 0.79,
      "at_boundary": false,
      "status": "ok", "uncertainty_reasons": []
    }
  ],
  "aggregate": {
    "status": "ok", "uncertainty_reasons": [],
    "n_ok_windows": 4,
    "lag_samples": 137,                 // ok 窗口中位数（四舍五入）
    "lag_median_samples": 137.0, "lag_mean_samples": 137.0,
    "lag_std_samples": 0.0, "lag_spread_samples": 0.0,
    "lag_seconds": 0.0085625,
    "mean_coherence": 0.998, "min_coherence_observed": 0.997
  }
}
```

- `profile_npz`：对象数组 `lags[i]`、`corr[i]`（加载需
  `np.load(..., allow_pickle=True)`）；
- `windows_csv`：逐窗口一行，便于外部工具核对。

## 6. 测试

```bash
python -m pytest
```

76 个用例覆盖：FFT 与直接法对拍、归一化界、抗直流、**正/负/零整数延迟**、
抛物线分数延迟、**静音**、低能量、不相关噪声、含噪副本、**周期信号多峰**、
扫频无模糊、边界峰、窗口切分、PCM（u8/s16/s32/f32）与 WAV（8/16/24/32-bit）
往返、服务端到端、以及 CLI 子进程。实测结果见 `RUNLOG.md`。

## 7. 目录结构

```
delay_correlator/
  __init__.py      # 对外接口
  correlator.py    # FFT 全滞后互相关 + 逐滞后归一化（NCC/ZNCC）
  estimate.py      # 峰检测、置信度、不确定状态、抛物线插值
  window.py        # 分块窗口
  signals.py       # 合成信号与已知延迟信号对
  pcmio.py         # 原始 PCM / WAV 读写
  service.py       # 请求 JSON -> 分析 -> 结果 JSON / npz / csv
  cli.py           # 命令行：run / demo
examples/          # 请求样例、样例数据与已生成结果
tests/             # pytest 测试
RUNLOG.md          # 实际运行命令与结果记录
```

## 8. 边界与限制

- 整数延迟峰检测精度为 1 样本；亚样本值仅由抛物线插值近似，强噪声/平顶时
  可能缺失（`fractional_lag = null`）。
- 周期信号存在与真实峰等高的周期副峰，即使相干度为 1 也会判
  `ambiguous_peaks`——这是有意的保守策略；窗口内若不足一个完整周期可能
  不触发，可用扫频/噪声或更长窗口消歧。
- 不做重采样；两路采样率假定一致。
- WAV 仅支持整数 PCM；浮点 WAV 请先用 `f32` 原始 PCM 输入。
