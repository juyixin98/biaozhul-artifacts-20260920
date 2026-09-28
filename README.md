# Sample Size Planner — 合成正态与二项检验的样本量规划后端

一个本地、可复现的样本量规划服务：给定**检验方向、分配比、显著性水平 α、目标效能与效应尺度**，
返回**最小整数样本量**，并证明"该样本量达标、减一则不达标"。同时用**独立的蒙特卡洛模拟**
（以及独立的解析 oracle）对结果做交叉验证。

- **技术栈**：Python 3.10+ · FastAPI · NumPy · SciPy · SQLite · pytest
- **数据**：全部为本地合成夹具（`data/fixtures/*.json`），无需任何生产账号或真实参与者数据。
- **工程组织**：统计契约 → 估计内核 → 证据/诊断 → 复现实验 → API/持久化，各层独立、可单独测试。

---

## 1. 快速开始

```bash
cd /home/admin/Downloads/xinbiaozhul/opp460/a
python3 -m venv .venv && source .venv/bin/activate   # 可选
pip install -e .                                       # 或 pip install -r requirements.txt
```

运行全部测试（单元 + 集成，约 6 秒）：

```bash
make test          # 等价于 bash scripts/run_tests.sh
```

真实输出结论（本仓库在 Python 3.12.3 / NumPy 2.4.6 / SciPy 1.15.3 上实测）：

```text
$ python3 -m pytest -q
...
162 passed in 5.86s

$ python3 -m pytest -m "not slow" -q    # 不含蒙特卡洛重模拟的快速子集
149 passed, 13 deselected in 4.43s
```

启动 API（SQLite 与日志默认落在 `data/`、`logs/`）：

```bash
make run           # 或 bash scripts/run.sh，默认 127.0.0.1:8000
```

健康检查（启动时会自动运行非中心 t 分布自检，失败会拒绝启动）：

```bash
$ curl -s http://127.0.0.1:8000/health
{
  "status": "ok",
  "versions": {"python": "3.12.3", "numpy": "2.4.6", "scipy": "1.15.3", ...},
  "noncentral_selfcheck": {"worst_size_error": 4.19e-12, ...}
}
```

用 CLI 回放所有合成夹具（规划 + 独立模拟，无需起服务）：

```bash
$ PYTHONPATH=src python3 -m ssp.cli fixtures --trials 20000
[OK  ] binomial_fisher.json          method=fisher_exact_two_sample          allocation={'n0': 126, 'n1': 126, 'total': 252} power=0.8014 n-1=0.7978 sim=0.7994
[OK  ] binomial_low_rate_exact.json  method=binomial_exact_one_sample        allocation={'n0': 301, 'n1': 0,   'total': 301} power=0.8001 n-1=0.7974 sim=0.8042
[APRX] binomial_moderate.json        method=binomial_normal_approx_two_sample allocation={'n0': 295, 'n1': 295, 'total': 590} power=0.8009 n-1=0.7996 sim=0.8075
[OK  ] normal_t_small.json           method=student_t_noncentral             allocation={'n0': 34,  'n1': 0,   'total': 34}  power=0.8078 n-1=0.7954 sim=0.8053
[OK  ] normal_textbook.json          method=normal_z_known_sigma              allocation={'n0': 32,  'n1': 0,   'total': 32}  power=0.8074 n-1=0.7950 sim=0.8058
[OK  ] normal_two_sample_d20.json    method=normal_z_known_sigma              allocation={'n0': 393, 'n1': 393, 'total': 786} power=0.8006 n-1=0.7996 sim=0.8011

6 OK, 0 flagged
```

`APRX` 不是失败：两样本二项的解析值是"固定标准误"近似，独立模拟执行的是随机分母的真实
score 统计量（操作效能），二者的差距已在夹具 `approximation_allowance` 与测试中显式记录。

---

## 2. 一个端到端例子

