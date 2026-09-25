# mospp — 多目标（双目标）最短路纯后端

一个 **纯后端** 的双目标最短路计算库与 JSON 接口。每条边带两个 **非负** 权重：
`time`（时间）与 `cost`（费用）。给定源点 `source` 与终点 `target`，求出全部
**非支配（Pareto 最优）简单路径**：不存在另一条路径在时间和费用上都不更差、
且至少一维严格更优。

* 语言：Python 3.10+（开发环境 3.12），仅依赖 NumPy。
* 核心算法：**自行实现** 的多目标标签算法（label-setting，广义 Dijkstra +
  每顶点非支配标签集），不调用任何最短路第三方库。
* 接口：纯 JSON dict 的函数 `solve_request()` + 命令行 `python -m mospp`。
* **不含任何前端 / Web 服务**。
* 面向小、中规模图（规模边界见下文“输入范围与限制”）。

---

## 1. 快速开始

```bash
# 安装依赖（仅 numpy）
pip install -r requirements.txt

# 求解一个请求文件，结果以 JSON 打到 stdout
python -m mospp examples/01_basic.json

# 或从标准输入
cat examples/01_basic.json | python -m mospp
```

库调用：

```python
from mospp import solve_request
resp = solve_request({
    "nodes": ["A", "B", "C", "D"],
    "edges": [
        {"from": "A", "to": "B", "time": 1, "cost": 4},
        {"from": "B", "to": "D", "time": 1, "cost": 1},
        {"from": "A", "to": "D", "time": 5, "cost": 0},
    ],
    "source": "A", "target": "D",
})
print(resp["status"])          # ok
print(resp["pareto"])          # [{'time': 2.0, 'cost': 5.0}, {'time': 5.0, 'cost': 0.0}]
```

---

## 2. 算法说明

### 2.1 标签

每个顶点 `v` 维护一个 **非支配标签集**。标签

```
(time, cost, visited, pred, arc)
```

表示源点到 `v` 的一条路径：

| 字段 | 含义 |
| --- | --- |
| `time` / `cost` | 路径上两维权重的累计和（非负） |
| `visited` | 路径已访问顶点的位掩码，用于强制 **简单路径** |
| `pred` / `arc` | 前驱标签与最后一条边，供事后 **重建路径** |

用最小堆按 `(time, cost, id)` 顺序弹出标签（时间优先的广义 Dijkstra 顺序），
沿出边扩展。权重非负，扩展只会使两维不减。

### 2.2 简单路径与环（含零权环）的处理

本实现限定输出 **简单路径（顶点不重复）**：扩展到已在 `visited` 中的顶点会被
直接跳过（计入 `stats.labels_cycle_skipped`）。因此：

* **零权环不会导致算法不终止** —— 任何“绕一圈回到已访问顶点”的扩展都被挡住；
* 零权环也不会凭空制造新的 Pareto 点（绕环不改变权重，只重复顶点）；
* 不输出含环路径，也不采用“允许有限次环”的松弛语义。非负权下环永远不会改善
  任一目标，因此简单路径已覆盖所有 Pareto 最优 *权重*。

### 2.3 支配规则（同顶点两标签 a、b）

1. **严格权重支配（与 visited 无关）**：若 `a` 在容差内一维严格小、另一维不大，
   则淘汰 `b`。同顶点标签拥有相同出边集合，`a` 权重严格更优时，其任意非负延伸
   都严格更优，故安全。
2. **权重容差内相等**：若两条路径的 **顶点序列相同** 视为同一路径（重复，只
   保留一个）；否则作为不同走法的“相等标签” **同时保留**——即使它们访问的
   顶点集合相同：同样若干顶点的不同排列（如 `0-2-1-3` 与 `0-2-3-1`）也是不同
   的简单路径。这里 **不做** “visited 子集剪枝”，因为本实现要枚举全部不同的
   最优简单路径，超集标签可能经由子集标签不能走的边形成一条同权但不同走法的
   Pareto 路径（多个零权路径即如此）。

> **路径身份 = 顶点序列。** 端点相同的平行边在顶点序列层面不可区分，默认只
> 保留一条（具体走的是哪条弧可在响应 `edges[].key` 中辨识）；这与“简单路径”
> 的标准定义及交叉验证枚举器一致，也避免解因平行边而重复膨胀。

终点标签不再延伸，收尾时再按 **纯权重** 做一次 Pareto 过滤（过程中因前缀
`visited` 互不包含而共存的标签，到终点可能一个严格支配另一个）。

