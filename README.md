# 精确背包求解（exact-knapsack）

纯后端 0/1 背包精确求解项目：Python + NumPy 计算库 + JSON 接口，无前端。
核心算法为自行实现的分支定界（branch and bound），上界采用线性规划松弛
（分数背包）并以**精确有理数**计算，保证上界可验证。

## 问题定义

```
maximize   sum(value_i * x_i)
subject to sum(weight_i * x_i) <= capacity
           x_i in {0, 1}
```

重量、价值、容量均为**整数**。允许零重量物品与负价值物品（见下文语义）。

## 安装与运行

```bash
pip install -r requirements.txt   # numpy, pytest
python -m pytest -q               # 运行自动化测试
python -m knapsack examples/request_small.json   # 运行示例请求
cat examples/request_small.json | python -m knapsack   # 或从标准输入读取
```

库调用：

```python
from knapsack import solve, solve_request

result = solve([3, 4, 5], [6, 7, 9], capacity=8)
print(result.status, result.value, result.upper_bound)  # optimal 15 15

resp = solve_request({"capacity": 8,
                      "items": [{"id": "a", "weight": 3, "value": 6}]})
```

## JSON 接口

### 请求

```json
{
  "capacity": 15,
  "items": [
    {"id": "laptop", "weight": 5, "value": 12},
    {"id": "camera", "weight": 3, "value": 7}
  ],
  "options": {"time_limit_ms": 5000, "max_nodes": 200000}
}
```

| 字段 | 类型 | 约束 | 默认 |
|---|---|---|---|
| `capacity` | 整数 | 0 .. 10^12 | 必填 |
| `items` | 数组 | 长度 0 .. 500 | `[]` |
| `items[].weight` | 整数 | 0 .. 10^12（允许 0） | 必填 |
| `items[].value` | 整数 | -10^12 .. 10^12（允许负值） | 必填 |
| `items[].id` | 字符串 | 唯一 | `"item-<下标>"` |
| `options.time_limit_ms` | 整数 | 1 .. 60000 | 5000 |
| `options.max_nodes` | 整数 | 1 .. 5,000,000 | 200,000 |

所有数值必须是 JSON 整数；布尔值与浮点数（如 `1.0`）一律拒绝，返回
`error`，不做隐式取整。

### 响应

```json
{
  "status": "optimal",
  "value": 33,
  "upper_bound": 33,
  "gap": 0,
  "selected_ids": ["laptop", "camera"],
  "selected_indices": [0, 1],
  "total_weight": 8,
  "capacity": 15,
  "nodes": 11,
  "elapsed_ms": 0.21,
  "message": "proved optimal: branch-and-bound tree fully explored, gap closed to 0"
}
```

### 状态与失败语义

| `status` | 含义 |
|---|---|
| `optimal` | 分支定界树完全展开，间隙闭合（`gap == 0`），`value` 已证明最优 |
| `feasible` | 超时或达到节点上限，间隙未闭合（`gap > 0`）。返回当前最好可行解 `value`（下界）与仍有效的全局上界 `upper_bound`，**绝不声称最优** |
| `error` | 输入不合法，`error.message` 说明具体字段与约束 |

不变式（任意状态下均成立，且有测试保证）：

```
upper_bound >= 真实最优值 >= value
total_weight <= capacity
```

## 数值容差策略

- 决策路径上**没有浮点运算**：可行性、最优性比较全在整数域进行。
- 上界用 `fractions.Fraction` 以精确有理数计算；整数上界取
  `floor(松弛值)`。因最优值必为整数，floor 后上界依然有效。
- 剪枝条件 `bound <= best` 为精确比较，无 epsilon。
- 因此本库不定义浮点容差：容差问题在设计上被消除，而非被调参掩盖。
  唯一的“间隙”概念是整数间隙 `upper_bound - value`，闭合判据为 `== 0`。

## 算法

1. **预处理**：零重量正价值物品必取（计入基础价值）；零重量非正价值、
   正重量非正价值物品永不取（空解恒可行，它们只会消耗容量）。剩余物品
   均为正重量正价值，密度为有限正数。若剩余物品总重量不超过容量，直接
   取完即为最优。
