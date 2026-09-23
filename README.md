# 多维位图索引服务（Multi-Dimensional Bitmap Index）

纯 Java 后端实现的**离线数据集位图索引服务**：为枚举列建立位图索引，支持
`AND / OR / NOT` 布尔查询与**删除掩码（deletion mask）**，通过 JDK 内置
`com.sun.net.httpserver.HttpServer` 提供 HTTP/JSON 接口。

- **零第三方依赖**：不需要 Maven/Gradle 下载任何 jar，只需 JDK 17+。
- JSON 解析/序列化、Roaring 风格压缩位图均在 `src/` 内自研。
- 行 ID 即加载时的 0 基原始行号，**删除只翻转存活掩码位，绝不压缩重排行号**。
- `NOT` 永远只在**当前存活行全集**内取补，已删除行不可能出现在 NOT 结果里。

---

## 1. 目录结构

```
.
├── build.sh                 # 零依赖构建/测试/打包脚本
├── dependencies.lock        # 依赖锁定（无第三方依赖，仅锁定 JDK 版本）
├── README.md
├── SHA256SUMS               # 源码校验和
├── src/
│   ├── bitmapindex/
│   │   ├── RoaringBitmap.java   # 压缩位图（Array/Bitmap 容器，按 65536 分块）
│   │   ├── BitMapIndex.java     # 多维索引、布尔表达式求值、删除掩码、统计
│   │   ├── HttpServerMain.java  # JDK HttpServer 服务 + 路由
│   │   ├── Json.java            # 零依赖 JSON 解析/序列化
│   │   └── Demo.java            # 离线演示（不启服务）
│   └── test/bitmapindex/        # 自研断言框架 + 全部测试
│       ├── Asserts.java
│       ├── TestRunner.java
│       ├── RoaringBitmapTest.java
│       ├── BitMapIndexAcceptanceTest.java
│       └── HttpE2ETest.java
└── examples/
    ├── load.json
    ├── query.json
    ├── not-query.json
    ├── delete.json
    └── run-examples.sh          # 端到端 curl 演示脚本
```

---

## 2. 环境要求与依赖

| 项目 | 要求 |
|---|---|
| JDK | **17 或更高**（开发/验证使用 Eclipse Temurin `17.0.20.1+1`） |
| 第三方库 | **无** |
| 构建工具 | 仅用 JDK 自带 `javac` / `java` / `jar`（无需 Maven/Gradle，构建无需联网） |
| HTTP 库 | JDK 模块 `jdk.httpserver`（随 JDK 提供） |

确认 JDK：

```bash
java -version   # 需要 17+
```

如系统没有 JDK，可免 root 使用（示例）：

```bash
curl -sL -o /tmp/jdk17.tar.gz \
  "https://api.adoptium.net/v3/binary/latest/17/ga/linux/x64/jdk/hotspot/normal/eclipse"
mkdir -p "$HOME/jdk17" && tar -xzf /tmp/jdk17.tar.gz -C "$HOME/jdk17" --strip-components=1
export JAVA_HOME="$HOME/jdk17"
export PATH="$JAVA_HOME:$PATH"
```

依赖锁定详见 [`dependencies.lock`](./dependencies.lock)。

---

## 3. 构建、测试、启动

所有动作都通过 `build.sh`：

```bash
./build.sh clean      # 清理 build/
./build.sh compile    # 仅编译主代码 -> build/classes
./build.sh test       # 编译并运行全部自动化测试
./build.sh package    # 编译并打包 -> build/bitmap-index.jar（可执行 jar）
./build.sh all        # 编译 + 测试 + 打包（默认）
./build.sh demo 100000   # 离线演示（不启 HTTP 服务）
./build.sh run --port=8080 --host=0.0.0.0
```

### 启动服务

方式一，可执行 jar：

```bash
./build.sh package
java -jar build/bitmap-index.jar --port=8080
# 也可显式：java -cp build/classes bitmapindex.HttpServerMain --port=8080
```

启动成功输出：

```
Bitmap index service listening on http://0.0.0.0:8080
Endpoints: POST /load, POST /query, POST /delete, POST /restore, GET /stats, GET /row/{id}, GET /health
```

参数：`--port=8080`（默认 8080，传 0 为随机端口）、`--host=0.0.0.0`（默认）。

### 离线演示（不需要服务）

```bash
java -cp build/classes bitmapindex.Demo 100000
```

