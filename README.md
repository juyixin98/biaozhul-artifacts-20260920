# 最小费用流（Min-Cost Max-Flow）纯后端

整数容量、整数费用的最小费用（最大）流计算库 + JSON 命令行接口。
纯 Python 3 + NumPy 实现，核心算法自行编写，无第三方图算法依赖，无前端。

## 功能与范围

- **问题**：单源单汇有向网络上的最小费用最大流（MCMF）；也可通过 `required_flow`
  请求指定流量的最小费用流（达不到时返回 `infeasible`）。
- **算法**：Successive Shortest Path（逐最短路增广）
  - 初始势函数：在允许负费用边的初始残量网络上跑 SPFA（队列 Bellman–Ford），
    同时检测**从源点可达的负费用环**；
  - 之后每次增广：在残量网络上以约化费用 `c'(u,v)=c(u,v)+p[u]-p[v]`
    跑 Dijkstra（二分堆），沿最短路按瓶颈容量增广，更新势函数 `p[v]+=dist[v]`；
  - 汇点不可达时停止，即得到最大流。
- **支持**：负费用边（前提：输入不含源点可达的负费用环）、平行边、自环、
  零容量边、反向增广（自动沿残量反向弧撤销先前流量）。
- **输出**：状态、总流量、总费用、增广次数、每条用户边的流量、最终势函数，
  以及是否已达最大流。

### 输入范围（小中规模）

| 项目 | 限制 |
|---|---|
| 顶点数 `n` | 2 ≤ n ≤ 2000，编号 `0..n-1` |
| 用户边数 `m` | m ≤ 10 000（自动配等量残量反向弧） |
| 单弧容量 `capacity` | 整数，0 ≤ cap ≤ 10⁹ |
| 单弧费用 `cost` | 整数，−10⁶ ≤ cost ≤ 10⁶ |
| `required_flow` | 省略=最大流；否则 0 ≤ f ≤ 10¹⁵ |

整数上限原因：约化费用、距离与势函数存储为 NumPy `int64`（范围 ±9.22×10¹⁸）。
任何可达最短距离的绝对值至多 `(n−1)·10⁶ ≤ 2×10⁹`，安全。
**总费用**用 Python 任意精度整数累计并二次核对，即使
`m·cap·cost` 达到 10¹⁹（超过 int64）仍精确。

### 数值容差

容量、费用、流量、势函数全部是整数，**全程无浮点运算，容差 EPS = 0**
（精确比较）。容量约束与流守恒在每次响应前用整数断言复核。

### 失败状态

| 响应 | 含义 |
|---|---|
| `result.status = "optimal"` | 已达最大流，或恰好达到 `required_flow` |
| `result.status = "infeasible"` | 指定流量超过最大可达流量；响应中仍给出已增广部分（该流量下的最小费用流）及其费用 |
| HTTP/CLI 外层 `error.code = "negative_cycle"` | 残量网络存在源点可达负费用环，最优费用无界（违反输入前提），拒绝求解 |
| `error.code = "invalid_request"` | 结构/类型/范围错误（严格整数：`true`、`3.0`、`"3"` 均拒绝） |
| `error.code = "invalid_json"` | 原始 JSON 无法解析 |

CLI 退出码：成功 `0`，输入/业务错误（含负环）`2`。

## 目录结构

```
mcf/
  __init__.py     对外接口
  limits.py       规模/数值限制与容差常量
  graph.py        有向网络与成对弧残量网络（NumPy int64 数组）
  solver.py       SSP + 势函数 Dijkstra；负环检测；FlowResult 及守恒自检
  api.py          JSON 请求校验、求解、错误响应
  cli.py          命令行入口（python -m mcf.cli）
examples/         6 个请求样例（基本/指定流量/平行边/不可达汇/不可行/负环）
tests/
  brute_force.py      独立的可行流穷举参考实现（纯 Python）
  test_bruteforce.py  200 个随机小图穷举逐流量费用对照
  test_fixed_cases.py 平行边、负边、反向增广固定用例
  test_api.py         JSON 校验、失败状态、CLI 子进程
  test_properties.py  势函数约化费用非负不变量、随机图守恒、中规模性能
```

## 使用

依赖：Python ≥ 3.10、NumPy（开发环境为 Python 3.12.3 / NumPy 2.5.3）。

```bash
pip install -r requirements.txt

# 从文件
python -m mcf.cli examples/01_basic.json

# 从标准输入
cat examples/01_basic.json | python -m mcf.cli
```

请求格式：

```json
{
  "n": 4,
  "source": 0,
  "sink": 3,
  "required_flow": 5,
  "edges": [
    {"from": 0, "to": 1, "capacity": 10, "cost": 2},
    {"from": 2, "to": 3, "capacity": 12, "cost": -3}
  ]
}
```

`required_flow` 可省略（求最大流）或为 `null`。

成功响应：

```json
{
  "status": "ok",
  "result": {
    "status": "optimal",
    "flow": 15,
    "cost": 29,
    "iterations": 4,
    "max_flow_reached": true,
    "potentials": [0, 2, 4, 8],
    "flows": [
      {"edge": 0, "from": 0, "to": 1, "flow": 10, "cost": 2}
    ]
  }
}
```

错误响应：

```json
{"status": "error", "error": {"code": "negative_cycle", "message": "..."}}
```

作为库调用：

```python
from mcf import FlowNetwork, min_cost_max_flow

net = FlowNetwork(4, 0, 3, [(0, 1, 10, 2), (1, 3, 10, 1)])
result = min_cost_max_flow(net)            # 最大流
result.verify()                            # 容量约束 + 流守恒断言
print(result.flow, result.cost, result.edge_flows, result.potentials)
```

## 测试

```bash
bash run_tests.sh        # 等价于：
python3 -m unittest discover -t . -s tests -v
```

穷举对照说明：对 n≤6、容量≤2 的随机小图，独立参考实现枚举残量网络上
所有“逐单位简单 s-t 路”增广序列，记录每个可行流量的最小费用；
被测算法对每个可行流量逐一求解并比较，同时校验最大流值、容量约束与流守恒。

实际执行的命令与结果（含耗时、压测与未通过项的处置）见 [RUNLOG.md](RUNLOG.md)。

## 已知限制

- SSP 的增广次数最坏可达总流量级（整数容量未做容量缩放）；
  在限制规模下（n≤2000、m≤10000）典型算例毫秒至数秒，
  单位容量、2000 次增广的对抗例约 5 秒（见 RUNLOG）。
- 仅支持单源单汇、整数容量/费用。
