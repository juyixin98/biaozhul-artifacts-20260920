# 动态图连通性（离线）后端

纯后端 C++17 项目：给定一张无向图上的**边增删 + 连通性查询**操作序列，**离线**回答每个查询时刻两点是否连通。

核心求解器为 **时间线段树（segment tree over time）+ 可回滚并查集（rollback DSU）**，全部算法自行实现，**不调用任何现成图算法/求解器库**。另提供独立的**朴素逐查询 BFS** 参考实现用于交叉验证。无任何第三方依赖（JSON 解析/序列化亦为手写）。

---

## 1. 算法

### 1.1 语义与“边实例”

操作按数组下标构成离散时间线（`0..m-1`），每条查询的默认时刻 `t` 就是它的操作下标（也可在请求中显式给 `t`，仅作标签原样回显）。

每次 `add(u, v)` 创建一个**边实例**，返回该实例的 `edge_id`（按 add 顺序 0,1,2,…）。`delete(edge_id)` **按实例精确匹配**删除：

- 平行边（同一对端点多次 add）是相互独立的实例，删除其中一个不影响其他实例；
- 删除一个不存在的 id 报错 `unknown_edge_id`；
- 对同一实例重复删除报错 `duplicate_delete`；
- 自环 `u == v` 合法。

一个边实例在操作序列上的存活期是 `[add 下标, delete 下标)`；未删除则存活到序列末尾。

### 1.2 离线化：查询编号区间

先线性重放操作流，只记录每个边实例覆盖的**查询编号区间** `[L, R)`（半开，编号按查询出现顺序）：

- add 时 `L = 当前已出现的查询数`；
- delete 时 `R = 当前已出现的查询数`；
- 未删除则 `R = 查询总数`。

若 `L == R`，该实例的存活期与任何查询都不相交（例如加了立刻删），求解时直接忽略。

### 1.3 时间线段树

在查询编号轴 `[0, Q)` 上建线段树。把每个边区间 `[L, R)` 拆成 `O(log Q)` 个标准线段树节点，边副本挂在这些节点上（存储总量 `O(E log Q)`）。

随后 DFS 线段树：

1. 进入节点时，把该节点上所有边并入并查集，记录 DSU 检查点；
2. 到叶节点时回答该编号对应的查询；
3. 离开节点时**回滚**到进入前的检查点。

每个边在每个挂载节点上恰好并入一次、回滚一次。

### 1.4 可回滚并查集（禁止路径压缩）

- 合并采用**按大小合并（union by size）**，树高 `O(log n)`；
- `find()` 只是朴素向上走，**绝不做路径压缩**——路径压缩会改写不在撤销栈上的父指针，使回滚无法恢复到精确的先前状态；
- 每次真正合并（两棵不同的树）压一条记录 `{childRoot, parentRoot, oldSize}`，回滚时恢复父指针与集合大小；冗余合并（两端已连通）不压栈。

复杂度：`O((E log Q + Q) · α' )`，无路径压缩下单次 `find` 为 `O(log n)`，整体 `O((E log Q + Q) log n)`，空间 `O(E log Q + n)`。

### 1.5 朴素参考（naive-bfs）

`algorithm: "naive-bfs"` 走另一条完全独立的代码路径：在线重放操作，维护活动实例集合；**每个查询都从零重建邻接表并跑一次迭代 BFS**。单次查询 `O(n + E)`，总体 `O(Q·(n+E))`，仅作为小规模参考答案与性能对照，不做任何复用/加速。

---

## 2. 构建与运行

要求：g++（支持 C++17，实测 g++ 13.3）、GNU make、Python 3（仅测试脚本需要，标准库即可）。

```bash
make            # 生成 build/dynamic_connectivity
```

运行方式（二选一）：

```bash
# 从文件
./build/dynamic_connectivity examples/basic_request.json

# 从标准输入
cat examples/basic_request.json | ./build/dynamic_connectivity
```

退出码：`0` 求解成功；`1` 输入文件/JSON 层面错误；`2` 请求内容或操作流被拒绝（错误详情仍以 JSON 输出到 stdout）。

---

## 3. JSON 接口

### 请求

| 字段 | 类型 | 说明 |
|---|---|---|
| `n` | int，`[1, 100000]` | 顶点数，顶点编号 `0..n-1` |
| `algorithm` | string，可选 | `"segment-tree"`（默认）或 `"naive-bfs"` |
| `operations` | 数组，长度 `≤ 200000` | 操作序列，按时间顺序 |

操作对象：

| type | 字段 | 含义 |
|---|---|---|
| `"add"` | `u`, `v`（int） | 新增一个边实例，其 id 由响应 `edge_ids` 按 add 顺序给出 |
| `"delete"` | `edge_id`（非负 int） | 删除**该实例**；id 不存在或已删除则请求失败 |
| `"query"` | `u`, `v`（int），可选 `t`（非负 int） | 查询 `u,v` 此刻是否连通；`t` 缺省为操作下标 |

### 成功响应

