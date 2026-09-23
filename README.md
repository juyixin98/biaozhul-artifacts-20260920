# vecq —— 列式选择向量单机内存查询引擎（纯后端）

不依赖任何 SQL 引擎 / 数据库 / 第三方库，用 Java 手写一个**列式 + 选择向量（selection vector）**
的单机内存查询引擎，核心过滤采用**按批向量化执行**，并与一个**逐行解释器**做差分对照。
对外提供 JSON 请求入口：CLI（`run` / `serve`）与基于 JDK 内置 `HttpServer` 的 HTTP 接口。
数据与执行计划可导出为 JSON 文件。本项目不含任何前端代码。

## 1. 功能概览

- **列式存储**：32 位整型列（`int[]`）、字符串列（`String[]`），值数组与 **NULL 位图**分离。
- **选择向量**：过滤产出的行下标数组，支持稀疏、有序、**重复下标**、惰性全选、多集合并；
  下标越界 / 负数在构造期 fail-fast。
- **批执行过滤**：对输入选择向量按 `batchSize` 切批，每批产出 `byte[]` 三值状态
  （TRUE/FALSE/UNKNOWN）再压实为输出选择向量。
- **三值逻辑（3VL）**：谓词遇 NULL 为 UNKNOWN，只有 TRUE 进入结果；
  `IS NULL` / `IS NOT NULL` 直接查位图；`AND/OR/NOT` 遵循 Kleene 真值表。
- **投影 / 聚合对稀疏与重复选择的正确处理**：
  只按选择下标 `gather`，不扫描整列；每个下标处理一次，重复下标重复投影、重复计入聚合
  （多重集语义）。支持 `count(*)/count(col)/sum/avg/min/max` 与单列 `groupBy`（NULL 自成组）。
- **顶层 `union`**：多个过滤分支各自产出选择向量后做**多重集并集**（同一行可被重复选择），
  与标准 `or`（每行至多一次）形成对照。
- **双引擎差分**：每个查询同时跑向量化引擎与逐行解释器，选择向量、投影、聚合逐项比较，
  不一致即抛 `EngineMismatchException`。
- **JSON 入口**：手写 JSON 解析 / 序列化，`POST /query`、`GET /tables`、`GET /health`。
- **可导出**：`exportDir` 下落盘 `table.json` / `plan.json` / `result.json`。

## 2. 环境与构建

- 已在 **OpenJDK 21**（`openjdk 21.0.12`，Linux x86_64）上验证；语法级别 Java 17+ 即可编译。
- 无任何外部依赖、无 Maven/Gradle 需求。

```bash
./build.sh          # 编译到 target/classes
./test.sh           # 编译并运行全部自动化测试
./run.sh examples/request_filter.json
./serve.sh 8080     # 启动 HTTP 服务并预加载 examples/table_orders.json
```

也可以直接用 `java`：

```bash
javac -d target/classes $(find src/main/java -name '*.java')
java -cp target/classes vecq.Main run examples/request_filter.json
java -cp target/classes vecq.Main serve --port 8080 examples/table_orders.json
cat req.json | java -cp target/classes vecq.Main run -
```

## 3. 目录结构

```
src/main/java/vecq/
  Json.java                 手写 JSON 解析/序列化（无第三方依赖）
  NullBitmap.java           NULL 位图（long[] 位集），与值数组分离
  Column.java / IntColumn.java / StringColumn.java   列抽象与两种列
  Table.java                内存表（等长列）、JSON 往返
  SelectionVector.java      选择向量（惰性全选/稀疏/重复/校验/多集合并）
  Tri.java                  三值逻辑
  FilterExpr.java           过滤计划 AST（compare/isNull/and/or/not/union）
  AggSpec.java / QueryPlan.java / PlanBuilder.java   聚合描述、逻辑计划、校验
  VectorFilter.java         向量化批过滤（被测核心）
  Accumulator.java          count/sum/avg/min/max 批量累加器
  ProjectAggregate.java     按选择向量的列式投影与聚合
  RowInterpreter.java       逐行解释器（参照实现）
  QueryEngine.java          双引擎执行 + 差分比较
  QueryResult.java / Exporter.java                   响应组装、JSON 导出
  VecqServer.java / Main.java                        HTTP 入口与 CLI
src/test/java/vecq/test/   无框架测试（13 个测试类，AllTests 统一入口）
examples/                  请求与表样例
build.sh / test.sh / run.sh / serve.sh
```

## 4. 请求 JSON 格式

最小完整示例见 `examples/request_filter.json`。字段：

| 字段 | 说明 |
|---|---|
| `table` | 内联表对象，或已注册的表名字符串（serve 预加载时可用） |
| `batchSize` | 批大小，正整数，默认 4 |
| `filter` | 过滤表达式（可省略 = 全选） |
| `projection` | 投影列名数组（列式输出） |
| `aggregates` | 聚合数组，可用 `"count(*)"` 简写或 `{"func","column","alias"}` |
| `groupBy` | 分组列名数组 |
| `selection` | 显式输入选择下标数组（可重复，会排序并校验范围） |
| `exportDir` | 结果导出目录 |

内联表：

