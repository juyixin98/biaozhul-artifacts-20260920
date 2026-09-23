# 分区哈希连接（Partitioned Hash Join）纯后端

单机、内存受限的查询引擎，核心运算**全部手写**（不依赖任何 SQL 引擎或第三方库），
以 JSON 作为请求/响应格式，提供 **CLI** 与 **HTTP** 两种入口。支持：

- `INNER` 内连接、`LEFT`（左外）连接；
- 多列连接键，完整 **SQL NULL 语义**（键含 NULL 永不匹配，左连接补全 NULL 行）；
- 建表侧超过内存阈值时**分区落盘**（Grace/Partitioned Hash Join）；
- 热点分区（单一/高频键）**递归换种子再分区**，无法打散时走**有界回退**（分块嵌套循环 BNL）；
- 重复键正确产生**笛卡尔积**；
- **磁盘额度（字节配额）**限制与耗尽报错；
- 执行计划、运行统计、溢写分区文件可导出。

仅需 JDK 17+（开发与验证使用 JDK 21），零外部依赖、零构建工具。

---

## 1. 目录结构

```
src/main/java/phj/
  json/        自写 JSON 解析/序列化（Json、JsonException）
  core/        值模型 Value、多列键 Key、行 Row、关系 Relation、
               连接类型 JoinType、键对 JoinKeyPair、请求/结果模型
  join/        核心引擎 HashJoinEngine、嵌套循环参考实现 NestedLoopJoin、
               溢写存储 SpillStore/SpillFile、磁盘位图 MarkerFile、
               热点检测、有界回退、哈希路由 HashMix、统计收集
  Main.java            CLI 入口
  DataGen.java         样例数据生成器
  QueryRunner.java     请求执行 / 响应组装 / 错误映射
  server/HttpServerMain.java   内置 HTTP JSON 服务（JDK com.sun.net.httpserver）
test/phj/      自研轻量测试框架 + 7 个测试类（51 个用例）
examples/      请求样例与已保存的真实输出（examples/output）
scripts/       build.sh / test.sh / run.sh / serve.sh
```

---

## 2. 构建与运行

```bash
# 编译（产物在 out/classes）
./scripts/build.sh

# 运行全部自动化测试（51 个用例）
./scripts/test.sh

# 对一个请求文件执行连接
./scripts/run.sh examples/inner-spill.json

# 或从标准输入
cat examples/left-spill-null.json | ./scripts/run.sh

# 启动 HTTP 服务（默认 8080，可传端口；0 = 随机端口）
./scripts/serve.sh 8080
```

### HTTP

```bash
curl -s http://localhost:8080/health
curl -s -X POST http://localhost:8080/query \
     -H 'Content-Type: application/json' \
     --data @examples/inner-spill.json
```

- `POST /query`：请求体为查询请求 JSON，响应体为查询响应 JSON。
- HTTP 状态码：`200` 成功；`400` 请求非法；`507` 磁盘额度耗尽；`405/500` 其他。

### CLI 退出码

`0` 成功；`2` 请求非法（INVALID_REQUEST）；`3` 磁盘额度耗尽（DISK_QUOTA_EXCEEDED）；`1` 其他内部错误。

### 样例数据生成器

```bash
# java phj.DataGen <leftRows> <rightRows> <distinctKeys> <nullPct> <joinType> [seed]
java -cp out/classes phj.DataGen 2000 1000 5 10 INNER 42 > big.json
java -cp out/classes phj.Main big.json
```

`distinctKeys=1` 即“全热点键”场景；`nullPct` 为含 NULL 键行的百分比。

---

## 3. 请求 / 响应 JSON 契约

### 请求

