# 最小割分区证据后端（mincut-backend）

纯后端、零第三方依赖的 **非负整数容量最大流 / 最小割** 求解与**可验证证据**
生成服务。核心求解（Dinic 最大流）从零实现，**不调用任何现成求解器或图算法
库**；JSON 解析/序列化也是手写的递归下降实现。

除主算法外，还提供：

- **独立校验器**：不信任求解器内部状态，只依据「每条边的流量」重新推导，
  逐条检查容量约束、流守恒、s/t 平衡、残量可达集、割边饱和、流值=割值；
- **朴素小规模参考**：穷举全部 `2^(n-2)` 个 s-t 分区求最小割（C++ 实现）；
- **自动化测试**：C++ 单元测试 + Python 差分测试，后者再用一份**独立的
  Python 穷举实现**交叉验证。

无前端，无网络服务；接口是 **stdin/文件 → stdout 的单文档 JSON**。

---

## 1. 构建与运行

要求：g++（支持 C++17）、GNU make、Python 3（仅测试用）。

```bash
make            # 产出可执行文件 ./mincut-backend
make test       # 构建并运行全部自动化测试
make clean
```

运行一次请求（两种等价方式）：

```bash
./mincut-backend examples/request_clrs.json | jq .
cat examples/request_clrs.json | ./mincut-backend | jq .
```

退出码约定：

| 退出码 | 含义 |
|--------|------|
| 0 | 正常处理（业务错误也在 JSON 里返回 `status:"error"`，退出码仍为 0） |
| 2 | 命令行用法错误，或输入不是合法 JSON |

---

## 2. JSON 接口

### 请求

