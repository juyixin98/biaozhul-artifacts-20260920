# 定点 IIR 滤波离线服务（fixedpoint-iir）

纯后端离线信号处理服务：以**二阶节（SOS）级联**实现定点 IIR 滤波，
输入合成信号或本地 PCM 数据，输出数值结果与文件。无播放器、无图形界面。

## 功能

- **定点 SOS 级联 IIR**：DF-II 转置结构，Python 整数宽累加器模拟硬件乘累加
- **明确的三要素语义**：
  - *定标*：Qm.n Q 格式（`word_bits` + `frac_bits`），系数与数据/状态各自独立格式
  - *舍入*：`trunc`（向零截断）/ `round`（最近舍入，平局远离零）/ `convergent`（平局取偶）
  - *溢出*：`saturate`（饱和）/ `wrap`（补码回绕）
- **浮点参考路径**：同一 SOS 的 float64 全精度滤波，用于逐样本对比
- **稳定性风险检测**：系数量化后逐节求极点，判定 `|pole| >= 1` 失稳、
  距单位圆小于裕度（默认 0.01）的临界风险、系数越界被饱和等情形
- **验收分析**：溢出计数（输入/各节状态/输出）、极限环检测（含直流死区）、
  量化前后频响误差（dB / 相位）、时域误差（max/RMS/SNR）

## 安装与运行

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 运行测试（55 个，覆盖率 98%）
.venv/bin/python -m pytest tests/ -q

# 生成 PCM 输入样例
.venv/bin/python scripts/make_example_pcm.py

# 运行离线滤波请求
.venv/bin/python -m fixedpoint_iir.service examples/request_stable_impulse.json --outdir out/stable
```

每次运行在输出目录生成：

| 文件 | 内容 |
|------|------|
| `report.json` | 稳定性、溢出、频响误差、时域误差、极限环全部数值结果 |
| `input.npy` / `output_float.npy` / `output_fixed.npy` | 输入、浮点参考输出、定点输出 |
| `output_fixed.pcm` | 定点输出的 PCM 文件（可在请求中关闭） |

## 请求格式

```json
{
  "name": "my_run",
  "signal": {"type": "impulse", "n": 256, "amplitude": 0.9},
  "sos": [[0.0976, 0.1953, 0.0976, 1.0, -0.9428, 0.3333]],
  "coef_format":  {"word_bits": 16, "frac_bits": 14, "rounding": "round", "overflow": "saturate"},
  "state_format": {"word_bits": 16, "frac_bits": 15, "rounding": "round", "overflow": "saturate"},
  "freq_response_points": 512,
  "limit_cycle_tail": 64,
  "outputs": {"write_pcm": true, "pcm_fmt": "s16le"}
}
```

- `sos`：每行 `[b0, b1, b2, a0, a1, a2]`，`a0 != 1` 时自动归一化
- `signal.type`：`impulse` / `step` / `sine` / `multi_sine` / `noise` / `chirp` / `pcm_file`
- 更多完整样例见 `examples/`（含稳定、量化失稳、PCM 输入、大幅输入溢出四个请求）

## 定点语义说明

```
输入 x ──量化(state_spec)──► 整数码
每节 DF-II 转置：
  acc  = b0*x + s1            (宽累加器，小数位 = coef.frac + state.frac)
  y    = acc >> coef.frac     (按 state_spec 舍入 + 饱和/回绕)
  s1'  = (b1*x - a1*y + s2) 同上舍入写回
  s2'  = (b2*x - a2*y)     同上舍入写回
```

所有"累加器 → 状态字长"的收窄都经过 `quantization.round_accumulator_to_spec`，
舍入与溢出语义全工程唯一。系数在滤波器构造时量化，越界系数被饱和并记录标志。

## 稳定 / 不稳定反例

- **稳定**：`examples/request_stable_impulse.json` — 4 阶 Butterworth 低通，
  Q2.14 系数下极点模长 0.577，频响误差 max 0.013 dB
- **量化失稳**：`examples/request_unstable_quantization.json` — 谐振器
  `a=[1, -1.99, 0.9961]`，浮点极点半径 0.998 稳定；Q2.6 量化把 `a2`
  舍入为 1.0，极点落到单位圆上，冲激响应转为自持振荡（实测尾部幅度 0.376 不衰减）

## 验收结果摘要

实测记录（含全部命令与输出）见 [docs/RUN_LOG.md](docs/RUN_LOG.md)：

| 验收项 | 结果 |
|--------|------|
| 冲激输入溢出检查 | 通过：合理定标下 0 溢出，冲激响应衰减到 0 |
| 大幅输入溢出检查 | 通过：1.5 倍满幅正弦触发 447 次饱和并被逐节计数，输出有界不发散 |
| 极限环检测 | 通过：Q1.7 一阶节检出直流死区；量化失稳谐振器检出持续振荡；另实测到低通 1 LSB 颗粒极限环 |
| 频响误差 | 通过：Q2.14 max 0.21 dB / mean 0.01 dB（-60 dB 以下阻带频点剔除并报告） |
| 稳定/不稳定反例 | 通过：均按预期被稳定性检测模块判定 |
| 自动化测试 | 55 个测试全部通过，行覆盖率 98% |

注：极限环检测针对"衰减型"响应（冲激/零输入）设计；对持续周期输入
（如 PCM 正弦），输出尾部天然周期，检测器会如实报告周期，这不属于极限环故障。

## 目录结构

```
fixedpoint_iir/
  quantization.py   # Q 格式、舍入、饱和/回绕、累加器收窄
  sos_filter.py     # 定点 SOS 滤波器 + 浮点参考路径
  stability.py      # 极点计算与量化稳定性风险检测
  signals.py        # 合成信号生成、PCM 读写
  analysis.py       # 频响误差、极限环、溢出统计、时域指标
  service.py        # 离线服务：JSON 请求 -> 数值报告 + 文件
examples/           # 四个请求样例（稳定/失稳/PCM/溢出）
scripts/make_example_pcm.py
tests/              # 55 个自动化测试
docs/RUN_LOG.md     # 实际运行命令与结果记录
```
