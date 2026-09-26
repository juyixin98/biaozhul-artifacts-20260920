# dagpaths — 有向无环图路径计数与第 K 条路径查询（纯后端）

C++17 实现的 DAG 图算法后端，提供 JSON 接口（stdin 文档流 + HTTP 两种传输方式）。
功能：

- **路径计数**：任意源点 `s` → 汇点 `t` 的有向路径条数（支持单对或批量矩阵）；
- **按字典序第 K 条路径**：按节点 ID 序列的字典序，1-based 返回第 K 条路径；
- **反向排名**：给定一条路径，返回它的字典序名次（第 K 条查询的逆运算）；
- **朴素枚举参考**：小规模下图上 DFS 枚举全部路径，供校验；
- 全程**任意精度大整数**，不调用任何现成图论/规划求解器，核心算法为自研实现。

无前端、无第三方运行时依赖（仅 C++ 标准库；集成测试需要 Python 3）。

---

## 1. 目录结构

```
.
├── Makefile
├── README.md
├── src/
│   ├── bigint.h        # 自研任意精度无符号整数（base 1e9）
│   ├── graph.h/.cpp    # DAG 校验、拓扑排序、计数、kth/rank、朴素枚举
│   ├── json.h/.cpp     # 自研严格 JSON 解析/序列化
│   ├── api.h/.cpp      # 请求校验与动作分发（CLI/HTTP 共用）
│   └── main.cpp        # stdin 文档流 CLI + 极简 HTTP/1.1 服务
├── tests/
│   ├── unit_tests.cpp        # C++ 单元测试（自带断言框架，200 项检查）
│   └── test_integration.py   # 端到端：CLI+HTTP，随机 DAG 全量交叉验证
└── examples/          # 9 个请求样例（可直接喂给二进制）
```

## 2. 构建

```bash
make            # 生成 ./dagpaths
```

要求 g++（实测 g++ 13.3，C++17）。无外部库依赖。

## 3. 运行方式

### 3.1 stdin 文档流（默认）

```bash
./dagpaths < examples/01_count.json
```

stdin 被解析为 **JSON 文档流**：支持多行美化格式、紧凑格式、NDJSON（一行一个），
也支持多个文档直接拼接。每个文档在 stdout 输出恰好一行响应。
某个文档是非法 JSON 时，输出一个 `INVALID_JSON` 错误响应，并从下一换行重新同步，
不影响后续文档。

### 3.2 HTTP 服务

```bash
./dagpaths --serve --port 8080     # 默认端口 8080
curl -s -X POST http://127.0.0.1:8080/ \
  -H 'Content-Type: application/json' \
  -d '{"action":"count","graph":{"nodes":2,"edges":[[0,1]]},"source":0,"target":1}'
```

仅接受 `POST /`，带 `Content-Length` 的 JSON 请求体；其他请求返回 `400`。
应用层错误（图非法、K 越界等）仍返回 HTTP 200，错误信息在响应体的 `error` 字段中。

## 4. 请求 / 响应协议

统一信封：

```json
{ "ok": true,  "data": { ... } }
{ "ok": false, "error": { "code": "ERROR_CODE", "message": "..." } }
```

图的表示：节点 ID 为整数 `0 .. nodes-1`，边为 `[from, to]`。
每个请求都**独立携带完整图定义**（无状态服务）。

### 4.1 `count` — 路径计数

请求：

```json
{
  "action": "count",
  "graph": {"nodes": 4, "edges": [[0,1],[0,2],[1,3],[2,3]]},
  "source": 0,
  "target": 3
}
```

`source` / `target` 也可以是整数数组，返回笛卡尔积矩阵（受
`source × target ≤ 100000` 限制）：

```json
{"action":"count", "graph":{...}, "source":[0,1], "target":[2,3]}
```

单对响应：`{"source":0,"target":3,"paths":"2"}`（`paths` 为十进制**字符串**，
因为可能远超 64 位）。批量响应：`{"results":[ {"source":..,"target":..,"paths":".."}, ... ]}`。

约定：**`s == t` 时计数为 1**（该节点上的空路径，长度为 0 的唯一路径）；
不可达时为 `0`。

### 4.2 `kth` — 字典序第 K 条路径

```json
{
  "action": "kth",
  "graph": {"nodes": 5, "edges": [[0,1],[0,2],[1,3],[1,4],[2,4],[3,4]]},
  "source": 0, "target": 4, "k": 2
}
```

响应：

```json
{"ok":true,"data":{"source":0,"target":4,"k":"2","total":"3","path":[0,1,4]}}
```

