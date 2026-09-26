# 强连通缩点分析（SCC Condensation Analyzer）

纯后端 C++17 项目：读入一个有向多重图的 JSON 请求，计算**强连通分量（SCC）**与
**缩点 DAG**，为每个非平凡/含自环分量给出**循环见证**，对跨分量边做**去重与多重度聚合**，
并用**独立校验器**和**朴素小规模参考实现**给出可验证证据。无任何前端、无第三方依赖、
核心求解不调用任何现成图算法库或求解器（JSON 解析也是手写的）。

---

## 1. 目录结构

```
.
├── Makefile
├── README.md
├── src/
│   ├── json.hpp/json.cpp        # 手写最小 JSON 解析/序列化（零依赖）
│   ├── graph.hpp                # Graph / Component / DagEdge / AnalysisResult 数据结构与规模常量
│   ├── graph_builder.*          # 请求校验、端点解析、多重边聚合
│   ├── scc.*                    # 核心求解：迭代版 Kosaraju + 循环见证 + 缩点
│   ├── naive.*                  # 朴素参考：Floyd-Warshall 可达性矩阵 O(n^3) + 互达 SCC
│   ├── verify.*                 # 独立校验器（BFS 强连通、Kahn 无环、见证合法性、去重核对）
│   └── main.cpp                 # CLI：读 JSON → 求解 → 校验 → 输出单个 JSON 文档
├── tests/
│   ├── test_scc.cpp             # C++ 单元测试（自包含断言框架，10 组 / 61 项检查）
│   └── run_e2e_tests.py         # Python 端到端 + 300 组随机微分测试（自带独立参考）
└── examples/
    ├── basic.json                          # 标签输入：3 环 + 三重边 + 自环 + 孤立点
    ├── basic_response.json                 # 上面请求的真实输出
    ├── multiedge_selfloop.json             # 下标输入：并行边 + 自环
    ├── isolated_vertices.json              # 含孤立点
    ├── bad_unknown_label.json              # 非法请求样例
    └── gen_scale.py                        # 生成 10 万元点 / 100 万边规模样例
```

## 2. 构建

```bash
make            # 生成 ./scc_analyze
make clean
```

要求 g++（实测 g++ 13.3.0，`-std=c++17 -O2 -Wall -Wextra -Wpedantic -Wshadow`，零警告）。

## 3. 请求 / 响应接口（JSON）

### 请求

```json
{
  "vertices": ["A", "B", "C"],
  "edges": [
    {"from": "A", "to": "B"},
    {"from": 1, "to": 2}
  ]
}
```

- `vertices`：字符串标签数组，**或**一个正整数顶点数（顶点编号 `0..n-1`）；
  也可省略，此时 `n` 由边端点最大下标 +1 推导（至少要有一条整数下标边）。
- `edges`：必填。`from`/`to` 可以是整数下标或字符串标签；允许**自环**与**多重边**
  （相同 `(from,to)` 出现多次即并行边）。

### 运行方式

```bash
./scc_analyze --pretty examples/basic.json     # 文件输入，缩进输出
cat examples/basic.json | ./scc_analyze        # 标准输入
```

退出码：`0` 成功且校验通过；`2` 请求非法（JSON 语法/模式/规模错误）；`1` 校验失败或内部错误。

### 响应字段（节选，完整样例见 `examples/basic_response.json`）

- `ok`、`verification.ok/failures`：独立校验结果；
- `stats`：`n`、`rawEdgeCount`、`uniqueEdgeCount`、`componentCount`、`dagEdgeCount`、
  `solverMicros`、`verifyMicros`；
- `components[]`：`id`、`representative`、`vertices`、`vertexLabels`、`cyclic`、
  `cycleWitness`（闭合游走顶点序列 `v0..vk`，隐含回边 `vk→v0`；自环为 `[v]`；
  无环单点为 `[]`）及对应标签；
- `condensation.edges[]`：去重后的跨分量边 `{from,to,multiplicity}`；
- `uniqueEdges[]`：输入多重图去重后的边及多重度；
- `naiveReference`：`n ≤ 64` 时给出 **Floyd-Warshall 可达性矩阵**、朴素互达划分
  `naiveComponentOf` 以及 `sccPartitionMatchesNaive`；超过阈值只回显 `enabled:false`。

### 规模限定（`src/graph.hpp`）

| 限制 | 值 |
|---|---|
| 最大顶点数 `MAX_N` | 100,000 |
| 最大原始边数 `MAX_RAW_EDGES` | 1,000,000 |
| 朴素参考阈值 `MAX_NAIVE_N` | 64（O(n³) 矩阵） |

超限请求返回退出码 2 与明确错误信息。核心算法复杂度 O(V+E)（去重排序 O(E log E)），
空间 O(V+E)。

## 4. 算法与确定性

1. **SCC**：手写**迭代式 Kosaraju**（两遍 DFS，显式栈，10 万点无递归栈风险），
   不使用任何现成求解器/图库。
2. **分量编号确定性**：Kosaraju 原始编号依赖边顺序，因此最终 id 按分量内
   **最小顶点升序**重新分配；分量内顶点升序排列；DAG 边按 `(from,to)` 升序。
   边列表任意置换 → 输出完全一致（有单元测试与微分测试覆盖）。
3. **缩点 DAG**：遍历去重后的边，跨分量边按键 `(cid(u),cid(v))` 聚合多重度；
   分量内边（含自环）不进入缩点。
4. **循环见证**：对每个非平凡分量（或含自环的单点），从代表元（最小顶点）做 BFS，
   找最短闭合游走；自环直接给 `[v]`。见证是分量内的简单环，逐边可核对。
