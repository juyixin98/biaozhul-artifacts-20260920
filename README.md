# 合成正态与二项检验的样本量规划后端

一个分层的后端工程，为**合成正态（z / 非中心 t）**与**二项（单/双比例）**
检验做样本量规划，并提供独立的 Monte Carlo 证据、组序贯（多次查看）边界
以及可复现实验。技术栈：Python · FastAPI · NumPy · SciPy · SQLite，全部使用
本地合成夹具，无生产账号或真实业务数据依赖。

---

## 1. 分层结构

工程严格按四个关注点组织（不是单文件调用壳）：

| 层 | 模块 | 职责 |
|---|---|---|
| **统计契约** | `contracts.py` | 检验方向、分配比、显著性/功效、效应尺度全部显式；失败类别为**封闭枚举** |
| **估计内核** | `estimation/` | 非中心分布功效核（z/t、二项正态近似、**精确二项**）与整数搜索**各自独立** |
| **证据与诊断** | `evidence/` `diagnostics/` | 独立 Monte Carlo、组序贯 α 消耗边界、运行日志、SQLite 审计 |
| **复现实验** | `scripts/` `tests/` | 一键验证脚本、单元/集成测试、样例夹具、CI |

```
src/sample_size_planner/
├── contracts.py                 # 显式统计契约 + 封闭失败类别
├── config.py                    # 独立配置层（环境变量覆盖）
├── estimation/
│   ├── noncentral.py            # 非中心分布功效核（与搜索分离）
│   ├── integer_search.py        # 连续求根 + 最小整数搜索（带回溯/取证）
│   └── solvers.py               # 组合内核与守卫（低基率、容量上限）
├── diagnostics/
│   └── interim.py               # 承诺制组序贯（O'Brien-Fleming / Pocock）
├── evidence/
│   ├── simulation.py            # 独立 Monte Carlo（从原始抽样重算判定）
│   ├── run_log.py               # run_id 关联的 JSONL 结构化日志
│   └── repository.py            # SQLite 持久化（输入/结果/判定可回溯）
└── api/
    ├── schemas.py  service.py  main.py   # FastAPI 传输层
```

---

## 2. 首次运行

需要 Python ≥ 3.10。建议使用虚拟环境（受 PEP 668 管理的系统 Python 需要 venv）：

```bash
make install          # 建 .venv 并安装包与测试依赖
make test             # 运行全部测试
make validate         # 运行可复现验证脚本
make run              # 启动服务  http://127.0.0.1:8000
```

