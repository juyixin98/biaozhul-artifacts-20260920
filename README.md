# 支配树故障定位（Dominance-based Fault Location）— 纯后端

对**控制流图（CFG）**计算从入口可达节点的：

- **立即支配树（immediate dominator tree，idom tree）**
- **支配边界（dominance frontier，DF）**
- **可达性分类**（入口可达 / 不可达，含不可达环）
- **支配关系查询**（`dominates` / `properlyDominates`）

全部用 C++17 从零实现，**不调用任何现成支配求解器或第三方库**（连 JSON 与 HTTP 都是手写的 POSIX 实现）。除近线性生产算法外，附带两个独立的朴素小规模参考实现用于交叉验证。无前端、无数据库、无外部进程依赖。

## 支配与故障定位的关系

若节点 `d` 支配故障点 `b`（从入口到 `b` 的**每一条**路径都经过 `d`），那么 `d` 一定在故障传播路径上；`idom(b)` 是离 `b` 最近的必经前置节点。支配边界 `DF(b)` 是控制流开始不再受 `b` 约束的汇合点（循环头、多分支汇聚、Φ 节点放置位置），用于判断"故障影响在何处与其他分支合流"。这些是编译器 SSA 构造与基于控制流的故障根因定位的标准原语。

### 形式化定义（仅对入口可达节点成立）

- `d dom b` ⇔ 每条 入口→`b` 路径都包含 `d`；约定每个节点支配自身。
- `idom(b)`：`b` 的支配者集合中离 `b` 最近的那个；`idom(entry)=entry`。
- `DF(b) = { n | ∃ 前驱 p→n : b dom p 且 b 不严格支配 n }`（Cytron et al. 1991）。
- **不可达节点**：`idom=null`、支配者集合为空、支配边界为空；不可达节点连入可达节点的边不影响任何结果。
- 一个 CHK 遍历的经典特例：当存在可达边进入入口（入口自环或回边到入口）时，按形式化定义入口属于其自身的支配边界；本实现显式处理了该情况（详见 `src/dominance.cpp` 注释）。

## 目录结构

```
src/json.hpp        手写 JSON 解析/序列化（零依赖）
src/graph.hpp       有向图、规模校验、入口可达性（BFS）
src/dominance.hpp   三种求解器 + 查询的接口
src/dominance.cpp   Lengauer-Tarjan；集合迭代参考；简单路径枚举参考；DF
src/main.cpp        POSIX socket HTTP/1.1 服务 + JSON API
tests/test_dom.cpp  手算用例 + 4000 图随机差分测试 + 规模/封顶测试
tests/api_test.sh   真实 HTTP 端到端测试（curl + python3 断言）
tests/run_all.sh    一键：清理构建 → 单元/差分测试 → API 测试
examples/           请求样例与保存的真实响应
Makefile
```

## 构建与运行

```bash
make                       # 生成 build/dom-server 与 build/test_dom
./build/dom-server --port 18090
```

要求：g++（C++17）、GNU make。无其他依赖。curl 与 python3 仅测试脚本使用。

### 规模限制（有界服务）

| 项 | 上限 | 超限行为 |
|---|---|---|
| 节点数 | 5000 | HTTP 422 拒绝 |
| 边数 | 50000 | HTTP 422 拒绝 |
| 节点标签长度 | 128 字节 | HTTP 422 拒绝 |
| HTTP 请求体 | 16 MiB | HTTP 400/422 拒绝 |
| 简单路径枚举 | 默认 200000 条（请求体 `pathCap` 可调） | 超限即中止并在响应中注明 |

主算法复杂度 O(E·α(V)) 量级（LT + 路径压缩）；朴素集合迭代为小规模参考，随规模平方增长，不用于规模保证。

## API

所有接口无状态：图随每次请求提交。返回统一信封 `{"ok": true, ...}` / `{"ok": false, "error": "..."}`。

### `GET /healthz`

### `POST /api/analyze`

请求字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `entry` | string，必填 | 入口节点标签 |
| `nodes` | string[]，必填 | 节点标签（唯一、非空） |
| `edges` | array，可选 | `["A","B"]` 或 `{"from":"A","to":"B"}`；重复边去重，自环允许 |
| `include.dominators` | bool | 输出每个节点完整支配者集合（由 idom 链导出） |
| `include.references` | bool | 同时跑两种朴素参考并回传一致性比对结果 |
| `pathCap` | number | 路径枚举上限 |

响应含：`reachable` / `unreachable`、每节点 `idom`（入口为 `null`）、`idomTreeDepth`、`dominanceFrontier`、可选 `dominators`、`idomTreeEdges`，以及 `references` 下的交叉验证布尔值。

```bash
curl -s -X POST -H 'Content-Type: application/json' \
  --data-binary @examples/cfg_fault_location.json \
  http://127.0.0.1:18090/api/analyze
```

主样例图包含：回边 `B3→B2`（自然循环）、循环多出口（`B3→B5`、`B4→B5/B6`）、不可达环 `U1↔U2`、以及不可达节点 `U3` 汇入可达节点 `B4`。核心结果（完整响应见 `examples/response_analyze.json`）：

