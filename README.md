# 精确 0/1 背包求解器（纯后端）

用 **Python 3 + NumPy** 实现的精确 0/1 背包（knapsack）计算库与 JSON 接口。
核心算法（分支定界 + 分数背包松弛上界）全部自行实现，无第三方求解器依赖。
限定小中规模（默认 ≤ 2000 件物品），对**整数重量、整数价值**给出可证明的
精确解；超时则返回当前可行解与**仍然有效**的上界，绝不把未闭合的间隙宣称为最优。

本项目只有后端：Python 库 + 命令行 JSON 接口，没有任何前端页面。

---

## 1. 问题定义

输入整数容量 `C` 与 n 件物品（整数重量 `w_i ≥ 0`、整数价值 `v_i`，允许负值），求

```
maximize   Σ v_i · x_i
subject to Σ w_i · x_i ≤ C,   x_i ∈ {0, 1}
```

空解（什么都不取）始终可行，目标值为 0。

## 2. 输入范围与数值容差

| 项目 | 范围 / 约定 |
|---|---|
| 物品数 `n` | `0 ≤ n ≤ 2000` |
| 容量 `capacity` | 整数，`[0, 10^12]` |
| 重量 `weights[i]` | 整数，`[0, 10^9]`（允许 **0 重量**） |
| 价值 `values[i]` | 整数，`[-10^9, 10^9]`（允许**负价值**） |
| `timeout_seconds` | 数值，`[1e-6, 300]`，缺省 `5.0` |

**整数策略（无舍入误差）**：物品重量/价值只接受真正的 JSON 整数；分支定界中
密度比较用交叉相乘（`v1·w2` vs `v2·w1`），分数上界用分子/分母的精确整数
表示，剪枝只比较整数 `floor`，全程不使用浮点。Python 整数为任意精度，
`n·v ≤ 2·10^12`，无溢出风险。

**容差**：唯一的浮点容差是展示用 `GAP_EPS = 1e-12`（见 `knapsack/validation.py`），
仅在 `relative_gap` 本应为 0 时把它显示为 `0.0`，**不参与任何求解或最优性判定**。

## 3. 失败状态语义

响应顶层 `ok` 表示请求是否合法；求解状态字段 `status` 有两种取值：

| `status` | 含义 | 保证 |
|---|---|---|
| `"optimal"` | 搜索闭合（或剩余分支上界已不能超过当前解） | `objective` 为精确最优值，`upper_bound == objective`，`relative_gap == 0` |
| `"timeout"` | 时限到达，搜索未闭合 | `objective` 是真实可行解；`upper_bound ≥ objective` 是**有效上界**；`relative_gap > 0`（除非界恰好证明最优，此时状态会升级为 `optimal`） |

**关键承诺**：只要存在未闭合的正间隙，状态一定是 `timeout`，绝不会报告 `optimal`。

输入不合法时不进行求解，返回结构化错误，CLI 退出码为 2：

```json
{"ok": false, "error": {"code": "validation_error",
  "message": "'capacity' 必须 >= 0，实际为 -5", "field": "capacity"}}
```

## 4. 算法

实现见 `knapsack/solver.py`、`knapsack/bounds.py`。

1. **预处理（均可证明安全）**
   - `w_i = 0, v_i ≥ 0`：不占容量，必取；`w_i = 0, v_i < 0`：必舍；
   - `w_i > C`：任何可行解都装不下，剔除；
   - `w_i > 0, v_i ≤ 0`：取了只挤占容量/降低目标值，剔除。
2. **初始可行解**：密度序 0/1 贪心（NumPy cumsum 向量化）与“单件最优”取较好者。
   incumbent 从第一步起就是可行解，任何时刻超时返回都合法。
3. **分支定界 DFS**：按密度精确降序（同密度按原始索引升序，保证确定性），
   每个节点的上界 = 路径已确定价值 + 后缀**分数背包 LP 松弛**；用整数 `floor`
   剪枝（`bound ≤ incumbent` 即剪）。先探“取”分支以尽快抬高 incumbent。
4. **可验证上界**：`upper_bound_numerator / upper_bound_denominator` 给出精确
   有理上界；任何使用方都可以独立核对 `objective ≤ 上界`。
5. **超时记账**：超时发生时，尚未处理的每个分支节点在**入栈时**就已带好上界，
   取这些上界（含刚被中断的节点本身）的最大值，作为未探索解空间的有效上界。

## 5. 快速开始

```bash
pip install -r requirements.txt   # 仅需 numpy

# 文件输入
python -m knapsack.cli -f examples/request_basic.json

# 标准输入
echo '{"capacity":10,"weights":[6,4,3,2],"values":[10,7,6,3]}' | python -m knapsack.cli
```

