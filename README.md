# 列式分区裁剪扫描服务 (Columnar Shard Scan with Partition Pruning)

纯后端、**零第三方依赖**的本地列式分片（shard / row group）扫描服务。
只使用 JDK 自带 API：HTTP 用 `com.sun.net.httpserver.HttpServer`，JSON 为手写的最小解析器，
数据落盘为自定义列式二进制文件。每个分片维护每列的 `min` / `max` / `nullCount` 统计，
查询时先做**分区裁剪（partition pruning）**，再对保留分片做范围过滤与聚合。

核心语义保证：

1. **统计只能排除“确定不命中”的分片**；任何缺失统计的分片**必须扫描**（宁可多读，不可漏读）。
2. **NULL 绝不等于 0**：NULL 与任何数值比较结果为 UNKNOWN（行被过滤）；
   聚合时 NULL 被跳过，`SUM/MIN/MAX/AVG` 对“全 NULL 输入”返回 `null` 而非 0；
   仅 `COUNT(*)` 统计所有行，`COUNT(col)` 不统计 NULL。
3. 所有裁剪查询都可通过 `/verify` 与同条件的强制全扫描逐字段对照，
   并报告真实的磁盘读取字节数（按 channel 计数，非估算）。

---

## 1. 环境与依赖

| 项 | 要求 | 实测版本 |
|---|---|---|
| JDK | 17+（仅用 JDK 自带模块，编译目标 17） | Temurin OpenJDK 17.0.20.1 |
| 构建 | `javac`（无需 Maven/Gradle） | 17.0.20.1 |
| 运行 | `java` | 17.0.20.1 |
| 演示脚本 | `bash`、`curl`、`jq`（仅 demo 用，服务本身不需要） | bash 5 / curl 7.x / jq 1.x |
| 第三方 Java 依赖 | **无** | — |

依赖锁定见 [`LOCKFILE.md`](./LOCKFILE.md)：无任何外部 Maven 坐标，
唯一“版本约束”是 JDK 17 特性（record-free，实际 11+ 也可，脚本按 17 验证）。

## 2. 构建 / 测试 / 启动

```bash
export JAVA_HOME=/path/to/jdk-17      # 若 java/javac 已在 PATH 可省略

./build.sh        # 编译 src/ -> out/
./test.sh         # 编译 src+test 并运行全部自动化测试（无测试框架，纯 main + 断言）
./run.sh          # 启动服务，默认 http://localhost:8080，数据目录 ./data
./run.sh --port 18700 --data ./data   # 自定义端口/目录
```

一键验收演示（自动起停临时服务，构造三类分片并对照全扫描）：

```bash
./scripts/demo.sh                # 输出完整 JSON 对照与字节数
PORT=19000 ./scripts/demo.sh     # 换端口
./scripts/demo.sh --keep         # 保留服务进程
```

## 3. HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 |
| GET | `/tables` | 表清单 |
| GET | `/tables/{table}` | 分片清单与每列统计（含 `statsPresent:false`） |
| POST | `/tables/{table}/shards/{shardId}` | 灌入一个分片 |
| DELETE | `/tables/{table}` | 删表（同时删除磁盘文件） |
| POST | `/query` | 查询（可指定 `mode:"FULL_SCAN"` 强制全扫描） |
| POST | `/verify` | 同一请求分别跑 PRUNED 与 FULL_SCAN，比对结果并报告节省字节 |

### 3.1 灌入分片

```bash
curl -s -X POST localhost:8080/tables/sales/shards/s1-all-null \
  -H 'Content-Type: application/json' \
  --data-binary @examples/ingest-s1-all-null.json
```

`rows` 为对象数组；缺失字段或显式 `null` 都表示**真正的 NULL**（presence bit = 0，不是 0）。
可选 `"missingStats": ["col1", ...]` —— 这些列在文件页脚中**不写任何统计**，
用来模拟统计未生成/损坏，验证裁剪必须退化为扫描。

