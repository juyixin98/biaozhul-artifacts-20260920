# spectral_peak — 频谱峰插值（纯后端）

离线信号处理服务：对合成信号或本地 PCM 数据做**加窗 FFT 峰检测 + 亚频点（sub-bin）插值**，
输出各峰的频率、幅度及邻峰干扰标志。只产出数值与文件（JSON/CSV），不含播放器或任何界面。

## 适用条件（重要）

- 插值估计假设**每个窗函数主瓣内只有一个主导峰**（单峰条件）。
  Hann 窗主瓣宽度为 4 个频点（bin）：两个频率间隔 < 4 bin 的音调会互相偏置估计，
  此时输出 `interference: true`（前提是两个峰仍能被分辨为局部极大值）。
- 间隔约 1.5–2 bin 的两个音调会**合并成一个峰**，无法被分别估计，也无法触发干扰标志
  ——这是采样信息本身的极限，本工具不承诺超出采样信息的精度。
- DC（0 Hz）与奈奎斯特（sr/2）频点只有单侧邻点，**不做亚频点插值**，
  按 bin 中心频率输出并置 `boundary: true`，其频率分辨率退化为 ±0.5 bin。
- 靠近 DC / 奈奎斯特（约 2 bin 以内）的峰受负频率泄漏影响，误差明显增大
  （实测 1.3 bin 处约 0.13 bin），属预期行为。
- 奈奎斯特频率的零相位正弦 `sin(2π·sr/2·t)` 在采样点上恒为零，物理上不可观测；
  验收中使用 π/2 相位（余弦）覆盖该边界。

## 环境

- Python 3.12，NumPy 1.26，pytest 7.4（本机通过 `apt install python3-numpy python3-pytest` 安装）
- 无其他依赖

## 用法

### 命令行

```bash
# 合成信号
python3 -m spectral_peak.cli synth --freq 1000.5 --amp 0.8 --sr 48000 --n 4096

# 本地 PCM 文件（裸数据，无文件头）
python3 -m spectral_peak.cli pcm --file examples/demo_tone.pcm --fmt s16le --sr 48000

# 结果写入文件
python3 -m spectral_peak.cli synth --freq 1000.5 --sr 48000 --out result.json
```

支持的 PCM 格式：`s16le` / `s24le`（32 位容器左对齐）/ `s32le` / `f32le` / `f64le`；
多声道用 `--channels` / `--channel` 选通道。请求样例见
`examples/request_synth.json`、`examples/request_pcm.json`。

### Python API

```python
from spectral_peak import analyze, tone

x = tone(1000.5, amplitude=0.8, sample_rate=48000, n=4096)
result = analyze(x, sample_rate=48000)  # window="hann", estimator="auto"
```

### 输出格式

```json
{
  "sample_rate": 48000.0, "n_samples": 4096, "bin_hz": 11.71875,
  "window": "hann", "estimator": "hann_ratio",
  "peaks": [
    {"bin": 85, "freq_hz": 1000.4999999876433, "amplitude": 0.7999999744268189,
     "delta_bins": 0.3759999989455637, "estimator": "hann_ratio",
     "boundary": false, "interference": false, "clamped": false}
  ]
}
```

- `freq_hz`：插值后的频率；`amplitude`：正弦峰值幅度（已做窗相干增益校正，非谱密度）。
- `delta_bins`：亚频点偏移量，限制在 ±0.5 bin；被截断时 `clamped: true`。
- `interference`：在窗主瓣宽度内检测到另一个峰时为 `true`。
- `boundary`：DC / 奈奎斯特峰，未插值，`estimator` 为 `"none"`。

## 算法

1. 加窗（默认周期 Hann，可选 `rect`、`blackmanharris`）后做 `rfft`。
2. 幅度谱上找局部极大值，按 `min_peak_ratio`（相对最强峰）过滤边带/噪声。
3. 亚频点插值（`estimator="auto"` 按窗选择）：
   - `hann_ratio`（Hann 默认）：δ = 2(|X[k+1]|−|X[k−1]|) / (|X[k−1]|+2|X[k]|+|X[k+1]|)，
     无噪声单音下实测误差 ~1e-9 bin；
   - `jacobsen`：复数三点估计器，对矩形窗近精确，对 Hann 系统性低估 |δ|（约一半），仅作对比保留；
   - `log_parabolic`：对数幅度抛物线插值，通用备选。
