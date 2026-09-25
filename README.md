# 稀疏共轭梯度（Sparse Preconditioned CG）— 纯后端

用 Python + NumPy 从零实现的**稀疏对称正定（SPD）线性系统求解库**，
带 JSON 请求/响应接口与命令行入口。无第三方依赖（除 NumPy），无前端。

- 核心算法：CSR 稀疏矩阵-向量乘法 + **Jacobi 预条件共轭梯度（PCG）**，全部手写
- 输入限定：对称正定、小中规模（默认 `n ≤ 10 000`、`nnz ≤ 1 000 000`）
- 数值可靠性：以**真实残差** `r = b − Ax` 判定收敛与停滞，非正曲率返回诊断
- 明确的容差、失败状态与错误码

## 目录结构

```
.
├── sparse_cg/
│   ├── __init__.py      # 包入口
│   ├── errors.py        # RequestError（code + message）
│   ├── csr.py           # CSR 矩阵：结构校验、matvec、对角元、对称性检查
│   ├── pcg.py           # PCG 核心：真实残差监控/停滞/曲率诊断/残差替换
│   ├── io_api.py        # JSON 请求校验与 solve_request
│   ├── cli.py           # python -m sparse_cg.cli
│   └── __main__.py
├── tests/               # unittest 自动化测试（82 个）
│   ├── test_csr.py      # CSR 合法/非法结构、对称性
│   ├── test_pcg.py      # 已知解、病态、零右端、负/零曲率、停滞、发散…
│   ├── test_api.py      # JSON API 端到端与全部错误码
│   └── test_cli.py      # CLI 子进程：stdin/文件/退出码/非法 JSON
├── examples/            # 请求样例（01–06）+ responses/ 对应响应
├── scripts/
│   ├── make_examples.py     # 重新生成 examples/*.json
│   └── acceptance_demo.py   # 独立验收演示（重算真实残差、比较已知解）
├── run_tests.sh
└── README.md
```

## 快速开始

环境：Python 3.10+，NumPy（开发验证于 Python 3.12.3 / NumPy 2.5.3）。

```bash
# 运行全部测试
./run_tests.sh

# 用样例请求跑一次 CLI
python3 -m sparse_cg.cli examples/01_laplacian.json

# 或从标准输入
cat examples/01_laplacian.json | python3 -m sparse_cg.cli
```

库调用：

```python
import numpy as np
from sparse_cg import CSRMatrix, pcg, solve_request

# 1) 直接用 CSR 三元组
n = 4
a = CSRMatrix(
    data=np.array([2., -1., -1., 2., -1., -1., 2., -1., -1., 2.]),
    indices=np.array([0, 1, 0, 1, 2, 1, 2, 3, 2, 3]),
    indptr=np.array([0, 2, 5, 8, 10]),
    n=n,
)
b = np.array([1., 0., 0., 1.])
result = pcg(a, b, rtol=1e-10)
print(result.status, result.residual_norm)   # converged  4.4e-15

# 2) JSON API（输入非法时不抛异常，返回 {"ok": false, "error": {...}}）
resp = solve_request({
    "matrix": {"n": n, "data": [...], "indices": [...], "indptr": [...]},
    "b": [1, 0, 0, 1],
    "rtol": 1e-10,
})
```

## JSON 接口

### 请求

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `matrix.n` | int ≥ 0 | 是 | 方阵阶数，上限 10 000 |
| `matrix.data` | number[] | 是 | 非零值，长度 = nnz，必须有限 |
| `matrix.indices` | int[] | 是 | 列下标，长度 = nnz，行内须**严格递增、无重复** |
| `matrix.indptr` | int[] | 是 | 行指针，长度 = n+1，`indptr[0]=0`、单调不减、`indptr[n]=nnz` |
| `b` | number[] | 是 | 右端项，长度 n，必须有限 |
| `x0` | number[] \| null | 否 | 初始猜测，默认全零 |
| `rtol` | number ≥ 0 | 否 | 相对残差容差，默认 `1e-8` |
| `atol` | number ≥ 0 | 否 | 绝对残差容差，默认 `1e-12`（零右端时起决定作用） |
| `max_iter` | int \| null | 否 | 迭代上限，默认 `max(1000, 10n)`，硬上限 100 000 |
| `preconditioner` | `"jacobi"` \| `"none"` | 否 | 默认 `jacobi` |

