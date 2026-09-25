# blp —— 有界线性规划纯后端求解器

用 Python + NumPy 从零实现的**两阶段单纯形法**，无任何优化求解器依赖
（不使用 scipy / pulp / cvxopt）。只做计算与 JSON 接口，不含前端。

- 支持 `<=`、`>=`、`=` 约束与非负变量（可给非负下界、有限/无限上界、固定变量）
- 三种确定结论：**最优解 / 不可行 / 无界**，每种都附**可独立核验**的证据
  - 最优：逐约束残差、非负性、上界违约量
  - 不可行：Farkas 替代证书（行乘子），响应内带独立核验结果
  - 无界：可行起点 + 方向射线（给出目标变化率）
- 默认 **Bland 规则**（带有限终止保证）；另有 Dantzig / 最大下降规则，
  检测到基重复（循环）时自动回退 Bland
- 明确声明**数值容差、失败状态与输入范围**，超限显式报错而非给出假答案
- 覆盖退化、冗余约束、循环风险（Beale 1955），并有顶点枚举参考解对照测试

---

## 1. 目录结构

```
src/blp/
  errors.py      异常类型（输入非法 / 数值失败 / 超限）
  model.py       问题模型、上下界处理、等式标准形（松弛列/人工列）
  simplex.py     单纯形表、枢轴、三种入基规则、第一阶段辅助表
  solver.py      两阶段编排、解/射线/Farkas 证书提取、结果回映
  verify.py      与求解器内部无关的独立残差/证书核验
  enumerate.py   顶点枚举（暴力法，仅供测试对照，限小规模）
  jsonio.py      JSON 请求解析、求解、核验、响应组装
  cli.py         命令行入口（python -m blp.cli）
examples/        请求样例 + 对应响应样例
tests/           pytest 自动化测试（63 个用例）
```

## 2. 安装与运行

需要 Python ≥ 3.10、NumPy ≥ 1.24。无需安装也可直接用（设 `PYTHONPATH`）：

```bash
pip install numpy                 # 如系统 Python 受 PEP 668 限制：
pip install --user --break-system-packages numpy

# 直接运行（推荐，免安装）
PYTHONPATH=src python -m blp.cli examples/01_optimal_matrix.json

# 或安装为包
pip install -e .
blp examples/01_optimal_matrix.json
```

标准输入 / 输出：

```bash
cat request.json | PYTHONPATH=src python -m blp.cli
PYTHONPATH=src python -m blp.cli req.json -o resp.json --rule bland
```

退出码：

| 退出码 | 含义 |
| --- | --- |
| 0 | 已给确定结论（optimal / infeasible / unbounded） |
| 2 | 输入非法（`status=invalid_input`） |
| 3 | 数值失败（`numeric_failure`） |
| 4 | 达到迭代上限（`iteration_limit`） |

## 3. 请求 / 响应格式

### 请求

两种约束写法可混用；矩阵形式：

```json
{
  "objective": {"c": [-3, -5], "sense": "min", "constant": 0},
  "constraints": {
    "A_ub": [[1, 0], [0, 2], [3, 2]],
    "b_ub": [4, 12, 18],
    "A_eq": [],
    "b_eq": []
  },
  "variables": {"lb": 0, "ub": null},
  "verify_certificate": true
}
```

逐行形式（`sense` 可取 `"<="`、`">="`、`"="`）：

```json
{
  "objective": {"c": [1, 2], "sense": "min"},
  "constraint_rows": [
    {"name": "r1", "row": [1, 1], "sense": "=",  "rhs": 1},
    {"name": "r2", "row": [1, 0], "sense": "<=", "rhs": 0.7}
  ],
  "variables": {"lb": [0, 0], "ub": [1, 1]}
}
```

要点：

- 变量固定：`lb[j] == ub[j]`；无上界用 `null`（JSON 的 +Infinity 不通用）
- 仅支持**非负变量**：`lb` 不允许负数；`lb > ub` 不报输入错，而是正常判定
  为**不可行**（空变量框也是一个合法的不可行问题）
- `sense` 仅 `"min"` / `"max"`；最大化按 `min -c^T x` 处理
- `constant` 为目标常数项，正确参与平移，不会与变量下界重复计入

### 响应（三种结论）

**optimal**：返回 `objective`、`x`，以及与内部表无关、在原问题上重算的
`residuals`（逐行残差、`overall_max_violation`、`feasible`）。

**unbounded**：返回 `ray`（方向 d）、`ray_objective_rate`（c^T d，
min 时为负 / max 时为正）、`witness = x + d` 示意点，以及射线核验：
起点可行、`d >= 0`、`A_ub d <= 0`、`A_eq d = 0`、有上界的分量 `d_j = 0`。

**infeasible**：返回 `certificate`，含每行的 Farkas 乘子与
`independent_check`（由 `verify.py` 用重建的标准形独立计算）：

- 原始 `<=` 行乘子 ≥ 0（等式行自由）；
- 每个结构变量列上 `mu^T A >= 0`；
- 常数 `mu^T b < 0`。

