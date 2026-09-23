# 列式选择向量查询引擎（Columnar Selection-Vector Engine）

纯后端、单机、内存查询引擎。核心运算全部自行实现：**不调用任何现成 SQL 引擎、查询优化器或第三方库**（连 JSON 解析/序列化都是自带的）。
语言为 Java 21，只用 `javac`/`java` 构建运行。

引擎以**批（batch）+ 64 位位图（bitmask）**方式执行过滤，过滤结果物化为一个**选择向量（selection vector）**——一个紧凑、有序、可重复的行下标数组；后续投影与聚合只遍历选择向量，从而天然处理**稀疏行**与**重复选择**。

每个请求都会同时跑两套执行路径并自动对账：

- **列式批执行器**（`ColumnarExecutor`）：64 位字批量谓词求值 → 选择向量 → 投影/聚合；
- **逐行参考解释器**（`RowInterpreter`）：独立实现的标量求值与聚合，作为正确性基准。

响应中的 `crossCheck` 字段给出两路选择向量与结果是否完全一致。

---

## 1. 目录结构

```
.
├── src/main/java/com/opp16/engine/
│   ├── json/Json.java          # 自带 JSON 解析/序列化（零依赖）
│   ├── NullBitmap.java         # 打包 NULL 位图（long[]，每位表示该行非空）
│   ├── ColumnType.java         # INT / STRING
│   ├── Column.java             # IntColumn(long[])、StringColumn(String[]) + 位图
│   ├── Table.java              # 内存表（等长列集合，按名寻址）
│   ├── SelectionVector.java    # 选择向量（下标数组，越界/负数校验，可重复）
│   ├── BatchStats.java         # 每批区间与幸存行数统计
│   ├── Predicate.java          # 谓词：批模式 TriMask 与标量模式，SQL 三值逻辑
│   ├── QueryPlan.java          # 逻辑计划（解析 + explain 文本）
│   ├── ColumnarExecutor.java   # 列式批执行路径
│   ├── Aggregator.java         # COUNT/SUM/AVG/MIN/MAX、GROUP BY、NULL 跳过
│   ├── RowInterpreter.java     # 逐行参考解释器（独立聚合实现）
│   ├── QueryEngine.java        # 请求编排、对账、JSON 响应
│   ├── Main.java               # CLI 入口（文件 / stdin，导出选项）
│   └── EngineException.java
├── src/test/java/com/opp16/engine/tests/
│   ├── Assert.java             # 零依赖断言框架
│   ├── RunTests.java
│   ├── NullBitmapTests.java
│   ├── SelectionVectorTests.java
│   ├── JsonTests.java
│   ├── PredicateTests.java     # 批 vs 逐行逐位等价
│   ├── EngineTests.java        # JSON 端到端
│   └── BatchBoundaryTests.java # 批/字边界专项（n 与 batchSize 全组合）
├── samples/                    # 请求样例（含一个故意非法的）
├── build.sh / test.sh / run-samples.sh
└── README.md
```

---

## 2. 构建与运行

需要 JDK（在 OpenJDK 21 上开发与验证）。无需 Maven/Gradle，无需联网下载任何东西。

```bash
./build.sh          # javac 编译主代码与测试代码 -> target/
./test.sh           # 运行全部自动化测试（失败时退出码非 0）
./run-samples.sh    # 跑 samples/ 下所有请求，响应/导出写入 out/
```

直接运行单个请求：

```bash
# 文件参数
java -cp target/classes com.opp16.engine.Main samples/01-filter-project.json

# 或从 stdin
cat samples/01-filter-project.json | java -cp target/classes com.opp16.engine.Main
```

退出码：`0` 成功；`2` 请求错误（非法选择下标、未知列、类型不符、JSON 格式错误等）；`1` 其它意外故障。

### 导出选项

```
--out PATH                 完整响应 JSON 写入 PATH（默认打到 stdout）
--export-plan PATH         导出逻辑计划文本
--export-data PATH         导出表数据（含 NULL 的列式 JSON）
--export-selection PATH    导出选择向量（JSON 下标数组）
--export-result PATH       导出物化结果（schema + rows）
```

响应本身始终包含 `plan`、`stats.batches`、`selection`、`result`、`data`、`crossCheck`，即**数据与执行计划（含选择向量与批统计）都可导出**。

---

## 3. 请求格式

顶层字段：

| 字段 | 说明 |
|---|---|
| `table` | 内联数据：`{"columns":[{"name","type":"INT|STRING","values":[...]}...]}`，值里用 `null` 表示 NULL |
| `batchSize` | 过滤批大小（正整数，默认 1024）。内部再按 64 行一个字推进 |
| `filter` | WHERE 谓词树 |
| `selection` | 直接给定行下标数组，**替代** filter（注入钩子，用于制造稀疏/重复/非法下标）；与 `filter` 互斥 |
| `project` | 输出列名数组；无聚合时引用基表列，有聚合时只能引用分组列或聚合别名 |
| `groupBy` | 分组列（NULL 自成一组） |
| `aggregate` | `[{"fn":"COUNT|SUM|AVG|MIN|MAX","column":"score"(COUNT 可省略或写 "*"),"alias":"..."}]` |
| `explain` | 为 true 时同样在 `plan` 字段返回可读计划（计划始终返回） |
| `requestId` | 原样回填 |

