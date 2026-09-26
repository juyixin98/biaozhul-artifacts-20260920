# dagpaths — 有向无环图路径计数与字典序第 K 条路径查询

纯后端 C++17 项目，零第三方依赖。核心图算法（拓扑排序、计数 DP、字典序
排名/反排名、朴素枚举）与大整数、JSON 解析均为自实现，**不调用任何现成
求解器或外部库**。

## 功能

| 操作 | 说明 |
|------|------|
| `count` | 统计 DAG 中从 source 到 target 的不同路径条数（任意精度） |
| `kth` | 返回按节点 ID 字典序的第 K 条路径（K 为 1 起始的任意精度整数） |
| `enumerate` | 朴素 DFS 参考实现：枚举全部路径（有上限，可截断），用于小规模验证 |

## 构建与运行

```bash
make            # 生成 ./dagpaths（需要 g++，C++17）
make test       # 构建并运行全部测试（单元 + 端到端 + 随机交叉校验）
make clean
```

程序从 **stdin 读取一个 JSON 请求**，向 **stdout 输出一个 JSON 响应**。
退出码：`ok:true` 时为 0，`ok:false` 时为 1。

```bash
./dagpaths < examples/count_request.json
# {"count":"3","ok":true,"op":"count"}
```

## 路径顺序定义

路径是节点 ID（整数）序列，按**字典序**比较：逐元素比较节点 ID 的数值，
第一个不同元素较小者排在前面。由于图无环，每条 source→target 路径恰好
在终点访问 target 一次，因此不存在一条路径是另一条的真前缀，字典序是
全序。`kth` 的 K 从 **1** 开始：K=1 是字典序最小的路径。

## 请求协议

公共字段：

```json
{
  "op": "count" | "kth" | "enumerate",
  "graph": {
    "num_nodes": 5,
    "edges": [[0, 1], [0, 2], [0, 4], [1, 3], [2, 3], [3, 4]]
  },
  "source": 0,
  "target": 4
}
```

- 节点 ID 为 `[0, num_nodes)` 内的整数；边为 `[from, to]` 有向边。
- 不允许自环（自环本身是长度为 1 的环）；重复边报错而非静默去重。
- `source == target` 定义为恰好 1 条路径：平凡路径 `[source]`。

### `count`

响应：`{"ok":true,"op":"count","count":"3"}`
`count` 是十进制**字符串**（可能超过 64 位）。不可达时为 `"0"`。

### `kth`

额外字段：`"k"` —— 十进制字符串（推荐，无精度损失）或 JSON 整数。

响应：`{"ok":true,"op":"kth","k":"2","path":[0,2,3,4]}`
K 越界（`k < 1` 或 `k > 路径总数`，含不可达时总数为 0）返回
`K_OUT_OF_RANGE`，message 中带有有效区间。

### `enumerate`（朴素小规模参考）

额外字段：`"max_paths"`（可选，默认 1000，上限 100000）。

响应：`{"ok":true,"op":"enumerate","returned":3,"truncated":false,"paths":[[0,1,3,4],[0,2,3,4],[0,4]]}`
按字典序逐条 DFS 枚举；达到 `max_paths` 时停止并置 `truncated:true`。
这是刻意保持简单的参考实现，用于与 `count`/`kth` 交叉验证。

### 错误响应

```json
{"ok":false,"error":{"code":"CYCLE","message":"graph contains a directed cycle"}}
```

| 错误码 | 含义 |
|--------|------|
| `PARSE_ERROR` | 请求不是合法 JSON（或含非整数 JSON 数字） |
| `INVALID_REQUEST` | 请求不是 JSON 对象 / 端点越界 |
| `MISSING_FIELD` / `TYPE_ERROR` | 缺字段 / 字段类型错误 |
| `INVALID_GRAPH` | 节点 ID 越界、自环 |
| `DUPLICATE_EDGE` | 同一条边出现多次 |
| `CYCLE` | 图含有向环（不是 DAG） |
| `LIMIT_EXCEEDED` | 超过规模限制（见下） |
| `K_OUT_OF_RANGE` | K 越界 |
| `UNKNOWN_OP` | 未知操作 |

## 规模限制

| 限制 | 值 |
|------|-----|
| 节点数 | ≤ 100 000 |
| 边数 | ≤ 1 000 000 |
| `enumerate` 的 `max_paths` | ≤ 100 000（默认 1000） |

超限请求被拒绝（`LIMIT_EXCEEDED`），不做静默截断。

## 大整数策略

路径数与排名 K 使用自实现的任意精度无符号整数 `BigUint`
（基 10^9 的 limb 数组，见 `src/bigint.cpp`），只实现所需运算：
加、减（不欠位）、比较、十进制互转。**不存在溢出**：计数可以任意大。
JSON 中凡可能超 64 位的量（`count`、`k`）一律以十进制字符串传输。

证据：70 层分层图（见 `tests/e2e_test.py` 的 `layered_graph(70)`）到第
70 层节点的路径数为 2^69 = 590295810358705651712 > 2^64−1，
`count` 与 `kth`（首/末/越界各一条）均验证通过。

## 算法

- **计数**：Kahn 拓扑序（构造时同时完成环检测），逆拓扑序 DP
  `count[u] = Σ count[v]（v 为 u 的后继）`，`count[target] = 1`。
  复杂度 O(V+E) 次大整数加法。
