# 树分解宽度启发式（Treewidth Heuristic）— 纯后端

无第三方依赖的 C++17 后端：对**无向简单图**计算消元序列（elimination order）、
由消元序列生成**树分解（tree decomposition）**，采用**最小填边（min-fill）**
启发式，并附带：

- 独立验证器：逐条核验**边覆盖**、**运行交集性质**、树结构、宽度与消元记账；
- 朴素小规模参考：枚举全部 `n!` 个排列求**最优树宽**；
- 记忆化精确搜索（非朴素、仅小规模）求**最优树宽**，作为更强对照；
- 纯 stdin/stdout 的 JSON 接口与批处理；
- C++ 单元/性质测试 + shell/python 端到端测试。

**核心求解不调用任何现成树宽/图求解器、不链接任何第三方库**（连 JSON 都是
手写的）。“精确/朴素”仅用于对启发式提供可验证对照，规模被严格限定。

> 术语诚实性：输出字段 `heuristic_width` 是树宽的一个**上界**，只有
> `optimal_treewidth`（由穷举/记忆化搜索在小规模上得出）才称为最优。
> 本项目从不把启发式宽度表述为最优树宽。仓库内置了一个最小填边**非最优**
> 的真实样例（见下文）。

## 目录结构

```
src/
  json.hpp/.cpp       手写 JSON 解析/序列化（无依赖）
  graph.hpp/.cpp      无向图模型与多种 JSON 图格式解析
  treewidth.hpp/.cpp  min-fill / min-degree 启发式、消元、树分解构造、
                      n! 朴素参考、记忆化精确搜索
  validate.hpp/.cpp   独立验证器（不依赖构造代码的结论，全部重新计算）
  cli.cpp             JSON 命令行接口
tools/
  exhaustive.cpp      小图全枚举取证工具（n≤7，复现非最优样例）
tests/
  test.cpp            单元 + 性质测试（含 n<=5 全部图穷举）
  e2e.sh              端到端 JSON 契约测试（python3 断言）
examples/             请求样例与对应响应样例
docs/                 RUN_LOG / PERFORMANCE / EXHAUSTIVE 实测取证
Makefile
```

## 构建

```bash
make all         # 生成 build/treewidth
make test        # 编译并运行 C++ 测试
make e2e         # 运行端到端测试（需要 python3）
make check       # 两者都跑
make exhaustive  # 小图全枚举取证（n≤7，约 53 秒；复现非最优样例）
```

要求：g++（支持 C++17，开发机为 g++ 13.3）、GNU make、python3（仅测试脚本用）。

## 使用

```bash
build/treewidth [--input FILE] [--compact]
```

从 stdin 或 `--input` 读取**一个** JSON 请求（也接受 JSON 数组，逐项处理、
逐项返回），把结果写到 stdout。成功响应包络：

```json
{ "ok": true, "action": "solve", "wall_ms": 0.12, "data": { ... } }
```

任何错误（JSON 语法、非法图、超规模、未知 action 等）返回：

```json
{ "ok": false, "error": "可读的错误原因" }
```

### 图的输入格式（三选一）

```json
{ "vertices": ["a", "b", "c"], "edges": [["a", "b"], ["b", "c"]] }
{ "n": 4, "edges": [[0, 1], [1, 2]] }
{ "adjacency": { "a": ["b", "c"], "b": ["a"] } }
```

顶点名可以是字符串或整数；自环被拒绝；重边/反向重复边折叠为一条；
`edges` 中出现但未声明的顶点会被自动补入。

### Action

| action | 说明 |
|---|---|
| `solve`（默认）| min-fill（或 min-degree）消元 + 树分解 + 独立验证 + 小规模精确对照 |
| `exact` | 仅跑记忆化精确搜索（可带 `limit`，硬上限 11） |
| `naive` | 仅跑 `n!` 全排列朴素参考（n ≤ 8） |
| `generate` | 生成可复现的 G(n,p) 随机图（带 `seed`），便于测试 |

`solve` 可选字段：`"heuristic": "min-fill" | "min-degree"`（默认 min-fill）、
`"exact_limit": <int>`（默认 10；超过图规模时精确项被跳过且明确标注）。

### 最小示例

```bash
cat examples/request_cycle5.json | build/treewidth
```

## 算法说明

### 消元与最小填边启发式

反复选一个顶点 `v`，把它当前的邻域连成团（添加“填边/fill edges”），
然后删除 `v`；删除顺序即消元序列。`v` 被删除时的度数，就是该消元袋大小减 1，
序列上最大度数 = 该消元顺序给出的宽度。

