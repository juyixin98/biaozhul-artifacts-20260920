# 突发异常信号检测（纯后端 / offline）

基于**滑动稳健统计**的脉冲异常检测离线服务。输入为合成信号或本地原始 PCM
数据，输出仅为数值（stdout / JSON）和文件（逐点 CSV、汇总 JSON）。**不含任何
播放器、图形界面或前端组件。**

- 语言：Python 3.10+，仅依赖 NumPy
- 形态：可导入的流式库 + 一个命令行入口（`python -m sudden_anomaly.cli`）
- 两种喂入方式（逐点 / 逐块）共用**同一条标量更新代码路径**，逐块拼接结果与
  一次性处理结果逐点完全一致（测试强制断言）

## 1. 检测原理与无未来信息泄漏

处理第 `t` 个样本时，统计量**只由严格早于 `t` 的已观测样本**构成；判定完成后
才把 `x[t]` 折入状态。任何未来样本都不会被读取。检测器内部维护一个仅含已观测
（非缺失）样本的滑动窗口，并在其上计算两个互补统计量：

1. **脉冲统计量 `pulse_score`** —— 当前样本相对整个滑动窗稳健中心的标准化偏差，
   负责**孤立尖峰**：

   ```
   pulse_score[t] = |x[t] − median(window)| / (1.4826 · MAD(window))
   ```

2. **位置偏移统计量 `shift_score`** —— 近期短窗中心相对更早基线窗中心的差异，
   按两个中心的合并（渐近）标准误归一化，负责**阶跃与缓慢漂移**。尺度只取自
   样本量更大、更稳定的基线窗（用短窗 MAD 作分母会使比值重尾、显著抬高误报，
   见 `docs/VALIDATION.md` 的实测对比）：

   ```
   shift_score[t] = |median(recent) − median(baseline)| /
                    (1.253 · σ_baseline · sqrt(1/n_base + 1/n_recent))
   ```

判定规则（预热结束后）：

- `pulse_score > threshold` 立即报警（孤立尖峰零延迟，无持续性要求）；
- `shift_score > threshold` 需**连续 `shift_persist` 个样本**成立才报警，用于
  抑制重叠窗中心高度相关导致的成串噪声超界；
- 任一成立即 `ANOMALY`，否则 `NORMAL`。

> 为什么单窗 z 分数不够：在缓慢单调漂移下，滑动窗自己的中心和尺度会把趋势
> "吸收"掉，z 分数始终不大。双窗位置比较才能把"近期水平已不同于更早基线"
> 这件事检测出来（代价是漂移需要累计到可分辨水平，存在固有延迟）。

## 2. 初始化（预热）策略

- 窗口按**已观测样本**计数（缺样不占位）。
- 累计已观测样本达到 `prime_size`（默认 64）、且窗内基线部分至少
  `min_history`（默认 16）个、近期部分至少 `recent_min`（默认 8）个之前，
  一律输出 `WARMING`，不给 NORMAL/ANOMALY，分数为空。
- 常量信号尺度为 0 时，分母用 `epsilon_scale`（默认 1e-9）兜底；常量基线上的
  任何非零突变都会被检出。

## 3. 缺样（missing）策略

- 缺失样本以 `NaN` 表示，**不做插补**：不更新任何统计量，输出
  `NOT_DECIDED`，分数为空。
- 缺口**不消耗**窗口容量；下一个已观测样本立即恢复判定。
- 缺失会**清零**位置偏移的连续计数（持续性只在连续已观测样本间累计）。
- 非 NaN 的 `±Inf` 被视为非法输入并报错（NaN 才是"缺失"的唯一编码）。

## 4. 参数与默认值

| 参数 | 默认 | 含义 |
|---|---|---|
| `threshold` | 5.0 | 两个标准化统计量的判定阈值（严格大于） |
| `window_size` | 512 | 滑动窗长度（按已观测样本计） |
| `prime_size` | 64 | 预热所需已观测样本数 |
| `min_history` | 16 | 基线子窗最少样本数 |
| `recent_size` | 32 | 近期子窗长度 |
| `recent_min` | 8 | 近期子窗最少样本数 |
| `shift_persist` | 3 | 位置报警所需连续超阈次数 |
| `scale_estimator` | `mad` | `mad`（Hampel MAD）或 `std` |
| `center` | `median` | `median`（稳健）或 `mean` |
| `epsilon_scale` | 1e-9 | 尺度下限，防止除零 |

约束：`min_history + recent_min ≤ prime_size ≤ window_size`，启动时校验。

## 5. 安装

无第三方运行依赖（仅需 NumPy）：

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt          # numpy>=1.24
# 或安装为包：
pip install -e .
```

## 6. 快速开始

```bash
export PYTHONPATH=src

# 三个合成场景（阶跃 / 漂移 / 孤立尖峰），写出 CSV+JSON 到 out/
python3 -m sudden_anomaly.cli --scenario all --out-dir out

# 逐块喂入（模拟分包到达），验证与一次性处理逐点一致
python3 -m sudden_anomaly.cli --scenario all --block-size 137 --out-dir/out_block

# 本地原始 PCM（s16 小端，单声道）
python3 -m sudden_anomaly.cli --pcm examples/data/demo_spike.s16 \
    --pcm-format s16 --sample-rate 100 --out-dir out/pcm

# 注入 5% 随机缺样
python3 -m sudden_anomaly.cli --scenario spikes --missing-rate 0.05 --out-dir out/miss
```

更多请求样例（含 Python API）见 [`examples/requests.md`](examples/requests.md)。

## 7. 输出（仅数值与文件）

- `<场景>_points.csv` / `pcm_points.csv`：逐点
  `index,time_seconds,value,decision,score,pulse/shift 见 JSON,center,scale,n_observed`。
  `decision ∈ {WARMING, NORMAL, ANOMALY, NOT_DECIDED}`。
- `<场景>_summary.json` / `pcm_summary.json`：配置、输入信息、逐点数组、异常
  下标、缺失率，以及合成场景的评估（每事件检测延迟、误报数与误报率、命中率）。

## 8. 测试与实测结果

```bash
./run_tests.sh        # 标准库 unittest，39 个用例，无需 pytest
```

覆盖：配置校验、预热、**因果性（前缀独立性）**、**逐块/逐点一致性（块大小
1,2,7,100,257,10000 全等）**、缺样语义、常量信号、三种合成场景的延迟与误报、
固定种子误报回归、PCM 读写往返、CLI 子进程。

实测命令、数字、以及开发中出现并修复的**未通过项**（漂移不可检的算法缺陷、
`_stitch` 变量 bug、误报率偏高的尺度修正等），如实记录在
[`docs/VALIDATION.md`](docs/VALIDATION.md)。

## 9. 已知边界

- **缓慢漂移存在固有检测延迟**：在累计偏移仍与噪声同量级时，任何因果方法都无法
  可靠区分；默认配置对斜率 0.004/样本（σ=1）的漂移延迟约 451 点（@100Hz 约
  4.5s）。增大 `recent_size`/采样率意义上的"坡度"会更早报警。
- 当前为逐样本标量实现，吞吐约数千样本/秒（见验证文档实测），面向离线分析而非
  高采样率实时流。
- 仅处理单声道交错 PCM；不做任何音频解码。
