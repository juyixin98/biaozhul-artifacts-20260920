# 特征统计漂移监测服务（Feature Drift Monitor）

纯后端、纯本地的数值特征分布漂移监测服务：用**基线窗口**建立固定分箱，把**当前窗口**
映射到同一套桶上，输出 **PSI** 等分布差异指标，并对缺值、溢出桶、空桶、小样本做
显式处理。

- 只依赖 **NumPy**；HTTP 服务只用 Python 标准库（`http.server`）。
- **不下载任何外部模型或数据**；验证用的数据全部由固定随机种子的合成生成器产生，结果可复现。
- 无前端、无数据库；基线档案保存在进程内存中，重启即失效。

> **重要措辞约束**：PSI 的常用切分点 0.1 / 0.25 只是**工程经验规则（rule of thumb）**，
> 本服务的 `psi_band` 字段名因此显式带 `_rule_of_thumb` 后缀。它们**不是**
> “分布相同/已漂移”的统计检验或证明。特别是在小样本、存在空桶或大量缺值时，
> PSI 可能严重失真——服务会在 `notes` / `small_sample` / `current_all_missing`
> 字段中提示，解读时必须结合样本量。

## 目录结构

```
drift/
  binning.py     # 固定等宽分箱、缺值桶、上下溢出桶、桶计数
  metrics.py     # PSI、JS 散度、TVD、缺值率差、三种平滑规则
  synthetic.py   # 可复现合成数据（同分布/平移/尺度/缺值/极端值/全缺失/小样本）
  pipeline.py    # 端到端便捷接口（单特征 / 多特征）
  service.py     # 标准库 HTTP 服务（内存基线存储）
  __main__.py    # python -m drift 入口
tests/           # 64 个 pytest 用例
examples/        # 请求样例 JSON、库 API 演示、样例生成脚本
RUN_REPORT.md    # 实际运行命令与结果的如实记录
```

## 环境与安装

- Python 3.10+（开发环境为 3.12.3），NumPy（开发环境为 2.5.3）；测试需要 pytest。

```bash
pip install -r requirements.txt
pip install pytest          # 仅运行测试时需要
```

## 快速体验

### 方式一：库 API（无需起服务）

```bash
python examples/demo_library.py
```

代码里最常用的入口：

```python
from drift.pipeline import monitor_feature
from drift import synthetic

baseline, current = synthetic.shifted_distribution(mean_shift=1.0, seed=42)
result = monitor_feature(baseline, current, feature="amount", n_bins=10,
                         smoothing="laplace", alpha=0.5)
print(result.psi, result.js_divergence, result.tvd)
print(result.to_dict()["psi_band"])   # severe_drift_rule_of_thumb（经验标签）
```

### 方式二：HTTP 服务

```bash
python -m drift --host 127.0.0.1 --port 8000
```

```bash
curl -s http://127.0.0.1:8000/healthz
curl -s -X POST http://127.0.0.1:8000/v1/monitor \
  -H 'Content-Type: application/json' -d @examples/monitor_same.json
```

## 分箱语义

每个特征建 `n_bins` 个**等宽内部桶**，边界仅由基线中的有限非缺失值决定
（`linspace(min, max, n_bins+1)`）。计数数组长度为 `n_bins + 3`：

```
[underflow] [bin_0] ... [bin_{n-1}] [overflow] [missing]
```

- 基线与当前窗口**强制使用同一边界**（边界不一致直接报错），桶才能逐桶对比；
- 内部桶为左闭右开 `[lo, hi)`，**最后一个内部桶右端点闭合** `[lo, hi]`：
  恰好等于基线最大值的点留在内部桶，严格更大的值进溢出桶；
- `-inf` / 小于基线最小值 → 下溢出桶；`+inf` / 严格大于基线最大值 → 上溢出桶；
- `NaN` 与 JSON `null` / Python `None` → 缺值桶；
- 两个溢出桶**参与分布与 PSI**；缺值桶**不进 PSI**，缺值率单独报告
  （`missing_rate_baseline/current/delta`）；
- 基线全部相等时退化为单点网格（`n_bins=1`，边界 `[x, x]`）；
  基线没有任何有限非缺失值时拒绝建档（HTTP 422）。

## 平滑规则（处理空桶）

未平滑时，若某桶只在一侧有质量，PSI 对应项为 `+inf`（如实暴露空桶问题）。
`smoothing` 支持：

| 方法 | 规则 | 说明 |
|---|---|---|
| `none` | 直接归一化，保留 0 | 一侧空桶 → PSI=`inf`（JSON 中输出字符串 `"inf"`） |
| `laplace`（默认） | `(c + α) / (N + α·B)` | 对称伪计数，默认 `alpha=0.5`（Jeffreys 式） |
| `floor` | 频率下限 `epsilon` 后双侧各自重新归一化 | 默认 `epsilon=1e-4` |

平滑是**工程约定而非统计修正**：不同方法给出的有限 PSI 数值会不同，
不应跨平滑方法比较阈值。

## 指标定义

令平滑后基线频率为 `p_b`、当前频率为 `q_b`：

