# 延迟估计相关器（delay-correlator）

基于**归一化互相关（Normalized Cross-Correlation, NCC）**的双通道延迟估计离线服务。
输入为合成信号或本地 PCM/WAV 文件，输出只有**数值结果与数据文件**（JSON / NPZ）。

> 纯后端项目：没有播放器、没有图形界面、没有网络服务，不依赖 NumPy 以外的第三方库。

## 1. 它解决什么问题

给定两路采集同一声源的信号 `ref` 与 `chan`，估计 `chan` 相对 `ref` 的时间延迟：

- 麦克风阵列 / 声学定位中的到达时间差（TDOA）；
- 双轨录音对齐、音频/工业振动信号的通道时差测量。

核心方法是逐 lag 计算 NCC，取最大峰位置作为延迟；支持最大延迟范围约束、分块窗口估计，
并对**低能量（静音）**与**多峰（周期信号二义性）**等不可靠情况输出 `uncertain` 状态。

## 2. 延迟符号约定（务必先读）

整个项目（代码、输出 JSON、测试）统一使用以下约定：

| 输出值 | 含义 |
|---|---|
| `delay_samples > 0` | `chan` 相对 `ref` **右移**，即 **chan 滞后**（特征在 chan 中更晚出现），ref 提前 |
| `delay_samples < 0` | `chan` 相对 `ref` **左移**，即 **chan 超前** |
| `delay_samples = 0` | 两通道同步 |

实现上，相关器在 lag `d` 处比较 `ref[k - d]` 与 `chan[k]`，因此当
`chan[k] = ref[k - D]`（chan 是 ref 右移 D 个样本）时，NCC 在 `d = D` 处取峰，
峰的符号与物理移位方向一致，无需取反。该约定由 `tests/test_correlator.py` 中的
`test_sign_convention_direct_shift_check` 与正/负延迟样例双向锁定。

## 3. 算法

### 3.1 归一化互相关

对每个候选 lag `d ∈ [-max_lag, max_lag]`，取等长重叠切片：

- `d ≥ 0`：`a = ref[0 : N-d]`，`b = chan[d : N]`
- `d < 0`：`a = ref[-d : N]`，`b = chan[0 : N+d]`

默认对两个切片分别去均值（Pearson 形式）：

```
NCC(d) = Σ (a−mean(a))(b−mean(b)) / sqrt( Σ(a−mean(a))² · Σ(b−mean(b))² )
```

- 取值范围 `[-1, 1]`：+1 完全同向对齐，-1 反相对齐，0 不相关；
- 对通道间的**直流偏置**与**固定幅度增益**不敏感（有测试验证）；
- 某一切片能量为 0（静音/常数）时相关无定义，该 lag 记 0.0，随后由能量门限判不确定。

直接切片实现，单窗复杂度约 `O(窗口长度 × max_lag)`；长信号用分块窗口控制单窗规模。
为便于审计，`tests/test_correlator.py` 中保留了朴素双重循环参考实现，两者逐元素对照。

### 3.2 峰选择与亚采样

- 按 `|NCC|` 取全局最大峰（保留峰的符号到 `peak` 字段，反相峰为负）；
- 默认输出**整数采样延迟**；`estimator.interpolate=true` 时在峰顶做三点抛物线插值，
  输出亚采样估计（对带限非周期信号有效，如 chirp）。

### 3.3 置信度（`confident` / `uncertain`）

每个窗口给出状态。以下任一条件成立即判 `uncertain`，并附上原因码：

| 原因码 | 判定 | 物理含义 |
|---|---|---|
| `low_energy` | 任一通道 RMS < `10^(min_rms_db/20)`（默认 -60 dBFS） | 静音/近静音，相关无意义 |
| `low_peak` | 最大 `|NCC|` 峰 < `min_peak`（默认 0.5） | 两通道不相关（独立噪声/错配） |
| `multiple_peaks` | 次强候选峰 / 主峰 > `max_secondary_peak_ratio`（默认 0.85） | 多峰二义性，周期信号（正弦）整周期重相关时必然触发 |
| `edge_hit` | 峰贴在 `|lag| == max_lag` 边界 | 真实延迟可能超出搜索范围 |

次峰在与主峰索引距离 ≥ `min_peak_distance`（默认 3）的位置中按 `|NCC|` 选最大，
按绝对值比较可同时识别强反相峰。`peak_ratio` 即次峰/主峰之比，是"峰置信度"的核心量化指标。

### 3.4 分块窗口

