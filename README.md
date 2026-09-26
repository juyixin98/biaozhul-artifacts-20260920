# 动态图连通性离线求解器（dynconn）

纯后端 C++17 项目：离线处理无向图的**边插入、边删除、连通性查询**序列，
核心算法为**时间线段树 + 可回滚并查集**，不调用任何现成求解器/图库；
另提供**逐时刻 BFS 朴素参考实现**作为正确性基准。

## 问题模型

- 操作序列 `ops[0..m-1]` 按顺序执行，操作类型为 `add(u,v)` / `del(u,v)` / `query(u,v)`。
- 位于下标 `i` 的查询，看到的是所有下标 `< i` 的增删操作作用后的图。
- 在下标 `a` 加入、下标 `d` 删除的边，对下标满足 `a < i < d` 的查询可见
  （即线段树叶区间 `[a+1, d-1]`）；从未被删除的边可见区间为 `[a+1, m-1]`。
- **删除按边实例匹配**：`del(u,v)` 删除最近一次尚未匹配的 `add(u,v)`（LIFO），
  因此支持**平行边**（同一对端点多次 add，需同样多次 del 才彻底断开）。
  边无向：`(u,v)` 与 `(v,u)` 视为同一条边。
- **重复删除是错误**：对没有存活实例的边执行 `del`，整个请求判定失败，
  返回 `op_index` 指向出错操作。
- 自环 `add(u,u)` 合法（对连通性无影响）；顶点恒与自身连通。

## 算法与正确性要点

1. 第一遍扫描把每条边实例映射为生命周期区间 `[a+1, d-1]`（同时完成全部校验）。
2. 把每个区间插入**时间线段树**的 O(log m) 个节点。
3. DFS 遍历线段树：进入节点时把该节点上的边全部 `unite` 进并查集，
   离开前 `rollback` 到进入时的检查点；叶子处回答查询。
4. 并查集**按大小合并、禁止路径压缩**——路径压缩会在历史日志之外修改
   parent 指针，使 rollback 无法精确还原。因此 `find` 为 O(log n)。
5. 复杂度：`O((E·log m + m)·log n)`，E 为边实例总数；空间 `O(E·log m + n)`。

参考实现 `solve_reference`：逐步重放操作，每次查询重新建图做 BFS，
`O(Q·(n+E))`，仅用于小规模对拍与 `--reference` 模式。

## 规模限制

| 项 | 上限 |
|---|---|
| 顶点数 n | 1 .. 200000 |
| 操作数 m | 0 .. 200000 |
| 顶点编号 | 0 .. n-1 |

## 构建与运行

```bash
make            # 生成 build/dynconn 与 build/test_random
make test       # 构建并运行全部自动化测试
make clean
```

依赖：支持 C++17 的 g++（无第三方库）。

## JSON 接口

请求（文件参数或 stdin）：

```json
{
  "n": 4,
  "ops": [
    {"type": "add",   "u": 0, "v": 1},
    {"type": "add",   "u": 1, "v": 2},
    {"type": "query", "u": 0, "v": 2},
    {"type": "del",   "u": 1, "v": 2},
    {"type": "query", "u": 0, "v": 2}
  ]
}
```

响应（每个 query 一个布尔值，按出现顺序）：

```json
{"ok":true,"answers":[true,false]}
```

操作语义错误（重复删除、顶点越界、n 越界等）：

```json
{"ok":false,"error":"delete of edge (0,1) with no active instance","op_index":2}
```

退出码：`0` = 请求已处理（看响应里的 `ok`）；`2` = 用法/IO/JSON 语法/schema 错误。

```bash
./build/dynconn examples/request_basic.json      # 离线线段树求解器
./build/dynconn --reference < examples/request_basic.json   # 朴素 BFS 参考
```

## 目录结构

```
src/rollback_dsu.hpp   可回滚并查集（按大小合并，无路径压缩）
src/solver.hpp         校验/区间规划 + 线段树离线求解器 + BFS 参考求解器
src/json.hpp           极简 JSON 解析器（无外部依赖）
src/main.cpp           CLI：JSON 请求 -> JSON 响应
tests/test_random.cpp  单元测试 + 随机差分测试（离线 vs BFS）
tests/run_tests.sh     端到端测试驱动（含 CLI 示例回归）
examples/              请求样例与期望输出
```

## 测试覆盖

- **随机差分**：3000 组随机操作序列（n≤9，含平行边、自环、85/15 有效/非法删除），
  离线求解器与逐时刻 BFS 全量比对（含错误下标一致性）。
- **边界手工用例**：首下标查询、add 紧邻 query、del 紧邻 query、
  生命周期为空（add 后立刻 del）、末尾查询（区间开到 m-1）、
  平行边删一次/删两次、端点顺序颠倒的平行边、重复删除报错、
  删除从未加入的边、自环、顶点越界、空操作序列、单顶点、非法 n。
- **并查集回滚**：随机 checkpoint/rollback 与逐对重算模型比对。
- **CLI 回归**：examples 下样例对离线/参考两种模式分别 diff 期望输出；
  stdin 输入、畸形 JSON、schema 错误的退出码。

## 验证记录（实际运行，2026-09-25，g++ -O2）

```
$ make
g++ -std=c++17 -O2 -Wall -Wextra -Wpedantic -Isrc -o build/dynconn src/main.cpp
g++ -std=c++17 -O2 -Wall -Wextra -Wpedantic -Isrc -o build/test_random tests/test_random.cpp
# 零警告

$ ./build/test_random
test_random: 3843048 checks, 0 failures

$ make test
== unit + randomized differential tests ==
test_random: 3843048 checks, 0 failures
== CLI example regression (offline solver) ==
PASS example basic (offline)
PASS example boundary (offline)
PASS example error_dup_delete (offline)
PASS example parallel (offline)
== CLI example regression (reference solver, same expected output) ==
PASS example basic (reference)
PASS example boundary (reference)
PASS example error_dup_delete (reference)
PASS example parallel (reference)
== stdin input ==
PASS stdin
== malformed JSON exits with code 2 ==
PASS malformed-json exit code
== schema error exits with code 2 ==
PASS schema-error exit code
ALL TESTS PASSED

# CLI 级差分（n=500, m=4000, 1015 个查询）：离线输出与参考输出 diff 完全一致
# 满规模性能（n=200000, m=200000，随机混合操作）：
$ time ./build/dynconn /tmp/req_max.json
real    0m0.613s   # 含 JSON 解析与输出
```

未通过项：无。

## 已知限制

- 纯离线：必须一次性拿到完整操作序列，不支持在线到达。
- 仅判连通性，不输出路径/连通块划分。
- JSON 数字若写成浮点形式（如 `4.0`）会被 schema 校验拒绝，请用整数。
