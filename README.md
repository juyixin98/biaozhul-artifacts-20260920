# diffc — 差分约束诊断后端

纯后端 C++17 项目：求解整数差分约束系统 `x - y <= c` 的可行性，负环返回具体约束 ID，
解做平移归一化，并对输出赋值做自校验，提供可验证证据。**核心求解为自实现的
Bellman-Ford，不调用任何现成求解器**；另附独立编写的朴素小规模参考实现用于交叉验证。
无任何第三方依赖（JSON 解析/序列化亦为手写）。

## 构建与运行

```bash
make            # 生成 ./diffc（g++ -std=c++17 -O2 -Wall -Wextra -Wpedantic，零警告）
make test       # 构建并运行 tests/run_tests.sh
```

## 接口（JSON over stdin/stdout）

| 命令 | 功能 |
|------|------|
| `diffc solve < req.json` | 可行性求解：可行返回归一化赋值+自校验证据；不可行返回负环约束 ID |
| `diffc verify < req.json` | 校验给定赋值，返回被违反的约束 ID 列表 |
| `diffc diagnose < req.json` | 最小矛盾集合（最小不可行子集）候选 |
| `diffc selftest [rounds] [seed]` | 随机系统上交叉验证主求解器与朴素参考 |

退出码：`0` 正常（含"不可行"结论），`1` 输入/用法错误，`2` selftest 失败。

### 请求格式

```json
{
  "variables": ["a", "b", "c"],
  "constraints": [
    {"id": "c1", "var": "a", "minus": "b", "bound": 5}
  ]
}
```

表示约束 `a - b <= 5`。`id` 可省略（自动命名为 `c<序号>`）。`verify` 模式额外要求
`"assignment": {"a": 7, ...}`。

### 规模限制（输入边界强制）

| 项 | 上限 |
|----|------|
| 变量数 | 5000 |
| 约束数 | 20000 |
| 界 \|c\| | 1e9 |
| diagnose 枚举 | 约束数 ≤ 18 才枚举；候选 ≤ 64；子集测试 ≤ 200000 |

## 输出示例（实际运行记录）

### 可行（含零环，约束被顶紧）

`./diffc solve < samples/solve_feasible.json`（约束 c1,c2,c3 构成权重和为 0 的零环）：

```json
{
  "status": "feasible",
  "assignment": {"a": 7, "b": 2, "c": 0, "d": 7},
  "normalization": "translated so that min(assignment) == 0",
  "verification": {"satisfied": true, "violated": []}
}
```

`verification` 块是可验证证据：程序在输出前重新校验赋值满足全部约束；
也可用 `verify` 模式由第三方独立复核（`d` 与 {a,b,c} 不连通，仍被正常赋值）。

### 不可行（负环返回具体约束 ID）

`./diffc solve < samples/solve_infeasible.json`：

```json
{
  "status": "infeasible",
  "negative_cycle": {
    "constraint_ids": ["k1", "k3", "k2"],
    "variables": ["x", "z", "y"],
    "total_weight": -1,
    "explanation": "bounds along this constraint cycle sum to -1 < 0"
  },
  "minimal_infeasible_subset": ["k1", "k2", "k3"]
}
```

负环上的约束本身即一个最小不可行子集（简单环的任意真子集是路径，必然可行）。

### 校验失败（返回被违反的约束 ID）

`./diffc verify < samples/verify_bad.json` → `{"satisfied": false, "violated": ["c1", "c4"]}`

### 诊断：最小矛盾集合候选

`./diffc diagnose < samples/diagnose_two_conflicts.json`（两组互相独立的矛盾 {m1,m2} 与 {m3,m4}）：

```json
{
  "status": "infeasible",
  "minimal_infeasible_candidates": [
    {"constraint_ids": ["m3", "m4"], "size": 2},
    {"constraint_ids": ["m1", "m2"], "size": 2}
  ],
  "enumeration": {"performed": true, "complete": true, "subsets_tested": 18}
}
```

第一个候选来自核心求解器的负环；约束数 ≤ 18 时用朴素参考按子集大小递增枚举，
找出全部最小不可行子集（`complete: false` 表示触到上限、结果不完整）。

## 设计要点

- **图模型**：约束 `x - y <= c` 即有向边 `y -> x`（权 c）。超级源点通过"所有距离初始化为 0"
  等效实现，因此**不连通变量**无需特殊处理。
- **负环提取**：第 n 轮仍发生松弛即存在负环；从被松弛点沿父指针回走 n 步必入环，
  再沿父指针收集整环，输出约束 ID 序列与环总权重。
- **平移归一化**：差分约束对整体平移不变，可行解统一平移到 `min(assignment) == 0`。
- **朴素参考**（`src/reference.cpp`，与主求解器独立编写）：
  1. 小规模暴力枚举——若系统可行，必存在 `[0, (n-1)·maxC]` 盒内的整数解，
     故枚举该盒（组合数 ≤ 2e6 时）逐一检验约束；
  2. 盒过大时退化为教科书式朴素 Bellman-Ford（无父指针、无环提取）。
- **诊断枚举**：按子集大小递增测试，含已知最小集的父集直接跳过，保证找到的
  每个候选都是极小的。

## 自动化测试（tests/run_tests.sh）

14 项断言，覆盖验收点：不连通变量、零环（顶紧值精确校验）、最小矛盾集合候选、
赋值全约束校验、自环矛盾、错误处理（坏 JSON / 未知变量 / 重复 ID / 超限）、
满规模压测（5000 变量 / 20000 约束）、3000 轮随机交叉验证。

## 实际运行记录（本机，g++ 13.3.0，Linux 6.8）

| 命令 | 结果 |
|------|------|
| `make`（clean 全量构建） | 成功，零警告 |
| `./tests/run_tests.sh` | **passed=14 failed=0** |
| `make test` 中 selftest | 3000 轮：可行 1176 / 不可行 1824 / 暴力参考 2329，failures=0 |
| 满规模压测 `solve`（5000 变量 / 20000 约束） | feasible，自校验通过，约 0.10 s |

未通过项：无。（开发过程中曾有一项压测用例因测试脚本自身的变量名生成
bug 失败，修正测试生成器后通过；被测程序无改动。）

## 目录结构

```
src/json.hpp/.cpp       手写 JSON 解析/序列化
src/model.hpp/.cpp      问题模型、规模限制、输入校验
src/solver.hpp/.cpp     核心 Bellman-Ford 求解器（负环提取、归一化、校验）
src/reference.hpp/.cpp  朴素小规模参考（暴力枚举 + 朴素松弛）
src/diagnose.hpp/.cpp   最小不可行子集候选枚举
src/main.cpp            CLI 分发（solve/verify/diagnose/selftest）
samples/                请求样例（可行/不可行/校验/诊断）
tests/run_tests.sh      自动化测试
```