谓词算子：

- 比较：`=`(或 `==`)、`!=`(或 `<>`)、`<`、`<=`、`>`、`>=`，形如 `{"op":">=","column":"score","value":10}`
- `{"op":"between","column":"x","low":0,"high":10}`（闭区间，INT）
- `{"op":"is_null","column":"x"}` / `{"op":"is_not_null",...}`
- 布尔组合：`{"op":"and","args":[...]}`、`{"op":"or","args":[...]}`（恰好两个参数）、`{"op":"not","arg":{...}}`

比较字面量不允许直接写 `null`（SQL 中 `col = NULL` 恒为 UNKNOWN），请改用 `is_null` / `is_not_null`。

### 语义要点

- **三值逻辑**：谓词结果是 TRUE / FALSE / UNKNOWN。批模式用两个 64 位掩码编码（`trueMask`、`unknownMask`）。WHERE 只保留 TRUE；NULL 输入的比较落入 UNKNOWN 被过滤，但 `IS NULL` 能保留全空行。AND/OR/NOT 按 SQL 三值逻辑组合。
- **选择向量**：过滤每个 64 行字得到 TRUE 位掩码，置位位置追加为行下标。`appendMask` 与所有 `append` 都做 `0 <= row < rowCount` 边界校验；**负数或越界下标直接抛错**（退出码 2）。
- **稀疏**：下游只访问选中行，未选中/NULL 行不物化。
- **重复**：同一行下标可出现任意次，投影逐次输出，聚合逐次计数（`[5,5,5]` 贡献 3 行）。
- **NULL 聚合**：`COUNT(*)` 计全部输入行；`COUNT(col)`/`SUM`/`AVG`/`MIN`/`MAX` 跳过该列 NULL；无 GROUP BY 且零输入时仍输出一行（COUNT=0，其余为 null）；AVG 返回 DOUBLE，其余为整数。

---

## 4. 核心实现说明

### 4.1 NULL 位图（`NullBitmap`）

`long[]` 打包，每位表示该行是否非空；提供：

- `presentMask(base, len)`：返回 `[base, base+len)` 区间按**位置**对齐的非空掩码，内部处理跨字移位（`len` 可以是 64，正确产生全 1）；
- `countPresent()`：尾部字用 `(1<<tail)-1` 屏蔽越界位后 `Long.bitCount`。

"全空列"就是一个所有位为 0 的位图，是每个算子都必须正常处理的一等场景。

### 4.2 批过滤 → 选择向量（`Predicate` + `ColumnarExecutor`）

- 表按 `batchSize` 分批，批内按 64 行一个字调用 `evalBatch(base,len)`；
- 比较谓词先用位图取出非空位置，只对非空位置做比较（`numberOfTrailingZeros` 逐位），TRUE 位置进 `trueMask`，非位置进 `unknownMask`；
- `sv.appendMask(trueMask, base, len)` 把 TRUE 位变成行下标追加进选择向量；
- `BatchStats` 记录每批 `[from,to)` 与幸存数，便于核对批边界（无间隙、无重叠地覆盖 `[0,n)`）。

### 4.3 投影 / 聚合消费选择向量

- 投影按选择向量顺序 `boxedAt(row)` 取值，重复下标产出重复行；
- 聚合对每个选中下标更新累加器；分组键由 `boxedAt` 得到（NULL 键正常入组）。
- 参考解释器用**另一套**标量代码做同样的事，`QueryEngine.resultsEqual` 做结构化比对（列名、类型、含 double 与 null 的值）。

### 4.4 无效选择下标

`SelectionVector.append` 是唯一的下标入口，任何路径（过滤追加、显式 `selection`、位掩码展开）都经过它，越界/负数一律失败且不产生半条结果。

---

## 5. 样例

| 文件 | 演示内容 |
|---|---|
| `samples/01-filter-project.json` | `batchSize=3` 跨批过滤 + 投影，响应含每批幸存数 [1,2,0] |
| `samples/02-nulls-3vl.json` | `!= ` 与 `IS NULL` 的 OR，展示三值逻辑下 NULL 部门行被找回 |
| `samples/03-groupby-agg.json` | GROUP BY（含 NULL 组）+ 六种聚合，投影重排 |
| `samples/04-explicit-selection.json` | 显式选择向量 `[5,5,5,0,7,7]`：重复下标重复计数，下标 7 的 score 为 NULL 被聚合跳过 |
| `samples/05-invalid-subscript.json` | 故意非法：3 行表给下标 3，退出码 2（**预期失败样例**） |
| `samples/06-all-null-column.json` | 全空列：`IS NULL` 保留全部行，`COUNT(*)=7, COUNT(v)=0, SUM(v)=null` |

