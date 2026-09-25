# drift-monitor — 特征统计漂移监测服务（纯后端）

用 Python + NumPy 实现的本地机器学习基础设施服务：在**基线窗口**上学习固定分箱，
之后对任意**当前窗口**计算特征分布漂移指标（PSI 等），并通过本地 HTTP JSON API 暴露。
不下载任何外部模型或数据；所有演示与测试都基于可复现的合成数据和一个 NumPy
实现的简单逻辑回归模型。

## 环境

- Python ≥ 3.10（开发验证环境：3.12.3）
- 依赖：仅 `numpy`（测试另需 `pytest`，覆盖率报告需 `pytest-cov`）
- HTTP 服务只用标准库 `http.server`，无 Web 框架依赖

```bash
pip install numpy pytest pytest-cov
```

## 核心机制

### 固定分箱（`drift_monitor/binning.py`）

边界在基线窗口上学习一次后**冻结**，之后所有窗口共用同一套边界，计数才可比。

- `strategy="quantile"`（默认）：基线分位数边界，每个有限桶在基线上近似等频，PSI 最常用；
- `strategy="uniform"`：等宽边界；
- `strategy="custom"`：用户传入完整边界数组（长度 `n_bins+1`，允许 `±inf` 做开口桶）。

桶布局（`n_bins` 个有限桶 + 3 个特殊桶）：

```
[0]        underflow   x <  edges[0]
[1..n]     finite      edges[k-1] <= x < edges[k]（顶桶右端点包含）
[n+1]      overflow    x >  edges[-1]
[n+2]      missing     x 为 NaN（HTTP 层用 JSON null 表示）
```

边界情况处理：

- **缺值**：独立 missing 桶，参与缺失率统计与 PSI（作为分布的一个桶）；
- **溢出**：当前窗口出现超出基线范围的值进入 underflow/overflow 桶，不会越界或报错；
- **空桶**：基线分位数并列（如离散特征）会产生零宽/空桶，桶数保持恒定，靠平滑保证 PSI 有限；
- **常数特征**：基线全同值时退化为以该值为中心的 ε 窗口，不崩溃；
- **全缺失基线**：无法定义分位数，`fit` 明确报错。

### 指标与平滑（`drift_monitor/metrics.py`）

| 指标 | 公式 | 说明 |
|---|---|---|
| PSI | `Σ (q−p)·ln(q/p)` | 主指标；数学上关于两窗口对称 |
| JS 散度 | `½KL(p‖m)+½KL(q‖m)` | 对称，有界 `[0, ln2]` |
| 总变分距离 | `½Σ|p−q|` | 有界 `[0,1]` |
| Wasserstein-1 | 桶中心近似 | 以特征自身单位表示；missing 桶无数值位置被剔除；一侧全缺失时为 NaN（JSON 中为 null） |

**平滑规则**：PSI/JS 前对两侧计数做 Laplace 加性平滑
`p = (c + α) / (n + α·k)`，默认 `α = 0.5`（Jeffreys-Perks）。`α = 0` 关闭平滑，
此时支撑集不相交会让 PSI 为 `inf`。空窗口（计数全 0 且 α=0）退化为均匀分布，
避免 NaN 污染，但监测器会把空窗口标记为 `undefined`。

### 关于阈值的声明（重要）

报告中 `stable / moderate / significant` 分档（PSI < 0.10 / 0.10–0.25 / ≥ 0.25）
是信用风险领域流传的**经验工程惯例，不是统计显著性检验**，不能作为"两窗口
分布不同"的证明。小样本会显著放大 PSI，因此报告始终附带非缺失样本量，
并在当前窗口非缺失数 < 100 时显式标记 `small_sample` 警告。模型 AUC 只是
性能代理指标，协变量漂移与概念漂移之间没有由本服务断言的因果关系。

## 快速开始

### 库用法

```python
from drift_monitor import DriftMonitor, make_dataset

ds = make_dataset("mean_shift", seed=42)          # 可复现合成数据
mon = DriftMonitor(n_bins=10, strategy="quantile", alpha=0.5)
mon.fit(ds.X_base)                                 # 在基线窗口冻结分箱
report = mon.score(ds.X_cur).to_dict()             # 评分当前窗口
print(report["summary"]["max_psi"])                # 9.22（显著漂移）
```

