# mospp —— 双目标最短路（时间 / 费用）纯后端服务

`mospp`（multi-objective shortest path problem）是一个只依赖 Python 与
NumPy 的双目标最短路计算库 + JSON 命令行接口。给定有向图上每条边的
**非负**时间与费用，计算从 `source` 到 `target` 的 Pareto 最优路径集合
（非支配标签集），并保证每条返回路径都可以重建、独立校验。

无前端、无网络服务框架、无第三方求解器；核心标签修正算法为自行实现。

---

## 1. 目录结构

```
mospp/
  __init__.py          # 包入口
  errors.py            # 错误码、输入规模硬限制、异常类型
  tolerance.py         # 数值容差 + 支配关系 + O(N log N) Pareto 过滤
  graph.py             # 有向图内部结构（NumPy 数组 + 邻接表）
  labeling.py          # 核心：双目标标签修正（label-setting）算法
  enumeration.py       # 简单路径暴力枚举（测试/校验用）
  api.py               # JSON 请求校验、求解、响应构造
  __main__.py          # 命令行接口（python -m mospp）
examples/              # 请求样例与实际响应（examples/output/）
tests/                 # unittest 自动化测试（61 个用例）
requirements.txt
RUNLOG.md              # 实际运行记录（命令、结果、已修复的问题）
```

---

## 2. 快速开始

```bash
pip install -r requirements.txt        # 只需要 numpy

python -m mospp examples/request_basic.json
# 或从标准输入：
cat examples/request_basic.json | python3 -m mospp
# 紧凑输出：
python -m mospp --compact examples/request_basic.json
```

退出码：

| 退出码 | 含义 |
|---:|---|
| 0 | 请求处理成功。`status` 为 `ok` / `no_path` / `truncated` 都算成功 |
| 2 | 请求校验失败（JSON 语法错误、字段错误、超过规模限制等），错误写到 stderr |
| 1 | 其他内部错误（如请求文件无法读取） |

库调用：

```python
from mospp import run_request
resp = run_request({
    "graph": {
        "nodes": ["A", "B", "D"],
        "edges": [
            {"source": "A", "target": "B", "time": 1, "cost": 4},
            {"source": "B", "target": "D", "time": 3, "cost": 1},
            {"source": "A", "target": "D", "time": 8, "cost": 2},
        ],
    },
    "source": "A", "target": "D",
})
```

---

## 3. 请求格式

```json
{
  "graph": {
    "nodes": ["A", "B"],
    "edges": [
      {"source": "A", "target": "B", "time": 3.0, "cost": 5.0, "id": "e1"}
    ]
  },
  "source": "A",
  "target": "B",

  "mode": "simple",
  "time_budget": 10.0,
  "cost_budget": 8.0,
  "node_label_cap": 10000,
  "total_label_cap": 500000,
  "atol": 1e-9,
  "rtol": 1e-9,
  "verify_paths": true
}
```

只有 `graph`、`source`、`target` 必填；任何**未知字段一律拒绝**。

### 3.1 字段说明

