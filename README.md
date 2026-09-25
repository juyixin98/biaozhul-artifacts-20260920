# 定点 IIR 滤波（纯后端离线服务）

Python + NumPy 实现的离线定点 IIR 信号处理服务：二阶节（biquad）级联，
明确定标、饱和与舍入语义，提供浮点参考路径，并对系数量化后的不稳定
风险做检测。**只返回数值与文件，不含播放器或任何界面。**

## 功能

- **二阶节级联 IIR**：转置直接 II 型（DF2T），浮点参考与定点仿真
  共用同一差分方程，差异仅来自量化。
- **明确定标**：信号 `q_sig`（默认 Q0.15）、系数 `q_coef`（默认 Q1.14，
  范围 [-2, 2)）、内部状态与信号同 LSB、仅整数域多 `guard_bits` 个
  保护位（默认 2）。所有移位/截位在代码中显式可见。
- **舍入模式**：`convergent`（最近偶）、`half_up`（远离零）、
  `truncate`（向零）、`floor`（向 −∞，即硬件算术右移）。
- **溢出模式**：`saturate`（饱和）与 `wrap`（二进制补码回绕），
  溢出事件按节计数并反馈到状态（可复现溢出极限环）。
- **稳定性检测**：量化后极点（含 Jury 代数判据）、近单位圆告警、
  系数饱和计数、零输入极限环扫描（量化颗粒环 / 溢出环）。
- **验收实验**：冲激响应、大幅输入（溢出压力）、频响误差
  （量化系数 vs 浮点设计）。
- **滤波器设计**：纯 NumPy 的 Butterworth / Chebyshev I 低通
  （模拟原型 + 预畸变双线性变换），或直接给 SOS 系数。
- **输入**：合成信号（正弦/多音/啁啾/冲激/噪声）或本地 PCM 文件
  （int16/int32/uint8，无文件头）。

## 安装与运行

```bash
pip install -r requirements.txt        # 仅 numpy + pytest
export PYTHONPATH=src                  # 或 pip install -e .

# 运行测试（67 个用例）
python3 -m pytest tests/ -q

# 运行全部示例（生成 out/ 下的数值与文件）
bash examples/run_examples.sh

# 处理单个 JSON 请求
python3 -m fixed_iir.cli run examples/request_stable.json -v

# 快速评估某设计的量化风险
python3 -m fixed_iir.cli check --order 4 --cutoff 0.2 --coef 2,8

# 生成合成 PCM 输入
python3 -m fixed_iir.cli synth --kind chirp -n 4096 -o out/chirp.pcm
```

CLI 退出码：`0` = 评估 ok/warning，`1` = unsafe（量化后不稳定或
溢出极限环），`2` = 请求错误。

## 请求格式（JSON）

```json
{
  "filter": {"type": "butter_lowpass", "order": 4, "cutoff": 0.2},
  "fixed":  {"sig": [1,15], "coef": [2,14], "guard_bits": 2,
             "rounding": "convergent", "overflow": "saturate"},
  "input":  {"kind": "multi_tone", "n": 2048,
             "components": [[0.05, 0.6], [0.35, 0.3]]},
  "output": {"dir": "out/stable", "basename": "stable",
             "pcm": true, "csv": true, "npy": true},
  "analyze": true
}
```

- `filter.type`：`butter_lowpass` / `cheby1_lowpass`（`order`, `cutoff`
  为奈奎斯特分数，`ripple_db`）/ `sos`（`sections` 为
  `[b0,b1,b2,1,a1,a2]` 行）。
- `input.kind`：`sine` / `multi_tone` / `chirp` / `impulse` / `noise`
  / `pcm`（`path`, `dtype`）。
- 响应为 JSON：输入/输出统计、量化后系数、每节极点半径、Jury 判据值、
  极限环列表、饱和计数、与浮点参考的误差、输出文件路径；`analyze`
  为真时附完整验收结果。输出文件：定点 PCM/CSV/NPY + 浮点参考 NPY +
  定点整数 NPY。

## 定点模型（定标语义）