---

## 4. HTTP 接口与请求样例

所有请求/响应均为 `application/json; charset=utf-8`。行 ID 为整数，
对应 `/load` 时数据行的下标，从 0 开始，**删除后保持不变**。

### 4.1 `GET /health`

```bash
curl -s http://localhost:8080/health
# {"status":"ok","totalRows":0,"liveRows":0}
```

### 4.2 `POST /load` — 加载/替换离线数据集

`columns` 可省略（此时从数据行推断列集合）。重新加载会清空旧数据，行号从 0 重新开始。

```bash
curl -s -X POST http://localhost:8080/load \
  -H 'Content-Type: application/json' \
  --data @examples/load.json
```

`examples/load.json` 摘录：

```json
{
  "columns": ["city", "color", "active", "sku"],
  "rows": [
    {"city": "Beijing",  "color": "red",  "active": "yes", "sku": "SKU-00001"},
    {"city": "Beijing",  "color": "blue", "active": "no",  "sku": "SKU-00002"},
    {"city": "Shanghai", "color": "red",  "active": "yes", "sku": "SKU-00003"}
  ]
}
```

响应：`{"loaded":true,"rows":12,"columns":["city","color","active","sku"]}`

### 4.3 `POST /query` — 布尔表达式查询

表达式是递归 JSON：

| 节点 | 形式 | 含义 |
|---|---|---|
| 等值谓词 | `{"op":"eq","column":"city","value":"Beijing"}` | 该列等于该值的行 |
| 与 | `{"op":"and","args":[节点, 节点, ...]}` | 交集；空 `args` 为存活全集（单位元） |
| 或 | `{"op":"or","args":[...]}` | 并集；空 `args` 为空集（单位元） |
| 非 | `{"op":"not","arg":节点}` | **仅在当前存活行全集内取补** |

查询参数：`limit`（默认 1000）、`offset`（默认 0）、`includeRows`（默认 true，false 只返回 ID）。

```bash
curl -s -X POST 'http://localhost:8080/query?limit=5' \
  -H 'Content-Type: application/json' \
  --data @examples/query.json
```

`examples/query.json`：

```json
{
  "op": "and",
  "args": [
    {"op": "eq", "column": "city", "value": "Beijing"},
    {"op": "or", "args": [
      {"op": "eq", "column": "color", "value": "red"},
      {"op": "eq", "column": "active", "value": "yes"}
    ]},
    {"op": "not", "arg": {"op": "eq", "column": "color", "value": "blue"}}
  ]
}
```

响应包含 `count`（总命中数）、`rowIds`（当前页，升序稳定行 ID）、`rows`（行内容）、
分页字段与当前 `liveRows`。

NOT 取补示例：

```bash
curl -s -X POST http://localhost:8080/query \
  -H 'Content-Type: application/json' \
  --data @examples/not-query.json
# {"op":"not","arg":{"op":"eq","column":"city","value":"Beijing"}}
```

### 4.4 `POST /delete` — 删除（按 ID 或按表达式）

```bash
# 按行 ID 删除
curl -s -X POST http://localhost:8080/delete \
  -H 'Content-Type: application/json' \
  --data '{"rowIds":[0,1]}'

# 按表达式批量删除：删除所有 red 行
curl -s -X POST http://localhost:8080/delete \
  -H 'Content-Type: application/json' \
  --data '{"expr":{"op":"eq","column":"color","value":"red"}}'
```

删除**只修改存活掩码**，列位图本身不动，因此：

- 删除 0、1 后，`NOT(city=Beijing)` 不会把 0、1“补”回来；
- 剩余 Beijing 行的 ID 仍是原来的 `8`，不会变成新的 `0`。

### 4.5 `POST /restore` — 恢复被删行

```bash
curl -s -X POST http://localhost:8080/restore \
  -H 'Content-Type: application/json' \
  --data '{"rowIds":[0,1]}'
```

### 4.6 `GET /stats` — 索引空间统计

返回总行数/存活数/删除数、每个列的 distinct 值数量、容器数量（array/bitmap）、
压缩后字节数、未压缩位图（每值每行列 1 bit）字节数与压缩比，以及存活掩码字节数。

```bash
curl -s http://localhost:8080/stats
```

### 4.7 `GET /row/{id}` — 按稳定 ID 取原始行

```bash
curl -s http://localhost:8080/row/0
```

### 4.8 一键演示脚本

