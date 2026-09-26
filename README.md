# 小规模 SAT 求解后端（纯后端，无前端）

用 C++17 实现的 CNF 可满足性求解后端：**不调用任何现成求解器**，核心为
DPLL 算法（单位传播 + 确定性分支），并输出**可回放的搜索证明记录**，
由**独立检查器**逐事件回放验证。另提供一个朴素的穷举参考求解器作为
小规模真值对照。

## 规模限定（明确声明，不宣称工业规模性能）

| 限制 | 值 | 说明 |
|---|---|---|
| 变量数 | ≤ 64 | 超出直接拒绝 |
| 子句数 | ≤ 4096 | 超出直接拒绝 |
| 子句长度 | ≤ 128 | 超出直接拒绝 |
| 穷举参考变量数 | ≤ 20 | 2^n 枚举，仅作小规模对照 |
| 搜索事件上限 | 2,000,000 | 超出返回 `unknown`，不谎报结果 |

求解器是教学级 DPLL（无子句学习、无启发式），只面向小规模公式。

## 目录结构

```
src/json.hpp       迷你 JSON 解析/序列化（无外部依赖）
src/cnf.hpp/.cpp   CNF 类型、校验与规范化（去重文字、范围检查）
src/dpll.hpp/.cpp  DPLL 求解器：单位传播 + 确定性分支 + 搜索轨迹
src/reference.*    朴素穷举参考求解器（n ≤ 20）
src/checker.*      独立检查器：赋值校验 + 搜索树回放验证
src/main.cpp       JSON 接口主程序（stdin/文件 → JSON 响应）
tests/run_tests.py 自动化测试（边界用例 + 300 组随机对照 + 篡改负例）
examples/*.json    请求样例
Makefile
```

## 构建与运行

```sh
make            # 生成 bin/sat_backend（g++ -std=c++17 -O2 -Wall -Wextra）
make test       # 构建并运行全部自动化测试
sh examples/run_examples.sh   # 跑一遍所有请求样例
```

程序从文件（`argv[1]`）或 stdin 读取一个 JSON 请求，向 stdout 输出一个
JSON 响应。成功退出码 0；请求非法退出码 2 且响应为
`{"status":"error","error":...}`。

## JSON 接口

CNF 表示：`num_vars` 为变量数，变量编号 1..n；`clauses` 为子句数组，
每个子句是文字数组，正整数 `v` 表示变量 v，负整数 `-v` 表示 ¬v。
输入会先做规范化：校验范围、去除子句内重复文字；重言式子句
（同时含 x 与 ¬x）保留，它恒被满足、不会成为单位子句或冲突子句。

### `solve` — DPLL 求解

```json
{"op": "solve", "num_vars": 3, "clauses": [[1, 2], [-1, 3], [-2, -3]], "proof": true}
```

响应（`proof` 可省略，默认 true）：

```json
{
  "status": "sat",                 // 或 "unsat" / "unknown"(超事件上限)
  "assignment": [false, true, false],  // 仅 sat；null 表示无关项
  "proof": [ {"ev": "decide", "var": 1, "value": false}, ... ]
}
```

### `reference` — 朴素穷举参考（n ≤ 20）

```json
{"op": "reference", "num_vars": 3, "clauses": [[1, 2], [-1, 3], [-2, -3]]}
```

```json
{"status": "sat", "num_satisfying": 2, "assignment": [false, true, false]}
```

按掩码 0..2^n-1 顺序枚举（变量 1 为最低位），返回首个满足赋值与
满足赋值总数，用作小规模真值对照。

### `check_assignment` — 独立校验赋值

```json
{"op": "check_assignment", "num_vars": 3, "clauses": [[1, 2], [-1, 3], [-2, -3]],
 "assignment": [true, false, true]}
```

```json
{"status": "ok", "valid": true}
```

赋值可以是部分的（`null` 表示未赋值），但要求每个子句都已被满足；
否则返回 `"valid": false` 并指出第一个不满足的子句。

### `verify_proof` — 独立回放搜索证明

```json
{"op": "verify_proof", "num_vars": 1, "clauses": [[1]], "result": "sat",
 "proof": [{"ev": "unit", "lit": 1, "clause": 0}]}
```

```json
{"status": "ok", "valid": true}
```

### `limits` — 查询内置限制

```json
{"op": "limits"}
```

