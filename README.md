# 差分约束诊断后端（Difference Constraints Diagnostics）

纯后端 C++ 服务：判定整数差分约束系统 `x - y <= c` 的可行性，不可行时
返回构成负环的**具体约束 ID**，可行时给出经过**平移归一化**的整数赋值，
并对输出逐条校验。核心求解不调用任何现成求解器或第三方库（JSON 解析亦为
内置极简实现）；另提供朴素小规模 Floyd-Warshall 参考算法交叉核对。

无前端、无 HTTP 服务；接口为 JSON-in / JSON-out 的命令行程序。

## 1. 数学原理

每条约束 `x - y <= c` 建模为约束图中的有向边 `y -> x`，权值 `c`。

- 系统可行 ⇔ 约束图（含一个连向所有顶点的 0 权超级源）**不含负环**。
- 主算法 **Bellman-Ford**：所有距离标号初始化为 0（等价于隐式超级源，
  自动覆盖每个连通分量）。收敛后的距离标号就是一组可行赋值。
- 第 n 轮仍可松弛 ⇒ 存在负环；沿前驱指针回退可重建该环，映射回原始
  约束 ID。沿环求和时变量差望远镜消去，得到 `0 <= <负数>` 的矛盾证据。
- **零权环**（环和恰为 0）不是负环：系统仍可行，环上约束以等式成立。
- 朴素参考 **Floyd-Warshall** O(n³)：仅在 n ≤ 300 时运行，判定与赋值
  与 Bellman-Ford 交叉核对。
- 解不唯一：整体平移不变。按**每个连通分量**平移到"分量内最小值为 0"，
  孤立变量归 0，不连通分量各自独立处理。
- 最小矛盾集合候选：小规模用**删除过滤法**得到不可约（minimal）不可满足
  子集；超阈值回退为负环见证。

## 2. 目录结构

```
.
├── Makefile
├── README.md
├── src/
│   ├── json.hpp              # 内置零依赖 JSON 解析/序列化
│   ├── diff_constraints.hpp  # Bellman-Ford、Floyd 参考、连通分量、最小候选
│   ├── solver_service.hpp    # JSON 请求 -> 求解 -> JSON 响应
│   ├── solver_service.cpp
│   └── main.cpp              # CLI 入口
├── examples/                 # 5 个请求样例
├── tests/
│   ├── test_solver.cpp       # C++ 单元测试（53 项断言）
│   └── run_tests.py          # Python E2E/性质测试（约 2157 项断言，独立参考实现）
└── docs/
    ├── API.md                # JSON 接口规范
    ├── TEST_PLAN.md          # 测试计划、覆盖矩阵、已知限制
    └── output/               # 各样例的实际响应（证据存档）
```

## 3. 构建

需要 g++（支持 C++17）与 GNU make，无任何第三方依赖。

```bash
make            # 生成 build/diffcon_solver
```

实测环境：Ubuntu 24.04，`g++ 13.3.0`，`Linux 6.8.0-90-generic x86_64`，
`Python 3.12.3`。编译标志
`-std=c++17 -O2 -Wall -Wextra -Wpedantic -Wshadow`，**零警告**。

## 4. 使用

```bash
# 文件入参
./build/diffcon_solver examples/01_feasible_disconnected.json
# 标准输入
cat request.json | ./build/diffcon_solver
# 输出到文件
./build/diffcon_solver examples/03_negative_cycle.json -o /tmp/resp.json
```

请求（接口完整说明见 [`docs/API.md`](docs/API.md)）：

```json
{
  "variables": ["x", "y", "z", "isolated"],
  "constraints": [
    {"id": "c1", "x": "x", "y": "y", "c": 2},
    {"id": "c2", "x": "y", "y": "x", "c": 1}
  ],
  "options": {"normalize": true, "minimal_candidate": true, "reference": "auto"}
}
```

规模限制：变量 ≤ 2000，约束 ≤ 8000，`|c| ≤ 10^9`（拒绝浮点 c）。

退出码：`0` 请求合法（可行/不可行皆然）；`2` 请求非法；`1` 用法或 IO 错误。

## 5. 不可行响应示例（负环 + 最小矛盾候选）

`x-y<=1, y-z<=1, z-x<=-4`（环和 -2）外加两条无关约束时，响应给出：

```json
"negative_cycle": {
  "constraint_ids": ["r1", "r3", "r2"],
  "variable_cycle": ["b", "a", "c", "b"],
  "sum_bounds": -2,
  "closes": true
},
"minimal_candidate": {
  "method": "deletion_filter",
  "constraint_ids": ["r1", "r2", "r3"],
  "verification": {"subset_infeasible": true, "irreducible": true}
}
```