未知字段一律拒绝（`unknown_fields`）；`NaN`/`Infinity` 不是合法 JSON，CLI 拒绝。

### 收敛判据与数值约定

```
||r_true||_2 ≤ atol + rtol · ||b||_2,      r_true := b − A x（真实重算）
```

| 参数 | 默认值 | 含义 |
|---|---|---|
| `rtol` | 1e-8 | 相对残差容差 |
| `atol` | 1e-12 | 绝对残差容差（`b=0` 时阈值即 atol） |
| 真实残差检查周期 | 30 次迭代 | 周期重算 `b−Ax` 并做**残差替换** |
| 收敛确认 | 每次命中阈值 | 递推残差达阈值后必重算真实残差，不拿递推值宣布收敛 |
| 停滞判据 | factor 0.999，patience 3 | 连续 3 次真实检查残差未降到历史最好值的 0.999 倍 → `stagnated` |
| 曲率容差 | 1e-12 · ‖A‖ | 瑞利商 `pᵀAp/pᵀp` 超过此界判负/零曲率 |
| 发散阈值 | 1e8 × 初始量级 | 真实残差超过即 `diverged` |
| 对称性容差 | 1e-9（相对） | `|Aij−Aji| ≤ 1e-9·max(1,|Aij|,|Aji|)` |
| 规模上限 | n=10 000，nnz=1e6，每行 nnz=1e4 | 中小规模定位 |

残差替换（residual replacement）只把真实残差校正进递推值，**搜索方向与 β
递推延续不重启**，从而保持 CG 的共轭性（整体重启会丧失 n 步有限终止性，
导致本项目早期版本在 n=500 系统上多走数十倍迭代——该问题已被测试捕获并修正）。

### 响应

成功（请求合法）：

```json
{
  "ok": true,
  "n": 4,
  "nnz": 10,
  "status": "converged",
  "converged": true,
  "algorithm_failed": false,
  "iterations": 10,
  "residual_norm": 4.378e-15,
  "initial_residual_norm": 1.414,
  "relative_residual": 1.769e-14,
  "residual_history": [1.414, 0.0, 4.378e-15],
  "message": "第 10 次迭代收敛：...",
  "x": [0.75, 1.0, 1.0, 0.75]
}
```

非法请求：

```json
{"ok": false, "error": {"code": "index_out_of_bounds",
                        "message": "indices[1]=9 越界，合法列下标范围为 [0, 2]"}}
```

> 注意区分：`ok=false` 表示**请求本身非法**（CSR 结构、参数、JSON）；
> 请求合法但算法失败（不收敛/非正定…）时 `ok=true`、`algorithm_failed=true`，
> 成败看 `status` / `converged`。

### 状态码（`status`）

| 状态 | 含义 |
|---|---|
| `converged` | 真实残差满足容差 |
| `max_iterations` | 迭代用尽，已用真实残差复核并返回当前最好的 x |
| `stagnated` | 连续多次真实残差检查无实质性下降 |
| `negative_curvature` | 瑞利商 < 0，矩阵对称但**不定**（非 SPD） |
| `zero_curvature` | 瑞利商 ≈ 0，矩阵可能**奇异**（半正定） |
| `preconditioner_breakdown` | 预条件子失效（Jacobi 对角元异常） |
| `diverged` | 残差非有限或超过发散阈值 |

### 输入错误码（`error.code`，节选）

CSR 结构：`index_out_of_bounds`、`indices_not_sorted`（乱序/重复列）、
`indptr_invalid`、`indptr_not_monotonic`、`indices_length_mismatch`、
`indptr_length_mismatch`、`indices_not_integer`、`data_not_finite`、
`too_many_nonzeros`、`n_too_large`、`n_negative`

SPD 前置校验：`matrix_not_symmetric`（结构或数值不对称）、
`non_positive_diagonal`、`diagonal_missing`

参数：`invalid_rtol`、`invalid_atol`、`invalid_max_iter`、
`invalid_preconditioner`、`dimension_mismatch`、`b_not_finite`、
`unknown_fields`、`missing_matrix`、`missing_b` 等