`k` 为 1-based，可传 JSON 整数或十进制字符串（建议大值一律用字符串，
避免客户端 JSON 数字精度问题，最多 10000 位十进制数字）。
`k > total`（含不可达时 `total = 0`）返回错误码 `K_OUT_OF_RANGE`。

### 4.3 `rank` — 反向排名

```json
{"action":"rank","graph":{...},"path":[0,2,4]}
```

响应 `data`：`{"path":[0,2,4],"rank":"3","total":"3"}`。
路径必须是图中真实存在的相邻边序列，否则返回 `INVALID_PATH`。

### 4.4 `enumerate` — 朴素枚举（小规模参考实现）

```json
{"action":"enumerate","graph":{...},"source":0,"target":4}
```

按字典序返回全部路径，超过 **100000 条**保护上限时返回 `ENUMERATION_LIMIT`。
这是刻意保留的朴素 DFS 参考实现，O(路径数)，只用于小图与交叉验证；
生产查询请用 `count`/`kth`。

### 4.5 错误码

| code | 含义 |
|---|---|
| `INVALID_JSON` | 传输层收到的不是合法 JSON（仅 CLI 流 / HTTP body） |
| `INVALID_REQUEST` | 字段缺失、类型错误、k 非法、未知 action 等 |
| `INVALID_GRAPH` | 节点数非法、端点越界、自环、重边、**含有向环** |
| `K_OUT_OF_RANGE` | `k < 1` 或 `k > 该对路径总数`（不可达对也归此码） |
| `INVALID_PATH` | `rank` 的节点序列不是图中的合法路径 |
| `ENUMERATION_LIMIT` | 朴素枚举超过 100000 条上限 |
| `INTERNAL_ERROR` | 不应出现的内部异常（防御性兜底） |

## 5. 算法与正确性说明

1. **建图与 DAG 校验**：检查节点数、端点范围、自环、重边；用 Kahn 算法做拓扑排序，
   若拓扑序不能覆盖全部节点则图中存在有向环（拒绝）。邻接表在构建时按节点 ID 升序排列。
2. **路径计数**：对固定终点 `t`，沿拓扑序**反向**动态规划：
   `ways[t] = 1`，`ways[u] = Σ ways[v]`（对所有边 `u→v`）。
   `ways[s]` 即 `s→t` 路径数。时间 O(V+E)，只做加法，使用自研 `BigInt` 精确计算。
3. **第 K 条路径（unranking）**：从 `s` 出发，按邻接表升序依次尝试后继 `v`。
   以 `v` 开头的路径恰有 `ways[v]` 条，构成字典序中连续的一块；
   若 `k ≤ ways[v]` 则路径经过 `v`，否则令 `k -= ways[v]` 继续尝试下一个后继。
   时间 O(V+E)。
4. **反向排名（ranking）**：沿给定路径，在每个节点把所有 ID 更小的合法后继
   对应的整块路径数累加，再加 1。它与第 3 步互为逆运算
   （测试中对每条枚举路径验证 `rank(kth(k)) == k` 与 `kth(rank(p)) == p`）。
5. **路径顺序定义（按节点 ID）**：路径按其节点 ID 序列做字典序比较
   （不等长时短序列在前当且仅当它是长序列前缀；本 DAG 设定下同源同汇路径间
   不出现“互为前缀还能同终点”的情况，因此顺序无歧义）。邻接表升序保证
   DFS 枚举顺序 = 字典序。
6. **朴素参考**：`enumerate` 为升序邻接表上的 DFS，显式列出所有路径。

### 大整数 / 溢出策略

- 计数与排名**不使用固定宽度整数**：`src/bigint.h` 为自研 base-10⁹ 任意精度
  无符号整数（加减、比较、十进制解析/格式化）。在允许的图规模内结果**精确无溢出**。
- 接口中的计数字段一律以十进制**字符串**返回；`k` 接受字符串或整数。
- 明确的有界策略（防止资源滥用，均在请求层拒绝而非静默截断）：
  节点 ≤ **10000**、边 ≤ **50000**、`k` ≤ **10000 位十进制数字**、
  批量对数 ≤ **100000**、朴素枚举 ≤ **100000 条路径**、HTTP body ≤ **16 MiB**。

## 6. 测试与实际运行记录

测试命令均可复现：

```bash
make test          # = 单元测试 + 集成测试
make unit-test     # 仅 C++ 单元测试
python3 tests/test_integration.py --binary ./dagpaths   # 仅端到端
```

### 6.1 已实际执行的命令与结果（本仓库开发环境，g++ 13.3 / Python 3.12）