- **第 K 条**：先算 `count[·]`，从 source 出发，每一步按节点 ID 升序
  扫描后继，若 `k > count[v]` 则 `k -= count[v]` 跳过整棵子树，否则
  进入该后继；到达 target 即得第 K 条。复杂度 O(路径长 × 出度)。
- **枚举**：迭代式 DFS，后继按 ID 升序访问，天然按字典序产出。

## 项目结构

```
src/bigint.hpp/.cpp   任意精度无符号整数
src/json.hpp/.cpp     极简 JSON 解析/序列化
src/dag.hpp/.cpp      图校验、拓扑排序、计数 DP、第 K 条、朴素枚举
src/main.cpp          请求分发与响应构造（stdin/stdout）
tests/test_bigint.cpp 大整数单元测试（无框架）
tests/e2e_test.py     33 个确定性协议用例
tests/random_check.py 300 个随机 DAG 交叉校验
tests/run_tests.sh    端到端入口
examples/*.json       请求样例
```

## 测试与验收记录（实际运行）

环境：Ubuntu 24.04，g++ 13.3.0，Python 3.12.3，GNU Make 4.3。

### 最终通过的运行

```
$ make clean && make && make test
rm -rf build dagpaths
mkdir -p build
g++ -std=c++17 -O2 -Wall -Wextra -Wpedantic -MMD -MP -c -o build/main.o src/main.cpp
g++ -std=c++17 -O2 -Wall -Wextra -Wpedantic -MMD -MP -c -o build/dag.o src/dag.cpp
g++ -std=c++17 -O2 -Wall -Wextra -Wpedantic -MMD -MP -c -o build/json.o src/json.cpp
g++ -std=c++17 -O2 -Wall -Wextra -Wpedantic -MMD -MP -c -o build/bigint.o src/bigint.cpp
g++ -std=c++17 -O2 -Wall -Wextra -Wpedantic -o dagpaths build/main.o build/dag.o build/json.o build/bigint.o
g++ -std=c++17 -O2 -Wall -Wextra -Wpedantic -o build/test_bigint tests/test_bigint.cpp build/bigint.o
./build/test_bigint
test_bigint: all tests passed
bash tests/run_tests.sh
e2e_test: all 33 cases passed
python3 tests/random_check.py
random_check: 300 random DAGs passed (count == enumerate == reverse kth ranking)
```

覆盖点：

- **小图枚举全部路径**：`enumerate_all`（3 条路径全列出）与随机校验中
  每个实例的全量枚举。
- **多源多汇**：`MULTI` 图（两个源 0/1、两个可达汇 3/4、一个孤立节点 5）
  的 4 个用例。
- **不可达**：`count_unreachable`（计数为 "0"）、`count_isolated_sink`、
  `kth_unreachable`（K_OUT_OF_RANGE）。
- **K 越界**：`kth_zero`、`kth_beyond_count`、`kth_big_beyond`（k=2^69+1）
  及随机校验中对每个实例检查 k=0 与 k=总数+1。
- **计数与反向排名一致**：随机校验对每个实例断言
  `count == 枚举条数` 且 `kth(i+1) == paths[i]`（路径数 ≤25 时全量，
  否则抽样 25 个），并验证枚举结果有序、无重复、每条路径合法。
- **大整数**：2^69 计数与首/末条路径（`kth_big_first`/`kth_big_last`）。
- **错误处理**：环、自环、重复边、越界节点、缺字段、类型错误、未知
  操作、非法 JSON、非对象请求。

### 请求样例的实际输出

```
$ ./dagpaths < examples/count_request.json
{"count":"3","ok":true,"op":"count"}
$ ./dagpaths < examples/kth_request.json
{"k":"2","ok":true,"op":"kth","path":[0,2,3,4]}
$ ./dagpaths < examples/enumerate_request.json
{"ok":true,"op":"enumerate","paths":[[0,1,3,4],[0,2,3,4],[0,4]],"returned":3,"truncated":false}
$ echo '{"op":"count","graph":{"num_nodes":2,"edges":[[0,1],[1,0]]},"source":0,"target":1}' | ./dagpaths
{"error":{"code":"CYCLE","message":"graph contains a directed cycle"},"ok":false}   # 退出码 1
```

### 开发过程中未通过、已修复的项（诚实记录）

1. **计数方向错误**：`countsTo` 初版从 target 沿**正向**边 DFS，只能
   到达下游节点，导致所有计数为 0（首轮 e2e 25 个用例失败）。修复：
   构造时保存 Kahn 拓扑序，逆拓扑序累加。
2. **增量构建产生布局不一致的目标文件**：给 `Dag` 增加 `topo_` 成员后
   Makefile 未跟踪头文件依赖，`main.o` 未重编译，-O2 下段错误
   （退出码 139）。修复：Makefile 增加 `-MMD -MP` 自动头文件依赖。
3. **测试期望值算错**：分层图到第 k 层节点的路径数实为 2^(k-1)（首跳
   只有一条边），且字典序首条路径应含节点 1；修正测试期望为 2^69 与
   正确路径后通过。实现本身无误（程序输出与手工推导一致）。

当前无未通过的测试项。
