# bounded-lp：有界线性规划两阶段单纯形求解器（纯后端）

一个**纯后端**线性规划求解库：Python + NumPy，核心两阶段单纯形**自行实现**
（不调用任何 LP 求解器），面向小中规模问题。提供 Python API 与 JSON 命令行接口，
输出**可行最优解 / 不可行（含 Farkas 证书）/ 无界（含改进射线）**三类明确结论，
并附带可独立核验的残差。

## 1. 支持的问题

```
min 或 max   c^T x
s.t.  A_ub x ≤ b_ub
      A_eq x = b_eq
      0 ≤ lb ≤ x ≤ ub          # lb 默认 0（仅支持非负变量），ub 默认 +∞
```

“有界线性规划”体现在：变量可声明有限上界 `ub`（预处理时转为普通 `≤` 行），
有限下界通过平移 `x = lb + z, z ≥ 0` 处理。

### 输入范围（硬限制）

| 限制 | 值 | 说明 |
|---|---|---|
| 变量数 | ≤ 300 | 决策变量 `x` 的维数 |
| 约束总数 | ≤ 300 | 不等式 + 等式 + 有限 `ub` 的合计行数 |
| 单个系数绝对值 | ≤ 1e6 | 容差是**绝对容差**，请保持系数量级合理 |
| 不允许 | NaN / `lb<0` / `ub<lb` / `-∞` 的 ub | 直接返回 `invalid_request` |

### 数值容差（`options` 可覆盖）

| 容差 | 默认 | 用途 |
|---|---|---|
| `feas_tol` | 1e-7 | 可行性：约束残差、变量边界、Phase I 最优值 w* |
| `pivot_tol` | 1e-10 | 枢轴列/枢轴元的零判定、比值平局 |
| `reduced_tol` | 1e-8 | 既约费用符号（最优性）检验 |

要求 `pivot_tol < reduced_tol < feas_tol`，三者都必须落在 `(0,1)`。

### 状态与失败语义

| `status` | 含义 | 附带 |
|---|---|---|
| `optimal` | 找到最优解 | `x`, `objective`, `residuals` |
| `infeasible` | 可行域为空 | `farkas_y`（Farkas 证书）, `residuals` |
| `unbounded` | 目标可沿射线无限改善 | `x`（可行点）, `ray`, `objective_direction` |
| `failed` | 未能得出结论 | `reason`: `iteration_limit` / `cycle_detected` / `numerical_breakdown` |
| `invalid_request` | 输入不合法 | `errors`：带字段路径的错误信息 |

> **失败不是结论。** 达到迭代上限、检测到基组合重复（循环）或枢轴过程中
> 出现不可恢复的负右端时返回 `failed`，响应中**不携带**可能误导的解。

## 2. 算法

教科书式稠密 **Gauss–Jordan 表上两阶段单纯形**（`bounded_lp/simplex.py`）：

1. **预处理（标准形）**：max→min；下界平移；有限 `ub` 转为 `≤` 行；
   右端为负的 `≤` 行乘 −1 变 `≥`。
2. 加**松弛变量**（`≤`，右端非负）或**剩余变量 + 人工变量**（`≥`/`=`）。
3. **Phase I**：最小化人工变量之和 w。w* 超容差 ⇒ 不可行；
   否则把零值占基的人工变量逐出行（全零行作为冗余 `0=0` 删除）。
4. **Phase II**：换原目标检验数行继续枢轴；无负检验数 ⇒ 最优；
   进入列在所有行上非正 ⇒ 无界，由终表直接构造射线。
5. **防循环**：默认 **Bland 规则**（进入列取下标最小的负检验数列，
   平局时离开行取基变量下标最小者），理论上保证有限步终止。
   另提供经典 **Dantzig 规则**（检验数最负入基）用于教学对比。
6. 同时用“已见基组合集合”监测循环，作为防呆。

### 可核验产物

* 最优解：原始问题上的最大违反量 `max_abs_residual`、等式残差、
  最小既约费用、表上目标值与直接代入值之差。
