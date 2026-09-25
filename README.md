# robustreg — 鲁棒线性回归（纯后端）

纯 Python/NumPy 实现的**鲁棒线性回归**计算库 + **JSON 接口**。核心算法（Huber
损失的迭代重加权最小二乘 IRLS、SVD 最小二乘、数值秩检测）全部自行实现，不依赖
`scikit-learn` / `statsmodels`；无 Web 框架、无前端。

- Python ≥ 3.9，唯一运行期依赖：`numpy >= 1.24`
- 定位：小到中规模问题（默认上限 n=10 000 行、p=500 列，见下文“输入范围”）
- 确定性算法：同一请求两次运行结果**逐位相同**（无随机初始化）

## 目录结构

```
robustreg/
  validation.py   # 请求校验、数值上下限、错误类型
  wls.py          # SVD 加权岭回归求解器 + 显式秩亏处理
  huber.py        # Huber 损失、IRLS、OLS 对照
  api.py          # JSON 请求 -> 响应信封（ok/error）
  cli.py          # stdin/文件 的命令行 JSON 接口
tests/
  test_wls.py         # 求解器单元测试
  test_huber.py       # Huber/IRLS/OLS 单元测试
  test_validation.py  # 校验与边界测试
  test_api.py         # JSON 层与 CLI 端到端测试
  test_acceptance.py  # 验收清单（也可独立运行）
examples/             # 请求样例 + 由 CLI 真实生成的响应样例
scripts/demo.py       # 端到端演示（合成离群数据对照 OLS）
```

## 快速开始

```bash
pip install -r requirements.txt

# 命令行：文件输入输出
python -m robustreg.cli -f examples/request_huber.json -o out.json

# 或标准输入/输出
python -m robustreg.cli < examples/request_huber.json

# 端到端演示
python scripts/demo.py

# 全部自动化测试
pip install -r requirements-dev.txt
python -m pytest tests/
python tests/test_acceptance.py   # 独立验收报告（退出码 0/1）
```

作为库使用：

```python
import numpy as np
from robustreg import huber_irls, ols_fit

rng = np.random.default_rng(0)
X = rng.normal(size=(100, 3))
y = 1.0 + X @ [2.0, -1.0, 0.5] + rng.normal(scale=0.3, size=100)
y[:14] += 12.0                       # 粗大离群点

ols = ols_fit(X, y)
hub = huber_irls(X, y, delta=1.345)  # IRLS
print(hub.coef, hub.intercept, hub.converged, hub.objective, hub.iterations)
```

## 算法说明

### Huber 损失

残差 r、过渡尺度 δ（默认 **1.345**，高斯噪声下约 95% 效率）：

```
ρ(r) = 0.5 r²                  当 |r| ≤ δ   （二次区，等价 OLS）
       δ (|r| − 0.5 δ)         当 |r| >  δ  （线性区，离群点影响有界）
```

### IRLS（迭代重加权最小二乘）

利用 ψ(r)=ρ′(r)=w(r)·r，其中权重

```
w(r) = 1              当 |r| ≤ δ
       δ / |r|        当 |r| >  δ      （恒为正，不做硬截断）
```

1. **初值**：等权（岭）最小二乘；
2. 用当前残差算权重，求解加权岭回归
   `min_b Σ_i w_i (y_i − A_i b)² + Σ_j λ_j b_j²`；
3. 更新残差、权重，记录目标值；
4. 收敛判据：`max|b_new − b_old| ≤ tol · max(1, max|b_new|)`，
   默认 `tol=1e-8`；达到 `max_iter`（默认 50）仍未满足则返回
   `converged=false` / `status="max_iter_reached"`，**不伪装成成功**。

报告的 `objective` 是**不含惩罚项**的纯 Huber 目标；另有
`penalized_objective` 含 `0.5·λ·‖slopes‖²`。

### 截距不惩罚

`fit_intercept=true`（默认）时内部在最前加全 1 列；惩罚向量在该列恒为 0，
岭惩罚 `lam`（默认 0）只作用于斜率。

### 秩亏的明确处理（不形成法方程）

每个加权子问题写成等价的堆叠普通最小二乘：

```
[ diag(√w) A ] b ≈ [ diag(√w) y ]
[ diag(√λ)     ]   [ 0            ]
```

对堆叠矩阵做 **SVD**（不构造 `AᵀWA`，避免平方条件数），以
`s_i ≤ rcond · s_1` 判定数值零奇异值。默认 `rcond = max(m,p)·eps`
（与 `numpy.linalg.lstsq` 一致；可在请求中覆盖，最小 1e-15）。