```bash
./examples/run-examples.sh http://localhost:8080
```

该脚本依次演示：健康检查 → 加载 → 复合查询 → 删除 → 删除后 NOT 不复活已删行 →
行 ID 稳定 → 按表达式删除 → 恢复 → 空间统计 → 按 ID 取行。

错误请求（未知列、未知 op、ID 越界、JSON 非法等）返回 HTTP 400：

```json
{"error": true, "status": 400, "message": "unknown column: nope (known: [city, ...])"}
```

---

## 5. 核心设计

### 5.1 压缩位图 `RoaringBitmap`

- 32 位行号空间按高 16 位分成 65536 个块（chunk），每块 65536 个位置。
- 每个块用一个容器存放：
  - **ArrayContainer**：有序 `short[]`，稀疏时使用（块内基数 ≤ 4096，2 字节/点）；
  - **BitmapContainer**：`long[1024]` = 8 KiB，稠密时使用（块内基数 > 4096）。
  - 所有集合运算后按基数自动在两种容器间转换（如稠密 AND 稀疏结果回落为数组容器）。
- 注意：块内位置按**无符号 16 位**比较（这是手写位图最容易出错的地方，
  测试中专门覆盖了 ≥32768 位置的场景）。
- 行号始终是原始整数；压缩只影响存储形态，迭代输出永远是升序的原始行 ID。

### 5.2 多维索引

- 每列维护 `值 -> 位图`：位 i 置位当且仅当第 i 行在该列取该值。
- 加载时按行号升序收集位置，一次性 `fromSorted` 构建容器，避免逐点插入。
- 谓词直接取位图；`and/or` 为分块归并；`not` 见下。

### 5.3 删除掩码与 NOT 语义（关键验收点）

- 单独维护一个**存活位图 `live`**：`1`=存活，删除即把该位清零，恢复即置位。
- 每个查询结果最后都会与 `live` 做一次 AND，双保险：**任何已删除 ID 都不可能返回**。
- `NOT(x)` 的实现是 `live \ x`（不是“全集 2³² 取反”）：

  ```
  eval(not(arg)) = universe(live) AND NOT eval(arg)
  ```

  因此空全集上 NOT 结果为空；删除后取补只在剩余存活行内进行；
  `count(P) + count(NOT P) == liveRows` 恒成立（验收测试有断言）。

### 5.4 行 ID 稳定性

删除不移动任何数据：`/row/{id}` 永远返回加载时第 `id` 行的原始内容，
删除前缀行后，后续行的 ID 与其内容的绑定关系不变（验收测试逐行比对）。

### 5.5 HTTP 服务

JDK `com.sun.net.httpserver.HttpServer` + 固定大小线程池；
请求体上限 512 MiB；表达式查询支持 `limit/offset` 分页与 `includeRows=false`。

---

## 6. 自动化测试

```bash
./build.sh test
```

零第三方测试框架（自研 `Asserts` 断言器，见 `src/test/bitmapindex/Asserts.java`）。

测试组成：

1. **`RoaringBitmapTest`** — 位图原语：跨块边界、200 组随机集合 AND/OR/NOT 与
   `HashSet` 真相比对、range 边界（含整块 65536）、删除、容器类型自动转换。
2. **`BitMapIndexAcceptanceTest`**（验收测试）：
   - **随机布尔表达式 vs 逐行扫描**：5000 行数据上 3000 个随机嵌套表达式逐一比对；
     再在随机穿插删除的情况下再跑 3000 个；
   - **空全集**：全新索引、加载 0 行、全部删除三种情况下 NOT 与任意表达式均为空；
   - **高基数列**：20000 行、每行唯一 SKU，500 次随机取值查询各命中恰好 1 行且与扫描一致；
   - **删除后取补**：删除连续区间 + 随机行后跑 1000 个随机表达式，结果与扫描一致且
     不含任何已删 ID；`P` 与 `NOT P` 恰好分割存活全集；
   - **行 ID 稳定**：删除前 1000 行后，命中行 ID 与行内容的绑定关系逐行不变；
   - **空间统计**：压缩后/未压缩字节、压缩比、容器类型计数；
   - 非法表达式（未知 op/列、越界删除）的错误处理。
3. **`HttpE2ETest`** — 在随机端口启动真实服务，用 JDK `HttpClient` 走完整
   生命周期（加载/复合查询/NOT/删除/按表达式删除/恢复/统计/取行/400/分页/空数据集 NOT）。

