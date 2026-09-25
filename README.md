# 突发异常信号检测（Burst Anomaly Detector）

纯后端、离线的信号处理服务：输入**合成信号**或**本地裸 PCM 数据**，
基于**滑动稳健统计**做脉冲/突发异常检测，只返回**数值与文件**
（逐点 CSV、汇总 JSON、等长标记 PCM）。不含播放器、不含任何界面。

- 语言：Python ≥ 3.10，唯一运行期依赖 **NumPy**
- 测试：pytest（60 个用例，含因果性/无未来泄漏、逐块=逐点一致性）
- 入口：`python -m burst_detector ...`

---

## 1. 算法

对第 `i` 个样本，只使用**严格早于它**的至多 `window_size` 个有效历史样本：

```
med_i   = median(x_past)                      # 稳健位置
MAD_i   = median(|x_past - med_i|)
scale_i = max(MAD_i / 0.67448975, min_scale)  # 稳健尺度（高斯一致估计 σ）
z_i     = |x_i - med_i| / scale_i
异常    ⇔ |z_i| > threshold 且预热完成 且连续越限数 ≥ min_duration
```

设计要点：

- **阈值只依赖过去样本**：`x_past` 是判决前的历史窗口，当前点与未来点
  永不参与自身统计（`burst_detector/detector.py` 中“先判决、后入窗”）。
- **稳健性**：中位数 + MAD 对孤立尖峰几乎不敏感，单点脉冲不会污染基线；
  异常样本判决后照常入窗（如实反映过程），瞬态脉冲对统计量影响极小。
- **去抖**：`min_duration`（默认 1）要求连续越限才确认，抑制毛刺误报。
- **尺度下限**：常量信号 MAD=0，`min_scale`（绝对量纲，默认 1e-9）
  防止阈值塌缩为 0 导致除零/处处报警。

### 初始化（预热）策略

- 前 `min_samples`（默认 30）个**有效**历史样本期间判为 `warmup`，
  **不产出异常判决、不计误报**；第 30 个有效样本之后的点才开始判决。
- 历史窗口长度从 1 单调增长到 `window_size`（默认 200），之后 FIFO 淘汰。
- 缺样不计数，预热按“有效样本数”推进（见下）。

### 缺样策略（NaN / +inf / -inf 均视为缺样）

通过 `missing_policy` 显式选择：

| 策略 | 行为 |
|------|------|
| `skip`（默认） | 判为 `missing`；**不入历史窗口、不改变任何状态、不参与统计**，输出 NaN，不计误报 |
| `hold` | 若已有有效历史，用上一有效观测值替代后正常判决，替代值入窗；尚无历史时退化为 `missing` |

缺样位在 CSV 中以空字段表示，在汇总 JSON 的 `missing_indices` 中列出。

### 为什么不会有未来信息泄漏

1. 判决函数 `update(x)` 内顺序固定：读历史统计 → 判决 → **才**把当前点加入窗口；
2. 流式参考实现与一个**独立的向量化实现**（`vectorized_detect`）逐点交叉验证；
3. 测试 `test_no_future_leakage_prefix_invariance` 在信号尾部追加任意数据，
   验证所有前缀的判决、z、中位数、尺度、历史长度**逐点不变**；
4. 测试 `test_past_window_excludes_current_sample` 用历史 `[1..5]` + 当前 `100`，
   验证所用中位数为 3（不含当前点；若泄漏则为 3.5）。

---

## 2. 安装与快速开始

```bash
pip install -r requirements.txt          # 仅 numpy

# 合成验收：阶跃 / 漂移 / 孤立尖峰 / 干净噪声
python -m burst_detector acceptance --out out/acceptance

# 对一个 JSON 请求执行检测（合成信号或本地 PCM）
python -m burst_detector detect examples/request_spike.json

# 也可从 stdin 读请求
cat examples/request_step.json | python -m burst_detector detect

# 生成示例 PCM
python -m burst_detector generate-pcm out/demo.pcm --kind spike
```

一键复现全部样例与基准：

```bash
bash examples/run_examples.sh            # 产物与日志在 out/
python3 examples/benchmark.py            # 多种子误报/延迟/漏报统计
```

## 3. 请求格式（`detect`）

```json
{
  "name": "任务名（决定产物文件名前缀）",
  "output_dir": "out/examples",
  "input": {
    "scenario": "spike",                 // 或 step / drift / clean
    "n": 2000, "sigma": 1.0, "seed": 0,
    "spike_positions": [600, 1200, 1700], "spike_amp": 8.0,
    "missing_positions": [601]           // 可选：注入缺样
  },
  "config": {
    "window_size": 200, "threshold": 6.0, "min_samples": 30,
    "min_scale": 1e-9, "min_duration": 1, "missing_policy": "skip"
  },
  "events": [                            // 可选：标注事件，用于评估
    {"kind": "spike", "start": 600, "eval_end": 605}
  ],
  "blocks": [37, 256, 512, 1195]         // 可选：切块到达，自动校验逐块=逐点
}
```

读取本地裸 PCM 时，`input` 改为：

```json
{ "pcm": "路径.pcm", "dtype": "int16", "channels": 1,
  "channel": 0, "big_endian": false, "normalize": true }
```

- 支持 `int16 / int32 / uint8 / float32 / float64`，多声道交错 PCM
  可用 `channel` 取单声道或 `"avg"` 平均；整型默认按满量程归一化到 ±1。

