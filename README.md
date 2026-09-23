# 有理比重采样离线服务（resample_service）

纯后端信号处理项目：Python + NumPy 实现带抗混叠 FIR 的**有理比（L/M）重采样**，
输入合成信号或本地 PCM 数据，输出数值文件与 JSON 报告。无播放器、无界面。

## 原理与实现

信号流（教科书形式）：

```
x[n] ─ 上采样 L（零插值） ─ 抗混叠低通 FIR h ─ 下采样 M ─ y[n]
```

实际计算不插零，采用**多相（polyphase）**等价实现。对每个输出样本：

```
t(n) = n·M + gd        输出 n 在上采样域的中心位置
p    = t mod L         多相相位
kmax = t // L
y[n] = L · Σ_{j=0..K-1} h[p + j·L] · x_ext[kmax − j]
```

其中 `K = ceil(num_taps / L)` 为每相抽头数，`x_ext` 为按边界策略扩展的输入，
增益 `L` 补偿零插值。每个输出样本的取值只依赖输入与系数，与分块方式无关，
因此**分块处理与整段处理结果一致**（实测最大偏差 ≤ 2e-15，见验收记录）。

### 抗混叠 FIR

- Kaiser 窗截断 sinc，直流增益归一化为 1（`resample_service/filters.py`）。
- 截止频率（上采样域，cycles/sample）：`cutoff = 0.5 / max(L, M) · (1 − transition)`，
  即同时满足抽取抗混叠与插值镜像抑制。
- 阶数由 Kaiser 公式按目标阻带衰减（默认 80 dB）与过渡带宽度估计，取奇数阶。

### 群延迟

线性相位 FIR 的群延迟为 `gd = (num_taps − 1) / 2` 个**上采样域样本**
= `gd / L` 个输入样本 = `gd / (L·fs_in)` 秒。实现把 `gd` 并入 `t(n)`，
即**输出样本 n 对齐输入时刻 n·M/L（输入样本单位）**，输出不含额外延迟。

### 边界填充

`pad_mode` 两种：

- `zero`（默认）：信号两端之外补零；
- `reflect`：端点镜像（`x[-1]=x[1]`、`x[N]=x[N-2]`），边缘瞬态更小；
  输入太短不足以镜像时退化为边缘复制。

右端填充只在输入结束（`flush()`）时确定，因此流式处理中末尾约
`gd/L` 个输出样本在 `flush()` 时才产生。

### 输出长度

`N_out = ceil(N_in · L / M)`。

## 目录结构

```
resample_service/
  filters.py     # Kaiser 窗低通 FIR 设计、频率响应
  resampler.py   # RationalResampler（整段）+ BlockProcessor（分块流式）
  synth.py       # 合成信号：正弦/脉冲/多音/扫频/噪声
  pcm_io.py      # 本地 PCM 读写：s16le / f32le / f64le / npy
  service.py     # 离线 JSON 请求服务（CLI）
examples/        # 请求样例（含生成的 PCM 输入与输出）
tests/           # pytest 自动化测试
run_acceptance.py# 验收脚本：脉冲/正弦/超奈奎斯特/分块一致性
```

## 使用

### 库方式

```python
from resample_service import RationalResampler

rs = RationalResampler(up=2, down=3, atten_db=80.0)   # 48 kHz -> 32 kHz
y = rs.process(x)                 # 整段
y2 = rs.process_blocks(x, 1024)   # 分块，与整段一致
print(rs.group_delay_in)          # 群延迟（输入样本）
```

### 服务方式（JSON 请求 → 文件）

```bash
python3 -m resample_service.service examples/request_sine_48k_to_32k.json
```

请求字段（完整样例见 `examples/`）：

| 字段 | 说明 |
|---|---|
| `input.type` | `sine` / `impulse` / `multitone` / `chirp` / `noise` / `pcm_file` |
| `input.sample_rate` | 输入采样率（Hz） |
| `resample.up/down` | 重采样比；或用 `target_sample_rate` 自动约分 |
| `filter.atten_db` / `transition` / `num_taps` | 滤波器参数（均可缺省） |
| `boundary.pad_mode` | `zero`（默认）或 `reflect` |
| `processing.block_size` | >0 时分块处理，0 为整段 |
| `output.path` / `format` | 输出文件与格式（`s16le`/`f32le`/`f64le`/`npy`） |
| `output.report_path` | JSON 报告路径（长度、群延迟、滤波器参数等） |

本地 PCM 输入示例（`examples/request_pcm_file.json`）：44.1 kHz s16le → 48 kHz，
比例自动约分为 160/147。

## 测试与验收

```bash
python3 -m pytest tests/ -q     # 自动化测试
python3 run_acceptance.py       # 验收：脉冲/正弦/混叠/分块一致性
```

## 实际运行记录

环境：Python 3.12.3，NumPy 2.5.3，pytest 9.1.1，Linux x86_64。

### 自动化测试

```
$ python3 -m pytest tests/ -q
.....................................................                    [100%]
53 passed in 13.86s
```

### 验收脚本（2026-09-24 运行，全部通过，无未通过项）

```
$ python3 run_acceptance.py
== 1. 脉冲：长度、位置、群延迟 ==
  [PASS] 输出长度: N_in=2000, L/M=3/2 -> N_out=3000 (期望 3000)
  [PASS] 脉冲位置（群延迟已补偿）: 峰值位于输出 #750 (期望 750=k0*L/M)
  [PASS] 群延迟定义: gd = 151 上采样样本 = 50.3 输入样本 = 1.049 ms @48kHz
  [PASS] 脉冲峰值 = L*h[gd]: |y[peak] - L*h[gd]| = 0.00e+00
== 2. 正弦：通带误差 (48 kHz -> 32 kHz, L/M=2/3) ==
  [PASS] 通带幅度误差: 拟合幅度 0.99999365 (误差 -6.35e-06)
  [PASS] 通带残差: 拟合残差 RMS 3.58e-07
== 3. 高于新奈奎斯特的信号：混叠抑制 ==
  [PASS] 混叠抑制: 20 kHz 分量（新奈奎斯特 16 kHz 以上）混叠到 12 kHz，相对通带参考 -74.8 dB
== 4. 分块处理与整段处理一致性 ==
  [PASS] pad=zero,    block=1/13/256/1024/4096: 最大偏差 ≤ 1.8e-15
  [PASS] pad=reflect, block=1/13/256/1024/4096: 最大偏差 ≤ 1.8e-15
总计 17/17 项通过
```

### 服务示例

```
$ python3 -m resample_service.service examples/request_*.json
[request_sine_48k_to_32k.json] 12000 -> 8000 样本 (L/M=2/3, 48000 Hz -> 32000 Hz, whole)
[request_impulse_upsample_x3.json] 2400 -> 7200 样本 (L/M=3/1, 48000 Hz -> 144000 Hz, blocks(512))
[request_alias_check.json] 12000 -> 8000 样本 (L/M=2/3, 48000 Hz -> 32000 Hz, whole)
[request_pcm_file.json] 8820 -> 9600 样本 (L/M=160/147, 44100 Hz -> 48000 Hz, blocks(4096))
```

## 已知限制

- 单声道、float64 内部计算；多声道需逐通道调用。
- `reflect` 填充在输入长度 ≤ 每相抽头数时退化为边缘复制。
- 大 L/M（如 160/147）滤波器较长，速度按多相点积规模增长；本实现优先正确与可验证。