- `make` —— 编译零警告（`-Wall -Wextra -Wpedantic`）。
- `make unit-test` —— **ALL TESTS PASSED: 200/200 checks passed**。
  覆盖：BigInt 进位/借位/解析/比较；建图拒绝（0 节点、越界、自环、环、重边）；
  菱形图、链、多连通分量、不可达、`s==t`；完全前向图 2^(n-2) 计数；
  **70 节点完全前向图计数 = 2^68（超过 uint64）**；枚举字典序；
  全部枚举路径的 kth/rank 往返；7 节点图所有 (s,t) 对的三重交叉验证；
  JSON 解析边界；API 各动作与全部错误码。
- `python3 tests/test_integration.py --binary ./dagpaths`
  —— **7790 passed, 0 failed / ALL INTEGRATION TESTS PASSED**（约 6 秒）。
  两种传输（CLI 与真实 HTTP socket）各跑一遍：
  - 手工用例：菱形图、**多源多汇/不连通分量**、批量计数矩阵（含不可达对）、
    对角线 `s==t=1`、环/自环/越界/重边拒绝、缺字段、`k=0/-1/"abc"`、
    非法路径、未知 action、70 节点 2^68 精确计数与 `k=total+1` 越界；
  - **24 个随机 DAG**（节点数 1..8、密度 0.2/0.5/0.9，固定随机种子）：
    对每个图的每个 (s,t) 对，用独立 Python DFS 参考实现核对计数、枚举顺序，
    并对全部（或大计数时的首/中/尾）名次验证 `kth` 与 `rank` 互逆；
    不可达对 `kth(k=1)` 必须返回 `K_OUT_OF_RANGE`；`k=total+1` 必须越界。
    每种传输各覆盖 **598 个 rank/kth 配对**。
  - CLI 帧：多行美化/NDJSON/混合流、坏行后恢复；HTTP：非 POST 返回 400、
    坏 JSON 走应用层 `INVALID_JSON`。
- `curl` 手工请求 HTTP 服务：count / kth / 400 / 环拒绝，输出均符合协议。
- 大整数端到端（`examples/09_big_integer_k.json`，66 层双节点全连接，132 节点 260 边）：
  `count = kth(k=2^64) = 18446744073709551616`（精确），对返回路径再调 `rank`
  得到同样的 2^64；`k=1` 路径排名为 1。
- 规模上限实测（10000 节点、50000 边随机 DAG）：count 端到端 **0.09s**，
  结果为 **1507 位**十进制数；大 k 的 kth 查询 **0.10s**，返回路径长 3868。
- 枚举保护：20 节点完全前向图（2^18=262144 条）enumerate 返回
  `ENUMERATION_LIMIT`，同图 count 精确返回 262144；18 节点（65536 条）正常枚举。
- AddressSanitizer + UndefinedBehaviorSanitizer 下编译运行全部单元测试：
  **200/200 通过，无任何 sanitizer 报告**。

开发过程中曾出现的、已修复并复测通过的问题（如实记录）：
初版 CLI 按行解析，多行美化的样例文件被逐行误判为非法 JSON —— 已改为 JSON
文档流解析（支持多行/拼接/NDJSON，坏文档下一换行重新同步），集成测试已覆盖。
另有 4 处测试断言自身的期望值写错（数组长度、转义字符串、误判菱形图某对不可达、
误算层状图指数），均已修正；被测算法代码的行为经独立参考实现核对为正确。

### 6.2 未通过项 / 已知限制

- 截至记录时，**无未通过测试**（单元 200/200、集成 7790/7790、sanitizer 干净）。
- 已知限制：HTTP 服务为演示级单连接串行实现（`Connection: close`），
  无并发、无 TLS、无鉴权，不建议直接暴露到公网；需要时应置于反向代理之后。
- 服务无状态、每次请求携带完整图，未做跨请求图缓存。

## 7. 样例清单

| 文件 | 演示内容 |
|---|---|
| `01_count.json` | 菱形图单对计数 = 2 |
| `02_count_batch.json` | 6×6 批量矩阵，多源多汇/不连通分量 |
| `03_kth.json` | 第 2 条路径 `[0,1,4]` |
| `04_k_out_of_range.json` | k=5 > total=2 → `K_OUT_OF_RANGE` |
| `05_kth_unreachable.json` | 不可达对 k=1 → `K_OUT_OF_RANGE`（total=0） |
| `06_rank.json` | 路径 `[0,2,4]` 反向排名 = 3 |
| `07_enumerate.json` | 朴素枚举全部 3 条路径（字典序） |
| `08_invalid_graph_cycle.json` | 含环图 → `INVALID_GRAPH` |
| `09_big_integer_k.json` | 2^64 大整数 k（建议字符串），精确返回 |

快速体验：

```bash
make
./dagpaths < examples/07_enumerate.json
```