CLI 另有：`invalid_json`（语法错误或 NaN/Infinity 常量）、`empty_request`、
`file_unreadable`。

退出码：`0` 请求合法（无论算法收敛与否）；`1` JSON/文件错误；`2` 用法错误。

## 算法要点

**预条件 CG**（M 为 Jacobi 预条件，M=diag(A)）：

```
r = b − Ax;  z = M⁻¹r;  p = z
循环:
    α = rᵀz / pᵀAp
    x ← x + αp;  r ← r − αAp
    周期性: r ← b − Ax（真实残差替换），检查收敛/停滞/发散
    z ← M⁻¹r;  β = rᵀz_new / rᵀz;  p ← z + βp
```

本实现的额外保障：

1. **曲率即正定性检验**：对称 A 的 `pᵀAp` 是瑞利商，SPD 必为正。
   按 `pᵀAp/pᵀp` 归一化判定（与方向尺度无关），负值→`negative_curvature`，
   近零→`zero_curvature`。正定性无法廉价预判，因此运行时检验是必要的。
2. **真实残差**：收敛只在重算 `b−Ax` 后宣布；`residual_norm` 与
   `residual_history` 全部来自真实残差。
3. **停滞保护**：在舍入地板（可达精度极限）上停止并如实报告，而非空转至
   `max_iter` 或谎报收敛。
4. **残差替换**抑制递推残差的舍入漂移，但不重启搜索方向。

## 样例

| 文件 | 场景 | 预期 |
|---|---|---|
| `01_laplacian.json` | n=20 一维拉普拉斯，已知解 | `converged`，‖r‖~4e-15 |
| `02_ill_conditioned.json` | 条件数 ~2e9 的缩放系统 | `converged`（残差下降 ~1e14 倍） |
| `03_zero_rhs.json` | b=0、非零 x0 | 收敛到零向量 |
| `04_identity_no_precond.json` | 单位阵、无预条件 | 1 步精确解 |
| `05_invalid_csr.json` | 列下标 9 越界（n=3） | `ok=false`，`index_out_of_bounds` |
| `06_indefinite.json` | [[1,2],[2,1]] 不定 | `negative_curvature`（第 2 步） |

`examples/responses/` 保存了以上每个请求的实际输出。重新生成：

```bash
python3 scripts/make_examples.py
python3 scripts/acceptance_demo.py     # 独立验收演示
```

## 测试

```bash
./run_tests.sh                # = 生成样例 + python3 -m unittest discover -s tests -v
```

82 个测试，覆盖：

- **CSR**：matvec 与稠密参考一致、对角元提取、零维；越界/乱序/重复列/
  indptr 各类非法、NaN/Inf、浮点下标被拒、对称性（结构缺失/数值不等/容差）
- **PCG（已知解驱动，断言真实残差与解误差，而非迭代次数）**：拉普拉斯
  n=50/500、对角系统、非零 x0、精确初值、零维
- **病态**：条件数 ~2e9 的缩放系统，可达/过紧致停滞，失败时残差仍如实
- **零右端**：b=0 零初值、b=0 非零初值收敛到零、零容差下判停滞
- **非正曲率**：不定矩阵负曲率（Jacobi/无预条件两条路径）、奇异矩阵零曲率
- **失败状态**：max_iter、停滞（精度受限算子注入）、发散（NaN 算子注入）、
  预条件失效（极小对角元）、非法参数
- **真实残差**：报告残差与独立重算一致、残差替换在 n=300 系统上保持精度
- **API/CLI**：端到端已知解、全部错误码、stdin/文件、退出码、NaN 常量拒绝

## 设计取舍与适用边界

- 只接受**对称正定**问题：对称 + 正对角是廉价前置检查；正定性由迭代中的
  曲率判据实际检验并诊断。非对称/非定系统不属于 CG 适用范围。
- 中小规模：纯 Python 逐行 CSR matvec，万阶、百万非零量级以内体验良好；
  更大规模应换用带编译内核的求解器。
- Jacobi 预条件对“对角占优/尺度不均”的问题有效；对一般椭圆型 PDE
  （如一维拉普拉斯）提速有限，这是 Jacobi 预条件本身的性质，测试中
  `test_jacobi_reduces_iterations_on_scaled_system` 专门验证其有效场景。
