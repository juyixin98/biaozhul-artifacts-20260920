# 实际运行记录（RUN_LOG）

本机：Linux 6.8.0-90-generic，g++ 13.3.0（C++17），Python 3.12.3，GNU Make 4.3。
日期：2026-09-25。以下命令与结果均为实际执行所得，输出文件存于
`examples/output/`。

## 1. 构建（从零干净构建，零警告）

```bash
$ make clean && make all
g++ -std=c++17 -O2 -Wall -Wextra -Wpedantic -Wshadow -Wconversion -Wno-sign-conversion ...
# 全部编译/链接成功，无 error、无 warning
$ echo $?
0
```

## 2. 全量自动化测试 `make check`

```text
build/unit_tests
C++ unit tests: 17602/17602 checks passed
python3 tests/run_tests.py

Python integration tests: 1054 passed, 0 failed
```

- **C++ 单元测试**：17602 项检查全部通过，包括 300 个随机小多重图的
  Dinic vs 穷举差分测试，覆盖平行边、零容量、自环、双向原图反向边、不连通。
- **Python 集成测试**：1054 项全部通过，覆盖所有样例三角对照、250 个随机图
  穷举交叉验证、已知值 CLRS=23、6 类篡改拒绝、7 种错误输入处理、
  200 节点/1000 边规模时限。

## 3. 四个样例 solve / brute / verify 实跑

```text
== basic ==
max_flow = 15 | brute min_cut = 15 | partitions = 4  | verify = True
  S = ['s']  T = ['a', 'b', 't']
  cut_edges = ['e1', 'e2']  cut_value = 15

== parallel_edges ==
max_flow = 12 | brute min_cut = 12 | partitions = 1  | verify = True
  S = ['s']  T = ['t']
  # p3 容量 0 也出现在割集中（贡献 0）；p1+p2=5+7=12
  cut_edges = ['p1', 'p2', 'p3']  cut_value = 12

== zero_capacity ==
max_flow = 3 | brute min_cut = 3 | partitions = 4  | verify = True
  S = ['s', 'b']  T = ['a', 't']
  # zero 边 dead_path_1 / blocked 虽列出但贡献 0；自环永不跨割
  cut_edges = ['dead_path_1', 'blocked', 'direct']  cut_value = 3

== clrs_network ==
max_flow = 23 | brute = 23 | partitions = 16 | verify = True
  S = ['s', 'v1', 'v2', 'v4']  T = ['v3', 't']
  cut_edges = ['e', 'h', 'j']  # v1->v3 12 + v4->v3 7 + v4->t 4 = 23
```

四个样例均满足 **流值 = 穷举最小割值 = 申报割值**，且独立校验全部通过。

### `verify` 示例输出（basic）

```json
{
  "valid": true,
  "errors": [],
  "source_outflow": 15,
  "sink_inflow": 15,
  "cut_value_computed": 15,
  "residual_reachable_count": 1
}
```

### 残量反向边与原图反向边"分别保存"实证（clrs，节点 v1/v2）

```text
v1:
   -> s  a artificial_reverse rcap=12   # a(s->v1) 的残量反向弧
   -> v2 c forward            rcap=10   # c(v1->v2) 自己的正向弧
   -> v2 d artificial_reverse rcap=0    # d(v2->v1) 的残量反向弧（与 c 分开！）
   -> v3 e forward            rcap=0
v2:
   -> s  b artificial_reverse rcap=11
   -> v1 c artificial_reverse rcap=0    # c 的人工反向弧
   -> v1 d forward            rcap=4    # d 自己的正向弧（不与 c 合并）
   -> v3 f artificial_reverse rcap=0
   -> v4 g forward            rcap=3
```

c、d 这对原图反向边各自占有独立的 `forward` 与 `artificial_reverse` 弧，
四条弧互不混淆，由校验器 C9 逐项核账。

## 4. 篡改与错误输入（负路径）

**篡改 `max_flow`（15→16）被拒，退出码 1：**

```json
{
  "valid": false,
  "errors": [
    "reported max_flow 16 != flow value derived from flows 15",
    "max flow 16 != cut value 15"
  ],
  "source_outflow": 15, "sink_inflow": 15,
  "cut_value_computed": 15, "residual_reachable_count": 1
}
```

其他实跑确认：流量超容量、割集遗漏/多列、汇点同时在两侧、残量弧 kind 被改
等篡改均 `valid=false`（集成测试 6 类全部命中）。

**错误请求，退出码 2：**

| 输入 | 响应 `error` |
|---|---|
| 负容量 -2 | `capacity must be non-negative` |
| 21 节点跑 brute | `brute force supports at most 20 nodes` |
| 非法 JSON `{bad` | `invalid JSON at offset 1: expected string key in object` |

## 5. 规模上限实测（200 节点 / 1000 边 / 容量 ≤ 1e9）

```text
solve: 0.017s  verify: 0.031s
max_flow = 1461663374   valid = true   bfs_rounds = 3
```

## 6. 已知正确值对照

| 图 | Dinic 流值 | 穷举割值 | 结论 |
|---|---|---|---|
| CLRS 6 节点经典网络 | 23 | 23（枚举 16 个割） | 与教科书一致 |
| 双平行 s-t 边 5+7（+0） | 12 | 12 | 平行边容量相加 |
| 零容量阻断 + 直连 3 | 3 | 3 | 零容量不载流 |
| 自环 100 | 5 | 5 | 自环不影响最大流 |

## 7. 未通过项 / 已知限制

- **无未通过项**：全部单元测试（17602）与集成测试（1054）通过，
  干净构建零警告。
- 开发过程中出现并已修复的问题（如实记录）：
  1. `main.cpp` 缺少 `using namespace minjson`，编译失败 → 已修复；
  2. 请求回显对象默认类型为 Null，导致 `request` 序列化为 `null`、
     集成测试一度批量失败 → 初始化为 Object 后通过；
  3. 两处单元测试弧数断言写错（平行边/双向边的弧归属节点）→ 已更正；
  4. CLRS 割边测试期望误记为 `{i,j}`，实际教科书割为 `{e,h,j}=23`
     → 已按实际正确割修正测试。
- 设计限制：容量与流量为 64 位整数（单边上界 10^12）；不处理分数容量；
  穷举参考指数级，节点 >20 显式拒绝而非挂起。
