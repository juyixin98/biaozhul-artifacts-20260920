# rational-resampler — 有理比重采样离线信号处理服务

纯后端、纯 Python + NumPy 的离线重采样服务。输入合成信号或本地 PCM/WAV 数据，
输出只有**数值（JSON 报告）和文件（PCM/WAV）**，不含任何播放器或界面。

## 功能

- 有理比重采样 `L/M`（上采样 L、下采样 M），`fs_out = fs_in * L / M`
- 抗混叠/抗镜像 FIR：Kaiser 窗低通，阻带衰减可配（默认 80 dB），直流增益归一化为 L
- 多相实现：按输出相位 `(m*M + D) % L` 预分解滤波器分支，每输出一次点积
- 分块流式处理：与整段处理**逐样本一致**（同一状态机驱动，测试按 bitwise 断言）
- 输入：合成信号（sine / impulse / multitone / noise）或本地文件（s16le / s32le / f32le / wav）
- 输出：PCM/WAV 文件 + JSON 数值报告

## 定义与约定

**核心公式**（零相位对齐）：

```
y[m] = Σ_i x[i] · h[m·M + D − i·L],   m = 0 .. n_out−1
```

- `h`：奇数长 N 的对称抗混叠 FIR，支撑区间 `[0, N−1]`
- **群延迟** `D = (N−1)/2`（上采样域采样点）＝ `(N−1)/(2L)` 输入样本 ＝ `(N−1)/(2M)` 输出样本。
  延迟项 `D` 已并入索引：输出 `y[m]` 对齐输入时刻 `t = m·M/L`，**群延迟由实现补偿**，
  输出与输入同一时间轴（非因果对称窗，属离线处理的标准做法）
- **输出长度**：`n_out = ceil(n_in · L / M)`
- **边界填充**：输入域填充，长度默认取群延迟对应的输入样本数，并向上取整为
  `M/gcd(L,M)` 的整数倍，使输出域裁剪量 `pad·L/M` 为精确整数。模式：
  - `edge`（默认）：端点值延拓
  - `reflect`：镜像反射
  - `zero`：补零
  - `none`：不填充（此时输出严格等于定义式在零延拓下的值，用于对照验证）

填充只影响边界约 `D` 个样本的暂态区；稳态区与填充方式无关。

## 安装与测试

```bash
pip install -r requirements.txt   # 仅 numpy + pytest
python -m pytest tests/ -q
```

## 使用

### 1. JSON 请求文件（请求样例见 `examples/`）

```bash
python examples/make_example_input.py                       # 生成示例 PCM 输入
python -m rational_resampler.cli --job examples/request_sine_3_2.json
python -m rational_resampler.cli --job examples/request_alias_1_2.json
python -m rational_resampler.cli --job examples/request_pcm_2_3.json
```

请求字段：`up` / `down`（必填）、`signal` 或 `input`（二选一）、`fs_in`、
`pad_mode`、`attenuation_db`、`block_size`、`output` / `output_format`、
`report`。完整说明见 `rational_resampler/service.py` 模块docstring。

### 2. 命令行直接参数

```bash
python -m rational_resampler.cli --signal sine --freq 1000 --duration 0.1 \
    --fs-in 48000 --up 3 --down 2 --output out.wav --output-format wav
```

### 3. Python API

```python
from rational_resampler import resample, StreamingResampler

y = resample(x, up=3, down=2)                    # 整段
r = StreamingResampler(3, 2, pad_mode="edge")    # 分块
for block in blocks:
    out = r.process(block)
tail = r.finish()
```

## 验收（实际运行记录）

以下为提交前在本机（Python 3.12.3 / NumPy 2.5.3 / pytest 9.1.1，Linux）的
真实运行结果，非预期值。

### 自动化测试

```
$ python -m pytest tests/ -q
...............................                                          [100%]
31 passed in 0.61s
```

未通过项：无。

### 1. 脉冲验证（长度 / 波形 / 峰位置）

`tests/test_acceptance.py::test_acceptance_impulse`：512 样本、脉冲位于 256，
3/2 重采样：

- 输出长度 = 768 = `ceil(512·3/2)` ✓
- 输出波形与定义式 `y[m] = h[m·M + D − i₀·L]` 逐样本一致（容差 1e-15）✓
- 峰值位于 `256·3/2 = 384` ±1 ✓

### 2. 通带正弦验证（通带误差）

1 kHz 正弦 @ 48 kHz，3/2 → 72 kHz（`examples/request_sine_3_2.json` 实际输出）：

```json
"sine_fit": {
  "amplitude": 0.9999685374064223,
  "residual_rms": 3.5276938373591484e-06,
  "residual_max": 7.048611059601306e-06
}
```

稳态通带误差 RMS ≈ 3.5e-6（约 −109 dB），幅度保持 0.99997 ✓（阈值 1e-3）

### 3. 混叠抑制验证（高于新奈奎斯特的信号）

20 kHz 正弦 @ 48 kHz，1/2 → 24 kHz（新奈奎斯特 12 kHz，
`examples/request_alias_1_2.json` 实际输出）：

```json
"alias_suppression_db": 108.95444291321385
```

混叠抑制 ≈ 109 dB ✓（阈值 60 dB；设计阻带衰减 80 dB，该频率已深入阻带）

### 4. 分块与整段一致性

- `tests/test_core.py::test_block_equals_whole`：块大小 1/2/3/7/100/500/997
  × 填充模式 zero/edge/reflect × 比例 3/2、2/3，全部 `assert_array_equal` 通过
- 服务级复核（同一请求 `block_size` 0 vs 7，输出文件逐字节一致）：

```
whole n: 7200 blocked n: 7200 identical: True
```

## 项目结构

```
rational_resampler/
  filter_design.py   # Kaiser 窗抗混叠 FIR 设计
  core.py            # 多相状态机 + 边界填充/裁剪的分块重采样器
  signals.py         # 合成信号（impulse/sine/multitone/noise）
  pcm_io.py          # s16le/s32le/f32le/wav 读写
  metrics.py         # 正弦拟合误差、抑制量等验收指标
  service.py         # 请求字典 → 数值报告 + 文件
  cli.py             # 命令行入口
tests/               # 31 项自动化测试（含验收三项）
examples/            # 请求样例 JSON、示例输入生成脚本、实际输出
```

## 限制

- 单声道、float64 内部处理；WAV 仅支持 16-bit PCM
- 偶数长自定义滤波器存在半样本对齐残差（设计器只产生奇数长）
- 大信号为逐输出点积实现，吞吐约每秒数百万输出样本量级；离线场景够用，
  未做 SIMD/分块卷积优化