- **min-fill**：每一步选择“使其邻域成团所需新增边数最少”的顶点；
  平局依次按 当前度数最小、下标最小 决断（保证结果确定、可复现）。
- **min-degree**：选择当前度数最小的顶点（作为第二基线）。

每一步都记录顶点、删除时度数、消元袋 `{v} ∪ 当前邻域`、以及**具体新增了
哪些填边**，便于独立复核。

### 由消元序列构造树分解

消元法的标准构造：每个顶点对应一个袋 `B_v = {v} ∪ {v 的后续（含填边）邻点}`；
`B_v` 的父亲取 `v` 的后续邻点中**消元位置最靠前**的那个对应的袋。该规则保证
运行交集性质。对**不连通图**，每个连通分量产生一棵消解树，代码用一条
`root_join_edges` 把各分量的根串成一棵树（并在输出中显式标出
`component_roots` 与这些连接边），使最终结果始终是一棵单一树。

### 独立验证器（`src/validate.cpp`）

验证器不读取构造代码的任何结论，全部用原图重新计算：

1. 每个袋非空、顶点合法、袋内无重复；
2. **边覆盖（T2）**：原图每条边都完整落在至少一个袋中；每个顶点（含孤立点）
   至少出现一次；
3. **运行交集性质（T3）**：对每个顶点，包含它的所有袋在分解树中诱导出一个
   连通子树（在“只经过含该顶点的袋”的规则下做 BFS 计数）；
4. **树结构（T4）**：袋邻接图连通且边数恰为 `袋数-1`，无自环、无重边；
5. 宽度 `max|袋|-1` 与构造代码自报宽度一致性检查（不一致给 warning）；
6. 独立重放消元序列，核对排列合法性、每步度数、袋内容、填边集合、总填边数、
   宽度；并核对树分解的袋集合与消元袋逐一吻合。

### 最优对照（小规模，明确不是求解器调用）

- **朴素参考 `naive`**：`std::next_permutation` 枚举全部 `n!` 个排列，逐一重放
  消元求宽度，取最小值。无剪枝、无记忆，纯粹用于交叉验证；n ≤ 8。
- **记忆化精确搜索 `exact`**：在“剩余顶点集合 + 当前填边集合”状态上做精确 DP
  （`Width(S)=min_v max(deg_S(v), Width(S∖v, …))`），用局部界
  `deg ≥ best` 剪枝（该剪枝安全：首个候选必被探索，每个记忆值都是该状态真实
  最优），并用备忘录重建最优顺序。受 64 位边掩码限制硬上限 n = 11，默认 n ≤ 10。

> 开发过程中发现并修复了一个真实缺陷：精确搜索早期版本还带了全局上界剪枝，
> 极端情况下某状态所有候选都被剪掉、哨兵值被写入备忘录，导致返回的“最优值”
> 偏大（从而表现为“启发式宽度 < 最优”的不可能现象）。测试因此失败；
> 删除该不安全剪枝后全部通过。该经验也是“独立验证/交叉对照必须存在”的原因。

## 规模限制（刻意限定）

| 项目 | 上限 | 理由 |
|---|---|---|
| 启发式输入 `solve` | n ≤ **100** | 邻接矩阵 + 每步 O(n³) 级扫描；实测 n=100 各图均 < 50 ms |
| 记忆化精确 `exact` | 默认 n ≤ 10，硬上限 **11** | 状态用 64 位边掩码；树宽精确计算本身 NP 难 |
| 朴素全排列 `naive` | n ≤ **8** | 8! = 40320；9! 已 36 万，纯参考无需更大 |

超限会得到 `ok:false`（启发式/朴素）或 `limit_exceeded`（精确），不会硬算。

## 验证证据（本机实际运行）

开发机：Ubuntu 24.04，g++ 13.3.0，GNU Make 4.3，8 核。下列命令均实际执行。

### 构建与测试

```bash
$ make all      # -std=c++17 -O2 -Wall -Wextra -Wpedantic -Wshadow，零告警
$ make test
6224 checks, 0 failures
ALL TESTS PASSED
$ make e2e
PASS c5 / k5 / disc / nonopt / batch / tiny / error-handling /
     file-and-stdin parity / random-crosscheck
E2E: 9 passed, 0 failed
```

测试覆盖要点：