### 2.4 数值容差（tolerance）

权重从 JSON 读入为 IEEE-754 float64，累计求和存在舍入误差，因此支配判定不用
严格的 `<=`，而用“容差带”。对两对标量 `(t1,c1)`、`(t2,c2)` 定义尺度

```
s = max(1, |t1|, |t2|, |c1|, |c2|)
tol = eps * s          # 默认 eps = 1e-9
```

* `a 在容差内 ≤ b`  ⇔ 两维都 `<= 对方 + tol`；
* `a 在容差内严格 < b` ⇔ 至少一维 `< 对方 - tol`；
* 两方向都没有“超出容差带”的维度 ⇒ 视为 **权重相等**。

这是绝对 + 相对混合容差：权重在 1 附近时带约 1e-9，在 1e6 量级时带约 1e-3。
可用请求字段 `eps` 调整（范围 `[0, 1e-2]`，默认 `1e-9`）。

### 2.5 预算剪枝

可选 `time_budget` / `cost_budget`（非负）。扩展后任一维超出预算（含容差带）
的标签直接不入集，计入 `stats.pruned_by_budget`。预算边界按“容差内满足”处理，
即恰好等于预算视为可行。

### 2.6 标签上限与截断

`label_cap` 限制求解过程中创建的标签总数（含源标签），默认 `200000`。达到上限
立即停止扩展，返回当前已找到的全部终点标签，同时：

* `status = "truncated"`；
* `stats.truncated = true`，并回报 `stats.label_cap`、`labels_created` 等。

**截断时 Pareto 前沿可能不完整**，调用方必须据 `status` / `stats.truncated`
自行判断是否接受该部分结果。这是显式报告，绝不静默截断。

---

## 3. 请求格式（JSON 对象）

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `nodes` | array | 否 | 顶点 ID 列表，元素为字符串或整数（不可为布尔）；省略则由边自动收集 |
| `edges` | array | 是 | 边对象数组，见下 |
| `directed` | bool | 否 | 默认 `true`；`false` 时每条边展开为方向相反、同权的两条有向弧 |
| `source` | string/int | 是 | 源点，必须存在于图中 |
| `target` | string/int | 是 | 终点，必须存在于图中；允许 `source == target` |
| `time_budget` | number | 否 | 时间预算，非负有限数 |
| `cost_budget` | number | 否 | 费用预算，非负有限数 |
| `label_cap` | int | 否 | 标签上限，`[1, 2_000_000]`，默认 `200000` |
| `eps` | number | 否 | 数值容差，`[0, 1e-2]`，默认 `1e-9` |

边对象：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `from` / `to` | string/int | 是 | 端点 |
| `time` / `cost` | number | 是 | 非负有限数 |
| `key` | any | 否 | 边标识，用于区分平行边并在重建结果中原样返回；省略时取边下标 |

允许自环与平行边。自环在简单路径语义下不会出现在结果中。

### 输入范围与限制（硬边界）

| 项 | 上限 / 范围 | 超限返回 |
| --- | --- | --- |
| 顶点数 | ≤ 2000 | `limit_exceeded` |
| 边数（无向图展开前） | ≤ 10000 | `limit_exceeded` |
| 单条边权重 | `0 ≤ w ≤ 1e9`，且有限（非 NaN/Inf） | 负/非有限→`invalid_request`；超上限→`limit_exceeded` |
| 预算 | 非负有限，≤ 1e9 | `invalid_request` / `limit_exceeded` |
| `label_cap` | `[1, 2_000_000]`，默认 `200000` | `limit_exceeded` |
| `eps` | `[0, 1e-2]` | `invalid_request` |

> 双目标 Pareto 路径数随图结构可能指数增长。以上边界是 *输入规模* 护栏；
> 真正决定运行时间的是标签数，必要时用 `label_cap` 收敛并处理 `truncated`。

---

## 4. 响应格式

成功 / 结果类响应（`ok`、`truncated`、`unreachable`）：