```
信号/输入/输出 : q_sig  = Q0.15（可配）
系数           : q_coef = Q1.14（可配，范围 [-2, 2)）
内部状态       : 与信号同 LSB（2^-15），整数域多 guard_bits 位
                 （默认 Q2.15 状态字）
乘积           : coef_int × sig_int 精确形成（Python int，无回绕），
                 右移 q_coef.frac_bits 后按 rounding 舍入
累加           : 每次状态/输出累加后按 overflow 模式处理
                 （saturate 或 wrap），处理后的值参与反馈
最终输出       : 仅最后一级输出截回 q_sig（无移位，仅截幅）
```

每节递推（DF2T，与浮点参考完全一致的结构）：

```
y   = b0·x + s1
s1' = s2 + b1·x − a1·y
s2' =      b2·x − a2·y
```

## 验收结果（实际运行记录）

命令：`python3 -m pytest tests/ -q` → **67 passed**（覆盖率 97%，
`coverage run -m pytest tests/` 实测）。

命令：`bash examples/run_examples.sh` → 4 个请求全部处理，关键指标：

| 请求 | 判定 | 浮点最大\|z\| | 量化最大\|z\| | 关键现象 |
|---|---|---|---|---|
| `request_pcm.json`（chirp PCM 输入） | warning | 0.88798 | 0.88798 | 1-LSB 颗粒极限环（周期 1，幅度 2.4e-4）；冲激/大幅无溢出 |
| `request_stable.json`（4 阶 Butterworth） | warning | 0.79545 | 0.79546 | 频响误差 0.043 dB / 0.0043°；冲激误差 ≤ 1 LSB；大幅 5× 输入饱和被计数、输出有界 |
| `request_unstable_floor.json`（反例 1） | **unsafe** | 0.95394 | **1.5** | 冲激发散到正轨并饱和（tail RMS 0.97，124 次状态饱和） |
| `request_wrap_cycle.json`（反例 2） | **unsafe** | 0.97468 | 0.97468 | 线性系数稳定，但 wrap 回绕产生幅度 0.92、周期 20 的溢出极限环 |

### 反例 1：浮点稳定 → 量化后严格发散

系数 `b=[0.25,0,-0.25], a=[1,-1.9,0.91]`：浮点极点半径 √0.91≈0.954
（稳定）。在 Q1.2 系数 + `floor`（算术右移）舍入下：
`a1: -1.9→-2.0`，`a2: 0.91→0.75`，Jury 判据 `1+a1+a2 = -0.25 < 0`，
极点变为 **0.5 与 1.5**（严格在单位圆外）。冲激响应从 -0.25 单调放大
到负满幅并持续饱和。

> 说明：最近舍入（convergent/half_up）在几何上无法把 Jury 稳定三角形
> **内部**的点映射到严格外部（只能到临界顶点 r=1）；`floor`/`truncate`
> 的系统性偏移可以。这是真实硬件（算术右移）中存在的风险，也是本项目
> 提供四种舍入模式的原因。

### 反例 2：线性系数稳定 → 回绕溢出极限环

系数 `b=[0.1,0,0], a=[1,-1.8,0.95]`（极点 r≈0.975，量化后仍稳定）。
无保护位（`guard_bits=0`）+ `wrap` 回绕时，零输入扫描发现幅度 0.92、
周期 20 的大振幅溢出极限环；同样系数改用 `saturate` 后仅剩
幅度 0.006 的颗粒环；加 2 个保护位后 1× 满量程激励下溢出事件为 0。

## 项目结构

```
src/fixed_iir/
  qformat.py    Qm.n 定点原语（定标、4 种舍入、饱和、回绕）
  biquad.py     SOS 容器、浮点参考、定点 DF2T 引擎
  design.py     Butterworth / Chebyshev I 设计（纯 NumPy）
  stability.py  极点/Jury 分析、零输入极限环扫描、综合评估
  analysis.py   冲激 / 大幅 / 频响验收实验
  signals.py    合成信号与 PCM/CSV 文件 I/O
  service.py    JSON 请求驱动的离线处理
  cli.py        命令行入口（run / check / synth）
tests/          63 个 pytest 用例（含验收与反例）
examples/       4 个 JSON 请求样例 + 运行脚本
```

## 已知限制

- 定点引擎为逐样本 Python 整数循环，重在语义精确而非速度；
  万级样本的离线处理为秒级。
- 滤波器设计仅覆盖低通（Butterworth / Chebyshev I）；其他类型可直接
  以 `sos` 系数传入。
- 极限环检测是确定性状态扫描（非穷举证明），未检出不等于不存在。
