# 运行记录（RUNLOG）

本文件记录项目在交付环境中的**实际**执行情况，包括过程中出现过的失败与
修复，未作美化。

- 日期：2026-09-25
- 环境：Ubuntu 24.04（Linux 6.8.0-90-generic，x86_64）
- 工具链：g++ 13.3.0（C++17）、GNU Make 4.3、Python 3.12.3、curl 8.x
- 依赖：无第三方库（JSON 解析为自带实现 `src/json.hpp`）

## 1. 构建

命令：

```bash
make clean && make
```

结果：**通过**，编译开关
`-std=c++17 -O2 -Wall -Wextra -Wpedantic -Wshadow -Wconversion -Wsign-conversion`
下零警告零错误，产物 `build/bipartite-matching`。

过程中出现过并已修复的编译问题：

1. `std::isfinite` 未声明 → 补充 `#include <cmath>`；
2. `for (unsigned char c : raw)` 触发 `-Wsign-conversion` → 改为先取 `char`
   再显式转换；
3. `brute.cpp` 最初漏写 `#include <functional>`（std::function），已补。

## 2. 自动化测试

命令：

```bash
python3 tests/run_tests.py        # 等价于 make test
```

最终结果：**全部通过，3571 项检查，退出码 0**。

```
[run] random graphs vs exhaustive enumeration
       +3501 checks passed
[run] fixed structural examples
       +24 checks passed
[run] duplicate edges and warnings
       +12 checks passed
[run] error handling and limits
       +12 checks passed
[run] batch / file modes
       +8 checks passed
[run] larger instance smoke test
  info: graph 2000x2000 with 808 edges solved in 0.006s, matching=584, solver_us=1627
       +3 checks passed
[run] HTTP service
       +11 checks passed

ALL TESTS PASSED: 3571 checks
```

其中“随机小图穷举”一节为 350 个随机图（每图约 10 项断言），测试内置
**独立 Python 穷举参考**（回溯枚举所有匹配；按基数递增枚举顶点子集求最小
覆盖），与 C++ Hopcroft–Karp/Kőnig 结果逐一比对；用例显式覆盖孤立点、
重复边、负数/稀疏 ID。

### 过程中出现过的失败（均已修复，记录在案）

1. **HTTP 测试挂起**：首版测试把 `proc.stderr.read()` 作为 `check(...)` 的
   参数，Python 立即求值导致阻塞，等待永不退出的服务器 stderr EOF。
   修复：先做 `wait_for_port` 再检查，移除提前 `read()`。该挂起导致首轮
   运行超过 300s 被超时终止，并非算法问题。
2. **2 项用例数据错误（测试本身的 bug，非被测代码）**：
   - C8 偶环用例把左右两侧都声明成 `[0,1,2,3]`（ID 重叠被服务端正确拒绝，
     是正确行为），但边使用的是 `10+u / 20+v`。已改为声明
     `[10..13] / [20..23]`。
   - “顶点超限”用例右侧声明 `[1]`，与左侧 `0..10000` 中的 id 1 重叠，
     先命中了“两侧 ID 重叠”错误而非 `LIMIT_EXCEEDED`。已改为右侧 `[-1]`。
3. 修复后再跑：3569/3569 通过。无未通过项遗留。

## 3. 手工接口验证（实际命令与结果）

### 3.1 stdin / 样例文件

```bash
./build/bipartite-matching --file examples/request_basic.json
./build/bipartite-matching < examples/request_batch.json
```

结果（输出已保存到 `examples/output/`）：

- `request_basic.json`（含 1 个孤立点 10、1 条重复边）：
  `matching_size=3`、`minimum_vertex_cover_size=3`、
  `verification.verified_optimal=true`、
  `brute_reference.agrees=true`、warnings 提示重复边已去重。
- `request_edge_pairs.json`（`[u,v]` 边形式、负数 ID、孤立点 404/-5）：
  `matching_size=3`，覆盖 `L:100,L:200,L:300`。
- `request_empty_edges.json`：`matching_size=0`、覆盖为空集。
- `request_batch.json`（3 个请求的数组）：批结果匹配数 `[2, 1, 2]`。

### 3.2 HTTP 服务（curl）

```bash
./build/bipartite-matching serve --port 18080
curl -s http://127.0.0.1:18080/health
curl -s -X POST http://127.0.0.1:18080/solve -d '{ ... }'
```

实际结果（完整转录见 `examples/output/http_curl_session.txt`）：

- `GET /health` → 200 `{"status":"ok",...}`；
- `POST /solve`（basic 请求，带 `brute_force`）→ 200，匹配数 3、覆盖 3、
  `verified_optimal=true`、`brute_reference.agrees=true`；
- 非法 JSON `{bad` → **HTTP 400**，`error.code=INVALID_JSON`；
- 两侧 ID 重叠等 `INVALID_REQUEST` → **HTTP 400**；
- 超规模请求 → **HTTP 413**，`error.code=LIMIT_EXCEEDED`；
- `GET /solve` → **HTTP 405**；未知路径 → **HTTP 404**。

## 4. 规模上限性能实测

生成左右各 10 000 顶点、200 000 条去重边的随机图（达到设定上限）：

```bash
/usr/bin/time -v ./build/bipartite-matching --file /tmp/big.json
```

实际结果：

```
ok: True | left/right: 10000 10000 | edges: 200000
        | matching: 10000 | cover: 10000
        | verified: True | solver_us: 308675
Elapsed (wall clock): 0:00.51
Maximum resident set size: 103988 kbytes (~102 MiB)
```

另：2000×2000、808 条边的随机稀疏图求解约 1.6 ms（见测试输出）。

## 5. 内存与未定义行为检查

使用 AddressSanitizer + UndefinedBehaviorSanitizer 单独构建（**在最终代码
上重跑**，包含 `--file` JSONL 与 HTTP 400/413 错误路径改动）：

```bash
g++ -std=c++17 -O1 -g -fsanitize=address,undefined -fno-omit-frame-pointer \
    src/main.cpp src/bipartite.cpp src/brute.cpp \
    src/protocol.cpp src/http_server.cpp -o /tmp/bipartite-asan
# 30 个随机小图组成的 JSON 数组（含重复边/孤立点，全部 brute_force）
/tmp/bipartite-asan < /tmp/asan-batch.json
# 另启动 ASan 版 HTTP 服务发起 /health、/solve 与非法请求
```

结果：30 个响应全部 `ok=true` 且 `verified_optimal=true`（穷举全部一致）；
HTTP 路径（含 ID 重叠返回 400）ASan/UBSan **无任何错误报告**。临时产物
已删除。

### 静态分析工具说明

本机未安装 `clang-format` / `clang-tidy` / `cppcheck`（`which` 均无结果），
因此 C++ 规则中建议的这两项未执行；替代措施为：编译开启
`-Wall -Wextra -Wpedantic -Wshadow -Wconversion -Wsign-conversion`
（零警告）+ 上述 ASan/UBSan 运行 + 3571 项自动化断言。

## 6. 已知未通过项 / 局限

- 无未通过的验收项。
- 局限（设计如此，非遗留缺陷）：HTTP 服务为单线程、无鉴权、无 TLS，定位
  本地/回环使用；朴素穷举参考限定总顶点 ≤ 20、边 ≤ 80，超出时返回
  `BRUTE_FORCE_TOO_LARGE` 结构化错误；核心求解器的服务限界为单侧
  10 000 顶点、200 000 边。