- `window_size` 缺省：整段信号作为一个窗口；
- 指定后按 `[start, start+window_size)` 切窗，`hop_size` 缺省等于窗长（不重叠）；
- 末尾不足一窗的部分**直接丢弃，不补零**，避免边缘归一化失真；
- 要求 `window_size > 2 × max_lag`，否则报错（搜索范围内没有完整重叠）。

## 4. 安装

只需 Python 3.10+ 与 NumPy：

```bash
pip install -r requirements.txt        # 仅 numpy
# 无需安装即可从仓库根目录运行（也可 pip install -e . 后全局使用）
```

## 5. 快速开始

```bash
# 1) 生成本地 PCM/WAV 样例（examples/data/）
python examples/generate_example_data.py

# 2) 合成信号：已知 +37 样本延迟、加 -20dB 噪声
python -m delay_correlator run examples/request_synthetic.json -o out/synth

# 3) 本地裸 PCM（16bit 交错双声道）
python -m delay_correlator run examples/request_pcm.json -o out/pcm

# 4) 本地 WAV，4000 样本分窗 + 亚采样插值
python -m delay_correlator run examples/request_wav_windowed.json -o out/wav

# 5) 负延迟 / 周期正弦（不确定）/ 静音（不确定）
python -m delay_correlator run examples/request_negative_delay.json -o out/neg
python -m delay_correlator run examples/request_periodic_sine.json -o out/sine
python -m delay_correlator run examples/request_silence.json      -o out/sil
```

一键复现全部样例：`bash scripts/run_examples.sh`（输出到带时间戳的 `example_runs/`）。

> 若未安装包，请在仓库根目录执行或设置 `PYTHONPATH=$(pwd)`。

## 6. 请求格式

请求是一个 JSON 对象，含 `source` 与可选 `estimator` 两段。下划线开头的键（如 `_comment`）
会被忽略，可用于在请求文件里写注释。未知参数名直接报错，防止拼写错误被静默吞掉。

### 6.1 合成信号 `source.mode = "synthetic"`

| 字段 | 默认 | 说明 |
|---|---|---|
| `kind` | `noise` | `noise`（白噪声）/`sine`（正弦）/`chirp`（线性扫频）/`mixed`（多正弦之和） |
| `duration` | 1.0 | 时长（秒） |
| `sample_rate` | 8000 | 采样率 Hz |
| `delay_samples` | 0 | **真值延迟**：chan 相对 ref 右移量，正=chan 滞后，负=超前 |
| `noise_db` | null | 两通道各自叠加独立白噪声的相对电平（dB，相对信号 RMS）；null=不加噪 |
| `gain` | 1.0 | chan 相对 ref 的幅度增益（验证 NCC 幅度不敏感性） |
| `amplitude` | 0.5 | 源信号幅度（noise 为 RMS，其余为峰值） |
| `seed` | 0 | 噪声随机种子 |
| `freq` | 440 | sine 频率 |
| `f0` / `f1` | 200/1000 | chirp 起止频率 |
| `freqs` | [440,880,1320] | mixed 的频率列表 |

### 6.2 本地文件 `source.mode = "file"`

| 字段 | 适用 | 说明 |
|---|---|---|
| `path` | 全部 | 文件路径，**相对请求 JSON 所在目录**解析 |
| `kind` | 全部 | `raw_pcm`（默认）或 `wav` |
| `sample_rate` | raw_pcm 必填 | 裸文件无头，需显式给出；WAV 从文件头读取 |
| `dtype` | raw_pcm | `s16`(默认)/`s32`/`u8`/`f32`，可加 `le`/`be` 端序后缀 |
| `channels` | raw_pcm | 交错声道数，默认按 ref/chan 索引推断 |
| `ref_channel` / `channel` | 全部 | ref 与 chan 的声道索引（0 起，立体声 0=左 1=右） |
| `max_samples` | 全部 | 可选，截断前 N 个帧 |

WAV 仅支持无压缩 8/16/32 位整数 PCM（标准库 `wave` 读取）；写出工具仅生成 16 位 WAV。

### 6.3 `estimator` 参数

| 字段 | 默认 | 说明 |
|---|---|---|
| `max_lag` | 512 | 搜索范围 `[-max_lag, max_lag]`（样本） |
| `min_peak` | 0.5 | 可信所需的最小 `|NCC|` 峰高 |
| `max_secondary_peak_ratio` | 0.85 | 次峰/主峰超过它即 `multiple_peaks` |
| `min_rms_db` | -60 | 能量门限（dBFS） |
| `min_peak_distance` | 3 | 主次峰最小间距（样本） |
| `window_size` / `hop_size` | 整段 / 等于窗长 | 分窗参数 |
| `subtract_mean` | true | Pearson 去均值；false 为纯能量归一化 |
| `interpolate` | false | 峰顶抛物线亚采样插值 |