- 默认 `allow_rank_deficient=false`：秩 < 列数时**直接失败**，抛出
  `RankDeficientError` / JSON 返回 `error.code="rank_deficient"`，并报告
  数值秩、零空间维数、阈值、最小奇异值和修复建议；
- `allow_rank_deficient=true`：返回 SVD **最小范数解**，结果中标记
  `rank_deficient=true` 并给出警告；
- 恢复手段：删除冗余列，或设 `lam>0`（对斜率的岭惩罚使共线方向满秩，
  截距方向仍不惩罚）。

## 数值容差与失败状态（汇总）

| 项目 | 默认 / 界限 | 说明 |
|---|---|---|
| `tol` | `1e-8`，要求 >0 且 ≤1 | 系数无穷范数相对收敛阈值 |
| `max_iter` | 50，范围 [1, 10 000] | IRLS 加权求解次数上限 |
| `delta` | 1.345，要求 >0，≤1e8 | Huber 过渡尺度 |
| `lam` | 0.0，要求 ≥0，≤1e12 | 仅斜率的 L2 惩罚 |
| `rcond` | `max(m,p)·eps≈2.2e-16·max(m,p)`，≥1e-15 | 奇异值相对截断 |
| 目标单调容差 | `1e-10 · max(1,|obj_0|)` | 超过该上升幅度才标记 `objective_decreased=false` |
| 零权重阈值 | `eps · max(w)` | 机器精度级正权重按零权重行剔除 |
| 数据有限性 | 必须为有限实数 | NaN/Inf 一律拒绝；`|值|≤1e100` |

失败状态：

1. **`invalid_json`**（仅 CLI，退出码 2）：请求体不是合法 JSON；
2. **`invalid_request`**（退出码 1）：缺字段、形状不符、越界、非有限值、
   未知字段（拼写错误不会被静默忽略）；
3. **`rank_deficient`**（退出码 1）：数值秩不足，含诊断细节与建议；
4. **`max_iter_reached`**（HTTP 语义上仍为成功信封，但 `converged=false`）：
   迭代预算用尽；
5. **全零权重且无惩罚**：库层抛 `ValueError("...unconstrained...")`，
   表示系统无约束。

## 输入范围（硬性上限）

- 行数 `1 ≤ n ≤ 10 000`；
- 用户列数 `1 ≤ p ≤ 500`，加截距后设计矩阵列数同样 ≤ 500；
- X/y 全部有限、`|x|,|y| ≤ 1e100`；长度必须匹配。

这些界限写在 `robustreg/validation.py` 顶部，可按部署需要调整。

## JSON 接口契约

### 请求

| 字段 | 类型 | 必填 | 默认 | 约束 |
|---|---|---|---|---|
| `X` | number[][] | 是 | — | n×p 实数矩阵 |
| `y` | number[] | 是 | — | 长度 n |
| `method` | string | 否 | `"huber"` | `huber` 或 `ols` |
| `fit_intercept` | bool | 否 | `true` | |
| `delta` | number | 否 | `1.345` | >0（huber；ols 时仅用于对照 Huber 目标） |
| `lam` | number | 否 | `0.0` | ≥0，只惩罚斜率 |
| `max_iter` | int | 否 | `50` | ≥1 |
| `tol` | number | 否 | `1e-8` | >0 |
| `rcond` | number\|null | 否 | `null`（自动） | ≥1e-15 |
| `allow_rank_deficient` | bool | 否 | `false` | |

### 成功响应（`method="huber"`，节选）

```json
{
  "ok": true,
  "method": "huber",
  "result": {
    "coef": [2.3235, -0.3497],
    "intercept": 0.8744,
    "residual": [ ... ],
    "weights": [1.0, 1.0, 0.0584],
    "objective": 31.0598,
    "penalized_objective": 31.0598,
    "iterations": 10,
    "converged": true,
    "objective_decreased": true,
    "status": "converged",
    "rank": 3,
    "rank_deficient": false,
    "nullspace_dim": 0,
    "smallest_singular_value": 1.2867,
    "singular_value_threshold": 6.28e-14,
    "singular_values": [ ... ],
    "objective_history": [53.79, 39.38, ...],
    "coef_norm_history": [ ... ],
    "warnings": [],
    "delta": 1.345, "tol": 1e-08, "max_iter": 50, "lam": 0.0,
    "fit_intercept": true, "n": 10, "p": 2
  }
}
```

`method="ols"` 时返回 `coef/intercept/residual/sse/huber_objective` 及同一套
秩诊断字段（无迭代字段）。`sse = 0.5·Σr²`；`huber_objective` 用请求的 δ 计算，
便于与 Huber 结果**同口径**比较。