---

## 7. 实际运行结果（如实记录）

运行环境：Linux x86_64，Eclipse Temurin JDK `17.0.20.1+1`，日期 2026-09-23。

### 7.1 自动化测试

```
RoaringBitmap                passed=627 failed=0
BitMapIndex acceptance       passed=1046 failed=0
HTTP end-to-end              passed=25 failed=0
------------------------------------------------------------
TOTAL: 1698 passed, 0 failed
ALL TESTS PASSED
```

### 7.2 离线 Demo（100000 行，4 列；含一列高基数 userId）

```
== Built index for 100000 rows in 181 ms ==
totalRows=100000 liveRows=100000 columns=4 totalDistinctValues=63324
liveMaskBytes=16416
indexBytes=1649040  uncompressedBitmapBytes=791550000  compressionRatioVsUncompressed=0.002
  city   (6 distinct)   bitmapContainers=12  indexBytes=98496
  color  (5 distinct)   bitmapContainers=10  indexBytes=82080
  active (2 distinct)   bitmapContainers=4   indexBytes=32832
  userId (63311 distinct, 高基数) arrayContainers=77227 bitmapContainers=0
         indexBytes=1435632  vs uncompressed 791387500

nested AND/OR/NOT query: index=16751 rows, scan=16751 rows, index query 843 us, match=true

== After deletions: live=85617 / total=100000 ==
same query after deletion: index=14275 scan=14275 match=true
NOT(red)=68580 + red(live)=17037 = 85617 == liveRows 85617 -> true
NOT(red) contains no deleted id: true
```

解读：低基数列（city/color/active）每值位图稠密，使用 bitmap 容器；
高基数列（userId）每个位图只有约 1 个点，全部使用 array 容器，
整体压缩到未压缩位切片的约 0.2%。删除 1.4 万余行后，同一表达式索引结果与逐行
扫描一致，且谓词与其 NOT 严格分割存活全集。

### 7.3 HTTP 端到端示例（12 行示例数据集）

`./examples/run-examples.sh` 实测要点（完整输出可复现）：

- 复合查询 `Beijing AND (red OR yes) AND NOT blue` → 命中 `[0, 8]`；
- 删除 `[0,1]` 后 `NOT(city=Beijing)` → `[2,3,4,5,6,7,9,10,11]`，**0/1 未被取补复活**；
- 此时 `city=Beijing` 仅剩 ID **8**（仍是原始第 8 行，没有重编号为 0）；
- 按表达式删除所有 red 行 → 删除 `[2,4,9,11]`；恢复 `[0,1]` 后 `liveRows=8`；
- `/stats` 与 `/row/0` 正常返回。

开发过程中发现并修复的真实缺陷（均已被测试回归覆盖）：

1. `ArrayContainer` 误用**有符号** `Arrays.binarySearch` 比较块内 `short`，
   导致位置 ≥32768 时排序/交集错误；改为无符号二分查找。
2. `range()` 在整块满（65536 个位置）时因 `& 0xffff` 优先级把满块建成空块，
   NOT 因此漏掉整段存活空间；重写边界计算修复。
3. `NOT` 初版操作数方向写反；已修正为 `universe \ bits` 并加测试锁定。

---

## 8. 未完成项 / 已知限制（如实说明）

- **服务为单进程、内存态**：数据与索引全部驻留堆内存，`/load` 为全量替换，
  未做持久化、分片、磁盘溢出（spill）或增量加载。
- **仅支持枚举等值列**：没有数值区间（`> / < / between`）、文本 LIKE、
  空值位图（加载时 null 值不建位图，也不参与等值匹配）。
- 位图只实现了 **Array / Bitmap** 两种容器，未实现 Roaring 的 RunContainer；
  对超长连续区间（如 NOT 产生的大块）用 bitmap 容器存放，空间仍有优化余地。
- 行 ID 为 32 位非负整数（`0 .. 2^31-1` 实用区间），未支持 64 位 ID。
- HTTP 层无鉴权/TLS、无请求限流；线程池固定大小；仅适用于受信内网/离线环境。
- 时间统计为 `System.nanoTime` 粗测，未做严格基准压测（JMH）与并发压测。
- JSON 解析器为满足本项目所需的最小实现，未做流式解析（超大请求体会整体读入内存，
  设有 512 MiB 上限）。
