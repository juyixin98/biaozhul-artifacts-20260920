# 确定性分组 TopK 查询引擎（纯后端）

单机内存计算的「分组 TopK」查询引擎，Java 21 实现，**零外部依赖**（不使用任何 SQL 引擎，
JSON 解析与 HTTP 服务均基于 JDK 自带能力）。核心运算为自研的分片聚合 + 可合并中间状态，
输入数据与执行计划可导出为 JSON。无前端。

## 1. 设计要点

### 确定性：稳定唯一序号 + 全序排列

每行在摄入阶段获得一个**全局唯一、稳定**的序号 `seq`（按输入顺序自动分配；也可在数据中显式
提供，需保证唯一，重复即报错）。行比较规则是一个**全序**：

```
先按 value 比较（desc 时大值在前，asc 时小值在前）；
value 相同（并列）时，一律按 seq 升序决定先后。
```

全序意味着任意两行都有确定的先后，因此 TopK 的**成员集合与行顺序都唯一**，与数据如何分片、
分片状态以何种顺序合并无关。

### 可合并中间状态 `TopKState`

每个组维护一个候选集，合并运算满足交换律、结合律（测试中直接验证）：

| 情形 | 行为 |
|---|---|
| 小组（行数 ≤ `groupBudget`） | 全部行保留在内存，最后排序输出（完整内存处理） |
| 大组（行数超过 `groupBudget`） | 立即裁剪为本地 top-k 候选（**预算约束**，内存 O(k)） |

裁剪为本地 top-k 不会损失正确性：任意出现在「全局 top-k」中的行，一定出现在某个分片的
本地 top-k 中。合并只是把两个候选集求并后重复同样的裁剪；由于排序是全序，
`merge(a,b)` 与 `merge(b,a)` 产生等价状态。

约束：`groupBudget >= k`（预算放不下必需的 k 个候选时，请求直接报错）；`k = 0` 合法，
返回每组空行；`k < 0` 非法，引擎抛错、HTTP 返回 400。

### 执行流水线（即导出的执行计划）

```
ingest(规范化并分配 seq)
  → shard(roundRobin / hash 分片)
  → partial-topk(各分片分组聚合，超预算裁剪)
  → merge(sequential / reverse / pairwise 三种合并顺序，结果相同)
  → finalize(组按名字排序，行按全序输出)
```

## 2. 目录结构

```
src/main/java/com/example/topk/
  json/Json.java                 零依赖 JSON 解析/序列化
  model/                         Row / RawRow / QueryRequest
  engine/RowOrdering.java        value + seq 全序比较器
  engine/TopKState.java          可合并、受预算约束的每组中间状态
  engine/GroupedTopKEngine.java  执行引擎、计划生成、文件加载与导出
  server/Main.java               JDK HttpServer JSON 入口（POST /query, GET /health）
src/test/java/.../TestRunner.java  无框架自动化测试（main + 断言）
samples/                         请求与数据样例
build.sh / test.sh / run-server.sh
```

## 3. 构建与运行

需要 JDK（开发/实测环境为 OpenJDK 21），无需 Maven/Gradle：

```bash
./build.sh        # 编译到 out/
./test.sh         # 运行自动化测试（退出码即结果）
./run-server.sh 8080   # 启动 HTTP 服务（默认 8080）
```

## 4. JSON 请求格式（`POST /query`）

```json
{
  "k": 3,                          // 必填，每组保留行数；>= 0，负数非法
  "order": "desc",                 // 可选，desc(默认) | asc
  "shards": 4,                     // 可选，分片数，默认 4（>= 1）
  "shardStrategy": "roundRobin",   // 可选，roundRobin(默认) | hash
  "mergeOrder": "pairwise",        // 可选，sequential(默认) | reverse | pairwise
  "groupBudget": 10000,            // 可选，每组内存行预算，默认 10000（必须 >= k）
  "data": [ {"group": "g1", "value": 12, "seq": 0} ], // 内联数据（seq 可省略）
  "dataFile": "samples/data.json", // 或从 JSON 文件/数组加载（与 data 二选一）
  "exportDir": "out/export-demo"   // 可选，导出 data.json / plan.json / result.json
}
```

响应：`k / order / shards / groupCount / groups / plan`。非法请求返回
`400 {"error": "..."}`。

快速试跑：

```bash
./run-server.sh 8080 &
curl -s -X POST localhost:8080/query -H 'Content-Type: application/json' \
  --data @samples/request.json
curl -s -X POST localhost:8080/query -H 'Content-Type: application/json' \
  --data @samples/request-file.json   # 演示 dataFile + exportDir
```

## 5. 自动化测试与验收

`TestRunner` 共 12 组测试，其中第 1 组是验收主测试：对 **25 个随机种子 × 6 种分片数
(1/2/3/5/8/16) × 2 种分片策略 × 3 种合并顺序 × 3 种预算（=k、k+3、大预算）= 2700 个
引擎配置**逐一执行，并与**全排序基准实现**（整组分桶、全排序、取前 k）逐行比较；随机数据
刻意使用小值域制造大量并列。其余覆盖：`k=0`、并列按 seq、asc、负 k（引擎+HTTP 400）、
重复 seq、大组预算裁剪与 `budget<k` 报错、合并交换律/结合律/单位元、JSON 往返、HTTP 端到端、
文件导出、dataFile 加载。

### 实际运行记录（2026-09-23，OpenJDK 21.0.12，本机）

命令：

```bash
./build.sh && ./test.sh
```

输出（完整原始输出）：

```
==> compiling main sources
==> compiling test sources
==> build OK
      (verified 2700 engine configurations against full sort)
PASS  full-sort baseline holds across shardings and merge orders
PASS  k = 0 yields empty rows for every group
PASS  ascending order inverts value ranking, seq still breaks ties
PASS  tied values are ordered by stable seq
PASS  negative k is rejected by the engine
PASS  explicit duplicate seq is rejected
PASS  large groups bounded by budget still match full sort; budget < k rejected
PASS  state merge is commutative and associative
PASS  JSON parse/write round-trip
PASS  HTTP endpoint: 200 happy path and 400 for negative k
PASS  export writes data.json, plan.json, result.json
PASS  dataFile loading matches inline data

passed: 12, failed: 0
```

未通过项：**无**。

HTTP 手工验证（服务 `./run-server.sh 8088`）的关键结果：

```
合并顺序无关性（同一份含并列的数据，5 分片）：
  sequential -> [(9, 2), (9, 5)]
  reverse    -> [(9, 2), (9, 5)]
  pairwise   -> [(9, 2), (9, 5)]      # 同值 9 并列时取 seq 最小的两行

负 k（{"k":-2,...}）：HTTP 400 {"error":"'k' must be >= 0, got -2"}
k = 0：每个组返回 "rows": []，组仍保留
预算 k=2/groupBudget=2、100 行大组：结果 [(99,99),(98,98)]，plan.boundedGroups=["g"]
budget < k（k=5,groupBudget=2）：HTTP 400 "'groupBudget' (2) must be >= 'k' (5)"
dataFile + exportDir：out/export-demo/ 下生成 data.json、plan.json、result.json
```

## 6. 边界与约定

- `value` 仅接受整数（long），保证排序无浮点不确定性。
- `seq` 全部省略时按输入下标分配；一旦提供就必须全部提供且唯一。
- 输出组按组名字典序排列，行按全序排列，保证输出字节级稳定。
- 纯内存单机实现，目标是确定性与合并语义，不做持久化与分布式调度。