等价的手动命令：

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[test]"
PYTHONPATH=src .venv/bin/python -m pytest -q
PYTHONPATH=src .venv/bin/python scripts/run_validation.py --replications 40000
PYTHONPATH=src .venv/bin/python -m uvicorn sample_size_planner.api.main:app --port 8000
```

### 配置（环境变量，均有本地默认值）

| 变量 | 默认 | 含义 |
|---|---|---|
| `SSP_HOST` / `SSP_PORT` | `127.0.0.1` / `8000` | 监听地址 |
| `SSP_DB_PATH` | `data/planner.db` | SQLite 路径 |
| `SSP_RUNS_DIR` | `logs/runs/` | 每次运行的 JSONL 追踪 |
| `SSP_LOW_RATE_THRESHOLD` | `5.0` | 低基率最小期望计数阈值 |
| `SSP_MAX_SAMPLE_SIZE` | `1_000_000` | 整数搜索容量上限 |
| `SSP_SIM_SEED` | `20260927` | Monte Carlo 默认种子 |
| `SSP_MAX_INTERIM_LOOKS` | `20` | 承诺查看次数上限 |

---

## 3. HTTP 接口

| 方法 路径 | 说明 |
|---|---|
| `POST /plan/normal` | 正态均值检验样本量（z 或非中心 t，单/双样本，显式分配比） |
| `POST /plan/binomial` | 比例检验样本量（低基率自动切换精确法） |
| `POST /simulate/power` | **独立** Monte Carlo 功效证据（不经过求解器） |
| `POST /interim/plan` | 承诺制组序贯边界（O-F / Pocock）与 α 核算 |
| `GET  /runs/{run_id}` | 按运行身份读取持久化审计记录 |
| `GET  /health` | 存活探针与依赖版本 |

样例请求体见 `data/fixtures/*_request.json`，例如：

```bash
curl -s -X POST localhost:8000/plan/normal -H 'Content-Type: application/json' \
  -d @data/fixtures/normal_request.json
```

成功响应（节选）——注意同时给出 n 与 n−1 的功效取证：

```json
{
  "success": true,
  "n_per_group0": 25, "n_per_group1": 25, "n_total": 50,
  "achieved_power": 0.80743,
  "power_at_n_minus_one": 0.79141,
  "method": "normal_approx", "failure_category": "none"
}
```

失败**不会**被伪装成成功。零效应返回 200 但 `success=false` 且带显式类别；
非法输入返回 422；承诺外的中途查看返回 422 + `interim_look_outside_commitment`。

---

## 4. 守住的统计边界

1. **检验方向 / 分配比 / 显著性 / 效应尺度显式**：均为契约字段，方向与效应
   不一致（如 `greater` 但 p1<p0）在契约层拒绝。
2. **非中心分布与整数搜索分别验证**：`noncentral.py` 不含任何搜索；
   `integer_search.py` 不依赖任何分布，可独立单测。
3. **整数临界条件被见证**：每个成功结果都满足
   `power(n) ≥ 目标` **且** `power(n−1) < 目标`，两个值都写入结果与日志。
4. **低基率近似失效 → 精确/标限制**：最小期望计数 < 阈值（默认 5）时，
   弃用正态近似，改用精确二项求和；正常区间也会用精确功效交叉核对，发现
   近似解实际不达标即回退精确重搜（带回溯，保证返回最小整数）。
5. **中途多次查看不在固定样本承诺范围**：查看次数必须事先固定（有上限），
   边界经多元正态积分校准使总 I 类错误恰为 α；计划外的额外查看被显式拒绝。

封闭失败类别：`invalid_input`、`effect_zero`、
`approximation_unreliable_low_rate`、`exact_limited_by_cap`、
`non_convergence`、`computation_error`、`interim_look_outside_commitment`、
`too_many_interim_looks`。

---

## 5. 验收要点与独立参考答案

测试断言**具体数值与失败类别**，且参考答案不来自被测核心自身：

- **解析特例**：Cohen d=0.8 双侧 z 检验 **25/组**、非中心 t 需 **26/组**、
  单样本 d=0.5 为 **32**、单侧为 **20/组**（均由裸 `scipy.stats.norm/nct`
  闭式公式独立给出）。
- **手算精确特例**：n=20、H0:p=0.5、单侧 α=.05 的拒绝域是 X≥15
  （P=0.02069 ≤ .05，而 X≥14 为 0.05766），对 p1=.7 的功效 0.41637。
- **精确两样本核**：在 n0=8,n1=6 的微型案例上对所有 (x0,x1) 组合**暴力枚举**
  score 统计量作为独立参照（误差 5.6e-17），并用原始抽样 Monte Carlo 复核。
- **效应趋零**：d=0 与 p0=p1 返回 `effect_zero`（功效恒为 α，不存在有限 n）；
  独立 MC 在零效应下拒绝率落在 α 的置信区间内。
- **极端比例**：p0=.001→.01 自动精确法（n0=699，双侧 0.8003/0.7998）；
  p0=.99→.999 经回溯从近似根 ~1259 修正到最小 698；p0=1e-4→3e-4
  在高精度下严格满足 0.8000021 ≥ .8 而 0.7999960 < .8。
- **模拟效能对照**：规划出的 n 交给**从原始高斯/二项抽样重算判定**的
  独立模拟器，目标功效必须落在其 95% Wilson 区间内。
- **组序贯**：K=1 退化为固定临界值；Pocock K=2 双侧常数 **2.178**、
  O-F K=4 双侧边界 **[4.049, 2.863, 2.337, 2.024]**（与发表表值一致），
  尺寸另用独立 Monte Carlo 复核。

---

## 6. 真实测试命令与输出结论

```text
$ PYTHONPATH=src .venv/bin/python -m pytest -q
............................................................................
84 passed in 13.8s
```

覆盖率（`make coverage`）：

```text
Name                                                Stmts   Miss  Cover
src/sample_size_planner/api/main.py                    45      3    93%
src/sample_size_planner/api/schemas.py                90      0   100%
src/sample_size_planner/api/service.py               109     14    87%
src/sample_size_planner/config.py                     34      1    97%
src/sample_size_planner/contracts.py                 134      9    93%
src/sample_size_planner/diagnostics/interim.py       113      8    93%
src/sample_size_planner/estimation/integer_search.py  85      9    89%
src/sample_size_planner/estimation/noncentral.py     163     10    94%
src/sample_size_planner/estimation/solvers.py        141     11    92%
src/sample_size_planner/evidence/repository.py        56      3    95%
src/sample_size_planner/evidence/run_log.py           65      7    89%
src/sample_size_planner/evidence/simulation.py        91     17    81%
TOTAL                                                1126     92    92%
```

可复现验证脚本（`make validate`）：

```text
Validation run run-20260928T081321-e49bff70
Python 3.12.3 | numpy 2.5.3 | scipy 1.18.1 | fastapi 0.141.1
  [PASS] normal d=0.8 two-sided -> 25/group
  [PASS] binomial low-rate 0.001 vs 0.01 -> exact
  [PASS] zero effect -> effect_zero failure
  [PASS] MC vs analytic normal power
  [PASS] Pocock K=2 two-sided boundary
  [PASS] unplanned look rejected
```

测试按标记分类：`unit`、`normal`、`binomial`、`simulation`、`integration`
（例如 `pytest -m simulation`）。

---

## 7. 运行身份、日志与可追溯性

每次计算都在一个 `run_id` 下执行，JSONL 追踪（`logs/runs/<run_id>.jsonl`）
逐行记录：依赖版本、输入、整数搜索进度（n 与对应功效）、以及**判定依据**。
判定只允许 `accept / reject / failure / error`，未知状态无法被记为成功；
异常走 4xx/5xx 信封而不是统一返回成功。完整输入与结果同时存入 SQLite，
可凭 `run_id` 重建并复跑。

---

## 8. 设计取舍与已知统计性质

- 精确两样本功效**精确计算 score 检验的拒绝域**（对 x0 求二次根并按真实
  score 值校正整数边界，再对 x1 求精确二项尾），因此在极稀疏计数下会继承
  score z 检验轻微反保守的渐近性质；这类情形由低基率守卫识别并走精确规划，
  测试如实记录该性质（如 n1·p=6 时尺寸约 0.0518）而非掩盖。
- 组序贯积分用多元正态 CDF 的望远镜等式（穿越概率 = 1 − 全程存活概率），
  避免 2^K 包含排斥；K≤5 校准在亚秒级。
- 二项连续校正（continuity correction）字段已在契约保留，默认采用
  score 检验约定；精确路径不依赖近似，故不受其影响。
