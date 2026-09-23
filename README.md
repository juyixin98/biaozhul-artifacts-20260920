# sparse-cg — 稀疏共轭梯度（纯后端）

一个纯 Python + NumPy 的稀疏线性求解后端：自行实现 **CSR 稀疏矩阵** 与
**预条件共轭梯度（Preconditioned CG, PCG）**，求解限定为**对称正定（SPD）**
系统 `A x = b`，通过 **JSON 请求 / JSON 响应** 暴露接口。无前端、无第三方
科学计算依赖（只用 NumPy；连测试也只用标准库 `unittest`）。

> 规模定位：小到中等规模问题（默认上限 `n ≤ 10 000`、`nnz ≤ 500 000`）。

---

## 1. 目录结构

```
.
├── sparse_cg/                 # 计算库
│   ├── csr.py                 # CSR 矩阵：严格校验、matvec、对称/正对角检查
│   ├── solver.py              # PCG 核心：真实残差、停滞、非正曲率、发散诊断
│   ├── api.py                 # JSON 请求/响应接口（请求校验 + 错误信封）
│   ├── cli.py                 # 命令行：stdin/文件 JSON 进，stdout/文件 JSON 出
│   ├── generators.py          # 用已知解生成稀疏 SPD 系统（拉普拉斯等）
│   ├── exceptions.py          # 带稳定错误码的异常体系
│   └── limits.py              # 输入范围常量
├── examples/                  # 请求样例与实际响应
│   ├── request_basic.json
│   ├── request_ill_conditioned.json
│   ├── request_zero_rhs.json
│   ├── request_invalid_csr.json
│   ├── request_non_symmetric.json
│   ├── request_coo_grid.json
│   └── responses/             # 上述样例实际跑出的响应
├── tests/                     # 自动化测试（87 个，unittest）
│   ├── test_csr.py            # CSR 构造、规范化、各类非法结构
│   ├── test_solver.py         # 收敛、病态、零右端、非正曲率、停滞
│   ├── test_api.py            # JSON 接口与错误信封
│   └── test_cli.py            # 子进程端到端、退出码、样例往返
├── acceptance_run.py          # 验收脚本：已知解 + 残差对比（14 个用例）
├── acceptance_results.txt     # 验收脚本实际输出（表格）
├── acceptance_results.json    # 同上的机器可读结果
├── run_tests.sh               # 一键运行全部检查
├── requirements.txt
└── pyproject.toml
```

## 2. 安装与环境

* Python ≥ 3.10（开发环境为 **Python 3.12.3 / NumPy 2.5.3**）
* 唯一依赖：`numpy>=1.24`

```bash
pip install -r requirements.txt        # 或系统已自带 numpy
```

无需安装即可从仓库根目录直接运行（模块以当前目录为包根）。

---

## 3. 快速开始

### 3.1 作为库使用

```python
import numpy as np
from sparse_cg import CSRMatrix, solve_pcg
from sparse_cg.generators import laplacian_1d, smooth_x, make_rhs_from_exact_solution

A, _ = laplacian_1d(500)                 # 三对角 SPD
x_true = smooth_x(500)                   # 选定已知解
b, _ = make_rhs_from_exact_solution(A, x_true)   # b = A x_true

result = solve_pcg(A, b, tol=1e-10, preconditioner="jacobi")

print(result.status)                     # 'converged'
print(result.residual_norm)              # 真实残差 ||b - A x||
print(np.linalg.norm(result.x - x_true)) # 与已知解的误差
```

### 3.2 命令行（JSON 接口）

```bash
# stdin → stdout
python3 -m sparse_cg.cli < examples/request_basic.json

# 文件输入/输出 + 美化
python3 -m sparse_cg.cli -i examples/request_basic.json \
                         -o /tmp/response.json --pretty
```

退出码：

| 退出码 | 含义 |
|---|---|
| `0` | 请求已处理。**数值是否收敛要看 `result.converged`**（非正曲率等求解失败仍是 0，因为接口本身正常工作并返回了诊断） |
| `2` | 请求被拒绝（JSON 非法、CSR 非法、非对称、非正定等），响应为 `{"ok": false, "error": {...}}` |
| `3` | 未预期的内部错误（bug） |