## 证明格式与独立检查器

求解器把搜索过程记录为事件序列（`proof` 字段）：

| 事件 | 字段 | 含义 |
|---|---|---|
| `decide` | `var`, `value` | 分支决策：对变量 var 赋值 value |
| `unit` | `lit`, `clause` | 单位传播：子句 clause 迫使文字 lit 成立 |
| `conflict` | `clause` | 子句 clause 在当前赋值下被 falsify |
| `backtrack` | `var` | 撤销决策 var 及其层级的所有传播赋值 |

分支规则是**确定性的**：总是选编号最小的未赋值变量，先试 `false`
再试 `true`。检查器（`src/checker.cpp`）不复用求解器代码，独立地
逐事件回放并验证：

- 每个 `unit`：该子句在当前赋值下确实只剩一个未赋值文字且其余全被 falsify；
- 每个 `conflict`：该子句确实被完全 falsify；
- 每个 `decide`：公式尚未全部满足、无待传播的单位子句、无已被 falsify
  的子句；首次分支必须是 `false` 且变量必须是当前最小未赋值变量；
  第二次分支必须是对同一变量试 `true`（即 false 子树已失败）；
- 每个 `backtrack`：必须先有未消化的 `conflict`，且回退的是当前层决策；
- 结论为 `sat`：最终（部分）赋值满足所有子句；
- 结论为 `unsat`：搜索树完整展开——根变量的两个方向都已探索并失败
  （或 0 层直接冲突），不存在未探索的分支。

因此 UNSAT 结论不依赖“相信求解器”，任何篡改（删事件、改文字、
调换分支顺序、谎报结论）都会被检查器拒绝，测试中有对应负例。

## 自动化测试

`make test` 运行 `tests/run_tests.py`，包括：

1. **边界用例**：空公式（sat）、空子句（unsat）、重言式子句、重复文字、
   单位传播链、单位冲突、二元 xor 不可满足；
2. **回溯用例**：鸽巢原理 3 鸽 2 洞（unsat，需真实回溯）；
3. **随机对照**：固定种子生成 300 个随机小公式（n ≤ 8，含空子句/
   重言式/重复文字），DPLL 结果与穷举参考逐一比对，且每个证明都经
   独立检查器验证、每个 sat 赋值都经赋值校验；
4. **篡改负例**：截断证明、翻转首个决策、翻转单位文字、错误子句下标、
   sat/unsat 互谎称——必须全部拒绝；
5. **限制执行**：变量数 65、参考求解 21 变量、文字越界、文字 0——
   必须全部报错。

## 验证记录（实际运行，如实记录）

环境：Linux 6.8.0-90-generic，g++（`-std=c++17 -O2 -Wall -Wextra -pedantic`），
Python 3。日期：2026-09-25。

```
$ make
mkdir -p bin
g++ -std=c++17 -O2 -Wall -Wextra -pedantic -o bin/sat_backend \
  src/main.cpp src/cnf.cpp src/dpll.cpp src/reference.cpp src/checker.cpp
# 构建通过，零警告。
```

首轮 `python3 tests/run_tests.py`：**1028/1031 通过，3 项未通过**，已如实修复：

1. `xor pair unsat: proof accepted by checker` 与 `pigeonhole 3->2` —
   检查器缺陷：`unwinding` 标志在 backtrack 后未允许后续 decide，
   把合法的“回溯后再分支”误判为“冲突后未回溯”。修复：decide 时清除
   该标志（“冲突后未回溯就 decide”仍由“decide 时存在被 falsify 的
   子句”检查兜底）。
2. `reference counts satisfying assignments` — 测试用例本身的期望值
   写错：`(x∨y)∧(¬x∨y)` 等价于 `y`，满足赋值数是 2 而非 3。修正期望值。

修复后完整重跑：

```
$ python3 tests/run_tests.py
...
RESULT: 1031/1031 checks passed
ALL TESTS PASSED
```

```
$ sh examples/run_examples.sh
# 5 个样例全部返回预期结果：
#   solve_sat.json       -> sat, assignment [false,true,false], 3 个证明事件
#   solve_unsat.json     -> unsat, 8 个证明事件（两次决策均冲突）
#   reference.json       -> sat, num_satisfying = 2
#   check_assignment.json -> valid: true
#   verify_proof.json    -> valid: true
```

当前无未通过项。
