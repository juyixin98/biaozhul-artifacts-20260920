# 支配树故障定位后端（Dominator-Tree Fault Localization Backend）

纯后端、无外部依赖的 C++17 服务。对给定**控制流图（CFG）**与入口节点，计算：

- 从入口可达 / 不可达节点的严格区分；
- 每个可达节点的**立即支配节点（immediate dominator, idom）**与支配树；
- 每个可达节点的**支配边界（dominance frontier, DF）**；
- 全部支配集合、回边（natural-loop back edge）等可核对证据；
- 在线支配查询接口（dominates / idom / frontier / 支配链 / 被支配子树等）。

核心求解器为**自实现**的迭代数据流算法，不调用任何现成图/编译器求解器；
另提供一个**朴素小规模参考实现**（枚举入口简单路径取交集），对小图做独立交叉验证。

只有 JSON 标准输入输出和一个命令行二进制，**不含前端**。

---

## 1. 快速开始

```bash
make            # 生成 build/domtree_backend（g++，零外部依赖）
make test       # 单元测试（ASan/UBSan）+ Python CLI 端到端测试
```

运行单次请求：

```bash
# 从文件
./build/domtree_backend -f tests/requests/diamond.json

# 从 stdin
echo '{"entry":"A","nodes":["A","B","C","D"],
       "edges":[["A","B"],["A","C"],["B","D"],["C","D"]]}' \
  | ./build/domtree_backend
```

`--help` 查看用法。退出码：`0` 请求被处理（应用层校验错误仍返回 0，
错误体现在 JSON 的 `success` 字段）；`2` 文件无法打开或 stdin 读取失败。

---

## 2. 实测结果（本机真实运行）

环境：Ubuntu 24.04，g++ 13.3.0，x86_64，`-O2`。命令与输出：

```
$ make test
build/unit_tests
random cross-check: 400 graphs compared
ALL TESTS PASSED (95/95 checks)
python3 tests/cli_tests.py --bin build/domtree_backend
ALL CLI TESTS PASSED (42/42 checks)
```

- **单元测试 95/95 通过**：含 JSON 解析、图校验、回边、多出口、
  不可达环、入口自环、嵌套循环等定点断言；
- **生产求解器 vs 朴素路径枚举参考：400 张随机图（2–12 节点、~25% 边密度、
  允许自环）+ 5 张定点图全部一致**（idom、支配边界、可达性逐节点比对）；
- **CLI 端到端 42/42 通过**：子进程驱动二进制，校验 JSON、退出码、
  idom 表、边界集合、回边证据、错误处理及全部样例文件。

规模/性能证据（结构化 CFG：主链 + 周期性菱形分支 + 周期回边 +
一个与入口断开的环；求解时间取响应中自报的 `elapsed_microseconds`）：

| 图 | 节点 / 边 | 数据流迭代轮数 | 求解耗时 | 挂钟(三次) | 峰值 RSS |
|---|---|---|---|---|---|
| scale_1000 | 1002 / 1120 | 2 | ≈2 ms | 0.00–0.01 s | 8 MB |
| scale_5000 | 5000 / 5598 | 2 | ≈53 ms | 0.08 s | 26 MB |

原始证据保存在 `tests/output/`：每个样例的完整响应 JSON、
`/usr/bin/time -v` 计时文件。复现：

```bash
python3 tests/gen_scale_sample.py        # 重新生成 scale_1000 样例
./build/domtree_backend -f tests/requests/scale_1000.json
```

### 2.1 手工可核对的小例子

菱形（`tests/requests/diamond.json`，朴素枚举 4 条路径，`match=true`）：

```
A -> B, A -> C, B -> D, C -> D
idom: A=null(根)  B=A  C=A  D=A
DF(B)={D}  DF(C)={D}  DF(A)=DF(D)=∅
```

单入口循环 + 多出口 + 不可达环（`tests/requests/loop_back_edge.json`）：

```
entry->header; header->body; body->latch; latch->header(回边); latch->exit
另有不可达环 unreachable1 <-> unreachable2

idom: header=entry, body=header, latch=body, exit=latch
DF(header)={header}  DF(body)={header}  DF(latch)={header}
回边证据: [["latch","header"]]
不可达: ["unreachable1","unreachable2"]
触及不可达节点的边: [["unreachable1","unreachable2"],["unreachable2","unreachable1"]]
```