```json
{
  "num_vertices": 6,
  "source": 0,
  "sink": 5,
  "edges": [
    {"id": "s-a", "from": 0, "to": 1, "capacity": 16}
  ],
  "bruteforce": true
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `num_vertices` | int | 顶点数，顶点编号 `0 .. n-1`，范围 `[2, 10000]` |
| `source` | int | 源点 s，必须在顶点范围内 |
| `sink` | int | 汇点 t，必须与 s 不同 |
| `edges` | array | 边数组，长度 `<= 100000` |
| `edges[].id` | string | 非空、全图唯一 |
| `edges[].from` / `to` | int | 端点（允许自环；允许平行边） |
| `edges[].capacity` | int | **非负整数**，`<= 1_000_000_000`；允许 0 |
| `bruteforce` | bool? | 可选。true 时附带穷举参考；要求 `n<=18` 且 `m<=2000` |

图按**有向图**处理：`(u,v)` 与 `(v,u)` 是两条独立的原图边；平行边也是各自
独立的记录，靠唯一 `id` 区分。

### 响应（成功）

顶层字段：

- `status`: `"ok"`；
- `input`: 回显的 n/s/t；
- `max_flow_value`: 最大流值（int64）；
- `flow[]`: 每条原图边的 `{id, from, to, capacity, flow}`；
- `min_cut`: 最小割分区证据：
  - `source_side` / `sink_side`: 两个顶点集合（划分为一个 s-t 割）；
  - `cut_edge_ids` / `cut_edges[]`: 从 S 指向 T 的原图边（割集证书）；
  - `cut_value`: 割容量；
- `verification`: 独立校验器对本次解逐项复核的结果：
  - `ok`: 全部检查是否通过；
  - `checks[]`: 每项检查名与布尔结果；
  - `failures[]`: 未通过项的具体说明；
  - `source_outflow`、`sink_inflow`、`cut_value`: 独立重算的数值；
- `bruteforce`（请求开启时）：`partitions_checked`（穷举分区数）、
  `min_cut_value`、`reference_source_side`、`matches_max_flow`。

完整样例见 `examples/response_clrs.json` 等文件（均由程序实际运行生成）。

### 响应（错误）

```json
{"status": "error", "error_code": "INVALID_REQUEST", "message": "..."}
```

错误码：`MALFORMED_JSON`、`INVALID_REQUEST`、`SCALE_LIMIT`、`BRUTE_TOO_LARGE`、
`SOLVER_SELF_CHECK_FAILED`（最后一个仅在求解器自检失败时出现，正常情况永不
出现）。

---

## 3. 算法与「证据」设计

### 3.1 最大流：Dinic（从零实现）

`src/maxflow.cpp`。BFS 构造分层图 + DFS 当前弧优化求阻塞流，容量为 int64。
整数容量保证终止；规模上界 `100000 边 × 1e9 = 1e14`，不会溢出 int64。

### 3.2 残量反向边与原图反向边**分别保存**

这是明确的实现约束，数据结构在 `src/maxflow.hpp` 的 `Arc`：

- 每条原图边 e=(u,v,c) 恰好生成**一条前向弧**（`edge_id = e`）和**一条配对的
  残量反向弧**（`edge_id = -1`，即 `Arc::kResidualArc`）；
- 即使输入里另有原图边 (v,u)，它也是一条**独立的前向弧**，绝不会被复用为
  (u,v) 的残量反向弧——两条弧作为邻接表中的独立条目并存；
- 平行边同理，各自拥有独立的前向弧与残量反向弧。

校验器（`src/verifier.cpp`）也遵守同样的独立原则：它从流量自行重建残量网
络时，每条原图边贡献自己的一对残量弧（正向 `c-f`、反向 `f`）。

### 3.3 割集证书

算法结束后在残量网络上从 s 做 BFS，可达集为 S、其余为 T（`src/mincut.cpp`）。
所有 `u∈S, v∈T` 的原图边构成割集，其容量之和为割值。

### 3.4 独立校验器检查的内容

1. 流量条数与边数一致、分区覆盖全部顶点；
2. **容量约束**：`0 <= f(e) <= c(e)`；
3. **流守恒**：除 s、t 外每个顶点出流之和等于入流之和（平行边逐条计入）；
4. s 净出流 == t 净入流，且等于求解器声称的流值；
5. 用流量**重新独立构建残量网络并 BFS**，声称的分区必须恰好等于残量可达集
   （s∈S、t∉S，即不存在增广路）；
6. 每条 S→T 边必须饱和（`f=c`），每条 T→S 边流量必须为 0；
7. **割值独立重算并与流值比较相等**。

### 3.5 朴素穷举参考

`src/bruteforce.cpp`：枚举 s、t 之外所有顶点的归属（共 `2^(n-2)` 种），对每种
分区直接累加 S→T 的原图边容量，取最小值。完全不涉及流算法，因此是对最小割
值的独立真值。仅允许 `n <= 18`（≤ 65 536 个分区）。

---

## 4. 规模限制

| 参数 | 上限 | 定义位置 |
|------|------|----------|
| 顶点数 | 10 000 | `src/limits.hpp` |
| 边数 | 100 000 | 同上 |
| 单条边容量 | 1 000 000 000 | 同上 |
| 穷举参考顶点/边 | 18 / 2 000 | 同上 |

超限请求返回 `SCALE_LIMIT` / `BRUTE_TOO_LARGE`，不会默默截断。

---

## 5. 自动化测试与如实运行记录

测试内容（`tests/`）：

- `unit_tests.cpp`：算法核心 + **负面测试**（人为构造超容量流量、负流量、
  守恒违例、错误分区、虚报流值，校验器必须全部拒绝）；
- `run_tests.py`：
  - 8 个手算答案的固定 CLI 用例（含平行边、零容量、原图反向边、自环、
    s-t 不可达）；
  - 400 组随机小图差分测试：流值/割值同时与 **C++ 穷举**、**Python 独立
    穷举**、Python 独立守恒与容量校验对照（每组 4 重比对）；
  - 12 个非法输入/规模边界用例；
  - 1 个大规模性能用例。

以下为本次交付时在本机的**实际运行记录**（无未通过项）。

环境：Ubuntu 24.04，Linux 6.8，g++ 13.3.0，Python 3.12.3，8 核；
日期 2026-09-25。

### 5.1 构建

命令：

```bash
make clean && make
```

结果：编译成功，`-Wall -Wextra -Wpedantic -Wshadow` 无警告，产出
`./mincut-backend`。

### 5.2 全量测试

命令：

```bash
make test
```

实际输出（末尾摘要）：

```
[ RUN  ] basic diamond
... (13 个单元测试用例) ...
[ RUN  ] self loop reports zero flow
...
32 checks, 0 failures          <- C++ 单元测试
== C++ unit tests ==
== fixed CLI cases ==
== randomized differential tests ==
== error/validation tests ==
== large scale test ==
    (large instance: 3600 vertices, 42480 edges, 0.39s, value=5990526843)