无关约束 `r4/r5` 被排除在最小候选之外；候选由后端重跑 Bellman-Ford
独立验证，Python 测试再用独立实现复验一次。

## 6. 自动化测试

```bash
make check      # = C++ 单元测试 + Python E2E（需先 make，Python 仅用标准库）
make test-cpp   # 仅 C++
make test-py    # 仅 Python E2E/性质测试
make sanitize   # ASan + UBSan 下运行 C++ 用例
```

可调随机强度：`python3 tests/run_tests.py --seed 123 --trials 500`。

### 实测结果（本仓库开发机，2026-09-25）

| 命令 | 结果 |
|---|---|
| `make all` | 成功，**零警告** |
| `make test-cpp` | **ALL TESTS PASSED (53 checks, 0 failures)**，exit 0 |
| `make sanitize` | **ALL TESTS PASSED (53 checks, 0 failures)**，ASan/UBSan 无报告，exit 0 |
| `make test-py` | **2157 checks, 0 failures — ALL PYTHON E2E TESTS PASSED**，exit 0 |

Python 套件对 200 个随机系统做了三重比对（后端 / 独立 Python Bellman-Ford /
极小例完整穷举），并对每个返回赋值逐条重算约束、对每个负环重算环和与闭合、
对每个最小候选重证不可约；本轮 200 例中生成了 104 个不可行实例。

样例实际运行（响应已存档于 `docs/output/`）：

```
examples/01_feasible_disconnected.json -> exit 0, feasible=true,  2 个分量
examples/02_zero_cycle.json            -> exit 0, feasible=true,  a-b==1 且 b-c==1（零环紧等式）
examples/03_negative_cycle.json        -> exit 0, feasible=false, 环和 -2, 候选 {r1,r2,r3}
examples/04_two_disjoint_cycles.json   -> exit 0, feasible=false, 候选隔离出单个 2 边环
examples/05_invalid_request.json       -> exit 2, error.code=duplicate_variable
```

规模实测（`elapsed_micros` 为后端自报的 Bellman-Ford CPU 时间）：

| 实例 | BF 轮数 | 松弛次数 | BF 耗时 | Python 端墙钟 |
|---|---:|---:|---:|---:|
| 最坏长链 n=2000, m=1999（逆序边，强制 2000 轮） | 2000 | 1,999,000 | 7.2 ms | 0.024 s |
| 稠密可行 n=2000, m=8000 | 994 | 985,769 | 16.9 ms | 0.061 s |
| n=2000 中含 3 边负环 + 600 杂边 | 5 | — | 0.01 ms 级 | 0.01 s |

**未通过项**：无。开发过程中测试先行曾暴露并已修复 1 个实现缺陷
（n=0 空系统下 Bellman-Ford 的段错误，详见 [`docs/TEST_PLAN.md`](docs/TEST_PLAN.md) §4），
另有 2 处测试断言自身数值写错，已按正确算法输出修正。

## 7. 覆盖的验收点

- [x] `x - y <= c` 整数差分约束可行性求解，核心不调用现成求解器
- [x] 不可行时返回负环的**具体约束 ID**（有序、闭合、环和为负）
- [x] 可行解按连通分量**平移归一化**（分量内 min=0，孤立变量为 0）
- [x] **不连通变量**与孤立变量正确处理（隐式超级源 + 分量划分）
- [x] **零环**判定为可行，且紧等式成立
- [x] **最小矛盾集合候选**（删除过滤，不可约；超规模回退环见证），双重独立验证
- [x] 输出赋值**满足全部约束**（后端自检 + Python 独立逐条复验）
- [x] 朴素小规模参考（Floyd-Warshall O(n³)）交叉核对，另有穷举参考
- [x] 限定规模并返回明确错误
- [x] 源码、README、请求样例、自动化测试齐备，命令与结果如实记录
- [x] 无前端

## 8. 已知限制

- 最小候选是"包含意义下不可约（minimal）"，非"全局最小（minimum）"，
  结果依赖输入顺序；删除过滤仅在 n ≤ 150、m ≤ 500 时启用。
- Floyd 参考仅在 n ≤ 300 时运行（O(n³)），其余规模自动跳过并在响应中说明。
- 单线程、批处理 CLI，无 HTTP/长连接封装。
- 开发机未安装 clang-format/clang-tidy/cppcheck，以严格编译警告 +
  ASan/UBSan 替代（见 [`docs/TEST_PLAN.md`](docs/TEST_PLAN.md) §5）。