### 3.3 请求 / 响应格式

请求：

```json
{
  "matrix": {
    "n": 60,
    "indptr": [0, 1, 3, ...],
    "indices": [0, 0, 1, ...],
    "data":    [2.0, -1.0, ...]
  },
  "b": [ ... ],
  "x0": [ ... ],
  "tol": 1e-10,
  "max_iter": 500,
  "preconditioner": "jacobi",
  "true_residual_every": 1,
  "stall_window": 50,
  "stall_tolerance": 0.8,
  "assume_spd": false,
  "include_history": true,
  "include_solution": true
}
```

矩阵也支持 COO 三元组形式：`{"n": ..., "rows": [...], "cols": [...], "values": [...]}`
（重复坐标求和）。CSR 与 COO 字段不可混用。

成功响应（节选）：

```json
{
  "ok": true,
  "result": {
    "status": "converged",
    "converged": true,
    "iterations": 14,
    "residual_norm": 2.22e-14,
    "initial_residual_norm": 0.256,
    "relative_residual": 8.69e-11,
    "residual_kind": "true",
    "true_residual_checks": 15,
    "max_residual_drift": 4.5e-17,
    "non_positive_value": null,
    "x": [ ... ],
    "history": [ {"iteration": 0, "residual_norm": ..., "kind": "initial", "alpha": 0.0}, ... ]
  },
  "diagnostics": {
    "n": 60, "nnz_stored": 178, "nnz_declared": 178,
    "duplicates_or_zeros_dropped": 0,
    "symmetry_max_abs_diff": 0.0,
    "tol": 1e-10, "max_iter_used": 500,
    "residual_verified_true": true
  }
}
```

被拒绝请求：

```json
{"ok": false, "error": {"code": "invalid_csr", "message": "'indptr' must be non-decreasing; ..."}}
```

---

## 4. 数值约定（容差与停止判据）

* **收敛判据（相对残差）**：`||b − A x||₂ ≤ tol · ||b||₂`。
  当 `b = 0` 且给定非零 `x0` 时，`tol·||b||` 恒为 0、不可达，此时基准自动
  切换为**初始残差** `||b − A x0||`（残差下降因子），与 PETSc 等实现的惯例一致。
* **`tol` 范围**：`[1e-14, 1e-2]`。注意可达到的最小相对残差受 matvec 舍入
  地板限制；当请求容差低于地板时，求解器返回 `status="stagnation"`，**不会**
  谎报收敛（见测试 `test_roundoff_floor_is_reported_as_stagnation_not_false_success`）。
* **真实残差监控**：递推残差 `r ← r − αAp` 会随舍入误差漂移。默认每步
  （`true_residual_every=1`）以及**终止时必定**重新计算真实残差 `b − A x`，
  用它判定收敛并对外报告；`max_residual_drift` 记录递推值与真实值的最大偏差，
  `history[*].kind` 标明每个残差是 `recurrence` / `true` / `true_final`。
* **迭代停滞**：连续 `stall_window`（默认 50）步残差都没有跌到“一窗之前”的
  `stall_tolerance`（默认 0.8）倍以下 → `stagnation`。采用**加窗对比**而非对比
  历史最优，是为了容忍 CG 超线性收敛前常见的中段平台（残差甚至可能几十步不
  下降或轻微回升）。
* **发散保护**：残差出现 NaN/Inf，或相对初始残差增长超过 `1e8` 倍 → `diverged`。

## 5. 失败状态（`result.status`）

求解**不抛异常**，而是返回带状态码的结果；只有**非法输入**才抛异常 /
在 JSON 层返回 `ok=false`。