- **PSI**：`Σ_b (q_b − p_b) · ln(q_b / p_b)`，另返回逐桶贡献 `psi_per_bin`；
- **JS 散度**（base 2，取值 [0, 1]）：`0.5·KL(p‖m) + 0.5·KL(q‖m)`，`m=(p+q)/2`；
  支撑完全不相交时为 1，比 PSI 更不容易被空桶主导；
- **TVD**：`0.5·Σ|p_b − q_b|`，取值 [0, 1]；
- **缺值率差**：`missing_rate_current − missing_rate_baseline`。

任一侧无非缺失观测时，JS/TVD 输出 `"nan"`，并在 `notes` 中说明。

## HTTP API

请求/响应均为 JSON；请求体上限 2 MiB；特征数 ≤ 200；
单特征样本数 ≤ 1,000,000。错误响应统一为 `{"error": "..."}` 且带 4xx/5xx 状态码。

### `GET /healthz`
存活检查。

### `POST /v1/monitor`（一次性计算，不落库）

请求：
```json
{
  "n_bins": 10,
  "smoothing": "laplace",
  "alpha": 0.5,
  "epsilon": 0.0001,
  "min_sample": 30,
  "baseline": {"value": [0.1, 0.2, null]},
  "current":  {"value": [1.1, 1.2, 1.5]}
}
```
`baseline` / `current` 的特征集合必须完全一致。缺值用 `null` 表示。

响应（节选）：
```json
{
  "disclaimer": "PSI 阈值打标（0.1/0.25）仅为工程经验规则，不是分布异同的统计证明。",
  "results": {
    "value": {
      "feature": "value",
      "psi": 1.0731,
      "js_divergence": 0.1702,
      "tvd": 0.4203,
      "psi_band": "severe_drift_rule_of_thumb",
      "missing_rate_baseline": 0.0,
      "missing_rate_current": 0.0,
      "missing_rate_delta": 0.0,
      "n_baseline": 2000, "n_current": 500,
      "n_baseline_observed": 2000, "n_current_observed": 500,
      "small_sample": false,
      "current_all_missing": false,
      "notes": [],
      "bins": {
        "labels": ["underflow", "[-3.65,-2.97)", "...", "overflow"],
        "baseline_counts": [0, 2, "...", 0],
        "current_counts": [0, 1, "...", 0],
        "baseline_freq": [0.0, 0.0012, "..."],
        "current_freq": [0.0, 0.0024, "..."],
        "psi_per_bin": [0.0, 0.0003, "..."]
      }
    }
  }
}
```

### `POST /v1/baselines`（建档，只保存桶定义与基线桶计数，不保存原始样本）
请求体含 `features` 与可选 `n_bins`，返回 `baseline_id` 与各特征桶摘要。

### `POST /v1/drift`（对已存基线计算当前窗口漂移）
请求体：`{"baseline_id": "...", "current": {...}, "smoothing": ...}`。
当前窗口包含基线中不存在的特征 → 400；基线 id 不存在 → 404。
基线中存在、当前未提供的特征按空窗口（全缺失）处理。

### `GET /v1/baselines/<baseline_id>`
查看已存基线的桶定义与基线桶计数。

### `POST /v1/demo`
服务端生成可复现合成数据直接出结果，便于无数据自检：
`{"scenario": "same|shifted|scaled|extremes|all_missing|small_sample"}`。

## 合成场景（验收对应关系）

| 场景 | 生成器 | 期望表现（非统计断言） |
|---|---|---|
| 同分布 | `synthetic.same_distribution` | PSI≈0.03，`little_drift_rule_of_thumb` |
| 均值平移 +1σ | `synthetic.shifted_distribution` | PSI≈1.07，明显大于同分布 |
| 尺度变化 | `synthetic.scaled_distribution` | PSI 增大 |
| 空桶 | 当前质量集中于单桶 | `none`→`inf`；laplace/floor→有限大值 |
| 当前全缺失 | `synthetic.all_missing_current` | `current_all_missing=true`，notes 给出口径警告 |
| 小样本 n=10 | `synthetic.small_sample` | `small_sample=true`；同分布也可能 PSI 很大——演示阈值不可当证明 |
| 缺值率漂移 | `inject_missing` | `missing_rate_delta` 显著为正 |
| 极端值 | `inject_extremes` | 质量进入溢出桶，逐桶贡献可定位 |

## 运行测试

```bash
python -m pytest -q
```

测试覆盖：分箱边界/缺值/溢出/常值基线/非法输入；PSI 数学性质与三种平滑；
全缺失、小样本、空窗口；合成数据可复现性；多特征流水线与桶序列化复用；
HTTP 全部端点与 4xx 路径（真实端口、真实 JSON 请求）。

## 已知边界与限制

- 仅支持**数值特征**（分类特征需要不同的分箱定义，未实现）。
- 内存存储、单进程；不适合作为生产高可用服务，定位是本地基础设施机制验证。
- 没有做时间窗口调度/自动触发；窗口由调用方组织后传入。
- PSI 不提供置信区间或假设检验；如需统计口径请改用专门的拟合优度检验，
  不要把阈值标签当成检验结论。