### 演示脚本

```bash
python scripts/demo.py                    # 全部 7 个场景
python scripts/demo.py --scenario same    # 单场景
python scripts/demo.py --json             # JSON 输出
```

### HTTP 服务

```bash
python -m drift_monitor.server --host 127.0.0.1 --port 8000
```

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 存活与拟合状态 |
| POST | `/fit` | 拟合/重置基线监测器（body 见 `examples/fit_request.json`） |
| POST | `/score` | 对当前窗口评分（body 见 `examples/score_shifted_request.json`） |
| GET | `/baseline` | 查看冻结的基线分箱 |
| POST | `/demo/synthesize` | 内置合成场景一键 fit+score（body 见 `examples/demo_request.json`） |
| POST | `/monitor/export` | 导出监测器 JSON（无请求体） |
| POST | `/monitor/load` | 从导出 JSON 恢复监测器 |

请求体约定：数值特征为等长数组，`null` 表示缺失（转为 NaN）；`NaN`/`Infinity`
等非有限 JSON 值会被 400 拒绝，请用 `null`。服务只绑定 localhost，监测器保存在
进程内存中（可用 export/load 持久化）。

curl 示例：

```bash
curl -s -X POST localhost:8000/fit   -H 'Content-Type: application/json' -d @examples/fit_request.json
curl -s -X POST localhost:8000/score -H 'Content-Type: application/json' -d @examples/score_shifted_request.json
curl -s -X POST localhost:8000/demo/synthesize -H 'Content-Type: application/json' -d @examples/demo_request.json
```

## 合成场景（`drift_monitor/synthetic.py`）

`make_dataset(scenario, seed=42)` 生成配对的基线/当前窗口，标签规则 `P(y|x)`
在所有场景中保持不变（只模拟协变量漂移），基线在同 seed 下跨场景完全一致：

| 场景 | 内容 | 预期 |
|---|---|---|
| `same` | 同分布 | PSI 低（< 0.10） |
| `mean_shift` | income/age_z/risk_score 平移 | 这些特征 PSI ≥ 0.25 |
| `scale_change` | tx_count 方差放大 | tx_count 漂移 |
| `missing_spike` | income 缺失率 2%→40% | 缺失率差 + PSI 上升 |
| `small_sample` | 当前窗口仅 12 行 | `small_sample` 警告 |
| `all_missing` | risk_score 当前窗口全缺失 | `all_missing_current` 警告，W1 为 null |
| `mixed` | 平移 + 缺失激增 | 两者同时出现 |

## 测试

```bash
python -m pytest -q                                   # 75 个测试
python -m pytest --cov=drift_monitor --cov-report=term-missing
```

覆盖：分箱边界/特殊桶/并列分位数/常数特征、指标公式与平滑、监测器端到端
场景（同分布、平移、空桶、全缺失、小样本、空窗口）、合成数据可复现性、
模型 AUC、HTTP 服务端到端（真实端口监听）与错误路径。实测结果见
[RUN_LOG.md](RUN_LOG.md)。

## 项目结构

```
drift_monitor/
  binning.py     固定分箱（quantile/uniform/custom，underflow/overflow/missing 桶）
  metrics.py     PSI / JS / TV / W1 与 Laplace 平滑
  monitor.py     DriftMonitor：fit 基线、score 当前窗口、序列化
  synthetic.py   可复现合成数据（7 个场景）
  model.py       NumPy 逻辑回归 + ROC-AUC（端到端验证用简单模型）
  server.py      stdlib http.server JSON API
scripts/demo.py  全场景演示 CLI
examples/        HTTP 请求样例 JSON
tests/           pytest 测试（75 个）
RUN_LOG.md       实际运行命令与结果记录
```

## 已知限制

- 仅支持数值特征；类别特征需先编码或另行实现频率表漂移。
- 监测器状态在进程内存中，多实例部署需自行共享 export/load 的 JSON。
- W1 是桶中心近似，不是基于原始样本的精确 Wasserstein 距离。
- PSI 阈值分档是经验惯例；本服务不提供也不声称提供统计显著性判定。