### 3.2 查询

请求体：

```json
{
  "table": "sales",
  "filter": { "column": "price", "op": ">=", "value": 30 },
  "aggregates": [
    { "fn": "COUNT_ROWS" },
    { "fn": "SUM", "column": "price", "alias": "sum_price" }
  ],
  "includeRows": false,
  "mode": "PRUNED"
}
```

* 过滤算子（叶子）：`=` `!=` `<` `<=` `>` `>=`（也接受 `EQ/NE/LT/LE/GT/GE`），
  以及 `IS NULL` / `IS NOT NULL`；布尔组合：`AND` / `OR` / `NOT`。
  任何比较遇到 NULL 单元格结果为 UNKNOWN → 行不匹配。
* 聚合：`COUNT_ROWS`（=count(*)，含 NULL 行）、`COUNT`、`SUM`、`MIN`、`MAX`、`AVG`。
  除 `COUNT_ROWS` 外全部跳过 NULL；无任何非空输入时 `SUM/MIN/MAX/AVG` 返回 `null`。
* `includeRows:true` 时返回命中的明细行（NULL 输出为 JSON `null`）。
* `mode`：`PRUNED`（默认）或 `FULL_SCAN`（忽略统计，扫所有分片）。

### 3.3 字节统计口径

响应中的 `io`：

* `bytesRead`：进程通过文件 channel **实际读取的字节总数**（计数器包裹 channel，非估算）。
* `metadataBytes`：读取文件头(6B)+页脚（JSON 统计）的开销，每个被扫描分片都有。
* `dataBytes`：列式数据块读取（存在位图 + 按需逐行读取的 int64；跳过的行不读其 8 字节）。
* `scannedFileBytes` / `prunedFileBytes` / `totalFileBytes`：文件大小维度的裁剪量参考。

列式文件布局见 `ColumnFile.java` 类注释（魔数 `CSHF/CSFT`、小端、存在位图 + int64 列 + JSON 页脚）。

## 4. 验收要点与实测结果

测试数据共 14 行 / 5 分片（`sales` 表，列 `id,price,qty`）：

| 分片 | price | 特征 |
|---|---|---|
| `s1-all-null` | NULL, NULL, NULL | **全 NULL**（qty 同样全 NULL） |
| `s2-price-10` | 10,10,10 | **边界相等**（min=max=10） |
| `s3-price-10-20` | 10,15,20 | 边界 + 一个 NULL qty |
| `s4-price-20-30` | 20,25,30 | 边界（含端点 20/30） |
| `s5-no-stats` | 99,100 | **price/qty 统计缺失**（值显然不命中多数查询，但仍必须扫描） |

`./scripts/demo.sh` 实测（与全扫描完全一致，`correct:true`）：

| 查询 | 命中 | 裁剪掉的分片 | 仍扫描 | 裁剪/全扫描 字节 |
|---|---|---|---|---|
| `price = 20` | 2（sum_price=40, avg=20, sum_qty=5） | s1 全NULL, s2[10,10] | s3,s4,**s5** | 822 / 1312 |
| `price >= 30` | 3（30、99、100，sum=229） | s1,s2,s3 | s4,**s5** | 541 / 1283 |
| `price IS NULL` | 3 | s2,s3,s4（nullCount=0） | s1,**s5** | 498 / 1259 |
| `price=15 AND qty IS NULL` | 1：`count(*)=1, count(qty)=0, sum(qty)=null, avg(qty)=null` | s1,s2,s4 | s3,**s5** | 517 / 1264 |
| `15 <= price <= 25`（含明细） | 4（min=15,max=25） | s1,s2 | s3,s4,**s5** | 929 / 1421 |

关键点全部被覆盖：

* **全 NULL 分片**：`s1` 的 `min/max` 统计天然不存在（JSON 中为 `null`，但 `nullCount=3` 存在）；
  数值比较可凭 nullCount 裁剪，`IS NOT NULL` 也可裁剪，`IS NULL` 则必须扫描。
