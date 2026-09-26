# 小规模 SAT 求解后端（纯 C++，无外部求解器）

一个从零实现的、**限定规模**的 SAT 求解后端：CNF 的 DPLL 搜索（单位传播 +
确定性分支），输出满足赋值或**可回放的完整搜索树证明**，并附带朴素穷举
参考实现与**独立证明检查器**。核心求解不调用任何现成 SAT 求解器，唯一
依赖是 C++17 标准库与 POSIX socket。

## 规模限定（硬性上限，不宣称工业性能）

| 项目 | 上限 |
|---|---|
| DPLL 变量数 | 100 |
| 穷举参考变量数 | 25 |
| 子句数 | 5000 |
| 搜索节点数 | 默认 200 000（可用 `node_limit` 调整） |
| HTTP 请求体 | 1 MiB |

超限请求会被明确拒绝（HTTP 400）或以 `"status": "limit"` 如实返回，
**绝不**在达到上限时猜测 SAT/UNSAT。

## 构建与测试

```bash
make            # 构建 build/sat_solver
make test       # C++ 单元测试 + Python 端到端测试（HTTP/CLI/交叉验证）
```

消毒器构建（ASan+UBSan）：

```bash
g++ -std=c++17 -g -O1 -fsanitize=address,undefined -Isrc \
    tests/test_solver.cpp src/json.cpp src/cnf.cpp src/solver.cpp -o build/test_solver_san
./build/test_solver_san
```

## 使用

### HTTP JSON API

```bash
./build/sat_solver serve 8080        # 监听 127.0.0.1:8080
```

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | 存活检查 |
| POST | `/solve` | DPLL 求解，返回状态 + 模型/证明 |
| POST | `/brute` | 朴素穷举参考（≤25 变量） |
| POST | `/verify` | 独立回放证明，返回 `{"valid": ...}` |

请求体（`/solve`、`/brute`）：

```json
{"num_vars": 3, "clauses": [[1], [-1, 2], [-2, 3]], "node_limit": 100000}
```

- 文字编码：`x > 0` 表示变量 x 为真，`x < 0` 表示 |x| 为假；`0` 非法。
- 空子句 `[]` 恒假（公式直接 UNSAT）；空子句列表 `[]` 恒真（SAT）。
- 重言式子句（同时含 `x` 与 `-x`）在求解前被移除，并在响应的
  `tautology_clause_indices` 中如实报告；重复文字会被合并。

`/solve` 响应（节选，完整样例见 `examples/response_solve_sat.json`）：

```json
{
  "status": "sat",
  "num_vars": 3,
  "normalized_clauses": [[1], [-1, 2], [-2, 3]],
  "tautology_clause_indices": [],
  "model": [{"variable": 1, "value": true}, ...],
  "stats": {"nodes": 1, "decisions": 0, "propagations": 3, "conflicts": 0, "node_limit": 200000},
  "proof": {"root": 0, "nodes": [...]}
}
```

### 命令行

```bash
./build/sat_solver solve examples/sat_chain.cnf          # DIMACS -> JSON（含证明）
./build/sat_solver solve examples/unsat_branch.cnf 1000  # 自定义节点上限
./build/sat_solver brute examples/sat_chain.cnf          # 穷举参考
./build/sat_solver verify examples/response_solve_unsat.json   # 回放验证证明
cat examples/sat_chain.cnf | ./build/sat_solver solve -  # 支持 stdin
```

退出码：`0` 正常（含 SAT/UNSAT），`1` 输入/校验错误，`2` 用法错误，
`3` 达到节点上限。

## 可验证证据：搜索树证明

每次 `/solve` 都输出完整搜索树（`proof.nodes`），节点类型：

- `branch`：决策节点。记录 `decision_var` 与两个子树
  （`positive_child` / `negative_child`）。分支规则完全确定：
  **编号最小的未赋值变量，先尝试赋真**。
- `conflict`：冲突叶子。`conflict_clause` 指向在当前赋值下被完全
   falsified 的（规范化后）子句。
- `sat`：可行叶子。`assignment` 记录完整赋值快照。
- 每个节点还记录进入该节点后执行的单位传播序列
  `propagations: [{literal, reason_clause}]`。

**独立检查器**（`verifyProof`，以及 `tests/sat_check.py` 中完全独立
重写的 Python 版本）只信任 CNF 与记录，逐步回放并强制检查：

1. 每条传播的 reason 子句在当时确实只剩一个未赋值文字，且被强制的就是
   记录的文字；
2. 分支前不存在未传播的单元子句、不存在已 falsified 的子句（即传播
   必须到达不动点）；
3. 冲突叶子的子句确实被完全 falsified；
4. UNSAT 结论要求两个极性的子树都被探索且都不可行；
5. SAT 叶子的赋值覆盖全部变量且满足所有子句；
6. 树无环、无重复访问、无不可达节点，根结论与响应的 `status` 一致。

Python 检查器还会**独立重做规范化**（去重、重言式识别），并与响应中的
`normalized_clauses` / `tautology_clause_indices` 逐一比对，防止求解器
通过篡改规范化结果作弊。

## 目录结构

```
src/json.hpp/.cpp     极简 JSON 解析/序列化（无第三方依赖）
src/cnf.hpp/.cpp      CNF 表示、DIMACS/JSON 解析、规范化、模型求值
src/solver.hpp/.cpp   DPLL 引擎、穷举参考、证明序列化与独立回放检查
src/server.hpp/.cpp   单线程 HTTP/1.1 服务（POSIX socket）
src/main.cpp          CLI
tests/test_solver.cpp C++ 单元测试（含 400 组随机交叉验证、篡改证明检测）
tests/sat_check.py    独立 Python 证明检查器（与 C++ 实现零共享代码）
tests/test_api.py     HTTP/CLI 端到端测试（250 组随机公式三方一致）
examples/             请求/响应样例与 DIMACS 示例
```

## 实测记录（本仓库交付时的真实运行结果）

以下命令均在本机（g++ 13.3，Linux x86-64）实际执行：

| 命令 | 结果 |
|---|---|
| `make` | 编译通过，无警告 |
| `make test`（C++ 单元测试） | `ALL TESTS PASSED` |
| `make test`（Python 端到端） | `ALL PYTHON TESTS PASSED`（全部 ok，无 FAIL） |
| ASan+UBSan 构建运行 | `ALL TESTS PASSED`，无消毒器报告 |
| 2000 组随机公式（1–10 变量）DPLL vs 穷举 | 991 SAT / 1009 UNSAT，全部一致，证明全部通过独立回放 |
| 鸽笼 PHP(5,4)（20 变量 45 子句，UNSAT） | 103 节点 / 51 决策 / 52 冲突，约 4 ms；独立回放通过 |
| 篡改证明（改冲突子句、丢分支、伪造传播） | 全部被检查器拒绝并给出原因 |

未通过项：无。曾发现并已修复的问题：DIMACS 裸 `0` 空子句被丢弃、
GET 请求无 Content-Length 被误判为畸形请求、节点上限测试用例选取不当
（公式在根节点即被单位传播解出，达不到上限）。

## 设计取舍与限制

- **教科书级 DPLL**：无 VSIDS、无子句学习、无 watched literals。这是
  有意为之——证明树因此与搜索过程一一对应，可逐步独立回放。代价是
  无法处理大规模实例，故用硬性上限明确拒绝。
- 证明体积随搜索节点数线性增长；节点上限同时约束证明大小。
- HTTP 服务为单线程、绑定 127.0.0.1，面向本地/测试用途，不是生产网关。