| status | converged | 含义 |
|---|---|---|
| `converged` | true | 最终真实残差达到容差 |
| `zero_rhs` | true | `b=0` 且 `x0=0`，SPD 系统唯一解 `x=0`，未迭代 |
| `max_iterations` | false | 达到迭代上限仍未满足容差 |
| `stagnation` | false | 达到舍入地板 / 长期无实质进展 |
| `non_positive_curvature` | false | 迭代中 `pᵀAp ≤ 0`（尺度化判据，见下），矩阵在当前 Krylov 子空间上非正定；`non_positive_value` 给出该值 |
| `breakdown` | false | `rᵀM⁻¹r ≤ 0`，预条件子非 SPD |
| `diverged` | false | 残差非有限或爆炸 |

**非正曲率判据**：精确 SPD 恒有 `pᵀAp > 0`。当 `pᵀAp` 为负、或不超过点积
舍入噪声地板 `1e-12·‖p‖·‖Ap‖`（数值奇异）时判为非正定。这是一个**相对尺度**
判据，因此 `A = 1e-20·I` 这类“数值很小但确实正定”的矩阵仍被正确接受。

> 半正定矩阵的已知数学细节：若 `b` 与零特征空间**正交**，CG 会走到最小范数
> 最小二乘解并正常“收敛”（例如 `[[1,1],[1,1]] x = (1,1)`）；若 `b` 含零空间
> 分量，则在第 2 步以 `pᵀAp = 0` 触发 `non_positive_curvature`。两种情形都有
> 测试覆盖并在此明确记录，而不是被掩盖。

## 6. 输入范围与校验（非法输入必拒绝）

在 `sparse_cg/limits.py` 集中定义：

| 量 | 范围 / 约束 |
|---|---|
| `n` | `1 ≤ n ≤ 10 000`，整数（`bool` 不算整数） |
| `nnz` | `≤ 500 000` |
| 矩阵/向量元素 | 有限实数，`|value| ≤ 1e100`（NaN/Inf 拒绝；严格 JSON 本就不允许 NaN） |
| `tol` | `[1e-14, 1e-2]` |
| `max_iter` | `[1, 100 000]`；缺省 `min(10n, 100 000)` |
| `preconditioner` | `"none"` / `"jacobi"`（Jacobi 要求对角元严格为正） |
| `stall_window` | `≥ 1`；`stall_tolerance ∈ (0,1)`；`true_residual_every ≥ 0` |

**CSR 结构校验**（全部有针对性的错误信息，错误码 `invalid_csr`）：
`indptr` 长度必须为 `n+1`、首元素为 0、末元素等于 nnz、单调不减；
`indices` 必须为整数、落在 `[0,n)`；`indices` 与 `data` 等长；
值必须有限且不超量级。每行自动按列排序、重复坐标求和、求和后为 0 的显式
零元素被剔除（规范化后的 nnz 与原始声明数都在诊断中给出）。

**SPD 校验**（默认开启，`assume_spd=true` 可跳过）：

* 稀疏模式必须对称（缺镜像项 → `non_symmetric_matrix`，并给出位置）；
* 成对值必须满足 `|Aij−Aji| ≤ atol + rtol·|Aij|`（默认 `rtol=1e-10, atol=1e-12`）；
* 对角元必须严格为正（`Aii ≤ 0 ⇒ x=eᵢ` 使 `xᵀAx ≤ 0`，直接非正定，错误码
  `not_positive_definite`，给出位置与对角值）。正对角是必要非充分条件，
  更深层的非正定性由迭代中的曲率诊断捕获。

JSON 层还拒绝：请求体非对象、缺 `matrix`/`b`、未知字段（拼错字段名如
`tolerance` 会直接报错而不是被静默忽略）、CSR/COO 混用、数组长度不符等。

## 7. 验收方法：比较残差，而不是只数迭代次数

`acceptance_run.py` 用**选定已知解** `x_true` 构造 `b = A x_true`，然后对每个
用例**独立重新计算** `‖b − A x‖`（不使用求解器自报的数）与解误差
`‖x − x_true‖/‖x_true‖`，再与请求容差比较。迭代次数仅作参考。

14 个用例覆盖：良态（1-D 拉普拉斯、质量弹簧链，none/Jacobi）、病态
（谱平移拉普拉斯，条件数精确设为 `1e8`、`1e10`）、零右端、不定矩阵曲率诊断，
以及 4 类非法 CSR + 非对称 + 零对角。