| 字段 | 类型 | 默认 | 范围 / 约束 |
|---|---|---|---|
| `graph.nodes` | 非空数组 | 必填 | 元素为非空字符串（长度 ≤ 64）或整数；不可重复。最多 **2000** 个 |
| `graph.edges[].source/target` | 同节点 id | 必填 | 必须出现在 `nodes` 中 |
| `graph.edges[].time/cost` | 数值 | 必填 | 有限数、**非负**、≤ `1e9`；不接受字符串、`NaN`、`±Infinity` |
| `graph.edges[].id` | 字符串/整数 | 无 | 给出则必须在所有边中唯一 |
| `source` / `target` | 节点 id | 必填 | 必须在 `nodes` 中；二者允许相同（返回权重 (0,0) 的平凡路径） |
| `mode` | `"simple"` / `"walk"` | `"simple"` | 见 [第 6 节](#6-简单路径与循环处理) |
| `time_budget` / `cost_budget` | 非负数值 | 无限制 | 可选；超出预算的路径被剪枝，边界值含容差包含 |
| `node_label_cap` | 整数 ≥ 1 | `10000` | 每个节点的永久标签数上限 |
| `total_label_cap` | 整数 ≥ 1 | `500000` | 全图永久标签总数上限 |
| `atol` / `rtol` | 数值 | `1e-9` / `1e-9` | 允许范围 `[0, 1e-2]` |
| `verify_paths` | 布尔 | `true` | 响应前独立重建并校验每条路径 |

图允许：重边（平行边）、自环、零权边、零权环。`simple` 模式额外限制
**节点 ≤ 64、边 ≤ 400**（visited 集合状态数随图密度组合增长）；
超过该规模请使用 `mode="walk"`（2000 节点 / 20000 边以内）。
同一对节点间平行边最多 50 条。

### 3.2 成功响应

```json
{
  "status": "ok",
  "request_echo": { ... 回显关键参数 ... },
  "pareto_front": [
    {
      "rank": 0,
      "time": 4,
      "cost": 5,
      "nodes": ["A", "B", "D"],
      "edges": [
        {"source": "A", "target": "B", "index": 0},
        {"source": "B", "target": "D", "index": 0}
      ],
      "equal_paths": [ {"nodes": [...], "edges": [...]} ],
      "equal_path_count": 2
    }
  ],
  "truncated": false,
  "truncation": [],
  "statistics": {
    "labels_created": 6,
    "labels_pushed": 6,
    "labels_popped": 6,
    "labels_rejected": 1,
    "labels_permanent": 5,
    "edges_scanned": 5,
    "budget_pruned": 0,
    "nodes_reached": 4,
    "permanent_label_total": 5,
    "pareto_front_size": 2,
    "paths_verified": true,
    "paths_verification_ok": true
  }
}
```

* `pareto_front` 按 `time` 升序排列，每个目标向量只出现一次。
* 边的表示：提供了 `id` 就返回 id；否则返回
  `{"source","target","index"}`，其中 `index` 是该 u→v 平行边中的序号（从 0 起）。
* **相等标签**：若多条拓扑不同的路径实现同一个 Pareto 点（时间、费用
  在容差内相等），第一条放在 `nodes/edges`，其余放在
  `equal_paths`，并给出 `equal_path_count`。只有 `simple` 模式会出现
  多条；`walk` 模式中等价走法在算法内部折叠为重复标签。
* 整数权重以整数输出，浮点权重保留 12 位小数。

### 3.3 状态（status）与失败状态

| `status` | 含义 |
|---|---|
| `ok` | 求解完整，`pareto_front` 是精确 Pareto 前沿（含全部等权最优简单路径，simple 模式） |
| `no_path` | 图中不存在满足条件的路径（含预算过紧、不连通）；`pareto_front` 为空 |
| `truncated` | 触发了标签上限，前沿**可能不完整**；`truncation` 给出截断位置与原因 |

> **必须检查 `truncated`。** `truncated=true` 时已返回的点仍然有效
> （已经过支配校验和路径重建），但不保证覆盖全部 Pareto 点。

错误响应（HTTP 无关，进程写到 stderr、退出码 2）：

```json
{"error": "negative_weight",
 "message": "graph.edges[0].time 必须非负，得到 -2.0（本项目只支持非负时间/费用）",
 "field": "graph.edges[0].time"}
```

错误码一览：

| `error` | 触发条件 |
|---|---|
| `invalid_json` | 请求体不是合法 JSON（仅 CLI 层） |
| `invalid_request` | 请求体不是对象、布尔参数类型错误等 |
| `missing_field` | 缺少必填字段 |
| `unknown_field` | 出现未声明字段（严格拒绝，防止拼写错误） |
| `invalid_graph` / `invalid_edge` | `nodes`/`edges` 结构错误 |
| `duplicate_node` / `duplicate_edge_id` | 节点 id 或边 id 重复 |
| `unknown_node` | 边或起终点引用了未声明的节点 |
| `invalid_id` | 节点/边 id 不是非空字符串或整数、超长 |
| `invalid_weight` | 权重不是数值、`NaN`/`±Infinity`、超过 `1e9` |
| `negative_weight` | 时间或费用为负 |
| `invalid_budget` | 预算为负或非数值 |
| `invalid_tolerance` | 容差不在 `[0, 1e-2]` |
| `invalid_cap` | 标签上限不是 ≥ 1 的整数 |
| `invalid_mode` | `mode` 不是 `simple`/`walk` |
| `limit_exceeded` | 超过节点/边/平行边/simple 模式规模硬限制 |
| `io_error` | 请求文件无法读取（仅 CLI 层） |

`truncation[].reason`：`node_label_cap`（某节点永久标签达到上限，
后续标签被丢弃）或 `total_label_cap`（全局上限触发，搜索提前终止）。

---

## 4. 数值容差

比较采用逐元素容差：

```
tol(x, y) = atol + rtol * max(|x|, |y|)
```

* `|x - y| <= tol`：该维视为**相等（中性）**，既不更优也不更差；
* `x - y > tol`：x 在该维严格更差；`y - x > tol`：x 严格更优；
* 支配（dominates）：两维都不劣 **且** 至少一维严格更优。

因此容差带内“几乎相等”的两个标签互不支配，都会被保守保留——
宁可多返回一个近邻点，也不会因为浮点误差丢掉真实的 Pareto 点。
`atol`/`rtol` 默认 `1e-9`，最大允许 `1e-2`。预算比较也使用同一容差
（预算边界是含容差包含的：路径权重 `<= budget + tol(budget)`）。

---

## 5. 算法说明

### 5.1 双目标标签修正（label-setting）

每条标签保存 `(time, cost, node, predecessor, edge, visited)`。
标签以字典序 `(time, cost)` 进入最小堆。由于两维权重均非负：

* 堆中标签的时间单调不减；
* 一个标签被弹出时，其**目标向量**不可能被以后弹出的标签支配
  （后者的时间不会更小）；
* 故“弹出即永久”，得到标签设定算法，而不是需要反复扫描的标签校正。

同一节点上标签之间的状态支配判据（`simple` 模式）：

1. 候选 B 被永久标签 A **严格支配**（两维不劣、一维严格更优）
   **且** `visited(A) ⊆ visited(B)`：B 的任何简单延伸能用的下一节点
   集合（`V \ visited(B)`）是 A 可选集合的子集，故 B 的每条可行延伸
   A 都能走且目标更优——安全剪枝，不丢 Pareto 点，也不丢实现
   Pareto 点的路径；
2. 目标相等且 `visited` 完全相同：重复状态，剪枝；
3. 目标相等但 `visited` 不同（包括严格子集、不可比）：**全部保留**。
   这是刻意的保守策略：严格子集剪枝虽然仍保证 Pareto *点集* 精确，
   但会丢掉部分实现同一点的等权最优简单路径。为满足
   “小图枚举简单路径逐条对照 Pareto 前沿 / 覆盖相等标签”的验收要求，
   这里选择枚举意义上的完整覆盖。

最终在终点永久标签上做目标级 Pareto 过滤（O(N log N) 的顺序扫描，
避免 O(N²) 内存；见 `tolerance.pareto_mask`），并将容差内等权的
拓扑不同路径归入同一前沿点的 `equal_paths`。

### 5.2 预算剪枝与完整性

给定时间/费用预算时，先在**反向图**上分别对时间、费用跑 Dijkstra
（非负权重，零权边直接支持），得到每个节点到终点的单目标下界
`lb_time[u]`、`lb_cost[u]`。扩展标签 `(t,c)@u` 时，若

```
t + lb_time[u] > time_budget + tol   或   c + lb_cost[u] > cost_budget + tol
```

则该标签不可能在预算内到达终点，安全剪枝。由于下界是真实最短距离，
剪枝不会删除任何可行的 Pareto 路径（测试中对大量“边界预算”做了
枚举对照，见 `tests/test_algorithm.py`）。

### 5.3 标签上限与截断报告

* `node_label_cap`（默认 10000）：单个节点保留的永久标签数；
  达到上限后该节点后续弹出的标签被丢弃并计数；
* `total_label_cap`（默认 500000）：全图永久标签总数；达到后立即
  停止搜索。

任一上限触发都会令 `truncated=true`、`status="truncated"`，
`truncation` 中按节点给出 `{node, reason, dropped_labels}`。
此时已经返回的点仍然经过支配校验、可安全使用，但集合可能不完整。

---

## 6. 简单路径与循环处理

本项目支持两种模式：

### `mode="simple"`（默认，小中规模图）

每条标签维护已访问节点集合 `visited`，扩展时跳过已在集合中的后继，
从根本上禁止任何环。输出的每条路径都是**简单路径**（节点不重复，
响应中会二次校验这一性质）。由于 `(节点, visited集合)` 状态数随图
密度组合增长，限定节点 ≤ 64、边 ≤ 400。

### `mode="walk"`（较大图）

不维护 `visited`，标签可以包含环。由于时间、费用均非负：

* 正权环只会增大目标值，走环的走法被去掉环后的路径严格支配；
* 零权环产生的标签与已有标签目标相等，在同节点状态支配检查中作为
  **重复标签**被丢弃，因此算法仍然必然终止（每节点只保留有限个
  互不支配的标签）；
* 任意走法的 Pareto 目标点集与其简单路径的 Pareto 点集完全一致。

因此 walk 模式得到的前沿**目标点**是精确的；区别仅在于它不保证枚举
出实现同一点的多条等权简单路径（重复走法内部折叠），且不维护 visited。

---

## 7. 路径重建与校验

每条标签保存 `(前驱标签 id, 到达边 id)`，`labeling.reconstruct`
沿前驱链回溯得到顺序的节点序列与边序列。响应构造时
（`verify_paths=true`，默认）对主路径和每条 `equal_paths`：

1. 检查首节点 = source、尾节点 = target；
2. 检查边数 = 节点数 − 1，且每条边的首尾与相邻节点衔接；
3. 重新累加边上的时间、费用，与标签声明的目标向量在容差内比对；
4. simple 模式额外检查节点无重复。

任何不一致都会令 `statistics.paths_verification_ok=false`
（若断言失败则抛出错误而不是静默返回坏路径）。

---

## 8. 自动化测试

```bash
python3 -m unittest discover -s tests -t . -v
```

测试覆盖：

* 容差与支配的单元语义（严格、相等、容差中性、近邻保留）；
* O(N log N) Pareto 过滤器对标量 O(N²) 语义的等价性；
* 手工小图（菱形图、平行边、不连通、source==target、自环）；
* **相等标签**：等权但拓扑不同的路径都在 `equal_paths` 中；
* **零权环 / 全零图 / 正权自环**：两种模式下结果正确且终止；
* **预算剪枝**：边界预算（取真实路径权重）与暴力枚举完全一致；
* 120+ 组随机小图：simple/walk 两种模式的 Pareto 点集与
  DFS 全简单路径枚举一致，simple 模式还逐条检查路径集合覆盖；
* **截断**：极小 `node_label_cap` / `total_label_cap` 时
  `status="truncated"` 且 `truncation` 报告节点、原因与丢弃数；
* JSON 校验的全部失败状态（负权、NaN/Infinity、未知字段、
  重复 id、未知节点、坏容差/预算/上限、规模限制等）；
* CLI 端到端（文件、stdin、`--compact`、退出码 0/1/2）。

暴力枚举器（`mospp.enumeration`，仅测试/校验用途）用带 visited 的
DFS 枚举全部简单路径，限制 24 节点 / 200 边 / 200000 条路径，
超限抛出 `EnumerationLimit`。

实际运行结果（含开发过程中发现并修复的问题）见
[`RUNLOG.md`](RUNLOG.md)。

## 9. 样例一览

| 文件 | 说明 |
|---|---|
| `examples/request_basic.json` | 基本双目标图，2 个 Pareto 点 |
| `examples/request_zero_cycle.json` | 零权双向环 + 零权自环 |
| `examples/request_budget.json` | 时间 + 费用双预算剪枝 |
| `examples/request_walk_mode.json` | walk 模式（8 节点示例，可用于更大图） |
| `examples/request_truncated.json` | 人为把 `node_label_cap` 设为 3，观察截断报告 |
| `examples/request_invalid_negative.json` | 负权请求（退出码 2） |
| `examples/output/*.response.json` | 上述样例的实际运行响应存档 |

## 10. 适用范围与局限

* 仅支持**非负**时间/费用；负权请求直接被拒绝（`negative_weight`）。
* 目标维度固定为 2（时间、费用）。
* 面向小中规模精确求解：simple ≤ 64 节点 / 400 边；
  walk ≤ 2000 节点 / 20000 边。双目标 Pareto 前沿本身可能指数级大，
  此时请通过标签上限换取部分结果并依据 `truncated` 处理。
* 无并发服务、无 HTTP、无前端——纯计算库 + 标准输入/文件 JSON 接口。
