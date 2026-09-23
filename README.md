# 列式分区裁剪（Columnar Shard Pruning）演示服务

纯后端 Java 服务：数据按**分片（shard）**以**列式二进制**落盘，维护每片每列的
`min / max / NULL 计数`统计；查询时用统计做**分区裁剪**，只扫描可能命中的分片，
并在同一次请求里同时返回**裁剪结果**与**全扫描（full scan）结果**做对照，
分别报告**实际读取字节数**。

仅用 JDK 标准库：HTTP 用 `com.sun.net.httpserver.HttpServer`，无界面、无框架、
**零第三方依赖**（含测试）。

---

## 1. 环境要求与启动命令

### 依赖

- **JDK 17+**（仅此一项；无 Maven/Gradle、无外部 jar，见 `dependencies.lock`）。
- 已在 **Temurin OpenJDK 17.0.20.1+1（x86_64 Linux）** 实测通过。
- 任意合规 JDK 17+ 发行版均可。

> 本次交付环境最初没有系统 Java，也无 sudo 权限，因此把免安装 JDK 放在
> `/home/admin/jdk17`。如果你有系统 JDK，直接用即可；下文命令均可用
> `JAVA_HOME=/path/to/jdk` 前缀覆盖。

### 构建 / 测试 / 启动

```bash
# 1) 编译（输出到 build/）
scripts/build.sh
# 或手动： javac -d build/classes $(find src/main -name '*.java')

# 2) 运行自动化测试（零依赖，约 70 个断言；先自动编译）
scripts/test.sh

# 3) 启动 HTTP 服务
scripts/run.sh 8080 ./data
#   参数1：端口（默认 8080，也可用环境变量 PORT）
#   参数2：数据目录（默认 ./data，也可用环境变量 DATA_DIR）
# 等价手动命令：
# java -Dserver.port=8080 -Dserver.host=0.0.0.0 -Ddata.dir=./data \
#      -cp build/classes colscan.server.Main
```

启动后输出：`列式分片扫描服务已启动: http://localhost:8080 ...`。

### 端到端演示（需要先启动服务）

```bash
BASE_URL=http://localhost:8080 scripts/demo.sh
```

脚本自动构造“普通 / 边界相等+NULL / 高值 / 全 NULL / 统计缺失”5 个分片，
跑 4 个查询并打印裁剪 vs 全扫描对照。实测摘要见
[`examples/demo-output-summary.md`](examples/demo-output-summary.md)。

---

## 2. HTTP 接口与请求样例

所有请求/响应均为 JSON（UTF-8）。

### `GET /health`

```bash
curl http://localhost:8080/health
# {"status":"ok"}
```

### `GET /tables`

列出所有表、schema、分片数。

### `POST /tables/{表名}/ingest`

追加**一个分片**（每次请求 = 一个新分片）。请求体：

| 字段 | 说明 |
|---|---|
| `types` | 可选，列类型映射：`LONG` / `DOUBLE`。新表首批若全 NULL 必须显式声明；之后分片必须与已有 schema 一致。缺省时从数据推断（整数→LONG，含小数→DOUBLE）。 |
| `computeStats` | 可选，默认 `true`。设为 `false` 时该分片写入 `hasStats:false` 的占位统计，用于模拟**统计缺失**。 |
| `rows` | 行对象数组，至少 1 行。缺字段或字段值为 `null` 即 **SQL NULL**。 |

```bash
curl -X POST http://localhost:8080/tables/sales/ingest \
  -H 'Content-Type: application/json' \
  -d @examples/ingest-normal.json
# {"table":"sales","shard":0,"rows":4,"statsWritten":true,"note":null}
```

`examples/ingest-normal.json`（注意 `id:4` 缺 amount、`id:3` 显式 null，均为 NULL）：

```json
{
  "types": {"id": "LONG", "amount": "LONG"},
  "rows": [
    {"id": 1, "amount": 100},
    {"id": 2, "amount": 200},
    {"id": 3, "amount": null},
    {"id": 4}
  ]
}
```

构造**统计缺失**分片（裁剪器对它必须保守扫描）：

```bash
curl -X POST http://localhost:8080/tables/sales/ingest \
  -H 'Content-Type: application/json' \
  -d @examples/ingest-no-stats.json
```

### `POST /query`

范围过滤 + 聚合。**响应同时包含 `pruned`（裁剪）与 `fullScan`（全扫描）两套结果**，
并给出 `consistentWithFullScan` 与 `bytesSaved`。

