# 最小割分区证据（Min-Cut Partition Evidence）— 纯后端

非负整数容量网络的 **最大流 / 最小割** 求解器，C++17 实现，无任何第三方依赖
（JSON 解析与序列化均为手写）。核心求解**不调用任何现成求解器**；另提供
**朴素穷举参考**（枚举全部 s-t 割）与**独立校验器**，输出可机读、可验证的证据。

## 它解决什么

给定有向多重图 `G=(V,E)`、源点 `s`、汇点 `t` 与非负整数容量 `c(e)`：

1. 用 **Dinic 阻塞流算法**求最大流值 `F`（整数运算，`int64`）。
2. 由最终残量网络中从 `s` 可达的点集 `S` 给出一个最小割 `(S, T)` 及割集。
3. 输出**证据 JSON**：每条边的流量、割两侧节点、割边集合、割值，以及完整
   残量弧清单。
4. 穷举参考枚举全部 `2^(n-2)` 个割，独立给出最小割值。
5. 校验器**不调用求解器**，从流量与割声明重新推导，逐条核验定理条件。

## 关键表示：残量反向边与原图反向边分别保存

每条输入边 `e=(u,v,c)` 都拥有**自己独立的一对**残量弧，求解器绝不合并、
去重或抵消对向边：

- `forward`：`u → v`，残量容量初值 `c`；
- `artificial_reverse`：`v → u`，残量容量初值 `0`（求解器为增广而创建）。

若输入中还存在一条原图边 `(v,u)`，它会得到**另一对**独立的弧。因此平行边、
原图反向边在整个求解过程和输出证据中始终可区分。每条弧都带
`edge_id` 与 `kind` 标记。自环（`u=v`）同样按独立弧对处理，不影响结果。

## 规模限制（显式限定）

| 项目 | 限制 |
|---|---|
| 节点数 | ≤ 200 |
| 边数（含平行边、自环、零容量边） | ≤ 1000 |
| 单边容量 | 0 ≤ c ≤ 10^12 |
| 穷举参考节点数 | ≤ 20（`2^(n-2)` 指数级，超限拒绝执行） |

## 构建

```bash
make            # 生成 build/mincut
make test       # C++ 单元测试
make check      # C++ 单元测试 + Python 集成测试
```

要求：g++（支持 C++17）、Python 3（集成测试，仅标准库）。

## 用法

```bash
mincut solve  [request.json | -]   # 最大流/最小割证据（默认读标准输入）
mincut brute  [request.json | -]   # 朴素穷举参考
mincut verify [response.json | -]  # 独立校验 solve 的输出
```

退出码：成功 `0`；校验未通过 `1`；请求/JSON 错误 `2`；用法错误 `64`。

### 请求格式

```json
{
  "nodes": ["s", "a", "b", "t"],
  "edges": [
    {"id": "e1", "from": "s", "to": "a", "capacity": 10},
    {"id": "e2", "from": "a", "to": "t", "capacity": 7}
  ],
  "source": "s",
  "sink": "t"
}
```

`id` 可省略（自动生成 `e0,e1,...`）；容量必须是非负整数；允许平行边、
零容量边、自环、双向边。

### 快速试跑

```bash
./build/mincut solve examples/basic.json
./build/mincut brute examples/basic.json
./build/mincut solve examples/clrs_network.json | ./build/mincut verify -
```

## 输出证据（solve）

- `max_flow`：最大流值；
- `flows[]`：每条输入边的流量 `flow`（由残量容量 `c - rcap_forward` 得出）；
- `cut.source_side / sink_side / cut_edges / cut_value`：最小割分区与割集；
- `residual.nodes[].arcs[]`：每个节点出发的每条残量弧，含 `to / edge_id /
  kind(forward|artificial_reverse) / residual_capacity`；
- `stats`：BFS 轮数、增广次数；
- `request`：归一化后的请求回显，校验无需原始输入。

## 校验器检查项（全部独立重算）

- **C1 容量界**：`0 ≤ flow(e) ≤ c(e)`；
- **C2 流量守恒**：除 s/t 外每个节点净流量为 0；
- **C3 终端平衡**：s 净流出 = t 净流入 = 申报的 `max_flow ≥ 0`；
- **C4 分区合法**：s∈S、t∈T、S∪T 为全集且不交；
- **C5 割集精确**：申报割边集合恰为所有跨 S→T 的输入边（不多不少）；
- **C6 割值**：割边容量之和等于申报 `cut_value`；
- **C7 强对偶**：`max_flow == cut_value`；
- **C8 残量一致性**：由各边流量独立重建残量图并 BFS，
  "s 残量可达集"必须与申报的 S 逐点一致（等价于割边饱和、无残量弧离开 S）；
- **C9 残量记账**：每条输入边恰有一条自己的 `forward` 和一条自己的
  `artificial_reverse` 弧，位置与残量容量正确——直接验证"分别保存"。

校验通过输出 `{"valid": true, ...}`，退出码 0；否则列出全部违例，退出码 1。

## 目录结构

```
src/
  minjson.h / json.cpp   手写 JSON 解析与序列化
  network.h              网络与残量弧数据结构（分别保存不变量）
  dinic.h                Dinic 最大流（核心求解，无外部求解器）
  brute.h / brute.cpp    朴素穷举参考（枚举 2^(n-2) 个割）
  verifier.h/.cpp        独立证据校验器（C1–C9）
  main.cpp               solve / brute / verify 三个子命令
  unit_tests.cpp         C++ 单元/差分测试（无测试框架）
examples/                请求样例
examples/output/         实跑产生的 solve/brute/verify 证据
tests/run_tests.py       自动化集成测试（标准库）
Makefile
docs/RUN_LOG.md          实际运行命令与结果记录
```

## 测试覆盖

- **C++ 单元测试**：Dinic 对穷举的差分测试（固定图 + 300 个随机小多重图，
  含平行边/零容量/自环/双向边/不连通）、流量可行条件、JSON 往返与拒绝。
- **Python 集成测试**：所有样例的 solve/verify/brute 三角对照、250 个随机图
  穷举交叉验证、已知值（CLRS=23）、**六类篡改必须被拒**、非法请求错误处理、
  200 节点/1000 边规模上限时限测试。