### 失败响应

```json
{
  "ok": false,
  "error": {
    "code": "rank_deficient",
    "message": "matrix is rank deficient: numerical rank 3 / 4 ...",
    "rank": 3, "p": 4, "nullspace_dim": 1,
    "smallest_singular_value": 10.26,
    "threshold": 5.19e-13,
    "hint": "remove redundant columns, add lam>0 ..., or set allow_rank_deficient=true ..."
  }
}
```

所有输出均为有限值；序列化使用 `allow_nan=False`，一旦出现 NaN/Inf 会直接报错
（这会是库的 bug，而不是被静默吐出的结果）。

### CLI 退出码

`0` 成功；`1` `invalid_request` 或 `rank_deficient`；`2` JSON 解析失败。

## 请求样例

- `examples/request_huber.json` / `examples/response_huber.json`（10 个点，
  最后一个是 +约 20 的离群点：权重被压到 0.058，目标 53.79→31.06 单调下降）
- `examples/request_ols.json` / `examples/response_ols.json`（同数据的 OLS）
- `examples/request_rank_deficient.json` /
  `examples/response_rank_deficient.json`（第 3 列=第 1 列+第 2 列，秩亏失败信封；
  响应文件由 CLI 实际运行生成，退出码为 1）

## 实际运行记录

环境：Ubuntu (Linux 6.8)，Python 3.12.3，NumPy 2.5.3，pytest 9.1.1。

**自动化测试**（2026-09-23 实际执行）：

```
$ python -m pytest tests/
============================== 75 passed in 1.67s ==============================
```

75 个用例全部通过，覆盖：SVD 求解器与 `numpy.linalg.lstsq` 的一致性、
加权/零权重/岭惩罚/截距不惩罚、精确与近共线秩亏、欠定系统、最小范数解、
Huber 分段值与权重、合成离群数据上对照 OLS、目标单调下降、`max_iter` 状态、
全零残差、常值响应、超大离群点不产生 NaN、参数校验与上下限、JSON 信封与
CLI 三种退出码。

**独立验收清单**：

```
$ python tests/test_acceptance.py
PASS  1. Huber beats OLS vs the clean truth under outliers  [max|err| Huber=0.0440 vs OLS=0.4643; Huber obj=316.1879 <= OLS-at-Huber obj=336.9411]
PASS  2a. Exact collinear columns raise an explicit rank error  [rank=3/4, nullspace_dim=1]
PASS  2b. JSON envelope reports rank_deficient with details  [error code=rank_deficient, rank=2/3]
PASS  2c. Ridge penalty on slopes resolves collinearity  [rank full (3); split coefs sum=2.0046]
PASS  3. Exactly zero residuals: no NaN, weights=1, objective=0  [max|residual|=0.00e+00, objective=0.00e+00, iters=2]
PASS  4. Objective descends monotonically and status is explicit  [iterations=10, 336.9411 -> 316.1879, monotone=True]
PASS  5. Reproducibility: identical request gives identical answer
7/7 acceptance checks passed
```

**演示脚本**（`python scripts/demo.py`，n=120、p=4、17 个 ±(10–20) 离群点）：
OLS 对真值最大绝对误差 0.464，Huber 为 0.044（改善约一个数量级）；Huber 目标
336.94→316.19 单调下降，9 次 IRLS 迭代收敛，17 个离群点权重 <1，103 个内点
权重为 1；精确线性拟合时残差为 0、权重全 1、目标为 0、无 NaN。

**未通过项 / 已知限制**：

- 无未通过项：开发过程中修复了两个自测发现的问题——(1) 全零权重且无惩罚时
  最初抛出秩亏错误而非明确的“无约束”错误，已改为显式 `ValueError`；
  (2) 一处验收构造数据本身共线，已更换为满秩设计。修复后 75/75 与 7/7 全通过。
- IRLS 是一阶不动点迭代，理论上只保证收敛到驻点；本库通过记录完整目标历史
  与 `objective_decreased` 让非单调行为可见。对“多个离群点恰好构成高杠杆结构”
  的极端构型，结果可能依赖初始 OLS 点（已在文档中明确，未做额外的高杠杆诊断）。
- 非高并发/流式场景：一次性读入 JSON、一次性 SVD；规模上限内（n≤1e4、p≤500）
  单机秒级完成，超出该规模不在设计目标内。

## 开发

```bash
python -m pytest tests/                 # 全部测试
python -m pytest tests/test_wls.py -q   # 单个模块
python tests/test_acceptance.py         # 验收清单（带 PASS/FAIL 报告）
python scripts/demo.py                  # 端到端演示
```