教科书单样本 z 检验：d = 0.5、α = 0.05 双侧、目标效能 80%。连续公式给出 n ≈ 31.4，
**但真正达到 80% 的最小整数是 n = 32**（n = 31 只有 0.7950）。

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/plans/normal \
  -H 'Content-Type: application/json' \
  -d '{"endpoint":"normal","alternative":"two_sided","alpha":0.05,"target_power":0.8,
       "effect":0.5,"effect_scale":"standardized_d","two_sample":false,"known_sigma":true,
       "mc_trials":2000}'
```

关键响应字段（节选真实输出）：

```json
{
  "status": "completed",
  "method": "normal_z_known_sigma",
  "allocation": {"n0": 32, "n1": 0, "total": 32},
  "achieved_power": 0.8074304194325572,
  "minimal_integer_check": {
    "passes": true,
    "power_at_total_minus_one": 0.7950080284018121,
    "allocation_minus_one": {"n0": 31, "n1": 0, "total": 31}
  },
  "noncentrality": 2.8284,
  "evidence": {"estimated_power": 0.8105, "trials": 2000, "mc_standard_error": 0.0088,
               "ci95": [0.7933, 0.8277], "seed": 5992230217073615449},
  "agreement": {"analytic_power": 0.8074, "simulated_power": 0.8105, "agrees": true,
                 "basis": "analytic power inside the simulation 95% binomial CI"}
}
```

### 低基率：近似失效自动切换精确检验

p0 = 0.01 vs p1 = 0.03、单侧 greater：期望成功数远小于 5，正态近似不可信，AUTO 自动切换到
**精确二项检验**，最小 n = 301，并在 `warnings` 中说明切换原因。若用户**强制**用渐近方法，
服务不会返回一个带风险的"成功"，而是以 `409` 和显式类别 `approximation_invalid` 失败：

```text
HTTP 409
{"status":"failed","error_category":"approximation_invalid",
 "message":"... expected cell count below threshold ... use method_preference=auto or exact",
 "details":{"min_expected_count":1.54,"threshold":5.0,"allocation":{"n0":154,...}}}
```

---

## 3. 守住的统计边界

| 边界 | 如何落地 |
| --- | --- |
| **检验方向显式** | `alternative ∈ {two_sided, greater, less}`；方向必须与效应符号一致，否则校验失败。 |
| **分配比显式** | `allocation_ratio = n1/n0`；规划变量是**对照臂 n0**，`n1 = round(r·n0)` 派生，保证两臂随 n0 单调不减，整数搜索因此单调、可核验。 |
| **显著性显式** | 0 < α < 1；双侧每尾 α/2、单侧 α，精确检验的临界域严格不超过 α（离散保守性如实报告）。 |
| **效应尺度显式** | 正态：`standardized_d`（Cohen's d）或 `absolute_difference`（需 σ）；二项：`proportions / risk_difference / relative_risk / odds_ratio`，p1 由所选尺度显式推导。 |
| **非中心分布单独验证** | z 用平移正态；σ 未知用 **非中心 t**（`scipy.stats.nct`）。启动与测试都校验"nc=0 退回中心 t、大 df 收敛到正态"。 |
| **整数搜索单独验证** | 扩张 + 二分，且结果**重新求值 n0 与 n0−1**；返回值必满足"n0 达标且 n0−1 不达标"。 |
| **低基率近似失效** | 期望单元格数 < 阈值（默认 5）时 AUTO 切换精确（单样本精确二项 / 两样本 Fisher 条件精确检验）；强制渐近则显式失败。 |
| **中途多次查看不在承诺内** | 固定样本承诺只定价**一次**预设检验；`interim_looks > 1` 直接被契约拒绝。 |
| **失败不伪装成功** | 每个失败有明确类别：`validation_error / effect_too_small / approximation_invalid / exact_cap_exceeded / no_feasible_sample / noncentrality_error / simulation_error / persistence_error`，并以对应 HTTP 状态码返回、落库。 |

整数分配的一个关键细节：若按"总样本量 N 固定、平局择优"搜索，奇偶 N 之间效能**不单调**
（如 Fisher 例中 N = 245 因奇偶平局可达标、N = 246 反而下降），二分法会漏掉可行点。
因此本工程采用统计软件通行的契约——**n0 为整数变量、n1 派生**，使效能对 n0 单调。
两样本 d = 0.2 的答案因此是 n0 = n1 = 393（总 786），而 n0 = 392（总 784）效能 0.79956 不达标。

详见 [`docs/STATISTICAL_CONTRACT.md`](docs/STATISTICAL_CONTRACT.md)。

---

## 4. 目录结构

```text
src/ssp/
  config.py            环境配置（SSP_* 变量，见 .env.example）
  errors.py            失败类别（绝不把异常/未知态统一返回成功）
  diagnostics.py       run_id、输入指纹、结构化日志（版本/进度/步骤/判定）
  contracts.py         统计契约：方向、α、效能、效应尺度、分配比、结果模型
  kernels/
    normal.py          z / 非中心 t 效能内核 + 连续解析初值 + 非中心自检
    binomial.py        渐近 score 内核；精确二项与 Fisher 条件精确检验
  planning.py          方法选择、整数搜索、n/n−1 核验、失败分类
  evidence.py          独立蒙特卡洛模拟效能（分块进度、种子、MCSE、95% CI）
  repro.py             种子派生、规划+证据实验编排、夹具加载
  storage.py           SQLite 持久化（成功与失败分类都落库）
  api/                 FastAPI 路由、pydantic 边界模型、服务编排
  cli.py               命令行复现入口