* 不可行：Farkas 证书 `y`，满足 `y^T A ≤ 0` 且 `y^T b > 0`，
  即 `{x≥0 : Ax=b}` 必为空（响应给出 `farkas_max_ytA`、`farkas_ytb`）。
* 无界：可行点 `x` 与射线 `d`，满足 `A_ub d ≤ 0`、`A_eq d = 0`、
  `d ≥ 0`、有限上界方向分量为 0，且费用方向严格改善；
  于是 `x + t d`（任意 `t ≥ 0`）可行而目标趋于 `−∞`/`+∞`。

## 3. 安装与使用

无需安装第三方求解器，只需 NumPy（开发环境 Python 3.12 / NumPy 2.5）。
在仓库根目录直接用模块方式运行，不需要安装：

```bash
python3 -m bounded_lp.cli examples/01_optimal.json
python3 -m pytest
```

若想得到 `blp` 命令，在虚拟环境中可编辑安装（某些系统 Python 受
PEP 668 保护，需先建 venv 或加 `--break-system-packages`）：

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
blp examples/01_optimal.json -o resp.json
```

### CLI

```
python3 -m bounded_lp.cli [请求.json] [-o 响应.json] [--indent 2]
```

省略文件或传 `-` 时从标准输入读取。退出码：

* `0`：optimal / infeasible / unbounded（得到明确结论）
* `1`：failed
* `2`：invalid_request
* `3`：文件读写/用法错误

### JSON 请求（矩阵式）

```json
{
  "sense": "min",
  "c": [-3, -5],
  "A_ub": [[1, 0], [0, 2], [3, 2]],
  "b_ub": [4, 12, 18],
  "lb": [0, 0],
  "ub": [null, null],
  "options": {"rule": "bland", "max_iterations": 10000,
              "feas_tol": 1e-7, "pivot_tol": 1e-10, "reduced_tol": 1e-8}
}
```

约束列表式（`op` 支持 `<=`、`>=`、`=`，不能与矩阵式混用）：

```json
{
  "c": [2, 3],
  "constraints": [
    {"a": [1, 1], "op": ">=", "b": 5},
    {"a": [2, 1], "op": "<=", "b": 12}
  ]
}
```

### Python API

```python
from bounded_lp import LPProblem, solve

p = LPProblem(
    c=[-3, -5],
    A_ub=[[1, 0], [0, 2], [3, 2]], b_ub=[4, 12, 18],
)
r = solve(p)                       # rule="dantzig" 可切换规则
print(r.status, r.x, r.objective, r.residuals)
```

## 4. 目录结构

```
bounded_lp/
  tolerance.py           容差与规模限制
  problem.py             问题数据结构 + 标准形预处理
  simplex.py             两阶段单纯形核心（自实现）
  enumerate_vertices.py  顶点枚举 / 衰退极射线枚举（测试用参考实现）
  io_json.py             JSON 请求解析与响应组装
  cli.py                 命令行入口
examples/                6 个请求样例及其响应
tests/                   pytest 自动化测试（101 个）
RUNLOG.md                实际运行记录（命令、结果、过程中修过的问题）
```

## 5. 测试

```bash
python3 -m pytest
```

覆盖：

* 小整数问题手算对照（最优值/顶点）；
* 退化顶点、重复/成比例/被包含的冗余不等式、重复等式；
* **循环风险**：经典 Beale 问题在 Bland 规则下求到最优，
  在 Dantzig 规则下保证终止；另用 `fractions.Fraction` 的精确算术
  确定性再现 Dantzig 下的 6 步循环（不依赖浮点运气）；
* 不可行 Farkas 证书与无界射线在原问题上逐条核验；
* **40 组随机小整数 LP 与顶点枚举参考解逐一对照**（状态与目标值一致），
  随机冲突等式的证书批量核验；
* JSON 校验路径、NaN 拒绝、CLI 退出码与 stdin/文件端到端。

## 6. 已知边界

* 稠密实现，教学定位：150 变量 / 250 行量级约数秒，不追求工业性能；
* 绝对容差，系数量级在 1e6 以上时应自行放大容差或缩放问题；
* 仅支持非负变量（任意有限非负下界通过平移支持）；不支持整数变量。