```json
{
  "joinType": "INNER",            // 或 "LEFT"
  "keys": ["k"],                  // 左右同名列简写
                                  // 多列 / 异名用配对：[["ka","kra"], ["kb","krb"]]
  "left":  { "name": "orders", "columns": ["oid","k","amt"],
             "rows": [[1,"a",10],[2,null,20]] },
  "right": { "name": "customers", "columns": ["cid","k","city"],
             "rows": [[10,"a","BJ"]] },
  "options": {
    "memoryThresholdRows": 4,     // 建表侧单分区可驻留内存的行数阈值
    "partitions": 0,              // 初始分区数；0/AUTO = 按阈值估算
    "diskQuotaBytes": -1,         // 溢写字节额度；-1 不限，0 完全禁止落盘
    "spillDir": "/tmp/phj",       // 溢写根目录（默认 java.io.tmpdir）
    "keepSpillFiles": false,      // true 保留分区 .jsonl 文件以便检查/导出
    "maxRecursiveLevels": 16,     // 递归再分区最大层数
    "exportPlan": true            // 响应中附带执行计划与统计
  }
}
```

标量类型：整数（JSON number 无小数 → LONG）、浮点（DOUBLE）、字符串、布尔、null。
LONG 与 DOUBLE 按数值比较（`1` 与 `1.0` 相等）；字符串大小写敏感；跨族（数值/字符串/布尔）不相等。

### 响应（成功）

```json
{
  "ok": true,
  "result": {
    "columns": ["oid","k","amt","cid","k","city"],
    "rowCount": 5,
    "rows": [
      { "values": [2,"b",20,12,"b","GZ"],
        "asObject": {"oid":2,"k#1":"b","amt":20,"cid":12,"k#4":"b","city":"GZ"} }
    ]
  },
  "executionPlan": {
    "plan":  { "operator": "partitionedHashJoin", "...": "...", "children": [ ... ] },
    "stats": {
      "outputRows": 5,
      "nullKeyBuildRowsDropped": 1,
      "nullKeyProbeRows": 1,
      "inMemoryPartitions": 3,
      "spillWaves": 1,
      "boundedFallbacks": 0,
      "hotKeyFallbacks": 0,
      "spillPeakBytes": 120,
      "spillLiveBytesAtEnd": 0,
      "diskQuotaBytes": -1,
      "warnings": []
    },
    "spillDir": "..."   // 仅 keepSpillFiles=true 时出现
  }
}
```

- 输出列顺序固定为 **左列在前、右列在后**。
- 重名列在 `asObject` 中以 `列名#位置` 消歧（权威数据请看 `values` 数组）。
- LEFT 无匹配时右侧列全部补 `null`。

### 响应（失败）

```json
{ "ok": false, "errorType": "DISK_QUOTA_EXCEEDED", "errorCode": 507,
  "error": "磁盘溢写额度耗尽……（额度 60 字节，已用 60 字节）",
  "diskQuotaBytes": 60, "usedBytes": 60 }
```

---

## 4. 执行模型与关键设计

### 4.1 总体流程

1. **解析 / 校验**：键列按名解析为位置索引（重名键列报错）。
2. **选建表侧**：
   - `LEFT` 固定以**右表**为建表侧（保证每个左行都被探测、可补 NULL）；
   - `INNER` 选择**行数较少**的一侧建表。
3. 摘出含 NULL 键的行：**不进入哈希表、不参与分区落盘**。
   - INNER 两侧 NULL 行均丢弃；LEFT 左表 NULL 行直接补 NULL 输出。
4. **建表侧有效行数 ≤ 阈值** → 纯内存哈希连接（`HashMap<Key, List<Row>>`），无落盘。
5. 否则进入分区流程（见 4.2）。

### 4.2 分区落盘（Grace Hash Join）

- 按哈希把建表侧、探测侧各切成 P 个分区，逐行以 **JSON Lines** 追加写入溢写文件。
- 逐分区处理：
  - 建表分区行数 ≤ 阈值：整分区读入建哈希表，探测侧**流式扫描**；
    LEFT 用位图（小分区内存位图 / 大分区磁盘位图）记录探测行是否匹配，再补未匹配行。
  - 建表分区超阈值：先做**热点检测**（流式统计不同键数与最高频键频次，键数过多提前终止）。
- 递归与回退见 4.3。

### 4.3 热点分区：递归再分区 / 有界回退

- **可打散**（多个不同键、无单一键超阈值）：换层种子重新哈希成更多子分区，递归处理，
  层数上限 `maxRecursiveLevels`。