RESULT: 2022 passed, 0 failed
```

其中大规模用例流值 5 990 526 843 > 2³¹，实际走过 int64 路径。

### 5.3 声明规模上限的压力测试

10 000 顶点、100 000 边、容量均匀取自 `[0, 1e9]` 的随机图（固定随机种子 7，
脚本经 stdin 传入）：

```
returncode=0 time=0.88s status=ok verified=True value=2967363605 cut=2967363605 cut_edges=9
STRESS OK
```

### 5.4 教材样例（CLRS 经典网络，`examples/request_clrs.json`）

```
max_flow_value = 23, cut_value = 23, cut_edge_ids = ["a-c","d-c","d-t"]
穷举参考 min_cut_value = 23, matches_max_flow = true
```

（12 + 7 + 4 = 23。）

### 5.5 开发中发现并修正的一处自环缺陷（如实记录）

代码复查中发现并修正了一处**自环（u==v）**场景下的真实缺陷
（`src/maxflow.cpp` 的 `add_original_edge`）：

- **现象**：自环边的前向弧与残量反向弧被插入同一个邻接表，旧代码在插入
  后用 `size()-1` 记录前向弧位置，实际指向了后插入的反向弧，导致
  `edge_flow()` 对自环错报为「容量」（样例中一条容量 99 的自环曾显示
  `flow:99`）。
- **影响范围**：不影响最大流值、割集、割值——分层图条件
  `level[v]==level[u]+1` 对 u==v 恒不成立，自环在 BFS/DFS 中永不参与增广；
  但每条边的「流量」证据对自环是错的，属于必须修正的证据正确性问题。
- **修正**：插入前先解析两条弧的配对索引（自环时反向弧位于前向弧
  `+1`），前向弧位置在插入前记录。
- **回归**：新增了针对性锁定——C++ 用例 `self loop reports zero flow`
  断言自环流量恒为 0；随机差分测试对每张图断言「所有自环 flow==0」；
  `examples/response_edge_cases.json` 已重新生成，自环现为
  `{"id":"loop",...,"capacity":99,"flow":0}`。修正后干净重建零警告，
  `make test` 结果即 5.2 的 **2022 passed, 0 failed**，无未通过项。

### 5.6 其他局限（设计如此，非缺陷）

- 穷举参考仅支持 n ≤ 18；
- 图模型为有向图，无向图需调用方自行加两条方向相反的边；
- 接口为一次性 stdin/stdout 文档处理，不是常驻网络服务。

---

## 6. 目录结构

```
.
├── Makefile
├── README.md
├── src/
│   ├── limits.hpp       # 规模上界
│   ├── graph.hpp        # Problem / InputEdge 数据结构
│   ├── json.hpp/.cpp    # 手写 JSON 解析与序列化（零依赖）
│   ├── maxflow.hpp/.cpp # Dinic 最大流（残量反向边 edge_id=-1）
│   ├── mincut.hpp/.cpp  # 残量 BFS 求割集证书
│   ├── verifier.hpp/.cpp# 独立复核（容量/守恒/可达性/割值）
│   ├── bruteforce.hpp/.cpp # 朴素穷举 2^(n-2) 分区参考
│   ├── protocol.hpp/.cpp# JSON 请求校验与响应组装
│   └── main.cpp         # CLI 入口（stdin 或文件参数）
├── tests/
│   ├── unit_tests.cpp   # C++ 单元 + 负面测试
│   └── run_tests.py     # 固定/随机差分/错误/大规模测试
└── examples/
    ├── request_clrs.json / response_clrs.json
    ├── request_edge_cases.json / response_edge_cases.json
    ├── request_all_zero.json / response_all_zero.json
    └── request_error_negative_cap.json / response_error_negative_cap.json
```