```json
{ "name": "orders",
  "columns": [
    { "name": "id", "type": "int", "values": [10, 20, null] },
    { "name": "status", "type": "string", "values": ["NEW", null, "NEW"] }
  ] }
```

NULL 两种等价表达，任选其一；同时给出时行集合必须一致：
- `values` 中直接写 `null`；
- `"nullRows": [3]` 位图声明。

过滤表达式：

```json
{ "column": "id", "op": ">=", "value": 20 }
{ "column": "status", "op": "=", "value": "NEW" }
{ "op": "isNull", "column": "id" }
{ "op": "isNotNull", "column": "status" }
{ "op": "and", "children": [ {...}, {...} ] }
{ "op": "or",  "children": [ {...}, {...} ] }
{ "op": "not", "child": {...} }
{ "op": "union", "branches": [ {...}, {...} ] }
```

- 整型支持 `= != < <= > >=`（`==`/`<>` 是别名）；字符串仅 `= !=`。
- 与字面量 `null` 比较恒为 UNKNOWN（判空请用 `isNull`）。
- `union` 只能作为根节点；各分支独立过滤后做多集合并，允许同一行重复出现。

聚合：`count(*)` 数选择向量行数（含重复、含 NULL）；`count(col)` 忽略该列 NULL；
`sum/avg` 仅整型；`min/max` 整型与字符串均可；无非 NULL 输入时 `sum/avg/min/max` 为 `null`；
`avg` 返回 double。

## 5. 响应 JSON 形态

```jsonc
{
  "ok": true,
  "table": "orders",
  "batchSize": 4,
  "selectedRows": [1, 2, 4, 5],     // 过滤后的选择向量（有序、可能重复）
  "selectedCount": 4,
  "projection": { "format": "columnar", "rowCount": 4,
                  "columns": { "id": [20,30,50,60], "amt": [null,300,null,600] } },
  "aggregates": { "kind": "global", "row": { "count(*)": 4, "amt_sum": 900 } },
  "plan": { ... },                   // 可导出的逻辑执行计划
  "execution": {
    "vector":        { "filterBatches": 2, "downstreamBatches": 2 },
    "rowInterpreter":{ "filterBatches": 6, "downstreamBatches": 2 },
    "enginesAgree": true
  }
}
```

分组聚合时 `aggregates.kind = "grouped"`，带 `groupColumns` 与 `rows`。
错误响应统一为 `{"ok": false, "error": "..."}`：计划非法 / 选择下标越界返回 HTTP 400。

## 6. HTTP 用法

```bash
./serve.sh 8080
curl -s localhost:8080/health
curl -s localhost:8080/tables
curl -s -X POST localhost:8080/query -H 'Content-Type: application/json' \
     -d @examples/request_by_table_name.json
curl -s -X POST localhost:8080/query -d '{"table":"orders","selection":[999]}'   # 400
```

## 7. 关键设计：稀疏行与重复选择如何被正确处理

1. 过滤阶段只操作选择向量（下标），不移动列数据；NULL 通过位图判定，不依赖哨兵值。
2. 投影通过 `Column.gather(rows)` 按**当前选择向量**收集，未选中的行物理上不可能被读到，
   因此稀疏选择不会串入无关行。
3. 聚合累加器按 `rows[off..off+len)` 逐下标更新；`count(*)` 直接累加批长度，
   因此重复下标被重复计数，`sum` 等重复累加（见 `request_union_duplicates.json`：
   分支重叠使行 1 出现两次，`count(*)=5`、`sum(id)=130`）。
4. 下游算子对**选择向量本身**切批（而非对源表行号），批边界与数据稀疏度无关。

## 8. 验收点与测试对照

`./test.sh` 运行 13 个测试类、191 个断言，其中：

| 验收要求 | 测试类 |
|---|---|
| 对照逐行解释器 | 引擎内每个查询强制差分；`RandomDifferentialTest`（固定种子 500 轮随机） |
| 批边界 | `BatchBoundaryTest`：batchSize 从 1 遍历到 n+2，断言结果不变且批数为 ceil(n/bs)，含空表/空结果 |
| 全空列 | `AllNullColumnTest`：比较全 UNKNOWN、IS NULL 全命中、聚合与 NULL 分组 |
| 重复索引 | `DuplicateSelectionTest`：显式重复 selection、union 多集合并、投影/聚合多重集 |
| 无效选择下标 | `InvalidSelectionTest`：越界、负下标、列不存在、类型不匹配、非法算子、union 嵌套 |
| NULL 位图基础 | `NullBitmapTest`、`ColumnTableTest` |
| 三值逻辑真值表 | `TriTest`、`FilterTest` |
| 投影/聚合/分组 | `ProjectAggTest` |
| JSON 解析往返 | `JsonRoundTripTest` |
| HTTP 真实链路 | `ServerTest`（随机端口起服务，200/400/405） |

## 9. 范围与限制（如实说明）

- 单机、内存、单线程；无查询优化器 / 索引 / JOIN / ORDER BY / LIMIT（均不在需求内）。
- 整型固定为 32 位有符号整数；字符串比较只支持等值；`avg` 为 double，`sum/count` 为 long。
- 核心运算全部手写，未使用任何现成 SQL 引擎；仅用 JDK 标准库（含内置 HTTP 服务）。
