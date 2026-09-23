# 区间重叠索引（Interval Overlap Index）

纯后端的时间区间检索服务：端点为 64 位整数、统一**左闭右开** `[lo, hi)`，
支持区间的插入、删除、交集查询和指定时刻的覆盖计数。

- **语言/运行时**：Java（JDK 17+），仅用 JDK 标准库
- **HTTP**：JDK 内置 `com.sun.net.httpserver.HttpServer`（模块 `jdk.httpserver`），无 Web 框架
- **第三方依赖**：**无**（连 JUnit 都不用，测试自带极简断言）
- **数据结构**：两棵增强 AVL 平衡树，子树维护“最大端点”等聚合值（见下）

---

## 1. 快速开始

### 1.1 准备 JDK

需要 JDK 17 或更高版本（用到了 records、switch 表达式、文本块）。

```bash
java -version    # 需要 17+
# 如 java 不在 PATH，设置 JAVA_HOME：
export JAVA_HOME=/path/to/jdk-17
```

本仓库实测版本：**Temurin OpenJDK 17.0.20.1**。
零外部 jar，因此没有需要 `mvn install` / 下载的依赖，依赖锁定见
[`dependencies.lock`](./dependencies.lock)。

### 1.2 编译

```bash
./build.sh
```

产物目录：`build/classes`（主代码）、`build/test-classes`（测试）。

### 1.3 启动服务

```bash
./run.sh                 # 默认 127.0.0.1:8080
./run.sh 9090            # 指定端口
PORT=9090 BIND_HOST=0.0.0.0 ./run.sh   # 允许外部访问
```

启动成功会打印：

```
Interval overlap index listening on http://127.0.0.1:8080
```

### 1.4 运行自动化测试

```bash
./test.sh                       # 编译 + 全部测试
java -Dseed=42 -cp build/classes:build/test-classes intervalindex.TestRunner  # 固定随机种子复现
java -cp build/classes:build/test-classes intervalindex.StressTest 20000 1000000 42  # 2 万次操作压测
```

也可以直接运行 [`examples/curl-demo.sh`](./examples/curl-demo.sh)，
它会在本地临时启动服务、依次调用全部接口、最后关闭服务。

---

## 2. HTTP 接口

所有请求/响应均为 JSON（`Content-Type: application/json; charset=utf-8`）。
区间端点必须是整数；`lo < hi` 必须成立，**空区间与逆序区间返回 400**。

| 方法 | 路径 | 说明 | 请求体 / 参数 |
|---|---|---|---|
| POST | `/intervals` | 插入区间（可一次插多份） | `{"lo":1,"hi":5,"count":1}`，`count` 可省略（默认 1，须为正整数） |
| DELETE | `/intervals` | 删除区间 | body 同上；也支持 `?lo=..&hi=..&count=..`。删不存在的区间返回 404 |
| GET | `/intervals` | 列出全部不同区间及份数 | — |
| POST | `/intervals/overlap` | 交集查询 | `{"lo":2,"hi":6}` |
| GET | `/intervals/coverage?t=3` | 指定时刻覆盖计数 | 查询参数 `t`（整数） |
| POST | `/intervals/coverage` | 覆盖计数（body 形式） | `{"t":3}` |
| GET | `/health` | 健康检查 | — |

### 2.1 插入

```bash
curl -s -X POST http://127.0.0.1:8080/intervals \
  -H 'Content-Type: application/json' \
  -d '{"lo":1,"hi":5}'
# {"inserted":1,"lo":1,"hi":5,"currentCount":1,"distinctIntervals":1,"totalIntervals":1}
```

重复插入同一区间是合法的“多重集”语义，`count` 累加：

```bash
curl -s -X POST http://127.0.0.1:8080/intervals \
  -H 'Content-Type: application/json' -d '{"lo":2,"hi":8,"count":3}'
# {"inserted":3,"lo":2,"hi":8,"currentCount":3,"distinctIntervals":2,"totalIntervals":4}
```

### 2.2 删除

删除指定份数；若剩余份数为 0，该区间从索引中移除。请求份数超过存量时，
只删除存量部分，并在 `removed` 字段返回实际删除数。

```bash
# JSON body
curl -s -X DELETE http://127.0.0.1:8080/intervals \
  -H 'Content-Type: application/json' -d '{"lo":2,"hi":8,"count":2}'
# {"removed":2,"lo":2,"hi":8,"remainingCount":1,"distinctIntervals":2,"totalIntervals":2}

# query string（body 留空即可）
curl -s -X DELETE 'http://127.0.0.1:8080/intervals?lo=2&hi=8&count=10'
```