作为库使用（可直接包进 Flask/FastAPI 等 Web 框架）：

```python
from knapsack.api import solve

resp = solve({
    "capacity": 10,
    "weights": [6, 4, 3, 2],
    "values": [10, 7, 6, 3],
    "timeout_seconds": 5.0,
})
# resp["status"] == "optimal", resp["objective"] == 17, resp["selected"] == [0, 1]
```

## 6. 请求 / 响应字段

请求：

| 字段 | 类型 | 必填 |
|---|---|---|
| `capacity` | int | 是 |
| `weights` | list[int]，长度 n | 是 |
| `values` | list[int]，长度 n | 是 |
| `timeout_seconds` | float | 否（默认 5.0） |

响应（成功）：

| 字段 | 含义 |
|---|---|
| `status` | `optimal` / `timeout` |
| `objective` | 最优值或当前最好可行值 |
| `selected` | 选中物品的**原始索引**（升序、无重复） |
| `total_weight` | 所选物品总重量（≤ capacity，可独立复核） |
| `upper_bound` | 整数上界（剪枝实际使用的值） |
| `upper_bound_float` | 分数松弛上界的浮点展示 |
| `upper_bound_numerator` / `upper_bound_denominator` | 上界的精确有理表示 |
| `relative_gap` | `(UB − obj) / max(1, |UB|)`，展示用 |
| `nodes_explored` / `elapsed_seconds` | 展开节点数 / 实际耗时 |
| `forced_zero_weight` | 因 0 重量且非负价值而直接取的物品 |
| `excluded_overweight` | 因超过容量而剔除的物品 |

## 7. 请求样例

`examples/` 目录：

- `request_basic.json` — 普通实例（恰好装满）；
- `request_zero_weight.json` — 含 0 重量正负价值、超重物品；
- `request_timeout.json` — 强相关难例，1ms 时限，演示 `timeout` 与正间隙；
- `request_invalid_negative_capacity.json` — 非法容量（退出码 2）；
- `request_invalid_length_mismatch.json` — 重量/价值长度不等（退出码 2）。

## 8. 自动化测试

```bash
python run_tests.py
```

不依赖 pytest（标准库 + numpy）。15 个测试函数覆盖：

- **枚举交叉验证**：2520+ 个 n ≤ 5 穷举实例、120 个 n ≤ 16 随机实例，
  分支定界最优值必须等于 2^n 全枚举真值，且上界闭合到最优值；
- **上界有效性**：300 个随机实例，用独立的 `fractions.Fraction` LP 实现
  核对 `objective ≤ upper_bound ≤ LP 松弛`；
- **零重量 / 负价值 / 相同密度**：必取必舍规则、容量为 0、全负价值空解、
  同密度多最优解等；
- **超时语义**：超时解可行且自洽、上界有效、正间隙必为 `timeout`、
  放宽时限后达到最优且最优值不超过此前给出的上界；
- **输入校验 / CLI**：各类非法输入、边界规模（n=0、n=2000）、
  stdin/文件/非法 JSON/退出码、examples 全部样例真实跑通。

## 9. 实测数据

实际运行的命令、输出与一轮中发现并修复的缺陷，见 **[RUN_LOG.md](RUN_LOG.md)**。

## 10. 目录结构

```
knapsack/
  __init__.py     # 包入口
  validation.py   # 输入范围、容差、结构化校验
  bounds.py       # 精确分数背包（LP 松弛）上界
  solver.py       # 分支定界求解器（核心算法）
  api.py          # JSON 风格 dict 进 / dict 出接口
  cli.py          # python -m knapsack.cli 命令行
tests/
  brute_force.py          # NumPy 向量化 2^n 枚举参考实现
  test_enumeration.py     # 枚举交叉验证、零重量/负价值/同密度
  test_bounds.py          # 上界有效性
  test_timeout.py         # 超时与失败状态语义
  test_api.py             # 校验、CLI、边界规模
examples/                 # 请求样例（含超时与非法输入）
run_tests.py              # 测试运行器（无需 pytest）
requirements.txt
RUN_LOG.md                # 实际运行记录
```

## 11. 已知边界

- 设计目标为小中规模（n ≤ 2000）。强相关难例在大规模下可能无法在时限内闭合，
  此时按承诺返回可行解 + 有效上界 + `timeout`，调用方应检查 `status` 与
  `relative_gap` 决定是否接受。
- 不接受浮点重量/价值（避免经典的浮点表示误差）；如有需要应在调用方先做
  定标取整。
- 时限基于挂钟时间，超时检查在节点粒度进行（每 256 个展开节点及根节点），
  因此实际耗时可能略超 `timeout_seconds`（毫秒量级）。
