# bmatch — 二分图最大匹配 + 最小顶点覆盖证书（纯后端）

C++17 实现的二分图最大匹配服务。核心求解为自实现的 **Hopcroft-Karp** 算法
（不调用任何现成求解器），并依据 **Kőnig 定理** 由最大匹配导出**最小顶点覆盖**
作为最优性证书。输入输出均为 JSON，保留调用方的原始顶点 ID。

另提供一个**朴素参考实现**（bitmask DP 穷举，仅适用小规模），用于交叉验证。

## 构建与运行

```bash
make            # 构建 build/bmatch 与 build/run_tests（需 g++ 支持 C++17）
make test       # 构建并运行全部自动化测试
```

CLI 用法：

```bash
./build/bmatch examples/request_basic.json     # 从文件读请求
cat request.json | ./build/bmatch              # 从 stdin 读请求
```

退出码：`0` = 求解成功（`ok:true`）；`1` = 请求校验失败；`2` = IO/JSON 解析失败。

## 请求格式

```json
{
  "left":  ["alice", "bob", "carol"],
  "right": ["task1", "task2", "task3"],
  "edges": [["alice", "task1"], ["alice", "task2"], ["bob", "task1"], ["carol", "task3"]]
}
```

- 顶点 ID 为字符串或整数，输出中原样保留。
- 边为 `[左ID, 右ID]` 二元组；引用的顶点必须已在 `left`/`right` 中声明。
- **孤立点**：只出现在 `left`/`right` 而不出现在任何边中，合法。
- **重复边**：自动去重，数量计入响应的 `stats.duplicate_edges_ignored`。
- 顶点 ID 重复、边引用未声明顶点、字段缺失/类型错误：返回
  `{"ok": false, "error": "..."}`，退出码 1。

### 规模限制

| 项 | 上限 |
|---|---|
| 每侧顶点数 | 20 000 |
| 边数（去重前） | 200 000 |

超限返回 `ok:false` 及错误说明。

## 响应格式

```json
{
  "ok": true,
  "verified": true,
  "matching_size": 3,
  "matching": [{"left": "alice", "right": "task2"}, ...],
  "vertex_cover": {"left": ["alice", "bob", "carol"], "right": [], "size": 3},
  "certificate": {
    "theorem": "konig",
    "matching_valid": true,
    "cover_covers_all_edges": true,
    "cover_size_equals_matching_size": true
  },
  "stats": {"left_vertices": 3, "right_vertices": 3, "edges": 4, "duplicate_edges_ignored": 0}
}
```

`certificate` 中的三个标志是服务端在响应前**独立重查**的结果（可验证证据）：

1. `matching_valid` — 匹配中每条边真实存在、无顶点被重复使用、两侧匹配数组一致；
2. `cover_covers_all_edges` — 顶点覆盖覆盖每一条输入边；
3. `cover_size_equals_matching_size` — |覆盖| == |匹配|（Kőnig 定理，证明匹配最优）。

三者全真时 `verified` 为 `true`。

## 算法说明

- **最大匹配**：Hopcroft-Karp，复杂度 O(E·√V)，见 `src/matching.cpp`。
- **最小顶点覆盖**：从未匹配的左部顶点出发做交错 BFS（非匹配边 L→R、
  匹配边 R→L），覆盖 = (L \ 可达L) ∪ (R ∩ 可达R)，即 Kőnig 构造。
- **朴素参考**：`naiveMaxMatchingSize` 用 bitmask DP 穷举右侧顶点子集
  （O(L·2^R)，限 R ≤ 24），仅用于测试交叉验证。

## 项目结构

```
src/json.hpp/.cpp      极简 JSON 解析/序列化（无第三方依赖）
src/matching.hpp/.cpp  Hopcroft-Karp + Kőnig 覆盖 + 朴素参考
src/api.hpp/.cpp       请求校验、ID 映射、证书自验证、响应构建
src/main.cpp           CLI 入口
tests/test_main.cpp    C++ 单元测试 + 随机交叉验证
tests/e2e.py           Python 端到端验收（穷举对照）
tests/run_tests.sh     一键测试脚本
examples/              请求样例（含孤立点/重复边/数字 ID/错误请求）
```

## 自动化测试

`make test` 或 `./tests/run_tests.sh` 执行三层验证：

1. **C++ 单元 + 随机测试**（`build/run_tests`，4 个种子 × 12523 项检查）：
   - 固定用例：空图、孤立点、单边、重复边、路径、K(3,3)、星形图；
   - 2000 个随机小图（≤8×8，含重复边）：Hopcroft-Karp 对拍 bitmask 穷举；
   - 500 个随机中图（≤60×60）：Hopcroft-Karp 对拍独立实现的 Kuhn 增广路算法；
   - 每个实例都验证证书三条件。
2. **Python 端到端**（`tests/e2e.py`，300 个随机小图 + 固定边界用例）：
   通过 CLI 管道求解，用 Python 独立**子集穷举**求最大匹配对照，
   并验证覆盖所有边、|覆盖|==|匹配|、原始 ID 保留、错误请求被拒绝。
3. **样例请求**：`examples/` 下所有请求逐一运行并检查退出码。

## 实测记录（2026-09-25，Ubuntu 24.04，g++ 13.3.0）

| 命令 | 结果 |
|---|---|
| `make all` | 通过（`-Wall -Wextra -Werror` 无警告） |
| `./build/run_tests {1,7,12345,999983}` | 4 种子 × 12523 项检查，0 失败 |
| `python3 tests/e2e.py ./build/bmatch 300 20260925` | 5834 项检查，0 失败 |
| `./build/bmatch examples/*.json` | 输出符合预期；错误样例正确返回 `ok:false`、退出码 1 |
| `./tests/run_tests.sh` | `ALL TESTS PASSED` |

未通过项：无。

开发过程中修复的问题（如实记录）：初版 `matching.cpp` 缺少 `<cstdint>`/
`<functional>` 头文件导致编译失败；测试目标缺少 `-Isrc` 包含路径。均已修复并复测通过。