### 2.3 交集查询

返回所有与查询区间 `[lo,hi)` 相交（`a.lo < b.hi && a.hi > b.lo`）的区间，
结果按 `(lo, hi)` 字典序排列；重复区间聚合为 `count`，同时给出展开后的
`matchCount`。

```bash
curl -s -X POST http://127.0.0.1:8080/intervals/overlap \
  -H 'Content-Type: application/json' -d '{"lo":5,"hi":11}'
# {"query":{"lo":5,"hi":11},
#  "overlaps":[{"lo":0,"hi":10,"count":1},{"lo":2,"hi":8,"count":3},{"lo":10,"hi":20,"count":1}],
#  "matchCount":5}
```

注意半开语义：查询 `[8,10)` 不会命中 `[2,8)`（右端点 8 不包含），
但会命中 `[10,20)` 吗？也不会——它命中的判定是 `10 < 10` 为假。
相邻端点的两个区间**不算相交**。

### 2.4 覆盖计数

时刻 `t` 的覆盖计数 = 满足 `lo ≤ t < hi` 的区间份数。

```bash
curl -s 'http://127.0.0.1:8080/intervals/coverage?t=5'
# {"t":5,"coverage":4}

curl -s -X POST http://127.0.0.1:8080/intervals/coverage \
  -H 'Content-Type: application/json' -d '{"t":5}'
```

### 2.5 列出与健康检查

```bash
curl -s http://127.0.0.1:8080/intervals
# {"intervals":[{"lo":0,"hi":10,"count":1},...],"distinctIntervals":4,"totalIntervals":6}

curl -s http://127.0.0.1:8080/health    # {"status":"ok"}
```

### 2.6 错误响应

| 场景 | 状态码 | 示例 |
|---|---|---|
| 空区间 / 逆序区间 | 400 | `{"error":"invalid interval [3, 3): lo must be < hi; ..."}` |
| JSON 非法 / 缺字段 / 类型错 / 小数端点 | 400 | `{"error":"invalid JSON: ..."}` |
| `count ≤ 0` | 400 | `{"error":"\"count\" must be a positive integer"}` |
| 删除不存在的区间 | 404 | `{"error":"interval [100, 200) does not exist","removed":0}` |
| 路径不存在 | 404 | `{"error":"no such endpoint: /nope"}` |
| 方法不允许 | 405 | `{"error":"PUT not allowed on /intervals"}` |

---

## 3. 数据结构与算法

核心在 [`src/intervalindex/AvlTree.java`](./src/intervalindex/AvlTree.java)
与 [`src/intervalindex/IntervalStore.java`](./src/intervalindex/IntervalStore.java)。

索引由**两棵增强 AVL 树**组成，每个不同区间在树中占一个节点，节点带
`count`（重复份数），每个子树维护三个聚合值：

- `subtreeTotal`：子树内区间份数总和；
- `maxSecondary`：子树内第二端点最大值——**start 树即“子树最大右端点”**，
  这正是题目要求的“维护子树最大端点”；
- `minPrimary` / `maxPrimary`：子树第一端点最小/最大值，用于交集查询剪枝。

| 树 | 键顺序 | primary | secondary（取子树最大值） |
|---|---|---|---|
| startTree | `(lo, hi)` 字典序 | `lo` | **`hi`（子树最大右端点）** |
| endTree | `(hi, lo)` 字典序 | `hi` | `lo` |

**覆盖计数**：`coverage(t) = #{lo ≤ t} − #{hi ≤ t}`。两次“第一端点 ≤ t”的
计数都利用 `subtreeTotal` 在 O(log n) 内沿树累加完成，无需遍历。

**交集查询**：在 start 树上中序搜索，对子树做两个保守剪枝——

1. `子树最大右端点 ≤ lo` → 子树内区间全部结束于查询起点之前（含相邻），跳过；
2. `子树最小左端点 ≥ hi` → 子树内区间全部开始于查询终点之后（含相邻），跳过。

输出复杂度 O(log n + k)（k 为命中数），结果天然按 `(lo,hi)` 有序。

**插入/删除**：AVL 旋转后重算聚合值，O(log n)；删除采用中序后继替换。
重复区间只增减节点 `count`，不新增节点。

所有公共操作经 `ReentrantReadWriteLock` 保护：查询并发、写入独占。
已用 20 个并发连接各插 50 份的方式实测，1000 份计数精确无丢失。

---

## 4. 验收点与测试对应