2. **排序**：按密度 `value/weight` 降序（`Fraction` 精确比较；等密度物品
   顺序不影响正确性，稳定排序保证可复现）。
3. **分支定界**：显式栈 DFS，优先探索“取”分支。每个子节点的上界用
   前缀和 + 二分在 O(log n) 内算出（Dantzig 分数背包界），入栈前即剪枝。
4. **提前终止**：每次迭代检查节点上限，按间隔检查时钟。终止时全局上界
   为 `max(当前最优, 栈中全部未探索节点的上界)`，该值对剩余搜索空间仍然
   有效，随当前可行解一并返回。

## 验收与测试

`tests/` 共 116 项测试，要点：

- **小规模枚举对照**（`TestAgainstBruteForce`，100 个参数化用例）：
  随机实例（重量含 0、价值含负数、小取值域制造大量等密度物品）下，
  求解器最优值与暴力枚举逐一相等，且上界 >= 枚举最优值。
- **边界情形**：零重量（正/零/负价值）、负价值、等密度、容量为 0、
  空物品集、全部装得下。
- **提前终止**：节点上限/超时触发时状态必为 `feasible`、间隙必大于 0、
  上界仍 >= 枚举最优值、返回解仍可行；未闭合间隙绝不报告 `optimal`。
- **接口校验**：非法类型、越界、重复 id、浮点数/布尔值等均返回 `error`。

## 实际运行记录（2026-09-23，本机 Python 3.12.3 / NumPy 2.5.3 / pytest 9.1.1）

```
$ python3 -m pytest -q
116 passed in 2.59s
```

首次运行曾有 1 项失败（`test_node_limit_returns_feasible_with_valid_bound`）：
节点上限只在每 512 个节点检查一次，小实例在检查前就已解完，导致
`max_nodes=1` 时仍返回 `optimal`。已修复为每次迭代检查节点上限，修复后
全部通过。

示例请求（`examples/`）实测：

```
$ python3 -m knapsack examples/request_small.json
status=optimal  value=33  upper_bound=33  gap=0   nodes=11

$ python3 -m knapsack examples/request_edge_cases.json
status=optimal  value=21  upper_bound=21  gap=0   nodes=9
（零重量正价值物品被取，负价值物品被排除，等密度物品正确处理）

$ python3 -m knapsack examples/request_timeout.json   # time_limit_ms=1, max_nodes=50
status=feasible  value=601  upper_bound=653  gap=52  nodes=50
```

中规模性能实测（随机实例，容量取总重量的 40%，时限 10s）：

| n | 结果 | 节点数 | 耗时 |
|---|---|---|---|
| 100 | optimal，gap=0 | 91,817 | 0.67 s |
| 200 | feasible，gap=590（相对上界约 0.8%） | 1,495,040 | 10 s（超时） |
| 500 | feasible，gap=595（相对上界约 0.3%） | 1,757,184 | 10 s（超时） |

## 规模限定与已知限制

- 定位为小中规模：`n <= 500`。n 在 100 左右通常可秒级证明最优；更大规模
  或难实例（容量接近总重量一半、密度接近）可能在时限内无法闭合间隙，
  此时按设计返回 `feasible` + 有效上界。
- 上界为单约束 LP 松弛界，未实现更强的界（如核心问题分解、surrogate
  relaxation），这是有意的范围裁剪。
- 仅支持单约束 0/1 背包；不支持多维、有界（重复选取）变体。
- 暴力枚举器 `knapsack.brute` 仅供验收对照，限 `n <= 22`。

## 目录结构

```
knapsack/
  __init__.py    # 包导出
  __main__.py    # CLI：python -m knapsack request.json
  solver.py      # 分支定界求解器（精确有理数上界）
  validate.py    # 输入范围校验
  api.py         # JSON 请求/响应接口
  brute.py       # 暴力枚举（验收对照用）
tests/           # pytest 测试（116 项）
examples/        # 三个示例请求：常规 / 边界 / 超时
requirements.txt
pytest.ini
```