data/fixtures/*.json  本地合成场景 + 独立计算的参考答案
tests/
  reference_oracle.py  独立 oracle：直接用 scipy 原语重算，绝不导入被测内核
  unit/                契约、内核、搜索、证据、诊断、错误路径
  integration/         API、SQLite、夹具端到端实验
scripts/               run.sh / run_tests.sh
docs/                  统计契约与方法说明
```

---

## 5. API 摘要

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET`  | `/health` | 版本与非中心分布自检结果 |
| `POST` | `/api/v1/plans/normal` | 正态端点规划（含独立模拟证据） |
| `POST` | `/api/v1/plans/binomial` | 二项端点规划（AUTO 精确/渐近切换） |
| `GET`  | `/api/v1/runs/{run_id}` | 读取某次运行（成功或失败分类） |
| `GET`  | `/api/v1/runs?limit=` | 最近运行列表 |

交互式文档：服务启动后访问 `http://127.0.0.1:8000/docs`。

### 诊断与可关联性

每次运行生成 `run_id`（时间前缀 + 随机后缀）与输入 SHA 指纹；日志同时输出到控制台与
`logs/<run_id>.log`，每行带 `run=... fp=...`，记录数值栈版本、连续估计、方法选择/切换、
每一步整数搜索的效能、n/n−1 边界核验、模拟进度与最终判定。例如：

```text
... | run=run-2026... | {"event":"step:boundary_verification","n0":154,"power":0.8022,
                          "n0_minus_one":153,"power_minus_one":0.7999,"target":0.8}
... | run=run-2026... | {"event":"agreement_judgement","analytic_power":...,"agrees":true,...}
```

---

## 6. 配置

所有配置通过 `SSP_*` 环境变量（见 `.env.example`）：`SSP_DB_PATH`、`SSP_LOG_DIR`、
`SSP_EXACT_ONE_SAMPLE_CAP`、`SSP_EXACT_TWO_SAMPLE_TOTAL_CAP`、`SSP_APPROX_MIN_EXPECTED`、
`SSP_MC_DEFAULT_TRIALS` 等。测试自动重定向到临时目录，不污染开发数据。

## 7. 说明与边界

- 精确两样本 Fisher 的无条件效能按 S = X0 + X1 混合条件超几何拒绝概率计算，并对备择分布的
  有效支撑截断（低基率下只需访问极少数 S 值），因此在近似最不可靠的小样本处仍然可算。
- 两样本二项的渐近解析值是固定标准误近似；随机分母的真实操作效能由独立枚举与蒙特卡洛佐证，
  差距被显式量化，而非隐藏。
- 效应趋零、极端比例导致超出精确上限时，返回 `exact_cap_exceeded` / `effect_too_small`，
  绝不返回退化的"成功"。