| 验收要求 | 覆盖位置 |
|---|---|
| 嵌套区间 | `StoreTest.nestedIntervals`（5 层嵌套的覆盖与查询） |
| 相邻端点（左闭右开） | `StoreTest.adjacentEndpoints`、`HttpIntegrationTest` |
| 重复区间（多重集） | `AvlTreeTest.duplicateTests`、`StoreTest.duplicateIntervals` |
| 随机删除后逐次对照暴力扫描 | `PropertyTest`（每轮 400 操作 ×20 轮，定期全量对照）、`runDrainScenario`（随机逐个删光，每步对照） |
| 拒绝空区间和逆序区间 | `AvlTreeTest.expectInvalid`、`HttpIntegrationTest.validationErrors` |
| 增强树不变量 | `AvlTree.verify`：BST 顺序、AVL 平衡、`subtreeTotal`、最大端点、min/max primary |
| 端到端 HTTP | `HttpIntegrationTest`：真实起服务 + JDK HttpClient 调全部接口 |
| 大规模性能/正确性 | `StressTest`：2 万次随机操作 + 400 次随机查询对照暴力 |

属性测试每次打印随机种子，可用 `-Dseed=<种子>` 精确复现。

### 实测结果（本机，Temurin 17.0.20.1）

```
AvlTreeTest OK        ✓ (约 45 ms)
StoreTest OK          ✓ (约 4 ms)
PropertyTest OK       ✓ (约 950 ms，20 轮 × 400 操作 + 删光专项)
HttpIntegrationTest OK ✓ (约 1.7 s，真实 HTTP 端到端)
All 4 test suites passed in ~2.7 s

# 另以种子 1 / 42 / 123456789 / 9999999999 / -12345 各完整跑一遍，全部通过
StressTest OK: 20000 ops, 16966 intervals live, 378 ms total
```

---

## 5. 目录结构

```
.
├── build.sh / test.sh / run.sh     # 编译 / 测试 / 启动（纯 javac，无需 Maven/Gradle）
├── dependencies.lock               # 依赖锁定（零第三方依赖的声明）
├── README.md
├── src/intervalindex/
│   ├── Interval.java               # [lo,hi) 值对象 + 合法性校验
│   ├── AvlTree.java                # 增强 AVL 树（子树最大端点/份数/边界 + 不变量校验）
│   ├── IntervalStore.java          # 双增强树存储：插入/删除/交集/覆盖计数（读写锁）
│   ├── Json.java                   # 极简整数 JSON 解析/序列化（零依赖）
│   └── HttpServerApp.java          # JDK HttpServer 路由与接口实现
├── test/intervalindex/
│   ├── Check.java                  # 极简断言
│   ├── TreeInvariants.java         # 白盒树不变量校验
│   ├── AvlTreeTest.java            # 树与存储单元测试
│   ├── StoreTest.java              # 嵌套/相邻/重复/极值端点定向测试
│   ├── PropertyTest.java           # 随机插入删除 ↔ 暴力扫描逐步对照（验收核心）
│   ├── StressTest.java             # 大规模压力测试（手动运行）
│   ├── HttpIntegrationTest.java    # 真实 HTTP 端到端测试
│   └── TestRunner.java             # 测试入口
└── examples/
    └── curl-demo.sh                # 可直接运行的接口示例脚本
```

---

## 6. 语义约定与边界说明

- 所有端点为 64 位有符号整数（`long`），支持 `Long.MIN_VALUE` / `Long.MAX_VALUE`；
  JSON 数字只接受整数写法，小数/指数形式一律 400。
- 半开区间：`t = hi - 1` 仍被覆盖，`t = hi` 不再覆盖；`[a,b)` 与 `[b,c)` 不相交。
- 区间是多重集：相同 `(lo,hi)` 可插入任意份，覆盖计数与交集计数都按份数计算；
  交集响应同时提供聚合后的 `count` 与展开份数 `matchCount`。
- 服务状态仅保存在内存中，重启即清空（本题未要求持久化）。
- HTTP 服务默认只绑定 `127.0.0.1`；需要对外暴露时显式设置 `BIND_HOST=0.0.0.0`。

## 7. 未完成项 / 已知限制

- 无持久化：数据只在内存，进程重启丢失（需求未要求）。
- 无批量导入/导出接口；当前仅支持单区间增删（可用 `count` 一次插多份相同区间）。
- 查询结果一次性收集到内存返回；对“命中数极大”的场景没有分页/流式输出。
- 仅提供 JSON over HTTP，没有鉴权与限流；默认仅监听本机回环地址。