请求字段：

| 字段 | 说明 |
|---|---|
| `table` | 表名（必填） |
| `filter` | 可选，`{column, op, value}`；`op ∈ eq,ne,lt,le,gt,ge`，`value` 为数字 |
| `aggregates` | 可选，数组，`{func, column?, alias?}`；`func ∈ count(COUNT(*)), count_col, sum, avg, min, max` |
| `returnRows` | 可选，默认无聚合时 `true`；返回命中行样例（NULL 原样输出为 `null`） |
| `rowLimit` | 可选，1..10000，默认 100 |

```bash
curl -X POST http://localhost:8080/query \
  -H 'Content-Type: application/json' \
  -d @examples/query-range.json
```

响应（节选）：

```json
{
  "table": "sales",
  "consistentWithFullScan": true,
  "bytesSaved": 1234,
  "bytesSavedRatio": 62.5,
  "pruned": {
    "matchedRows": 7,
    "scannedShards": 2, "prunedShards": 3,
    "statsBytesRead": 410, "dataBytesRead": 326, "totalBytesRead": 736,
    "aggregates": {"cnt": 7, "total": 1290},
    "shards": [
      {"shard": 0, "scanned": false, "reason": "pruned:no_range_overlap", ...},
      {"shard": 1, "scanned": true,  "reason": "scan:range_overlaps", ...},
      {"shard": 4, "scanned": true,  "reason": "scan:stats_missing", ...}
    ],
    "sampleRows": [...]
  },
  "fullScan": { ... 同样结构，但每个分片都 scanned=true ... }
}
```

每个分片都给出 `scanned / reason / matchedRows / statsBytesRead / dataBytesRead /
columnsRead`，便于核对裁剪决策与读取字节。

错误请求（表不存在、类型不符、op 非法、请求体 >16MB 等）返回 HTTP 400
`{"error":"..."}`；方法不允许返回 405。

---

## 3. 裁剪规则（统计只能排除“确定不命中”的分片）

设过滤列在某分片的非 NULL 值区间为 `[min, max]`（全 NULL 时分片无 min/max）。
谓词对 `[min,max]` 内**所有可能值**都为假时，才允许跳过该分片；边界相等时
一律保守扫描（因为边界行确实可能存在并命中）。

| 谓词 | 可裁剪的条件 | 边界相等处理 |
|---|---|---|
| `eq v`  | `v < min` 或 `v > max` | `v==min` 或 `v==max` → **扫描** |
| `ne v`  | 仅当分片是常量分片且常量 `== v` | 其余一律扫描 |
| `lt v`  | `min >= v` | `min==v` 可裁剪；`max==v` 扫描 |
| `le v`  | `min > v` | `min==v` → **扫描**（最小值行命中） |
| `gt v`  | `max <= v` | `max==v` 可裁剪；`min==v` 扫描 |
| `ge v`  | `max < v` | `max==v` → **扫描**（最大值行命中） |

**NULL 语义（与 SQL 一致，禁止把 NULL 当 0）：**

- 任意比较谓词对 NULL 行结果为 UNKNOWN（不命中）；NULL 不参与 `sum/avg/min/max`，
  但计入 `count(*)`，不计入 `count(col)`。
- **全 NULL 分片**：任何比较谓词都不可能命中 → 可裁剪（`pruned:all_null_column`）。
  注意这是“确定无值可比较”，不是把 NULL 当 0；`eq 0` 同样不命中全 NULL 分片里的行。
- **统计缺失**（`stats.json` 物理缺失，或 `hasStats:false`，或缺该列统计）：
  无法证明任何事 → **必须扫描**（`scan:stats_missing`），宁可多读不可漏读。

---

## 4. 磁盘布局与列式文件格式

```
{dataDir}/{table}/
  table.json                 # {name, columns:{col:TYPE}, shardCount}
  shard-0/
    shard.json               # {rowCount}
    stats.json               # {hasStats,rowCount,columns:{col:{nullCount,min,max}}}
    id.col                   # 二进制列文件
    amount.col
```

列文件（大端序，见 `ColumnFile.java`）：

```
magic    7B   "CLCOL1\0"
type     1B   1=LONG(int64) / 2=DOUBLE(float64)
rows     4B   int32 总行数（含 NULL）
nulls    4B   int32 NULL 行数
nullbits ⌈rows/8⌉B  置位=非 NULL
values   (rows-nulls) 个定宽值
```