## 4. 输出文件（只有数值，无界面）

| 文件 | 内容 |
|------|------|
| `<name>_points.csv` | 逐点：`index,value,zscore,median,scale,decision,n_history` |
| `<name>_summary.json` | 配置、各类判决计数、异常/缺样索引，以及（若给 events）延迟/漏报/误报率 |
| `<name>_marks.pcm` | 与输入等长的 uint8 标记流：`1=异常，0=其他` |

`decision ∈ {warmup, missing, normal, anomaly}`。

## 5. Python API

```python
from burst_detector import (
    BurstAnomalyDetector, DetectorConfig, detect_signal, detect_blocks)

det = BurstAnomalyDetector(DetectorConfig(threshold=6.0))
for x in stream:                 # 真正的逐块/逐点到达场景
    r = det.update(x)            # 先判决
    print(r.decision, r.zscore)  # 再由 update 内部入窗

# 离线整段（逐点参考实现）与切块到达，结果严格一致：
a = detect_signal(x, cfg)
b = detect_blocks(x, cfg, [37, 256, 512, 1213])
```

---

## 6. 实际运行结果（本仓库实测，环境见下）

环境：Python 3.12.3、NumPy 2.5.3、pytest 9.1.1、Linux x86_64。
默认参数 `window_size=200, threshold=6, min_samples=30, min_duration=1`。

### 6.1 自动化测试

```
python3 -m pytest -q
60 passed in 9.49s
```

### 6.2 合成验收（固定 seed=0，`python -m burst_detector acceptance`）

| 场景 | 设定 | 首报延迟 | 事件命中 | 评估窗外误报 |
|------|------|---------:|---------:|-------------:|
| 孤立尖峰 | 3 个 8σ 尖峰 @600/1200/1700 | **0 / 0 / 0 点** | 3/3 | 0 / 1970 |
| 阶跃 | 6σ 阶跃 @1000 | **0 点**（首报 #1000） | 1/1 | 0 / 1970 |
| 漂移 | 0.2σ/点 线性漂移 @800 | **41 点**（首报 #841） | 1/1 | 0 / 1970 |
| 干净噪声 | 纯高斯 2000 点 | — | — | **0 / 1970 = 0%** |

每个场景均通过：**逐块（块大小 [1,7,64,100,256,余量]）与逐点输出完全一致**，
且流式实现与独立向量化实现逐点一致。完整原始输出见 `out/run_log.txt`、
机读报告 `out/acceptance/acceptance_report.json`。

### 6.3 多种子基准（`examples/benchmark.py`，原始结果 `out/benchmark.txt`）

- **误报**：干净高斯噪声 50 个种子 × 1970 个可判决点 = 98 500 点，
  threshold=6 与 8 下误报均为 **0**。
- **阶跃延迟**（10 种子，onset=1000）：6σ 全部命中，延迟
  `[0,0,0,0,0,1,1,3,3,5]`，平均 **1.3 点**；8σ 全部 **0 点**。
- **孤立尖峰漏报**（50 种子 × 3 个 = 150 个事件，命中点延迟均为 0）：
  8σ 命中 143/150（**漏报率 4.67%**），10σ 149/150（0.67%），12σ 150/150（0%）。
- **漂移**（10 种子，评估窗 onset 后 600 点）：0.2σ/点 10/10 命中，
  延迟 24–41（平均 32.2）；0.12σ/点 4/10；0.05σ/点 0/10。

### 已知局限（如实记录）

1. **8σ“尖峰”并非必检出**。注入是在随机底噪上叠加：若该点底噪本身约
   −2σ，实际偏差降到 ~6σ 临界处可能漏报（150 个事件中 7 个）。需要更高
   灵敏度可调低 `threshold`（误报随之上升）或对保证幅度 ≥10–12σ 的脉冲，
   漏报率降到 <1%/0%。这是单样本判决的固有边界，不是实现缺陷。
2. **慢漂移会被滑动窗口“跟踪”自适应**：当每点漂移量远小于噪声尺度时，
   中位数随窗口缓慢平移，z 始终不越限（0.05σ/点基准中 10/10 未在窗内报警）。
   本检测器定位于**脉冲/突发**异常；持续缓慢趋势需要配合趋势项/更长窗或
   双窗口变化点检验，不在当前实现范围。
3. 阶跃后窗内统计需要时间反映新电平，报警集中在阶跃后约一个窗口的
   过渡期内，属预期。

---

## 7. 项目结构

```
burst_detector/
  robust_stats.py   # 环形窗口流式中位数/MAD（仅过去样本）
  detector.py       # 因果检测器：预热、缺样策略、去抖
  processing.py     # 逐点/逐块执行 + 独立向量化实现 + 一致性断言
  signal_io.py      # 合成信号、缺样注入、裸 PCM 读写
  evaluation.py     # 检测延迟、事件命中/漏报、误报率
  reporting.py      # CSV / JSON / 标记 PCM 输出
  cli.py            # detect / acceptance / generate-pcm
tests/              # 60 个 pytest 用例
examples/           # 请求样例 JSON、一键脚本、多种子基准
```

## 8. 运行测试

```bash
python3 -m pytest                 # 全部
python3 -m pytest tests/test_detector.py -v
```