样例 04 的实际结果：`COUNT(*)=6`、`SUM(score)=40`（下标 5 三次=30，下标 0=10，下标 7 两次为 NULL 跳过）、`AVG(score)=10`（40/4 个非空）。

样例 05 的错误响应：

```json
{ "ok": false, "error": "invalid selection subscript 3 for table with 3 rows (valid range: 0..2)" }
```

---

## 6. 实际运行记录（如实记录）

以下为在本仓库内真实执行的命令与结果（OpenJDK 21，Linux）。

### 6.1 干净构建

命令：`rm -rf target out && ./build.sh`

结果（退出码 0）：

```
BUILD OK -> target/classes, target/test-classes
```

### 6.2 自动化测试

命令：`./test.sh`

结果（退出码 0）：

```
TOTAL: 606, PASS: 606, FAIL: 0
```

覆盖范围：

- **NullBitmap**：空位图、130 行全空/全满、跨字（行 60–69、136–139）`presentMask`、64 宽全 1、尾部屏蔽；
- **SelectionVector**：重复下标保留、负数/等于行数/远超范围下标拒绝、`appendMask` 跨批映射、最后一个合法行可中、越界位拒绝、1000 次重复扩容；
- **Predicate（批 vs 逐行逐位等价）**：INT 六种比较、STRING 比较、BETWEEN、IS [NOT] NULL、AND/OR/NOT 复合三值逻辑、**全空列**（TRUE=0、UNKNOWN 覆盖全部位置、IS NULL 全中）；
- **批边界专项**：行数 n ∈ {0,1,63,64,65,127,128,129,255,256,257,1000} × batchSize ∈ {1,2,7,31,63,64,65,100,128,1024} 全组合（120 组），每组同时断言：列式与逐行的选择向量一致、结果一致、批区间无间隙无重叠覆盖 `[0,n)`、每批幸存数之和等于选择向量长度；另断言普通过滤的选择向量严格升序；
- **端到端（JSON 入口）**：过滤/投影、NULL 三值逻辑、显式稀疏+重复选择、非法下标、COUNT(*) 与 COUNT(col) 在 NULL 下的差异、重复下标进聚合、GROUP BY 含 NULL 组与投影重排、空输入聚合一行、全空列、空表、格式/语义非法请求（坏 JSON、缺 table、batchSize=0、未知列、STRING 列配 INT 字面量、null 字面量比较、filter 与 selection 同设、未知投影列、未知聚合函数）、计划/数据/选择向量导出。

开发过程中首次运行有 6 个失败项，逐一核查后确认**均为测试期望值写错，引擎行为正确**，已修正：

1. 位图测试手算 `0b1011` 有误（实际 64/66/67 行为 bit 0/2/3 = `0b1101`）；
2. JSON 转义用例把 Java/JSON 两层反斜杠数错（单反斜杠 `A` 就是 `A`）；
3. 批幸存数顺序写反（batchSize=3 时三批依次为 1、2、0）；
4. 显式选择 `[5,5,5]` 的下标 5 对应 score=10 而非 5；
5. 类型不匹配报错文案预期应为 "not INT"；
6. `id > 5` 命中 id 6/7/8（下标 [5,6,7]），原期望漏了一行。

修正后 606/606 通过，无未通过项。

### 6.3 样例批处理

命令：`./run-samples.sh`

结果：样例 01–04、06 退出码 0；样例 05 退出码 2（预期的非法下标错误）。各请求的完整响应与 `plan/data/selection/result` 导出物写入 `out/`（该目录不入库）。

抽查：

- `01`：过滤 `score>=10` 返回 id 1/4/6，批统计 `batchSize=3` 下幸存数 `[1,2,0]`；
- `03`：分组聚合 `eng=[4,2,20,10,10,10]`、`sales=[2,2,10,5,5,5]`、`null=[2,2,27,13.5,7,20]`，`crossCheck.passed=true`；
- `04`：`[6,40,10]`，两路选择向量与结果一致；
- `06`：全空列 `[[7,0,null]]`，`crossCheck.passed=true`。

### 6.4 stdin 入口

命令：`cat samples/03-groupby-agg.json | java -cp target/classes com.opp16.engine.Main`

结果：正常从标准输入读取并输出完整 JSON（退出码 0）。

---

## 7. 未实现 / 范围外

- 无网络接口、无前端（按要求纯后端；CLI + JSON 文件/stdin 即入口）；
- 仅支持 INT（64 位长整型）与 STRING 两类物理列；AVG 输出 DOUBLE；
- 不做 JOIN、ORDER BY、LIMIT、表达式算术（谓词与聚合已覆盖验收所需算子）；
- 单机内存，不落盘（导出靠响应字段与 CLI `--export-*`）；
- SUM 按 Java `long` 语义，溢出时回绕（未做decimal/大数处理）。