```jsonc
{
  "status": "ok",
  "paths": [
    {
      "nodes": ["A", "B", "D"],
      "edges": [
        {"from": "A", "to": "B", "time": 1.0, "cost": 4.0, "key": 0},
        {"from": "B", "to": "D", "time": 1.0, "cost": 1.0, "key": 1}
      ],
      "time": 2.0,
      "cost": 5.0
    }
  ],
  "pareto": [{"time": 2.0, "cost": 5.0}, {"time": 5.0, "cost": 0.0}],
  "stats": { "labels_created": 6, "labels_accepted": 4, "labels_rejected": 1,
             "old_labels_killed": 0, "labels_cycle_skipped": 0,
             "pruned_by_budget": 0, "heap_pops": 5, "dead_pops": 0,
             "truncated": false, "label_cap": 200000, "nodes": 4, "arcs": 5 },
  "parameters": { "source": "A", "target": "D", "directed": true,
                  "time_budget": null, "cost_budget": null,
                  "label_cap": 200000, "eps": 1e-9 }
}
```

失败类响应（`invalid_request`、`limit_exceeded`、`internal_error`）：

```jsonc
{ "status": "invalid_request", "error": "中文错误说明",
  "paths": [], "pareto": [], "stats": null, "parameters": {} }
```

### 状态码（`status`）

| status | 类别 | 含义 | CLI 退出码 |
| --- | --- | --- | --- |
| `ok` | 结果 | 至少找到一条 Pareto 路径（`source==target` 时为空路径） | 0 |
| `unreachable` | 结果 | 源点在预算约束下不可达终点（不是错误） | 0 |
| `truncated` | 结果（不完整） | 达到 `label_cap`，前沿可能不完整 | 0 |
| `invalid_request` | 失败 | 输入结构/类型/取值非法 | 1 |
| `limit_exceeded` | 失败 | 超过规模/范围硬边界 | 1 |
| `internal_error` | 失败 | 兜底内部错误（不回吐堆栈） | 1 |

> CLI 中，**语义层面的错误响应**（如 `invalid_request`）与正常结果一样写到
> **stdout**（便于用 JSON 解析），仅以退出码 1 标识失败；只有“请求文件无法
> 读取 / JSON 本身无法解析”时才写到 **stderr**。

---

## 5. 目录结构

```
mospp/                  计算库
  __init__.py           公开接口
  errors.py             MOSPError 与全部失败状态码
  tolerance.py          容差、支配/相等判定、Pareto 过滤参考函数
  graph.py              图结构 + JSON 请求结构/范围校验
  solver.py             标签算法（Label / Solver / SolveLimits）
  api.py                solve_request() JSON 接口与响应编码
  __main__.py           命令行入口
tests/                  自动化测试（pytest）
  reference.py          独立的 DFS 全量简单路径枚举（交叉验证用）
  test_basic.py         Pareto、路径重建、不可达、s==t
  test_equal_and_zero_cycle.py  相等标签、零权环、浮点容差
  test_budget_and_limits.py     预算剪枝、标签截断、无向图
  test_validation.py    输入校验与失败状态
  test_reference_agreement.py   随机小图：标签算法 vs 枚举前沿
  test_cli.py           命令行端到端
examples/               请求样例
examples/output/        各样例的实际运行输出（已提交，便于核对）
requirements.txt
```

---

## 6. 验收点对照

* **小图枚举简单路径对照 Pareto 前沿**：`tests/test_reference_agreement.py`
  用与求解器完全独立的 DFS 枚举所有简单路径，在 30+ 个随机 DAG / 含环图 /
  带预算实例上逐点比对前沿。
* **相等标签**：同权不同路径共存、同权同 visited 去重，见
  `test_equal_and_zero_cycle.py`。
* **零权环**：验证算法终止、结果仍为简单路径、不产生伪 Pareto 点，见
  `test_zero_weight_cycle_terminates_and_is_simple` 等。
* **预算剪枝**：单维 / 双维预算、边界容差、剪枝计数，见
  `test_budget_and_limits.py`。
* **路径可重建**：每条结果回放出边序列并重新累计权重，校验与标签一致、节点
  首尾正确且不重复，见 `test_path_reconstruction_weights_and_nodes` 与
  `test_reconstructed_path_exists_in_enumeration`。

---

## 7. 运行测试

```bash
python -m pytest -q
```

---

## 8. 已知边界与设计取舍

* 仅支持 **非负** 时间/费用；负权在入口即被拒绝（非负是标签算法终止与支配
  安全的前提）。
* 输出限定 **简单路径**；不支持含环 Pareto 路径（非负权下没有最优含环路径）。
* 每顶点标签集用 O(k) 线性比较（k 为该点非支配标签数），契合小中规模；未做
  针对超大标签集的二维数据结构加速。
* 大规模图的瓶颈是 Pareto 标签的固有指数性，用 `label_cap` + `truncated`
  显式兜底而非静默给假结果。