### 实际运行结果（本次交付环境，Python 3.12.3 / NumPy 2.5.3）

```
case                                   status                      n    it   req.tol true rel.res   x rel.err  pass
-------------------------------------------------------------------------------------------------------------------
laplacian1d_smooth_none                converged                 500    14   1.0e-10    8.694e-11   2.515e-14    OK
laplacian1d_smooth_jacobi              converged                 500    14   1.0e-10    8.694e-11   2.515e-14    OK
massspring_random_none                 converged                1000    31   1.0e-09    5.002e-10   1.188e-09    OK
massspring_random_jacobi               converged                1000    31   1.0e-09    5.002e-10   1.188e-09    OK
scaledlaplacian_kappa1e8               converged                 400   398   1.0e-08    1.646e-09   1.109e-02    OK
scaledlaplacian_kappa1e10              converged                 400   398   1.0e-08    1.635e-09   1.109e-02    OK
zero_rhs                               zero_rhs                  200     0   1.0e-10    0.000e+00   0.000e+00    OK
indefinite_curvature                   non_positive_curvature      2     1   1.0e-10    1.000e+00           -    OK
illegal_csr_indptr_decrease            rejected(invalid_csr)       3     -         -            -           -    OK
illegal_csr_column_oor                 rejected(invalid_csr)       2     -         -            -           -    OK
illegal_csr_indptr_end                 rejected(invalid_csr)       2     -         -            -           -    OK
illegal_csr_nan_value                  rejected(invalid_csr)       1     -         -            -           -    OK
non_symmetric_values                   rejected(non_symmetric_matrix) 3    -         -            -           -    OK
zero_diagonal_semidefinite             rejected(not_positive_definite) 2   -         -            -           -    OK
-------------------------------------------------------------------------------------------------------------------
14/14 cases behaved as specified.
```

（`acceptance_results.txt` / `.json` 为本次实际输出存档。）

**病态矩阵的关键观察**：`κ=1e8/1e10` 时相对残差降到 `1.6e-9`（满足 `tol=1e-8`），
但解的前向误差约 `1.1e-2`——残差很小而解误差被条件数放大约 `κ` 倍，这是病态
系统的预期现象，也正是“以残差为准绳、同时暴露解误差”的原因。

### 自动化测试实际结果

```bash
python3 -m unittest discover -s tests
# Ran 87 tests in ~4 s
# OK
```

87 个测试全部通过，覆盖：

* CSR：构造、COO→CSR、重复项求和、乱序排序、显式零剔除、与稠密 matvec 对拍、
  所有非法结构、对称模式/数值检查；
* 求解器：已知解恢复（none/Jacobi）、与 `numpy.linalg.solve` 稠密对拍、热启动、
  病态、零右端（零/非零初值）、不定/半正定曲率、微小但真正定不误报、真实残差
  与独立重算一致、加窗停滞不误杀正常平台、舍入地板下诚实报停滞、预算不足报
  `max_iterations` 且残差确有下降、配置越界；
* API：成功信封、CSR 与 COO 输入、重复项诊断计数、历史/解可省略、`assume_spd`、
  各类拒绝码、残差诚实性；
* CLI：子进程端到端、退出码、非法 JSON、`-i/-o/--pretty`、随仓库样例往返。

## 8. 一键复现

```bash
./run_tests.sh                 # 单元测试 + 验收 + 样例 CLI 冒烟
# 或分步：
python3 -m unittest discover -s tests
python3 acceptance_run.py
python3 -m sparse_cg.cli < examples/request_basic.json
```

## 9. 设计说明与边界

* matvec 采用逐行点积（`n ≤ 1e4` 下透明且足够快；未做花哨向量化，便于审计）。
* 预条件子目前提供 `none` 与 `jacobi`；接口为新增预条件子预留了位置。
* 本项目**不做前端**；所有交互通过库 API 或 JSON CLI。
* 本项目不保证对非对称 / 非正定系统给出有用解：这类输入默认被拒绝；若调用方
  显式 `assume_spd=true` 跳过前置检查，迭代中仍会通过非正曲率诊断返回失败状态。