多出口（`tests/requests/multi_exit.json`）：`B -> {C,D,E} -> F`，
`idom(F)=B`；注意按形式化定义 **DF(B)=∅**（B 严格支配汇合点 F），
而 `DF(C)=DF(D)=DF(E)={F}`——这正是容易写错、由朴素参考兜底核对的点。

入口自环 `A->A, A->B`：按定义 A 严格支配 A 为假、A 支配其前驱 A，
故 **A ∈ DF(A)**；朴素参考与求解器一致。

---

## 3. 算法（均为自实现）

### 3.1 生产求解器 `src/dom.cpp`

1. **可达性**：迭代 DFS，得到可达标记与逆后序（RPO）。不可达节点不进入
   后续任何支配计算。
2. **支配集合（数据流迭代）**：
   - `Dom(entry) = {entry}`；
   - `Dom(v) = {v} ∪ ⋂ Dom(p)`，仅对**可达前驱** p 取交集；
   - 集合用 64 位位图表示，按 RPO 反复扫描到不动点，自报迭代轮数。
3. **立即支配节点**：`idom(v)` = v 的严格支配节点中支配集合最大者
   （支配链上最深的节点）；入口 idom 为空。
4. **支配边界**：标准前驱 "runner" 算法——对每个块 b，从其每个可达
   前驱沿 idom 链上溯到 `idom(b)`，沿途节点的 DF 加入 b；用单份
   时间戳标记数组避免重复（O(n) 额外内存）。
5. **支配树/深度**：按 idom 反向连边形成森林（可达部分为以入口为根的树）。
6. **证据**：回边 = `u->v` 且 v 支配 u（两端均可达）；至少一端不可达的
   边单列报告（不可达环内部的边因此**不会**被误报为回边）。

### 3.2 朴素参考 `src/naive.cpp`（仅用于校验）

- 用**独立的 BFS** 重新求可达性（与生产代码的 DFS 不同路径）；
- 对每个可达目标 v，DFS **枚举所有入口→v 的简单路径**（带上的节点
  集合），取交集；d ∈ 交集 ⇔ d 支配 v；
- idom 同样取最深严格支配者；
- 支配边界**直接按形式化定义**逐条边/逐节点判定：
  `b ∈ DF(v) ⇔ v 支配 b 的某个前驱 p 且 v 不严格支配 b`。

该方法最坏情况指数级，硬性保护：节点 ≤ 64、路径数预算 2,000,000；
超限则在响应的 `naive_verification` 中如实返回 `ran:false` 与原因
（生产结果照常返回），绝不静默。

### 3.3 不可达节点的语义

支配关系只对入口可达节点有定义。不可达节点：`reachable=false`、
`idom=null/-1`、支配边界为空、不出现在支配树边中；针对它们的查询
返回 `ok:false, reachable:false` 及说明，而不是编造一个支配者。

---

## 4. JSON 接口

### 4.1 请求

```jsonc
{
  "entry": "A",                       // 必填，入口节点标签
  "nodes": ["A", "B", "..."],         // 必填，字符串标签，不可重复
  "edges": [["A","B"], {"from":"A","to":"B"}],  // 可选；两种写法可混用
  "queries": [ /* 见下，可选 */ ],
  "verify_naive": false               // 可选，true 时附加朴素交叉验证
}
```

规模限制（`src/graph.hpp`）：节点 ≤ 10,000，边 ≤ 50,000，
标签长度 ≤ 256；平行边自动去重。超限返回 `success:false`。

查询类型（`queries` 数组，逐项返回）：

| type | 参数 | 返回 |
|---|---|---|
| `dominates` | `a`,`b` | `result`：a 是否支配 b（自反，允许 a=b） |
| `strictly_dominates` | `a`,`b` | 同上，但要求 a≠b |
| `idom` | `node` | `idom`：标签或 `null`（入口自身） |
| `frontier` | `node` | `frontier`：支配边界标签数组 |
| `dominators` | `node` | `dominators`：全部支配节点（含自身） |
| `dominated_by` | `node` | `subtree`：支配树中以该节点为根的子树 |
| `dom_chain` | `node` | `chain`：node, idom(node), …, 入口 |

