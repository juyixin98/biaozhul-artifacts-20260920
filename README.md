# 强连通缩点分析后端 (scc-backend)

纯后端 C++17 项目：读取 JSON 描述的有向图，计算强连通分量（SCC）、缩点
DAG、每个分量内的循环见证，以及去重后的跨分量边。核心求解为自实现的
迭代式 Tarjan 算法，**不调用任何现成图求解器**；另提供基于可达性矩阵的
朴素小规模参考实现用于对拍验证。无前端、无第三方依赖（JSON 解析/序列化
亦为自实现）。

## 构建

```sh
make            # 生成 build/scc_backend 与 build/scc_verify
make test       # 构建并运行全部自动化测试
make clean
```

要求：g++（支持 C++17）、python3（仅测试脚本需要）。

## 使用

```sh
./build/scc_backend [--reference] [--pretty] [input.json]
```

- 不带文件参数时从 stdin 读取请求。
- `--reference`：使用朴素可达性矩阵参考实现（限 n ≤ 3000）。
- `--pretty`：缩进格式化输出。
- 退出码：`0` 成功；`2` 请求非法（此时 stdout 输出 `{"ok":false,"error":...}`）；`1` 内部/IO 错误。

### 请求格式

```json
{
  "vertices": 5,
  "edges": [[0, 1], [1, 2], [2, 0], [2, 3], [3, 4], [4, 3]]
}
```

- `vertices`：顶点数，顶点编号为 `0..vertices-1`。
- `edges`：有向边 `[from, to]` 列表。允许**自环**（`[u, u]`）与**多重边**
  （重复边在算法层面去重，但 `edge_count` 按原始输入计数）。
- 孤立点（无任何边的顶点）是合法输入，各自成为单点分量。

### 响应格式

```json
{
  "ok": true,
  "algorithm": "tarjan-iterative",
  "vertex_count": 5,
  "edge_count": 6,
  "component_count": 2,
  "components": [
    {"id": 0, "size": 3, "vertices": [0, 1, 2], "cycle_witness": [0, 1, 2, 0]},
    {"id": 1, "size": 2, "vertices": [3, 4], "cycle_witness": [3, 4, 3]}
  ],
  "condensation_edge_count": 1,
  "condensation_edges": [[0, 1]],
  "numbering": "components ordered by smallest contained vertex id"
}
```

- `components[].vertices`：分量内顶点升序。
- `components[].cycle_witness`：分量内一条**简单环**（首尾相同、内部顶点
  不重复、每条边都存在于原图），作为该分量强连通的循环见证；单点且
  无自环的分量为 `null`（无环可证），单点带自环为 `[v, v]`。
- `condensation_edges`：缩点 DAG 的边集，跨分量边**已去重**、按
  `(from, to)` 字典序排列。
- **确定性编号**：分量按其最小顶点编号升序赋 id（含顶点 0 的分量即
  id 0）；邻接表排序去重后遍历，同一输入永远产生字节级一致的输出。

### 规模限制

| 项 | 上限 |
|---|---|
| 顶点数 | 100 000 |
| 边数（原始输入） | 2 000 000 |
| 参考实现 / 最大性验证 | 3 000 顶点（O(n³/64) 位集传递闭包） |

超限请求返回 `{"ok": false, "error": ...}` 并以退出码 2 结束。

## 独立验证器

`build/scc_verify <request.json> <result.json>` 对求解结果做独立校验：

1. 分量构成 `[0, n)` 的划分（不漏、不重、升序、编号按最小顶点排序）；
2. 循环见证合法：闭合、简单、顶点属于该分量、每条边在原图中存在；
   单点无自环分量必须为 `null`，其余分量必须有见证；
3. 跨分量边集与原图导出的去重边集**精确相等**且有序；
4. 缩点图无环（Kahn 拓扑排序）；
5. **分量最大性**（n ≤ 3000 时）：用可达性矩阵检查任意两顶点
   “同分量 ⇔ 互达”；大图跳过并明确标注 `maximality=skipped`。

全部通过输出 `VERIFY OK ...` 并以 0 退出，否则 `VERIFY FAIL: <原因>`。

## 自动化测试

`python3 tests/run_tests.py`（或 `make test`）覆盖：

- 固定用例：空图、孤立点、自环、多重边去重、单环、双 SCC + 平行跨边、
  纯 DAG、完全图、大 SCC 内含自环、混合情形；
- 错误用例：JSON 畸形、缺字段、负数/超限顶点数、端点越界、非整数端点等；
- 随机对拍：300 个随机小图（n ≤ 12，含自环与多重边），Tarjan 结果与
  可达性矩阵参考逐位比对，且每个结果都过独立验证器；
- 确定性：同一请求连跑 5 次输出完全一致；
- 大图冒烟：n = 100 000 链 + 回边构成单 SCC，验证规模上限内可用。

## 项目结构

```
Makefile               构建与测试入口
src/json.{hpp,cpp}     自包含 JSON 解析/序列化（对象键有序，输出确定）
src/scc.{hpp,cpp}      图解析校验、迭代 Tarjan、循环见证、缩点边、朴素参考
src/main.cpp           JSON 接口 CLI
src/verify_main.cpp    独立结果验证器
examples/              请求样例（双 SCC、自环+多重边、孤立点、纯 DAG）
tests/run_tests.py     自动化测试驱动
```

## 实际运行记录

以下为本仓库在 Linux（g++，`-O2 -Wall -Wextra -pedantic`）上的真实运行
结果，未做任何修饰。

```
$ make
g++ -std=c++17 -O2 -Wall -Wextra -pedantic -c -o build/json.o src/json.cpp
g++ -std=c++17 -O2 -Wall -Wextra -pedantic -c -o build/scc.o src/scc.cpp
g++ ... -o build/scc_backend ...
g++ ... -o build/scc_verify ...
（编译零警告）

$ python3 tests/run_tests.py
== fixed cases ==
  PASS empty graph
  PASS isolated vertices only
  PASS self loop singleton
  PASS multi edges dedup
  PASS single cycle
  PASS two scc with parallel cross edges
  PASS pure dag
  PASS complete digraph
  PASS self loop inside larger scc
  PASS mixed isolated selfloop cycle
== error cases ==
  PASS malformed json
  PASS missing edges
  PASS missing vertices
  PASS negative vertices
  PASS vertices over limit
  PASS edge endpoint out of range
  PASS negative endpoint
  PASS edge not a pair
  PASS non-integer endpoint
== randomized cross-validation (tarjan vs reference) ==
  PASS fuzz 300 random graphs (n<=12, self-loops+multiedges)
== determinism ==
  PASS identical output across 5 runs
== large graph smoke test (limit size) ==
  PASS chain+loop n=100000 in 0.43s

22 passed, 0 failed

$ for f in examples/*.json; do ./build/scc_backend "$f" > /tmp/res.json \
    && ./build/scc_verify "$f" /tmp/res.json; done
VERIFY OK: vertices=4 components=4 condensation_edges=4 dag=true maximality=checked
VERIFY OK: vertices=6 components=5 condensation_edges=2 dag=true maximality=checked
VERIFY OK: vertices=4 components=4 condensation_edges=2 dag=true maximality=checked
VERIFY OK: vertices=5 components=2 condensation_edges=1 dag=true maximality=checked
```

**未通过项：无。** 已知限制（非失败）：n > 3000 时验证器跳过最大性全量
校验（输出 `maximality=skipped`），其余校验（划分、见证、缩点边精确
匹配、无环）在全规模内仍然执行。