- **不可打散**（全分区单一热点键，或某键频次本身已超阈值）或达到层数上限：
  进入 **有界回退 = 分块嵌套循环（Block Nested Loop）**：
  - 建表侧**流式切块**（每块 ≤ 阈值行），逐块建哈希表并扫描一遍探测侧；
  - 内存驻留上界 ≈ 阈值行的建表数据 + 哈希表开销，探测侧始终流式、不整块载入；
  - LEFT 用与探测行数等大的**磁盘位图标记文件**（`.mark`，1 字节/行）记录匹配，
    所有块扫完后再流式扫一遍探测侧，输出未匹配行并补 NULL。

### 4.4 三个容易出错、已被测试钉死的正确性点

1. **分区路由哈希必须与连接匹配哈希同源。**
   连接判定允许 LONG/DOUBLE 数值跨型相等（`1` = `1.0`），
   因此分区路由直接使用 `Key.hashCode()`（与 `Key.equals` 满足 hashCode/equals 契约），
   再叠加每层不同的种子做扰动。绝不能另写一套哈希，否则数值跨型相等的键会被
   拆到不同分区而漏匹配（早期版本确实踩过，被 `fuzzManyThresholdsPartitions` 抓到）。
2. **NULL 语义分层。**
   - 连接键判定（`Key.equals`）：SQL 三值逻辑，NULL 与 NULL 也不相等；
   - 结果行的多重集比较（`Row.equals`）：数据语义，同位置 NULL 视为相等。
   二者刻意区分，各自的 hashCode 都与自身 equals 保持一致。
3. **笛卡尔积。** 哈希表按 Key 聚成 `List<Row>`，探测命中时输出两侧全部行组合。

### 4.5 磁盘额度

`SpillStore` 对所有溢写（分区 `.jsonl` 按 UTF-8 字节累计、左连接 `.mark` 位图按行数）
做统一字节预留；超过 `diskQuotaBytes` 立即抛 `DiskQuotaException`（携带额度与已用字节），
不会先写超再报错。默认（`-1`）不限。`keepSpillFiles=true` 时保留分区文件供检查，
但内部 `.mark` 位图始终删除。

### 4.6 流式输出

`HashJoinEngine.execute(RowSink)` 逐行推送结果，不缓存结果集——
已用 1 亿输出行（10000×10000 全热点）在 `-Xmx64m` 下验证不 OOM。
无参 `execute()` 与 CLI/HTTP 默认仍收集为完整 JSON 响应。

---

## 5. 自动化测试

自研零依赖迷你框架（`@Test` + 反射 + 断言），`scripts/test.sh` 一键运行。**共 51 个用例，全部通过。**

| 测试类 | 覆盖内容 |
|---|---|
| `JsonTest` | JSON 解析/转义/Unicode/错误输入/round-trip |
| `EngineSemanticsTest` | NULL 语义、多列键、LONG/DOUBLE 跨型、笛卡尔积、空表、建表侧选择、补 NULL 列宽 |
| `SpillMechanicsTest` | 落盘触发/不触发、保留与清理、递归再分区、全热点 BNL、左连接位图（内存/磁盘）、分区数配置 |
| `CorrectnessFuzzTest` | **260 个随机用例 + 阈值×分区矩阵 60 例 + 24 个大全热点 + 40 个多列键**，全部与嵌套循环参考实现做**多重集比对** |
| `DiskQuotaTest` | 额度 0、初始分区耗尽、递归阶段耗尽、左连接标记位图耗尽、内存路径不受额度影响、宽限额度成功 |
| `CliEndToEndTest` | 子进程 CLI：stdin/文件输入、退出码 0/2/3、错误类型、计划导出 |
| `HttpServerTest` | 进程内真实 HTTP：/health、/query 200/400/507/405 |

验收要求的场景均有对应用例：

- **多重集比对**：`TestUtil.assertMultisetEquals` 以行频次（HashMap）比较，顺序无关、重复次数敏感；
  每个模糊用例都同时跑 `HashJoinEngine` 与 `NestedLoopJoin`（朴素 O(N·M) 参考实现）。