未知节点、不可达节点、缺字段、未知查询类型都在对应查询项里返回
`ok:false` 与 `error`，不影响其他查询项。

### 4.2 响应（摘要）

```jsonc
{
  "success": true,
  "result": {
    "entry": "A",
    "statistics": { "nodes_total":.., "edges_total":..,
      "reachable_nodes":.., "unreachable_nodes":..,
      "dataflow_iterations":.., "elapsed_microseconds":.. },
    "reachable": [..], "unreachable": [..],
    "immediate_dominators": [ {"node":"B","idom":"A","tree_depth":1}, .. ],
    "dominator_tree_edges": [["A","B"], ..],
    "dominance_frontiers": { "A": [..], "B": ["D"], .. },
    "dominator_sets": { "A": ["A"], .. },   // <=200 节点时全量输出
    "evidence": { "back_edges": [..], "edges_touching_unreachable": [..] }
  },
  "queries": [ .. ],
  "naive_verification": { "ran":true, "paths_enumerated":4,
                          "match":true, "mismatches":[] }
}
```

全量 `dominator_sets` 为 O(n²)，节点 > 200 时自动省略并给出
`dominator_sets_note`，改用 `dominators` 查询按需获取（idom、边界、
支配树等其余字段在任何规模下都完整输出）。

---

## 5. 目录结构

```
src/
  json.hpp/.cpp   最小 JSON 解析/序列化（无第三方库）
  graph.hpp/.cpp  带标签有向图、校验、去重与规模限制
  dom.hpp/.cpp    生产求解器：可达性/支配集/idom/支配边界/支配树
  naive.hpp/.cpp  朴素路径枚举参考 + 与生产结果逐节点比对
  api.hpp/.cpp    JSON 请求解析、响应组装、查询、朴素校验
  main.cpp        命令行入口（stdin 或 -f 文件）
tests/
  unit_tests.cpp  C++ 单元测试（含 400 张随机图交叉验证）
  cli_tests.py    子进程级端到端测试
  gen_scale_sample.py
  requests/       请求样例（diamond / loop_back_edge / multi_exit /
                  scale_1000 / scale_5000）
  output/         实际运行留存的响应 JSON 与计时文件（证据）
Makefile
```

---

## 6. 测试与验收对照

| 验收要求 | 落点 |
|---|---|
| 入口可达节点的 idom 树 / 支配边界 | `src/dom.cpp`；`immediate_dominators`、`dominator_tree_edges`、`dominance_frontiers` |
| 区分不可达节点 | 可达性 DFS + `reachable/unreachable` 字段；不可达环不加 idom、不报回边 |
| 支配查询接口 | 7 种查询类型，见 §4.1 |
| 朴素小规模参考（枚举入口路径） | `src/naive.cpp`，路径枚举取交集；边界按定义直算 |
| 覆盖回边 | `test_loop_back_edge`、`test_nested_loop`（嵌套两层回边）、`back_edges` 证据 |
| 覆盖多出口 | `test_multi_exit`、`multi_exit.json` |
| 覆盖不可达环 | `test_unreachable_cycle`、`loop_back_edge.json` 中 u1<->u2 |
| 核对边界集合 | 求解器 runner 算法 vs 朴素定义直算，逐节点比对 + 随机 400 图 |
| 限定规模并输出可验证证据 | 节点/边硬上限；响应含迭代轮数、计时；`tests/output/` 留档 |
| 不调用现成求解器 | 全部算法手写，仅用 C++ 标准库 |
| 源码 / README / 请求样例 / 自动化测试 | 见目录结构；`make test` 一键运行 |
| 如实记录命令、结果、未通过项 | 本节与 §2；开发中出现过 1 次测试期望写错（多出口 DF(B)），算法与朴素参考一致，已修正测试期望；无未通过项遗留 |

未覆盖/已知边界：朴素参考为指数级，仅适用于 ≤64 节点小图（超限如实
报 `ran:false`）；本项目是无状态的批处理 JSON 接口，未提供常驻网络
服务（需求为纯后端 JSON 接口，未要求 HTTP）。
