# 最小费用流（Min-Cost Flow）纯后端服务

纯 Python + NumPy 的最小费用**最大流**计算库与 JSON 接口。无前端、无第三方服务
依赖（仅 NumPy）。核心算法（Bellman–Ford / Dijkstra 逐次最短路、剩余网络、
势函数维护）全部自行实现。

- 容量、费用均为**整数**
- **允许负费用边**，但输入保证：不存在**从源点可达的负费用环**（若存在则检测
  并以失败状态返回，不会给出错误答案）
- 支持平行边、反向增广（剩余网络抵消已发流量）、汇点不可达、零容量边
- 输出最大流流量、最小总费用、逐条边的流量以及最终势函数

## 目录结构

```
mcf/
  __init__.py        包入口
  solver.py          核心算法（势函数 + 逐次最短路）
  api.py             JSON 请求校验与响应封装
  cli.py             命令行入口（文件 / stdin）
  __main__.py        支持 python -m mcf
examples/
  request_basic.json                基础图（含一条负边）
  request_parallel_negative.json    平行边 + 负边 + 限量
  request_unreachable_sink.json     汇点不可达
  request_negative_cycle.json       源可达负费用环（失败样例）
tests/
  test_mcf.py        自动化测试（30 个）
  brute_force.py     小图暴力穷举参照（纯 Python）
requirements.txt
```

## 运行环境

实测：Python 3.12.3 + NumPy 2.5.3（Linux）。

```bash
pip install -r requirements.txt   # 仅 numpy
```

## 算法说明

**逐次最短增广路（Successive Shortest Path）+ 势函数（Reduced Cost）：**

1. **初始势函数**：在初始剩余网络上，从源点跑 Bellman–Ford（用 NumPy
   向量化松弛），得到最短距离 `h` 作为初始势。第 n 轮仍可松弛即判定存在
   源点可达的负费用环，抛出 `NegativeCycleError`。
2. **每轮增广**：在剩余网络上以约化费用
   `c'(u,v) = c(u,v) + h[u] - h[v]` 跑 Dijkstra。势函数不变量保证所有
   从已达节点出发的正容量弧 `c' ≥ 0`。
3. **更新势函数**：对本轮可达的每个节点 `h[v] += dist[v]`，随后沿最短路
   按瓶颈容量增广，并同步更新正/反向剩余容量。
4. 重复直到汇点不可达（达到最大流）或达到 `max_flow_limit`。

> **实现注意（开发中实测发现并修复的问题）**：Dijkstra 不能在汇点弹出时
> 提前结束。势函数对本轮所有可达节点更新；提前结束会使一批仅有"暂定
> 距离"的节点漏掉更新，后续增广轮次出现负约化费用，破坏正确性。
> 修复为每轮 Dijkstra 跑完整可达分量。回归测试：
> `test_large_dag_potential_invariant_regression`（256 节点 / 2000 边）。

### 数值约定（容差）

- 容量、费用、流量、势函数全部按 **int64 整数**运算，费用/流量判定只用
  严格的整数比较（剩余容量 `cap > 0`、约化费用 `rc < 0`）。
- **不使用任何浮点数**，因此不存在浮点容差/误差问题。
- 返回的总费用使用 Python 任意精度 `int`：即使累加结果超出 int64
  （例如容量 2⁶³−1、费用 2³¹−1，费用约 1.98×10²⁸）也精确。
- Bellman–Ford 内部哨兵 `INF = 2⁶²`，大于任何真实路径长度
  （`|费用|·节点数 ≤ 2³¹·256 ≈ 5.5×10¹¹`），不会与真实距离混淆；
  JSON 输出中不可达节点的势为 `null`。

### 输入范围（硬限制，小中规模）

| 项目 | 范围 |
|---|---|
| 节点数 `n` | `2 … 256` |
| 边数 `m` | `0 … 2000` |
| 容量 | 整数，`0 … 2⁶³−1` |
| 费用 | 整数，`−2³¹ … 2³¹−1` |
| 源 / 汇 | 不同的合法节点下标 |
| 自环 | 拒绝（负自环等价负环，统一在输入层报 `invalid_request`） |
| 增广轮数 | 上限 200 000，超过返回 `iteration_limit` |

容量为 0 的边允许（忽略）；平行边允许。

## JSON 接口

### 请求

```json
{
  "n": 4,
  "source": 0,
  "sink": 3,
  "edges": [
    {"u": 0, "v": 1, "capacity": 3, "cost": 2}
  ],
  "max_flow_limit": null
}
```

`max_flow_limit` 可选：给定后求"费用最小且流量达到该值"的流（若最大流
不足则返回最大流）。

### 成功响应（`status: "optimal"`，含汇点不可达、流量 0 的情形）

```json
{
  "status": "optimal",
  "flow": 5,
  "cost": 24,
  "sink_reachable": false,
  "edge_flows": [3, 2, 2, 1, 4],
  "potential": [0, 2, 5, 8],
  "edges": [ {"u": 0, "v": 1, "capacity": 3, "cost": 2, "flow": 3} ]
}
```

- `flow`：总流量；`cost`：总费用（= Σ flow·cost）
- `edge_flows`：与请求边严格同序的流量
- `potential`：最终势函数（不可达节点为 `null`）
- `edges`：回显每条边并附 `flow`，便于直接核对
- `sink_reachable`：终止时汇点是否仍可达；`false` 且 `flow=0` 即
  "汇点不可达"

### 失败状态