5. **朴素参考**：`naive.cpp` 用 Floyd-Warshall 传递闭包算可达性矩阵，
   并用并查集按“互相可达”朴素划分 SCC，与核心算法**完全独立**。
6. **独立校验** `verify.cpp`（BFS/Kahn 重写，不复用求解器中间状态）：
   - 划分合法性：每个顶点恰好出现一次、编号规范、`compOf` 一致；
   - 每个分量内代表元与任意顶点**双向可达**（强连通）；
   - 小图上用独立 BFS 全点对可达性核对**分量最大性**（互达必同组、同组必互达）；
   - 缩点边集合与多重度和原图逐一核对（无多、无缺、无自环、无重复、有序）；
   - **Kahn 拓扑排序验证缩点无环**；
   - 每个见证：不出分量、不重复顶点、每一步（含闭合边）都是真实存在的边。

## 5. 自动化测试

```bash
make test      # C++ 单元测试
make pytest    # Python 端到端 + 微分测试（需要先 make）
make check     # 两者都跑
```

- C++ 单元测试（`tests/test_scc.cpp`）覆盖：孤立点、自环、多重边聚合、
  3 环+源+汇、两个含环 SCC 间并行跨边、与朴素划分一致、边置换确定性、
  字符串标签、非法请求拒绝，以及对结果**故意篡改**后校验器必须报错（伪造缩点环、
  错误合并分量、越界见证、重复 DAG 边）。
- Python 测试（`tests/run_e2e_tests.py`）自带**独立的迭代 Kosaraju 与 Floyd-Warshall**，
  对 300 组随机图（n≤20，随机自环/多重边/孤立点，边序随机洗牌）逐项比对：
  SCC 划分、可达性矩阵、DAG 边与多重度、去重回显、每个见证的合法性、
  校验器结论，以及边序反转后的输出确定性；另含一组 2 万点链规模用例。

## 6. 实际运行记录（本机实测）

环境：Ubuntu 24.04 / Linux 6.8，g++ 13.3.0，Python 3.12.3。以下均为本人实际执行结果。

### 6.1 构建（零编译警告）

```
$ make
g++ -std=c++17 -O2 -Wall -Wextra -Wpedantic -Wshadow ...   # 各编译单元
$ echo $?
0
```

### 6.2 C++ 单元测试

```
$ make test
[ RUN      ] isolated vertices each form singleton acyclic SCC
[       OK ] ...
...（共 9 组）
61 checks, 0 failures
```

### 6.3 Python 端到端 + 300 组随机微分测试

```
$ python3 tests/run_e2e_tests.py
[ RUN      ] fixed: self-loop + multi-edges + isolated vertices
[ RUN      ] fixed: all vertices isolated
[ RUN      ] fixed: malformed requests exit code 2
[ RUN      ] random differential tests (300 graphs)
[ RUN      ] scale: 20000-vertex sparse DAG-ish graph
             solver=6951us verify=13510us

3433 checks, 0 failures
ALL E2E TESTS PASSED
```

### 6.4 AddressSanitizer + UndefinedBehaviorSanitizer

```
$ g++ -std=c++17 -O1 -g -fsanitize=address,undefined -fno-omit-frame-pointer -I src \
      src/*.cpp tests/test_scc.cpp -o /tmp/test_asan
$ /tmp/test_asan
61 checks, 0 failures        # 退出码 0，无任何 ASan/UBSan 报告
```

### 6.5 规模上限实测（100,000 点 / 1,000,000 原始边）

```
$ python3 examples/gen_scale.py
$ /usr/bin/time -v ./scc_analyze examples/scale_100k.json > /tmp/scale_out.json
	Elapsed (wall clock) time:   0:02.31
	Maximum resident set size:   419944 kbytes (~410 MiB)
```

响应 `stats` 与校验：

```json
{"n": 100000, "rawEdgeCount": 1000000, "uniqueEdgeCount": 149999,
 "componentCount": 50000, "dagEdgeCount": 49999,
 "solverMicros": 687421, "verifyMicros": 405242}
```

`ok: true`，`verification.failures: []`；该规模超过朴素阈值，`naiveReference.enabled=false`。
结构为 5 万个两两环被链成 SCC 链 + 边 0→1 的大量并行拷贝，缩点 49,999 条边经 Kahn
验证无环，分量 0 的见证为 `[0, 1]`。

### 6.6 非法请求

```
$ ./scc_analyze examples/bad_unknown_label.json
{ "ok": false, "error": "edge 1 references unknown vertex label 'GHOST'" }   # 退出码 2
$ echo '{bad' | ./scc_analyze
{ "ok": false, "error": "JSON parse error at offset 4: ..." }                # 退出码 2
```

## 7. 未通过项 / 已知限制（如实说明）

- **无未通过测试**：上述 61 + 3433 项检查全部通过，sanitizer 无告警。
- 本机未安装 `clang-format / clang-tidy / cppcheck`，故 C++ 规则中要求的这三项
  静态检查本次**未能执行**；已做到 g++ 严格警告（`-Wall -Wextra -Wpedantic -Wshadow`）零警告、
  ASan/UBSan 清洁。安装后可直接运行：
  `clang-format -i src/*.cpp src/*.hpp tests/*.cpp`、`clang-tidy src/*.cpp -- -std=c++17`、
  `cppcheck --enable=all src/`。
- 朴素 O(n³) 参考按设计只在 n ≤ 64 输出；大图的正确性证据来自独立 BFS/Kahn 校验器
  与小图区间的随机微分测试，而非全点对矩阵。
- 该项目是 JSON-in/JSON-out 的命令行后端，不含 HTTP 服务与任何前端。