| 节点 | 可达 | idom | dominanceFrontier |
|---|---|---|---|
| B0（入口） | ✓ | null | [] |
| B1 | ✓ | B0 | [] |
| B2（循环头） | ✓ | B1 | **[B2]** |
| B3（循环 latch） | ✓ | B2 | **[B2, B5]** |
| B4 | ✓ | B2 | [B5, B6] |
| B5 | ✓ | B2 | [B6] |
| B6 | ✓ | B2 | [] |
| B7 | ✓ | B5 | [] |
| U1, U2, U3 | ✗ | null | [] |

`references` 实测：`naiveIterativeRounds=2`，LT 与朴素迭代在 idom / DF / 支配集合上全部一致；简单路径枚举共 12 条入口简单路径，支配集合一致。

### `POST /api/dominates`

请求在同一图结构上额外携带 `queries: [{"a": ..., "b": ...}, ...]`，逐项返回 `known`、`reachableA/B`、`dominates`、`properlyDominates`。自支配为真；涉及不可达节点的严格支配一律为假。示例与真实响应：`examples/dominates_query.json`、`examples/response_dominates.json`。

### 错误码

- `400` JSON 语法错误 / 请求体过大
- `404` 未知路径
- `405` 方法不允许
- `422` 图校验失败（入口未知、标签重复、边引用未知节点、超规模）

## 三套独立求解器如何互相验证

1. **Lengauer-Tarjan（生产）**：半支配子 + 路径压缩 EVAL/LINK，后处理用 Cooper-Harvey-Kennedy 的 idom 树遍历求 DF，并补齐入口边界特例。
2. **朴素集合迭代（参考 1）**：`Dom(entry)={entry}`，`Dom(b)={b} ∪ ⋂ Dom(p)`，逆后序迭代到不动点；idom 取支配集合中最大的严格支配者；DF 直接按 Cytron 的前驱/集合定义计算（与 LT 的 DF 算法相互独立）。
3. **简单路径枚举（参考 2）**：DFS 枚举从入口出发的**全部简单路径**（指数级，带封顶），对到达每个节点的路径节点集合求交集得到支配集合——即"小图枚举入口路径"的定义级实现。

单元测试中的随机差分测试生成 **4000 个随机小图**（含前向/回边/自环/不可达节点），要求三者在 idom、支配集合、支配边界上完全一致；另有 CHK 论文明例、不可归约循环、不可达环等 12 个手算用例。

## 自动化测试

```bash
make test          # 仅单元/差分测试
./tests/api_test.sh          # 启动真实服务跑 HTTP 端到端断言
./tests/run_all.sh           # 一键全流程（推荐）
```

## 实测验证证据（本机实跑，非推断）

环境：Ubuntu 24.04，`g++ 13.3.0`，GNU make 4.3，curl 8.5.0，python3 3.12.3。

命令与结果（完整日志可复现：`./tests/run_all.sh`）：

```
make clean && make        # 零 warning 通过（-Wall -Wextra -Wpedantic -Wshadow）
./build/test_dom
  [fuzz] 4000 random graphs; 4000 cross-checked by simple-path enumeration; disagreements: 0
  [scale] LT on 4000 nodes / 8795 edges: 0~1 ms
  [scale] naive iterative on 1200 nodes: 1 ms (2 fixpoint rounds)
  8173 checks, 0 failures
./tests/api_test.sh       # ALL API TESTS PASSED（50 项）
```

手算用例各自的简单路径枚举计数（节选）：单节点 1、链 3、菱形 5、回边 4、回边入入口 3、多出口 7、不可达环 2（只枚举到可达部分）、不可归约 9、CHK 论文图 17。封顶用例（10 节点完全 DAG，cap=50）按预期抛错并被测试确认。

### 开发过程中被测试抓出并已修复的问题（如实记录）

1. **入口支配边界特例不完整**：差分测试发现"存在可达回边进入入口"时（不止入口自环），CHK 遍历会漏掉 `entry ∈ DF(entry)`。以朴素形式化实现为权威修正，反例已固化为回归用例 `back-edge-into-entry`。
2. **路由顺序**：未知路径的 GET 曾返回 405 而非 404（方法检查先于路径匹配），已修正为先匹配路径。
3. **关停挂起**：glibc 的 `std::signal` 默认带 `SA_RESTART`，信号中断 `accept()` 后自动重启导致服务不退出；改为不带 `SA_RESTART` 的 `sigaction`，SIGTERM/SIGINT 现可干净退出（exit 0）。

**未通过项：无** —— 最终版本地全部测试通过；上述 3 项均为开发期发现并已修复、且有对应自动化测试守护。

## 限制与说明

- HTTP 实现为教学/评测级的逐连接短连接服务，未做并发与 TLS；定位是"可验证的算法后端"，不是生产级 Web 服务器。
- 路径枚举是指数级参考实现，仅用于小规模可信交叉验证；大图会在封顶处中止且不影响主算法结果。