NULL 用 bitmap 表示，**不占 value 空间、绝不用 0 占位**。查询只读取被引用列的文件
（真正的列式裁剪）。读取字节用 `CountingInputStream` 按实际 read() 返回值累加。

---

## 5. 自动化测试

`scripts/test.sh` 运行 `src/test/java/colscan/Tests.java`（零依赖，纯 main + 断言，
HTTP 用 JDK 自带 `java.net.http.HttpClient` 在随机端口起真实服务）。覆盖：

- 全 NULL 列往返（4 个值全为 null，文件中 0 个 value 字节）；含 `0`、负数、NULL
  混合列往返；读取字节数 == 文件大小
- 统计计算（NULL 计数、min/max；全 NULL 片 min/max 为 null 而非 0）
- **裁剪判定矩阵**：六种操作符 × 边界相等（值==min / 值==max）× 严格不重叠 ×
  常量分片 × 含 NULL × 统计缺失
- **端到端验收夹具**（5 片，每片 64 行）：
  - EQ 10 只命中边界相等片，NULL 不命中，统计缺失片强制扫描
  - 裁剪与全扫描的命中行数、聚合逐项一致（`consistentWithFullScan`）
  - EQ 0 命中 0 行且 SUM/AVG/MIN/MAX 为 null（NULL≠0）
  - 被裁剪分片数据读取 0 字节；裁剪总字节 < 全扫描
  - 物理删除 stats.json 等价于统计缺失
- 聚合 NULL 安全：命中行聚合列全 NULL 时 `count(*)=3` 但 `count(col)=0`、
  SUM/AVG 为 null
- DOUBLE 类型边界相等
- HTTP 端到端：health、ingest、tables、query、400 校验

### 实测结果（2026-09-23）

```
通过: 69，失败: 0
全部测试通过          （scripts/test.sh 实测约 0.7s，退出码 0）
```

开发过程中测试确实抓到并已修复的真实缺陷（如实记录）：

1. 列文件读回时三目运算符 `cond ? long : double` 把 LONG 值**提升成了 double**；
2. `Acc.sum` 返回处同类三目把 LONG 的 SUM 提升成 double；
3. 裁剪器 LT / NE 的跳过条件写反（边界语义错误）；
4. HTTP 工作线程池用了非守护线程，`HttpServer.stop()` 又不关闭外部传入的
   ExecutorService，导致测试 JVM 跑完后**挂住不退出**；改为守护线程工厂后
   测试 ~0.7s 内以 exit 0 结束。

均已修复并有回归断言。

---

## 6. 已知限制 / 未完成项（如实说明）

- **仅支持数值列（LONG/DOUBLE）**：无字符串、日期、布尔列；谓词字面量也必须是数字。
- **无字符串/字典编码、无压缩**：列文件是定宽裸值，统计是 pretty JSON（偏大）。
  因此在**极小分片**（如演示里每片 4 行）上，读取 stats.json 的成本会超过跳过
  微型数据文件的节省，`bytesSaved` 可能为负——这是真实的工程权衡，不是错误，
  正确性（与全扫描一致、跳过片 0 数据字节）始终成立；分片较大时为正收益
  （测试已覆盖，每片 64/128 行时裁剪严格省字节）。生产形态应让统计文件更小
  （二进制 footer）或缓存统计，本次未实现。
- 单节点、无并发控制优化：写入在 Catalog 实例上加锁；每次查询都重新从磁盘读
  统计（不缓存），以便字节账真实可比。
- `ne` 仅凭 min/max 无法排除非常量分片（没有 NDV/基数信息）；未实现 bloom filter、
  zone map 之外的统计。
- 无鉴权/TLS；HTTP body 上限 16MB；服务直接 Ctrl-C 停止，无优雅退出钩子。
- 行返回上限 10000，仅用于核对样例，不是完整结果集分页协议。

## 目录结构

```
src/main/java/colscan/
  json/Json.java          极简 JSON（解析/序列化），零依赖
  store/                  类型、列文件、统计、Catalog
  query/                  Filter/AggSpec/Pruner/Acc/QueryEngine/QueryRequest
  server/                 HttpApi（路由）+ Main（入口）
src/test/java/colscan/Tests.java   零依赖自动化测试
scripts/                  build.sh / test.sh / run.sh / demo.sh
examples/                 请求样例 JSON + 实测输出摘要
dependencies.lock         依赖锁定（外部依赖 = 0）
```