* **边界相等**：`price=20` 不会错误裁掉 min/max 端点为 20 的 s3/s4；`>=30` 保留含 30 的 s4。
* **统计缺失**：s5 在所有查询中都出现在 `scannedShardIds` 里，即使其真实值域 [99,100]
  明显与 `price=20` 不相交——响应同时给出每条裁剪的人类可读 `reason`。
* **NULL 不是 0**：`price=15` 命中的行 qty 为 NULL，`sum(qty)` 为 `null`（不是 0），
  `count(qty)=0`；明细投影输出 `"qty": null`。
* 字节数由真实 channel 计数，每次查询确定性可复现（测试中有断言）。

> 说明：按行 seek 读取的演示数据量很小（每片 2–3 行），metadata（JSON 页脚）占比偏高，
> 因此“省字节”主要来自整片跳过；数据列越大，`dataBytes` 的列式/逐行节省越明显。

## 5. 自动化测试

`./test.sh` 编译并运行 `test/colscan/TestRunner.java`（零框架，6 个测试组、100+ 断言）：

1. 分片统计：全 NULL 无 min/max、nullCount、注入缺失统计后数据仍在；
2. 裁剪判定：各算子边界、`!=` 对 nullCount 的依赖、IS [NOT] NULL、AND/OR、缺失统计必扫；
3. NULL 语义：六种比较对 NULL 均为 false、聚合对全 NULL 的返回值；
4. 列式文件磁盘往返：存在位图与数值分离存储；
5. 引擎：裁剪结果（行数/聚合/明细）逐字段等于全扫描、字节严格更少且确定；
6. HTTP 端到端：起真实 Server，灌片（含缺失统计）、`/verify` 一致、错误请求 4xx。

最近一次实测输出：

```
PASS  shard statistics
PASS  pruning decisions
PASS  null is never zero
PASS  column file round trip
PASS  engine pruned vs full scan   (28 assertions, pruned=896 bytes, full=1388, saved=492)
PASS  http end to end
ALL 6 TEST GROUPS PASSED
```

## 6. 目录结构

```
src/colscan/            主源码（包 colscan）
  Value.java            值类型：long 或 NULL（NULL 是独立标记，绝非 0）
  Shard.java            内存分片 + 每列 min/max/nullCount 统计 + 缺失统计注入
  ColumnFile.java       列式二进制文件、计数 channel、逐行惰性扫描
  Json.java             零依赖 JSON 解析/序列化
  Predicate.java        过滤谓词（三值逻辑，AND/OR/NOT/IS [NOT] NULL）
  Aggregator.java       count/count(*)/sum/min/max/avg 的 NULL 语义
  Catalog.java          表->分片目录，灌片/删表/重启恢复
  QueryEngine.java      裁剪判定 + 执行 + 字节统计
  Server.java           JDK HttpServer 路由
test/colscan/           自动化测试
examples/               curl 与 demo 使用的请求样例
scripts/demo.sh         一键验收演示
build.sh / test.sh / run.sh
data/                   默认运行时数据目录（自动创建）
```

## 7. 已知限制 / 未完成项

如实列出：

* 仅支持 64 位整数列（外加 NULL）；无字符串/浮点/日期类型、无投影裁剪（`SELECT` 列选择）
  与 GROUP BY/JOIN/ORDER BY/LIMIT。
* 无鉴权、TLS、并发写控制；单进程、嵌入式目录，非生产级服务。
* 为使“读取字节”真实可信，扫描按行 seek；未实现批量化预读、解压、延迟物化等工程优化，
  极小分片下元数据占比高，吞吐数字不代表真实列存性能。
* JSON 解析器为最小实现（满足本服务协议所需），非完整 JSON 工具库。
* 页脚 JSON 统计若被人为篡改，裁剪信任其内容（只校验魔数/长度）；
  “统计缺失”路径有完整测试，“统计错误（而非缺失）”不做校验和兜底。
