# 测试计划与覆盖说明

被测对象：`x - y <= c` 整数差分约束可行性诊断后端（C++17，无第三方依赖）。

## 1. 测试分层

| 层 | 文件 | 内容 | 运行 |
|---|---|---|---|
| C++ 单元测试 | `tests/test_solver.cpp` | 算法层与服务层，53 项断言 | `make test-cpp` |
| Python 端到端/性质测试 | `tests/run_tests.py` | 经 CLI 黑盒验证，约 2100+ 项断言 | `make test-py` |
| 手工样例 | `examples/*.json` | 5 个固定场景，响应存于 `docs/output/` | 见 README §6 |

Python 套件**独立于 C++ 实现**：自带 Bellman-Ford 与小规模穷举参考，
不信任后端的自检字段，直接对响应中的赋值/环/候选重新验证。

## 2. 需求点 → 用例覆盖

| 验收需求 | 覆盖位置 |
|---|---|
| 基本可行性求解（无负环给解） | C++ `testSimpleFeasible`；Python 随机 200 例；样例 01 |
| 不可行返回**具体约束 ID** 的负环 | C++ `testNegativeCycleWitness`、`testServiceInfeasible`；样例 03/04；Python `verify_infeasible_response` 重算环和并检查首尾闭合 |
| 不连通变量 | C++ `testDisconnected`（含孤立顶点、3 个连通分量）；样例 01；Python 检查分量划分构成划分且各分量最小值为 0 |
| **零环**（环和恰为 0，可行且等式紧） | C++ `testZeroCycle`（校验两个紧等式）；样例 02 |
| 最小矛盾集合候选 | C++ `testMinimalCandidate`、`testTwoCyclesMUS`（两个不相交负环只取其一）；样例 03/04；Python 独立重证"子集不可行且删任意一条变可行" |
| 解平移归一化 | C++ `testDisconnected`（相对差不变、各分量 min=0）；Python 每个可行随机例与样例都查 |
| 输出赋值满足全部约束 | C++ 每个可行用例调 `assignmentSatisfies`；响应 `verification.all_satisfied`；Python 对返回赋值逐条重算 |
| 朴素小规模参考（Floyd-Warshall） | 每个小规模响应含 `evidence.reference` 交叉核对；C++ 单测直接对照；Python 另用独立 BF + 穷举三重比对 |
| 限定规模 | Python `test_options`/`test_scale`：n=2000/m=8000 可解，2001/8001 被拒，Floyd 在 n>300 自动跳过 |
| 输入校验 | 重复变量/重复 ID、浮点 c、越界 c、非法 JSON、非对象顶层、缺字段 |
| 确定性 | 同一请求两次输出逐字节相同 |
| 边界 | 空变量集、无约束、n=0 时 Bellman-Ford 不崩溃（该路径曾发现并修复段错误） |
| 内存/未定义行为 | `make sanitize` 以 ASan+UBSan 跑全部 C++ 用例 |

## 3. 性质测试设计

`test_randomized`（固定种子 20260925，默认 200 次）：

- n ∈ [1,10]，随机约束数，c ∈ [-3,3]（小界值提高负环出现率）；
- 对每个系统：C++ 后端判定 vs Python 独立 Bellman-Ford 判定必须一致；
- 嵌入的 Floyd 参考判定必须与主算法一致；
- n≤3 且 m≤5 时再与**完整穷举**（[-3m,3m] 盒内枚举）比对；
- 可行：独立校验返回赋值的每条约束；
- 不可行：重算见证环和为负、首尾闭合、最小候选不可约。

## 4. 已发现并修复的缺陷（TDD 实证）

1. **n=0 时段错误**：Bellman-Ford 循环零次后无条件进入"负环"分支，
   对 `predNode[-1]` 取值。修复为仅在第 n 轮确有松弛时才提取负环。
2. 两处**测试断言数值错误**（等式约束界写错、环和期望值写错）——
   由算法正确输出反推修正，属于测试 bug，非实现 bug。
3. `--help` 原输出到 stderr，改为 stdout（错误用法仍写 stderr）。

## 5. 未覆盖 / 已知限制

- 未做 HTTP 封装（需求明确为纯后端，接口即 JSON-in/JSON-out CLI）。
- 未做模糊测试（fuzzing）与多线程压测；求解器为单线程、无共享状态。
- 未安装 clang-format/clang-tidy/cppcheck（本机环境无），以
  `-Wall -Wextra -Wpedantic -Wshadow` 零警告 + ASan/UBSan 替代。
- 最小矛盾集合为"不可约"而非"全局最小"（见 `docs/API.md` §5）。
