# RUN_LOG — 实际运行记录

环境：Linux 6.8.0-90-generic，Python 3.12.3，NumPy 2.5.3，pytest 9.1.1。
所有命令在仓库根目录执行。记录时间：2026-09-25。

## 1. 自动化测试

```
$ python3 -m pytest -q
.................................................................  [100%]
75 passed in 12.07s
```

```
$ python3 -m pytest --cov=drift_monitor --cov-report=term-missing -q
75 passed in 12.07s
Name                         Stmts   Miss  Cover
------------------------------------------------
drift_monitor/__init__.py        6      0   100%
drift_monitor/binning.py        92     10    89%
drift_monitor/metrics.py        67      3    96%
drift_monitor/model.py          64      1    98%
drift_monitor/monitor.py       115      1    99%
drift_monitor/server.py        177      6    97%
drift_monitor/synthetic.py      65      3    95%
------------------------------------------------
TOTAL                          586     24    96%
```

未通过项：无（最终状态）。开发过程中曾出现并修复的失败：

- 初版 `_bucket_index` 桶边界包含约定错误（`side="left"` 把 `x == edges[0]` 误判为
  underflow），改为 `side="right"` 并显式把 `x == edges[-1]` 归入顶桶；
- 合成数据标签在全缺失注入之后计算，NaN 进入 logit 导致 `all_missing` 场景标签
  全 0、AUC 为 NaN；改为标签由潜在（未缺失）特征值生成；
- `/monitor/export` 误要求请求体（应无 body）；
- 空窗口 + `alpha=0` 时平滑归一化产生 `RuntimeWarning: invalid value encountered
  in divide`，改为分母为 0 时退化为均匀分布。

## 2. 端到端演示

```
$ python3 scripts/demo.py
```

关键结果（完整输出较长，此处摘录）：

| 场景 | 结果 |
|---|---|
| same | 全部特征 PSI ∈ [0.0022, 0.0092]，均 stable；模型 AUC 0.8476→0.8231 |
| mean_shift | income PSI=9.22、age_z PSI=1.26、risk_score PSI=1.06（significant）；tx_count PSI=0.0022 保持 stable；AUC 0.8476→0.8156 |
| scale_change | tx_count PSI=9.30（significant），其余 stable |
| missing_spike | income 缺失率 2%→41%，PSI=1.46；其余特征 stable |
| small_sample | 当前窗口 12 行，4 个特征均触发 `small_sample` 警告（PSI 噪声大，不可过度解读） |
| all_missing | risk_score 缺失率 2%→100%，`all_missing_current` 警告，W1=null，PSI 有限（平滑生效）；AUC 0.8476→0.8144 |
| mixed | income PSI=8.55 且缺失率 2%→41%，age_z/risk_score significant |

## 3. HTTP 服务实测

```
$ python3 -m drift_monitor.server --port 8177 &
```

（注：首次尝试 8123 端口失败，`OSError: [Errno 98] Address already in use` ——
该端口被机器上另一个本地服务占用，且该服务恰好也返回 `{"status":"ok"}`，
换用 8177 后正常。）

```
$ curl -s http://127.0.0.1:8177/health
{"status": "ok", "fitted": false, "features": []}

$ curl -s -X POST :8177/fit -d @examples/fit_request.json
→ fitted=true，income 冻结边界 [3200, 3300, 3600, ..., 5200]（11 条边）

$ curl -s -X POST :8177/score -d @examples/score_shifted_request.json
→ max_psi=2.57，income/age_z 均 significant；income 附带小样本警告
  （当前窗口仅 11 个非缺失值）与空桶平滑说明

$ curl -s -X POST :8177/score -d @examples/score_all_missing_request.json
→ income：missing_rate 0.083→1.0，all_missing_current=true，W1=null，
  PSI=1.97（有限，平滑生效）

$ curl -s -X POST :8177/demo/synthesize -d @examples/demo_request.json
→ mean_shift：max_psi=9.22，flagged=[income, age_z, risk_score]，
  model_auc baseline=0.8476 current=0.8147

$ curl -s -X POST :8177/monitor/export > /tmp/exported.json
$ curl -s -X POST :8177/monitor/load -d @/tmp/exported.json
→ {"loaded": true, "features": ["income", "age_z", "tx_count", "risk_score"]}

$ curl -s -X POST :8177/score -d '{"data": {"wrong_feature": [1,2,3]}}'
→ {"error": "feature mismatch (unknown=['wrong_feature'], missing=[...])"}
```

错误路径（pytest 中覆盖）：未拟合先 score → 409；列长不一致 → 400；
非数值/非有限值（Infinity）→ 400；非法 JSON → 400；未知路由 → 404；
全缺失基线 fit → 400。

## 4. 声明

- 上述 PSI 分档（0.10 / 0.25）为经验工程惯例，不构成统计显著性证明；
  小样本场景已按设计显式告警而非静默给出高 PSI。
- 全部测试与演示数据为本地合成，未下载任何外部模型或数据。