若原系统可行，把非负 x 代入必然推出 `mu^T b >= 0`，与严格负矛盾。
完整推导见 `solver.py` 中 `_infeasibility_certificate` 的文档字符串。

## 4. 算法与数值约定

### 标准形与两阶段

1. 变量平移 `x = y + lb`（`y >= 0`），固定变量直接代入并记录
2. `<=` / 上界行加非负松弛列；`>=` 在解析层取反为 `<=`
3. 等式行与负右端不等式行加人工变量；负右端行整体取反保证 `b >= 0`
4. **第一阶段**最小化人工变量之和：最优值 > 容差即不可行
   （此时构造 Farkas 证书）；=0 则把人工变量驱出基、删除冗余零行
5. **第二阶段**换入原目标行，从第一阶段末的基继续求解

单纯形表：前 m 行是典范等式约束，最后一行为目标行，Gauss–Jordan 枢轴。
检验数与右端的符号约定在 `simplex.py` 模块头注释中完整写明，目标行
随枢轴同步消元。

### 枢轴与防循环

- `bland`（默认）：最小合格下标入基；最小比值并列时选最小基列下标出基
- `dantzig`：最正检验数入基
- `largest_decrease`：按"一步目标下降量"估值入基
- 每次迭代对**排序后的基集合**做哈希查重：同一基再次出现即判定循环。
  Bland 下出现重复基视为数值崩溃（`numeric_failure`）；其他规则自动用
  Bland 整题重试，并在 `warnings` 中记录。

### 数值容差（`model.TOL`，集中定义，禁止散落魔数）

| 名称 | 值 | 用途 |
| --- | --- | --- |
| `zero` | 1e-12 | 判定结构零元素 |
| `pivot` | 1e-10 | 枢轴绝对值下限，低于即认为基奇异 |
| `feas` | 1e-8 | 可行性 / 残差违约容差 |
| `reduced` | 1e-8 | 检验数最优性容差（另加 1e-10 的相对项） |
| `rhs_negative` | 1e-7 | 枢轴后右端允许为负的程度 |
| `cert` | 1e-7 | Farkas 证书核验容差 |

### 输入范围（显式声明，超出即 `invalid_input`）

| 项目 | 上限 |
| --- | --- |
| 变量数 | 200 |
| 约束行数 | 200 |
| 标准化后总列数 | 1000 |
| 系数 / 右端 / 界绝对值 | 1e9 |
| 单阶段迭代数 | 10000（超过为 `iteration_limit`） |

不接受 NaN / Infinity 系数（无上界用 `null`）；不接受布尔值混充数字。

## 5. 失败状态语义

- `optimal` / `infeasible` / `unbounded`：确定结论，证据已随响应给出
- `invalid_input`：问题描述不合法（维度、类型、范围、未知符号）
- `numeric_failure`：枢轴过小、基矩阵奇异、右端失去可行性、
  证书自核验不过——**不猜测答案**
- `iteration_limit`：迭代超限；非 Bland 规则下若先检测到循环会自动重试

## 6. 作为 Python 库使用

```python
import numpy as np
from blp import make_lp, solve_lp
from blp.verify import solution_residuals

lp = make_lp(
    c=[-3, -5],
    A_ub=[[1, 0], [0, 2], [3, 2]], b_ub=[4, 12, 18],
)
r = solve_lp(lp)                 # SolveResult(status, objective, x, ...)
print(r.status, r.objective, r.x, r.iterations)

from blp.jsonio import run_request
resp = run_request({...})        # 直接拿可 JSON 序列化的 dict
```

## 7. 测试与如实运行记录

```bash
python -m pytest tests/ -q
```

测试内容：

- `test_basic_lp.py`：经典小整数问题、等式/不等式/上下界/固定变量、
  三种终态、目标常数
- `test_degeneracy.py`：高度退化顶点、重复/蕴含/矛盾的冗余约束、
  零行约束、Beale 循环例（Bland 终止、Dantzig 检出循环并回退、
  底层第 6 步精确复现基重复）、随机退化题 Bland 不重复基
- `test_enumeration.py`：手写用例与 60 组随机小整数题，逐一与
  **顶点枚举参考解**对照最优值与解点；枚举器自身的顶点数、可行性
- `test_json_api.py`：两种约束写法、max、界、固定变量、空框、
  证书/射线核验、输入校验、规模上限、CLI 退出码与标准输入输出
- `test_random_stress.py`：400 组随机题三种状态全覆盖并全部独立核验；
  另跑 3000 组题的一次性模糊检查（1264 最优 / 1327 不可行 /
  409 无界，0 失败）；50×60 中等规模性能与近病态约束

> 3000 题模糊测试不是 pytest 的一部分（耗时数秒），命令记录见
> `RUNLOG.md`。

## 8. 已知边界与取舍

- 面向小中规模稠密问题（表格式 Gauss–Jordan 为 O(m²n)/枢轴）；
  不追求大规模稀疏性能，超规模直接拒绝
- 未做：灵敏度分析、内点法、整数规划、自由变量（允许负的变量）
- 极端条件数问题（系数跨 ~9 个数量级以上）可能落入
  `numeric_failure`，此时返回明确状态而非不可信答案
