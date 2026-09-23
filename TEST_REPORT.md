# 测试与运行记录（TEST_REPORT）

本文件如实记录在交付环境上的实际命令与结果。环境：

- OS：Linux 6.8.0-90-generic (Ubuntu 24.04)
- 编译器：g++ 13.3.0（`-std=c++17 -O2 -Wall -Wextra -Wpedantic`）
- make：GNU Make 4.3；Python：3.12.3（端到端测试仅用标准库）
- 日期：2026-09-24

---

## 1. 干净构建 + 全量测试（最终状态）

命令：

```bash
make clean && make test
```

实际输出（摘自 `test_run.log`）：

```
mkdir -p build
g++ -std=c++17 -O2 -Wall -Wextra -Wpedantic -c src/geometry.cpp -o build/geometry.o
g++ -std=c++17 -O2 -Wall -Wextra -Wpedantic -c src/simplification.cpp -o build/simplification.o
g++ -std=c++17 -O2 -Wall -Wextra -Wpedantic -c src/json.cpp -o build/json.o
g++ -std=c++17 -O2 -Wall -Wextra -Wpedantic src/main.cpp build/geometry.o build/simplification.o build/json.o -o build/simplify
g++ -std=c++17 -O2 -Wall -Wextra -Wpedantic tests/test_unit.cpp build/geometry.o build/simplification.o build/json.o -o build/test_unit
./build/test_unit
Running unit tests...
77 checks, 0 failures
python3 tests/run_e2e.py --bin ./build/simplify
Running end-to-end tests...
519 checks, 0 failures
```

**结果：编译零警告；C++ 单元测试 77 项检查全过；端到端 519 项检查全过；未通过项 = 0。**

---

## 2. 验收要求对照

| 验收点 | 覆盖位置 | 结果 |
|---|---|---|
| **回折**（含非相邻重复点） | 单元 `TestDPFoldback`（对称 U 形，多档 epsilon）；端到端 `foldback-*`、`foldback-multi` | 全过 |
| **自交** | 单元 `TestDPSelfIntersect`（蝴蝶结，小/大 epsilon + 单侧界）；端到端 `self-intersect-*`（含自交尖刺） | 全过 |
| **重复点**（零长段） | 单元 `TestDedup`、`TestDPZeroLengthSegments`；端到端 `duplicates-*`、`all-identical` | 全过 |
| **阈值为零** | 单元共线/锯齿 `eps=0`；端到端 `zero-eps-*`（共线、锯齿、重复+共线） | 全过 |
| **逐点检查约定误差界** | 端到端用 Python **独立重算**每个原始点到简化折线的距离，逐项与程序报告值及 `within_bound` 比对，并断言 `max ≤ epsilon + tol`；C++ 侧另有 DP 不变量穷举 | 全过 |
| **确定性结果** | 单元同输入两次逐位比对、并列取最小下标；端到端同一请求连跑 5 次断言 stdout 字节级一致 | 全过 |
| **端点保留 / 零长段处理** | 所有成功用例断言首尾点保留；重复点删除下标核对 | 全过 |
| **不声称双向 Hausdorff** | 响应 `bound_semantics` 固定为 `one_sided_original_to_simplified`；端到端断言该字段；README 第 3 节明确反向不保证 | 符合 |
| **错误输入与退出码** | 端到端 `test_errors`：缺字段/类型错/负 epsilon/非有限数/非法 JSON/空输入/文件缺失 | 全过 |

端到端独立校验逻辑见 `tests/run_e2e.py` 的 `point_to_segment /
point_to_polyline / verify_success`：它不链接被测 C++ 代码，几何量在
Python 中重新计算，因此“程序报告的距离”与“误差界是否成立”是被独立验证的。

---

## 3. 典型请求实测

### 3.1 回折（examples/foldback.json，epsilon=0.95）

```bash
./build/simplify examples/foldback.json
```

保留点 `source_index = [0, 2, 4]`；两个回折点（下标 1、3）到简化折线
距离均为 `0.89442719099991586 < 0.95`，`within_bound = true`，
`max_distance = 0.89442719099991586`。

注：`epsilon` 回显为 `0.94999999999999996`，这是 `0.95` 的 binary64
精确值经 `%.17g` 输出，属预期（见 README 精度约定）。

### 3.2 重复点 + 零阈值（examples/duplicates_zero_threshold.json）

输入 6 点（含两处相邻重复），删除原始下标 `[1, 4]`；eps=0 下共线点全部
去除，保留 2 个端点，`max_distance = 0`。

### 3.3 自交（examples/self_intersect.json，epsilon=1）

蝴蝶结 4 点全部保留（各点到弦距离超过 1），`within_bound = true`。
当 epsilon=5 时仅保留端点 `(0,0)-(0,4)`，此时点 `(4,4)` 的单侧距离为
`4 ≤ 5`，单侧界成立——程序并未对反向（弦上的点到原折线）作任何承诺。