4. 幅度在插值频率 (k+δ) 处直接计算加窗信号 DTFT 并除以 Σw 校正，避免抛物线拟合的幅度偏差。

## 验收实测记录

以下均为实际运行结果（2026-09-24，本机 Python 3.12.3 / NumPy 1.26.4）。

### 单元测试

```
$ python3 -m pytest tests/ -q
.................................                                        [100%]
33 passed in 0.14s
```

未通过项：无。（开发过程中曾有 11 项失败：Jacobsen 估计器对 Hann 窗存在系统性偏差、
Nyquist 零相位正弦采样恒为零、numpy 布尔值 JSON 序列化问题，均已修复并保留对应回归测试。）

### 验收扫描

```
$ python3 scripts/acceptance_sweep.py
```

配置：sr=48000 Hz，N=4096（bin = 11.71875 Hz），Hann 窗，hann_ratio 估计器。
完整数据在 `results/acceptance_*.csv` 与 `results/acceptance_summary.json`。

**单音扫描**（200 个非整频点，2.5 bin 至 Nyquist−2.5 bin，幅度 0.8，随机相位）：

| 指标 | max | RMS | 均值 |
|---|---|---|---|
| 频率误差 (bin) | 4.70e-05 | 3.32e-06 | -2.42e-07 |
| 频率误差 (Hz) | 5.51e-04 | 3.90e-05 | -2.83e-06 |
| 幅度相对误差 | 1.59e-04 | 1.12e-05 | -7.54e-07 |

干扰标志误报：0 / 200。

**双近邻音调**（f1 = 100.3 bin，等幅 0.8，间隔扫描）：

| 间隔 (bin) | 检出峰数 | f1 误差 (bin) | f2 误差 (bin) | 干扰标志 |
|---|---|---|---|---|
| 1.0 | 2 | 0.399 | 0.542 | True |
| 1.5 | 1（合并） | 1.324 | — | False |
| 2.0 | 1（合并） | 1.539 | — | False |
| 2.5 | 2 | 8.5e-04 | 2.9e-03 | True |
| 3.0 | 2 | 1.4e-02 | 5.8e-02 | True |
| 3.5 | 2 | 3.3e-05 | 9.9e-05 | False |
| ≥ 4.0 | 2 | ≤ 3.9e-03 | ≤ 9.4e-03 | False |

结论：间隔 < 主瓣宽度（4 bin）时误差显著增大且干扰标志触发（能分辨时）；
间隔 1.5–2 bin 时两峰合并，无法分别估计（如实报告，不做超出采样信息的承诺）。

**边界**：

| 用例 | bin | 输出频率 (Hz) | 幅度 | boundary |
|---|---|---|---|---|
| 直流 0.5 | 0 | 0.0 | 0.5 | True |
| 奈奎斯特（π/2 相位） | 2048 | 24000.0 | 0.8 | True |
| 近直流 1.3 bin | 1 | 13.705（真值 15.234） | 0.785 | False |
| 近奈奎斯特 1.3 bin | 2047 | 23986.295（真值 23984.766） | 0.785 | False |

边界峰不插值、频率/幅度准确；距边界 1.3 bin 的峰因负频率泄漏频率误差约 0.13 bin，属已知限制。

## 已知限制

- 未加噪声模型：估计方差随 SNR 下降而增大，未做 Cramér–Rao 界标定。
- 合并峰（间隔 ≲ 2 bin）不可分辨，不报错也不虚构第二个峰。
- 不支持 packed 3 字节 s24le PCM。
- 单帧分析，无跨帧相位/频率跟踪。

## 目录结构

```
spectral_peak/      # 库：windows / interpolate / detector / analyzer / signalgen / pcm / cli
scripts/acceptance_sweep.py   # 验收扫描（单音、双音、边界）
tests/              # 33 项 pytest 测试
examples/           # 请求样例 JSON 与演示 PCM
results/            # 验收扫描输出（CSV + JSON 摘要）
```