## 7. 输出

每次运行在输出目录写三个文件（**已存在则拒绝覆盖**），并在 stdout 打印文本汇总：

- **`results.json`**：完整结果——参数、来源元数据（合成模式含真值延迟）、每窗明细、整体汇总；
- **`correlation.npz`**：`lags`（lag 轴）与 `ncc`（每窗一行的 NCC 曲线），供二次复核；
- **`signals.npz`**：实际参与计算的 `ref` / `chan` 两通道与 `sample_rate`。

单窗结果字段：

```json
{
  "window": 0, "start_sample": 0, "end_sample": 8000,
  "delay_samples": 37.0, "delay_seconds": 0.004625,
  "peak": 0.990, "second_peak": 0.031, "peak_ratio": 0.031,
  "rms": 0.502, "rms_db": -5.97, "edge_hit": false,
  "status": "confident"
}
```

不确定窗口额外含 `"reasons": ["multiple_peaks", ...]`；无法 JSON 表达的 ±inf/NaN 输出为 `null`
（如静音窗的 `rms_db`）。汇总 `summary` 给出窗口数、可信/不确定计数、可信窗口平均延迟、
窗口间散布与一致性标签（`stable`/`varying`/`none`）。

## 8. 作为 Python 库使用

```python
from delay_correlator import EstimatorConfig, estimate_lag
from delay_correlator.signals import SyntheticSpec, make_delayed_pair

ref, chan = make_delayed_pair(SyntheticSpec(kind="noise", delay_samples=37, sample_rate=8000))
est = estimate_lag(ref, chan, EstimatorConfig(max_lag=128))
print(est.delay_samples, est.confident, est.peak, est.peak_ratio)
```

## 9. 验收测试

```bash
python -m pytest          # 32 个用例，见 tests/
```

覆盖任务要求的验收点：

- **已知整数延迟**：白噪声 ±37、±12、+21 等精确检出（`test_positive/negative_integer_delay_*`）；
- **负延迟**：`test_negative_integer_delay_white_noise` 与直接移位符号双向核对
  `test_sign_convention_direct_shift_check`；
- **周期信号多峰**：200Hz 正弦判 `multiple_peaks`（`test_periodic_sine_is_flagged_multiple_peaks`）；
- **静音**：双静音与单路静音均判 `low_energy`，且无 NaN；
- **噪声鲁棒 / 不相关**：-15dB 加噪仍稳定检出；两路独立噪声判 `low_peak`；
- **符号定义**：正延迟对应右移、峰位=移位量，已在第 2 节锁定；
- **峰置信度**：`peak` / `second_peak` / `peak_ratio` 数值在全部上述场景中输出并被断言；
- 另含：亚采样插值（chirp 上 2.5 样本偏差 < 0.3）、边界 `edge_hit`、反相峰保留负号、
  Pearson 对直流/增益免疫、分块窗分段延迟、PCM(u8/s16/s32/f32)/WAV 往返、CLI 端到端。

实际运行的命令、输出与未通过项的如实记录见 **[RUNLOG.md](RUNLOG.md)**。

## 10. 目录结构

```
delay_correlator/        # 源码包
  config.py              # EstimatorConfig 参数与校验
  correlator.py          # NCC、峰选择、插值、置信度判定（核心）
  windows.py             # 分块窗口与逐窗估计
  signals.py             # 合成信号、裸 PCM / WAV 读写
  pipeline.py            # 请求解析、编排、JSON/NPZ 落盘
  cli.py / __main__.py   # 命令行入口（纯文本）
examples/                # 请求样例 + 样例数据生成脚本
scripts/run_examples.sh  # 一键复现
tests/                   # pytest 自动化测试（32 例）
RUNLOG.md                # 实际运行记录（命令/结果/未通过项）
```

## 11. 已知边界与设计取舍

- NCC 只能在给定 `max_lag` 内定位；延迟出界会表现为 `edge_hit`，应调大范围重跑。
- 周期信号的多峰是**数学本质**而非缺陷：NCC 在每个整数周期处重复取峰，此时结果
  必然 `uncertain`，需借助非周期调制、先验或多麦几何约束消歧。
- 线性（补零）移位信号在远离真实延迟的极端 lag 上会比较两段零填充，可能产生边缘假峰；
  真实录音中两路信号各自独立，不存在共享的零填充段。`edge_hit` 边界语义与分窗策略
  已在文档与测试中明确。
- 直接法 NCC 为可读性优先；超大窗口/超大 max_lag 的场景可在不改接口的前提下替换为 FFT 实现。