```json
{
  "ok": true,
  "algorithm": "segment-tree",
  "n": 6,
  "edge_ids": [0, 1, 2, 3],
  "results": [
    {"op_index": 3, "t": 3, "u": 0, "v": 2, "connected": true}
  ],
  "stats": {
    "num_vertices": 6,
    "num_operations": 10,
    "num_adds": 4,
    "num_deletes": 1,
    "num_queries": 5,
    "segment_placements": 4,
    "union_calls": 4,
    "merge_calls": 4,
    "max_rollback_stack": 4
  }
}
```

（上面 `results`/`stats` 即 `examples/basic_response.json` 的真实输出。）

`results` 按查询出现顺序排列。统计字段用于提供可验证证据：

- `segment_placements`：边挂到线段树节点上的副本总数（`O(E log Q)`）；
- `union_calls` / `merge_calls`：并入次数 / 真正合并次数（前者 ≥ 后者）；
- `max_rollback_stack`：DFS 中 DSU 撤销栈的最大深度（`≤ 活动边数`）。

### 错误响应

```json
{
  "ok": false,
  "error": {"code": "duplicate_delete", "message": "op[2]: duplicate delete ...", "op_index": 2}
}
```

错误码：`invalid_json`、`invalid_request`、`invalid_n`、`invalid_algorithm`、
`invalid_operations`、`invalid_operation`、`too_many_operations`、
`vertex_out_of_range`、`unknown_edge_id`、`duplicate_delete`、`solver_error`。

---

## 4. 规模限制（硬边界）

| 项 | 上限 |
|---|---|
| 顶点数 `n` | 100 000 |
| 操作总数 | 200 000 |
| add 实例总数 | ≤ 操作总数 |

超限请求被拒绝并返回对应错误码，不进入求解。

---

## 5. 自动化测试

```bash
make test          # 先构建，再运行 python3 tests/run_tests.py
# 或直接：
python3 tests/run_tests.py
```

测试内容：

1. **随机差分测试**：7 组不同规模、每组 5 个固定种子的随机合法操作流，
   `segment-tree` 与 `naive-bfs` 的输出分别与**测试脚本内独立实现的 Python BFS 预言机**逐查询比对；
2. **固定用例**：平行边独立删除、重复删除报错（两种算法都查）、未知 id、负 id、
   查询时刻边界（首条即查询 / 加后立即删 / 最后一条才查询 / 永不删除）、
   自环、空操作、无查询、显式 `t` 回显、顶点越界、合并-断开循环；
3. **输入校验/规模边界**：非法 JSON、非对象体、`n` 边界（0/-1/100001）、
   未知操作类型、operations 非数组、200001 条操作被拒；
4. **统计量自检**：`merge_calls ≤ union_calls`、`segment_placements ≤ E·2⌈log₂Q⌉`、
   撤销栈深度不超过 add 数；
5. **性能/规模证据**：中等混合负载两算法直接计时对比；
   密集长寿命边 + 大量查询用例体现渐近差距；大规模（n=20000，120000 ops）限时运行，
   并对前缀用独立 Python 预言机抽查答案；
6. **Sanitizer**：以 `-fsanitize=address,undefined` 单独构建并跑完整测试集
   （`DC_BINARY` 环境变量可切换被测二进制）。

每条调用的命令、字节数、返回码、耗时都写入 `tests/test_results.log`。

### 实测记录摘要（本机，-O2）

> 完整输出见 `tests/test_results.log`；以下为实际运行摘录，非估算。

```
dense n=2000, E=1500, Q=5000: segment-tree 24.6 ms vs naive-bfs 148.5 ms
LARGE n=20000, ops=120000, queries=45849: segment-tree 332.8 ms
LARGE stats: segment_placements=39882, union_calls=39882,
             merge_calls=39879, max_rollback_stack=17
PASSED checks: 255
FAILED checks: 0
```

（计时随机器波动；历次运行 dense 约 25–27 ms vs naive 约 148–175 ms、
LARGE 约 333–410 ms，量级与结论一致。原始逐条命令记录在
`tests/test_results.log`，ASan 轮次记录在 `tests/test_results_asan.log`。）

ASan+UBSan 构建下同一测试集 255/255 通过，无任何 sanitizer 报告。

---

## 6. 目录结构

```
.
├── Makefile
├── README.md
├── src/
│   ├── json.h / json.cpp      # 手写 JSON 解析与序列化（无第三方依赖）
│   ├── solver.h / solver.cpp  # RollbackDSU、时间线段树求解器、朴素 BFS 参考
│   └── main.cpp               # CLI：stdin/文件 -> JSON 请求/响应、规模限制、校验
├── examples/
│   ├── basic_request.json / basic_response.json
│   ├── parallel_edges.json / parallel_edges_response.json
│   └── duplicate_delete.json / duplicate_delete_response.json
└── tests/
    ├── run_tests.py           # 差分/固定/边界/统计/性能测试
    └── test_results.log       # 实际命令与结果记录（测试运行时覆写）
```

## 7. 设计约束与非目标

- 纯后端：提供命令行 JSON 接口，不含任何前端或网络服务；
- 核心求解不使用现成求解器/图库，JSON 亦不引第三方库；
- 无向图；同一对端点的多条边是不同实例，删除按实例 id 匹配；
- 离线语义：整个操作流一次性给出；`t` 字段是标签而非任意时刻穿越查询。