| `status` | 含义 | CLI 退出码 |
|---|---|---|
| `invalid_request` | JSON 非法、字段缺失/类型错（含布尔、浮点）、越界、自环 | 2 |
| `negative_cycle` | 检测到源点可达的负费用环 | 3 |
| `iteration_limit` | 增广轮数超过 200 000（超出目标规模） | 3 |
| `internal_error` | 不应发生的内部不变量破坏 | 4 |
| `optimal` | 成功（流量可能为 0） | 0 |

## 命令行用法

```bash
# 文件输入
python -m mcf examples/request_basic.json

# 标准输入
cat examples/request_basic.json | python -m mcf
```

## Python 库用法

```python
from mcf import min_cost_max_flow
res = min_cost_max_flow(
    n=3, source=0, sink=2,
    edges=[(0, 1, 4, -1), (1, 2, 4, 2)],
)
print(res.flow, res.cost, res.edge_flows, res.potential)
```

## 实测命令与结果

以下均为本次开发中在本仓库真实执行的结果（Python 3.12.3，NumPy 2.5.3）。

### 1. 四个样例

```text
$ python -m mcf examples/request_basic.json
exit=0   flow=5  cost=24  sink_reachable=false（达到最大流后自然不可达）
         edge_flows=[3,2,2,1,4]  potential=[0,2,5,8]

$ python -m mcf examples/request_parallel_negative.json
exit=0   flow=5（max_flow_limit=5） cost=-1
         edge_flows=[4,1,0,5]  potential=[0,1,3]

$ python -m mcf examples/request_unreachable_sink.json
exit=0   flow=0  cost=0  sink_reachable=false  potential=[0,2,6,null]

$ python -m mcf examples/request_negative_cycle.json
exit=3   {"status": "negative_cycle", "error": "...still relaxing in Bellman-Ford round n)"}
```

手算核对：

- basic 图三条可能路线的单价：0→1→2→3 为 2−1+1=2；0→1→3 为
  2+6=8；0→2→3 为 5+1=6。容量允许的最优解：路线 0→1→2→3
  2 单位、0→1→3 1 单位、0→2→3 2 单位（对应 edge_flows=[3,2,2,1,4]），
  费用 2·2+1·8+2·6=24，与输出一致。
- 平行边样例：4 单位走 −3 的 0→1 边、1 单位走 +1 的 0→1 边，汇侧
  全部走费用 2 的平行边：4·(−3+2)+1·(1+2)=−4+3=−1，与输出一致。

### 2. 自动化测试

```text
$ python3 -m unittest discover -s tests -v
...
Ran 30 tests in 9.149s

OK
```

**30 个测试全部通过，无未通过项、无跳过项。** 覆盖：

- 与小图**暴力穷举**（枚举全部满足容量的流量向量、逐个校验流守恒，
  再取最大流/最小费用）的对照：
  - 120 个随机小图（节点 2–5，含负边、平行边）
  - 30 个随机小图 + 随机 `max_flow_limit`
  - 200 个带正费用回边的非 DAG 小图（强制走反向剩余弧的场景）
  - 固定 6 边形状（含一对平行边）的**结构化穷举**：7 组容量配置 ×
    4⁶=4096 组费用全枚举，外加 4 组费用模式 × 3⁶=729 组容量全枚举，
    共 **31 588 个实例逐一对照**（穷举侧用 NumPy 矩阵化计算所有费用
    配置的最优值，见 `test_fixed_small_complete_enumeration`）
- 每个结果独立复核：容量界 `0 ≤ flow ≤ capacity`、所有内部节点
  流守恒、`Σ flow·cost == cost`
- **平行边**（费用低者先被填满）、**反向增广**（手工构造的例子中
  只允许正向弧的贪心算法只能得 1 单位流，本实现经反向剩余弧增广
  达到最优 2 单位/费用 110，并与穷举一致）、**汇点不可达**（0 流）
- 负费用环检测（Bellman–Ford 第 n 轮松弛）、自环/负自环拒绝
- JSON 层：非法 JSON、缺字段、类型错（浮点、布尔）、各类越界
- CLI 子进程端到端与退出码（0/2/3）
- 迭代上限失败状态（10 条单位平行边，强制每轮仅增 1）
- 256 节点 / 2000 边势函数不变量回归测试

### 3. 规模 / 边界实测

```text
容量 2^63−1、费用 2^31−1 的单边图：
  flow=9223372036854775807
  cost=19807040619342712359383728129（Python int，超出 int64 仍精确）
  耗时 0.000s

100 节点单位宽链（费用为负）：flow=4611686018427387903，费用核对一致，0.004s

256 节点 / 2000 边 DAG（费用遍布 int32 区间）：
  flow=4737，cost=-39849550820221，0.286s
  另以独立的 Bellman–Ford 校验最终剩余网络：不存在负费用 s→t 路、
  不存在负环，流守恒/容量全部成立（最优性条件）。

随机含双向边的 256 节点图（int32 随机费用）：
  正确返回 negative_cycle（此类图几乎必然含源可达负环，拒绝属预期）。
```

## 局限性

- 面向小中规模（n ≤ 256，m ≤ 2000）。超大规模或需要容量缩放/成本缩放、
  预流推进等高级算法的场景不在目标内。
- 增广轮数与流量规模相关（每轮至少增 1 单位整数流量），设 200 000 上限
  作为失败保护，触发时返回 `iteration_limit` 而非静默变慢。
- 不含 HTTP 服务与前端；仅提供库 API、`python -m mcf` 的 JSON CLI。