- **小图穷举最优作对照**：对 n = 1..5 的**每一个**标号简单图（枚举所有边子集，
  数量分别为 2^0=1、2^1=2、2^3=8、2^6=64、2^10=1024，共 1099 个图）比较
  记忆化精确与 n! 朴素参考，两者宽度必须完全一致，且重建出的最优顺序确实达到
  该宽度，且启发式宽度永不小于最优；
- **环**：C3/C4/C5/C8/C10/C11（与精确对照，树宽均为 2）；
- **团**：K4/K5/K8/K10/K11（树宽 n−1）；
- **不连通图**：两个三角形 + 一条独立边、K5 ⊔ C4、K4 加孤立点等；验证
  `tw(不相交并) = max(分量树宽)`，且分量树被 `root_join_edges` 正确连成一棵树；
- 路径、星树、弦图（随机区间图上 min-fill 必须 0 填边且最优）、大量 G(n,p)；
- **负面测试**：故意破坏袋/树/宽度，确认验证器能拒绝（漏边、破坏运行交集、
  断树、非法顶点 id、虚报宽度等）；
- ASan/UBSan 构建下整套测试零报错：
  `g++ -fsanitize=address,undefined ... && ./tw_asan` → 全部通过。

### 精确搜索性能（n=11，硬上限）

| 图 | 最优树宽 | wall |
|---|---|---|
| 空图 / 路径11 / 环11 / K11 | 0/1/2/10 | 均 < 0.05 ms |
| G(11, 0.2–0.6) | 3–6 | ≈ 0.1–0.2 ms |

n=12 明确被拒（`limit_exceeded: true`）。完整含记忆状态/搜索节点的数据见
`docs/PERFORMANCE.md`。

### 启发式性能（n=100，均通过独立验证）

| 图 | m | 宽度 | wall |
|---|---|---|---|
| 星 | 99 | 1 | ≈ 3 ms |
| 10×10 网格 | 180 | 13 | ≈ 7 ms |
| G(100,0.05–0.5) | 267–2523 | 27–84 | ≈ 12–48 ms |

（耗时随机器负载和随机种子波动；响应中的 `elapsed_ms`/`wall_ms` 为本次实测，
逐次数值见 `docs/PERFORMANCE.md`。）

### 最小填边**非最优**的真实样例

`make exhaustive` 枚举 n=1..7 的**全部**标号简单图（n≤6 同时交叉核对朴素参考）：
精确搜索与朴素参考在 n≤6 的 33863 个图上零分歧；**min-fill 在 n≤6 全部最优，
最早的次优出现在 n=7**，共 140 个（详见 `docs/EXHAUSTIVE.txt`）。

`examples/request_heuristic_nonoptimal.json` 是其中第一个（K_{4,3} 一侧加一个
三角形）：

```
heuristic_width = 5（min-fill），optimal_treewidth = 4，gap = 1
verification.passed = true（启发式分解本身仍合法，只是宽度不是最小）
```

这正面说明：合法的树分解 + 更小的宽度 ≠ 最优树宽；本项目始终区分二者。

## 输出里有什么（solve 的 data）

- `input` / `n` / `m`：规范化后的图；
- `elimination`：`heuristic`、`order`（标签序列）、逐步 `steps`
  （顶点、删除时度数、消元袋、新增填边）、`heuristic_width`、`total_fill`、
  以及“这是上界、不是最优”的 `note`；
- `tree_decomposition`：`bags`（id / vertices / size / parent）、
  `tree_edges`、`root_join_edges`、`width`、`component_roots`；
- `verification`：`passed`、`width_recomputed`、`edges_covered/total`、
  `tree_connected`、`errors`、`warnings`；
- `elimination_self_check`：消元序列独立重放结果；
- `exact`：小规模时的 `optimal_treewidth` / `optimal_order` / 搜索统计；
- `comparison`：`heuristic_is_optimal`、`gap`、以及对启发式顺序宽度的独立复算。

## 局限（如实说明）

- 启发式与精确搜索都面向**小规模**：精确树宽是 NP 难问题，这里刻意限定规模
  以提供可验证证据，而非通用求解器。
- 邻接矩阵存储 O(n²)、min-fill 朴素实现每步 O(n³) 级别；n=100 是产品化前的
  演示上限，不是高性能实现。
- 只支持无向简单图（无自环、无重边、无向）。
- 无前端、无网络服务；JSON 仅通过 stdin/stdout（与文件）交互。
- `examples/response_*.json` 中的耗时字段随运行变化，其余字段稳定可复现。