### 3.4 非法输入

```bash
$ echo '{bad json' | ./build/simplify ; echo exit=$?
{"error":{"code":"invalid_json","message":"JSON parse error at byte 1: expected string key in object"},"status":"error"}
exit=2

$ echo '{"epsilon":-0.1,"points":[]}' | ./build/simplify ; echo exit=$?
{"error":{"code":"invalid_value","message":"'epsilon' must be >= 0"},"status":"error"}
exit=1
```

全部样例逐个运行结果（`for f in examples/*.json; do ./build/simplify "$f"; done`）：

```
basic_zigzag.json              kept=5/5 max_dist=0        within=True
duplicates_zero_threshold.json kept=2/6 max_dist=0       within=True
error_negative_epsilon.json    status=error invalid_value (exit 1)
foldback.json                  kept=3/5 max_dist=0.894427 within=True
self_intersect.json            kept=4/4 max_dist=0        within=True
single_point.json              kept=1/1 max_dist=0        within=True
```

---

## 4. 健全性 / 规模测试

### 4.1 消毒器（ASan + UBSan）

用 `-fsanitize=address,undefined -O1 -g` 重新编译单元测试与主程序，再分别
运行单元测试与完整端到端套件：

```
./build/test_unit_san          -> 77 checks, 0 failures   （无 ASan/UBSan 报告）
run_e2e.py --bin simplify_san  -> 519 checks, 0 failures  （无 ASan/UBSan 报告）
```

未发现越界、未初始化读、内存泄漏或未定义行为。

### 4.2 规模与耗时（同一正弦+噪声数据，seed 固定，epsilon=0.02）

| 输入点数 n | 保留点数 k | 墙钟时间 |
|---|---|---|
| 100 | 24 | < 0.01 s |
| 1,000 | 204 | 0.01 s |
| 10,000 | 2,052 | 0.40 s |
| 50,000 | 10,010 | 8.06 s |
| 200,000 | 40,320 | 131.6 s |

200,000 点用例结果：`status=ok`，`max_distance=0.01999674271217301 ≤ 0.02`，
`within_bound=true`，退出码 0。耗时随 n·k 增长，瓶颈在逐点验证
（O(n·k)，定义固有，见 README 第 9 节）；DP 本身为迭代实现，200k 点
无栈溢出。该规模仅作压力验证，非功能验收门槛。

---

## 5. 开发过程中出现、已定位并修复的问题（如实记录）

以下问题在开发自测中被测试发现，均已修复并加入回归，最终状态为全绿：

1. **JSON 容器浅拷贝导致的自引用别名（真实实现缺陷）**。
   `json::Value` 最初用 `shared_ptr` 持有数组/对象且使用默认拷贝构造，
   拷贝只复制指针。`MakeError` 中“把子对象 `e` 放进根对象后继续操作 `e`”
   会经由同一底层容器改到根对象自身，产出错误 JSON
   （字符串字段被序列化成 `true`）。
   端到端测试以大面积 `status ... got=True` 暴露。
   **修复**：为 `Value` 实现深拷贝构造/赋值（`json.hpp` 的 `CopyFrom`）。

2. **`const char*` 被错误转换成 `bool`（真实实现缺陷）**。
   仅有 `Value(bool)` 与 `Value(std::string)` 构造时，字符串字面量
   `"ok"` 走“指针→bool”的标准转换（优先于“指针→std::string”的
   用户定义转换），导致所有字符串字段变成布尔 `true`。
   **修复**：增加 `Value(const char*)` 重载；并在单元测试中加入
   “字面量是字符串、显式 bool 仍是 bool”的回归断言。

3. **若干测试期望值手算错误（测试本身问题，非算法问题）**。
   初版测试按“点到无限延长线的垂直距离”心算，而实现刻意采用
   “点到闭线段（端点钳制）”距离，二者在回折/分裂后弦改变时不同。
   首次 `make unit-test` 出现 4 处失败：空折线距离的 inf 比较方式、
   以及 3 处回折/共线期望下标。逐条手工复核几何后更正为基于
   闭线段度量的正确期望值（对称 U 形回折），算法输出本身正确。

4. **Makefile 缺少头文件依赖（构建问题）**。
   修改 `json.hpp` 后未触发 `main.cpp` 重链接，一度让人误判问题未修复。
   **修复**：加入 `HEADERS := $(wildcard src/*.hpp)` 依赖。

修复后从干净状态全量重建，单元与端到端测试均 0 失败。

---

## 6. 当前未通过项 / 已知限制

- 自动化测试：**无未通过项**（77/77 单元、519/519 端到端，ASan/UBSan 干净）。
- 已知非缺陷限制：逐点验证为 O(n·k) 朴素实现，超大输入较慢（离线可接受，
  README 第 9 节已说明）；输出按 JSON 键名字母序排列（`std::map`），
  仅风格问题；不提供双向 Hausdorff，且这是明确的设计取舍而非遗漏。