- **全热点键**：`singleHotKeyTriggersBoundedFallback`、`hotKeyWithLeftJoinMarksAllProbes`、`fuzzLargeHotKeyWithNulls`。
- **空表**：`emptyTables`（左空/右空/双空 × INNER/LEFT）。
- **NULL**：`nullKeySemanticsBothSides`、`allNullKeys`、多列键含 NULL、模糊用例 0/15/60/100% NULL 比例。
- **磁盘额度耗尽**：`DiskQuotaTest` 全部 + CLI/HTTP 的 3/507 用例。

---

## 6. 实际运行记录（本机真实执行）

环境：Ubuntu，OpenJDK 21.0.12，无 Maven/Gradle，零第三方依赖。

### 6.1 全量测试

```
$ ./scripts/test.sh
……
== HttpServerTest ==
  PASS getOnQueryIs405 / healthOk / queryBadRequestReturns400 /
       queryQuotaExceededReturns507 / querySuccess
----------------------------------------
总计 51，失败 0
全部通过 ✅
```

### 6.2 样例逐个执行（命令 + 结果）

| 命令 | 退出码 | 结果 |
|---|---|---|
| `run.sh examples/inner-spill.json` | 0 | `ok=true`，5 行（a 键 2×2 笛卡尔积 + b 键 1 行；NULL 全排除） |
| `run.sh examples/left-spill-null.json` | 0 | `ok=true`，7 行（a 4 行 + b 1 行 + NULL/z 各补 NULL） |
| `run.sh examples/multi-column-keys.json` | 0 | `ok=true`，3 行（仅 (x,10) 双键匹配，含 2×1 笛卡尔积） |
| `run.sh examples/empty-table.json` | 0 | `ok=true`，0 行 |
| `run.sh examples/all-hot-key.json` | 0 | `ok=true`，24 行（6×4 全热点，走 BNL 回退） |
| `run.sh examples/disk-quota-exceeded.json` | 3 | `ok=false`，`DISK_QUOTA_EXCEEDED`/507，额度 60、已用 60 |

完整响应保存在 `examples/output/*.out.json`。

磁盘额度样例的真实错误体：

```
磁盘溢写额度耗尽，无法写入分区 'L0-probe-1'（本次需要 8 字节）（额度 60 字节，已用 60 字节）
```

全热点样例计划中的真实告警：

```
分区 L0-build-3 触发有界回退：分区为单一热点键 [0]（… 行），再分区无法打散
```

### 6.3 HTTP（真实请求）

```
$ ./scripts/serve.sh 47391
PHJ HTTP 服务已启动：http://localhost:47391 （POST /query，GET /health）

$ curl -s http://localhost:47391/health
{"ok":true,"service":"partitioned-hash-join"}

$ curl -s -X POST .../query --data @examples/inner-spill.json
ok= True rows= 5

$ curl -s -o - -w "%{http_code}\n" -X POST .../query --data @examples/disk-quota-exceeded.json
http_status=507
errorType= DISK_QUOTA_EXCEEDED
```

### 6.4 流式 / 大规模与资源

```
10000 × 10000 全热点键（1 亿输出行），java -Xmx64m，流式 sink：
output rows = 100000000 (expect 100000000)
spill peak bytes = 198890
hot fallbacks = 1
```

结果不落地收集，64MB 堆内完成；正确性仍由模糊测试中的多重集比对覆盖。

---

## 7. 范围与未做项（如实说明）

- 无前端（按要求只做后端）。
- 仅等值连接（equi-join）的 INNER / LEFT；未实现 RIGHT/FULL/SEMI/ANTI、非等值条件、投影下推、聚合。
- 内存阈值以“**建表行数**”刻画（题目以内存阈值表述），不是字节级精确内存计量；真正的内存上界保证来自
  “建表分区 ≤ 阈值才整体驻留 + BNL 每块 ≤ 阈值 + 探测侧流式”。
- 单机单线程执行（HTTP 请求线程池除外），无并行分区、无网络 shuffle。
- 溢写文件用 JSON Lines 文本格式（可读、易导出），体积/速度不如二进制列式格式。
- JSON 数字：整数解析为 LONG、含小数/指数为 DOUBLE；超大整数会退化为 double 精度。
