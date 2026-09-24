# 三值逻辑列批次查询执行器（TVL Query Executor）

纯后端服务：用 **Java + JDK 内置 `com.sun.net.httpserver.HttpServer`** 实现的列式
（columnar / 向量化）SQL 查询执行器，核心是 **SQL 三值逻辑（3VL：TRUE / FALSE /
UNKNOWN）** 与 **NULL 位图向量执行**。无界面、**零第三方依赖**、不借助任何外部 SQL
引擎执行核心逻辑。

---

## 1. 功能范围

### SQL 子集（受限 SELECT）

```sql
SELECT * | 标量表达式 [AS] 别名 (, ...)
FROM 表名
[WHERE 谓词]
```

- **标量表达式**：列名、整数/浮点/字符串字面量、`NULL`、`TRUE`/`FALSE`、位置参数 `?`
- **比较**：`=` `<>` `!=`（`<>` 别名）`<` `<=` `>` `>=`
- **空值判断**：`IS NULL`、`IS NOT NULL`
- **逻辑连接词**：`AND`、`OR`、`NOT`
- **括号** `( ... )` 显式分组；默认优先级 `NOT > AND > OR`
- 注释：`-- 行注释`、`/* 块注释 */`
- 关键字大小写不敏感；列名大小写敏感
- **不支持**：JOIN、GROUP BY、聚合、子查询、算术运算、投影中的布尔谓词、`IN/LIKE/ORDER BY/LIMIT`
  （均在本文"未完成项/边界"中声明）

### 三值逻辑语义（SQL 标准）

| AND | F | U | T |     | OR | F | U | T |     | NOT |   |
|-----|---|---|---|-----|----|---|---|---|-----|-----|---|
| **F** | F | F | F |     | **F** | F | U | T |     | F | T |
| **U** | F | U | U |     | **U** | U | U | T |     | U | U |
| **T** | F | U | T |     | **T** | T | T | T |     | T | F |

- 任何含 NULL 的比较结果为 **UNKNOWN**（`NULL = NULL`、`1 <> NULL` 都不是 TRUE）
- `WHERE` **只放行 TRUE**；UNKNOWN 与 FALSE 一起被过滤
- 经典陷阱也正确：`a = b OR a <> b` 在 NULL 行上是 **UNKNOWN** 而非 TRUE（排中律失效）

### 强类型 / 拒绝隐式混转

- 类型：`INTEGER`、`DOUBLE`、`STRING`、`BOOLEAN`
- 数值同族允许提升：`INTEGER` 与 `DOUBLE` 可互比（统一按 double 取值）
- **字符串与数值/布尔之间不做任何隐式转换**：`'1' = 1`、把字符串 `"1"` 绑给
  INTEGER 参数、STRING 列与数字比较 —— 一律 422 `TYPE_ERROR`
- 参数是**强类型绑定**：`paramTypes` 显式声明，不靠 JSON 值猜类型；
  `1.5` 绑 INTEGER 拒绝，`3.0`（整数值 double）绑 INTEGER 接受

### NULL 位图向量执行

- 每列 = `类型值数组 + boolean[] NULL 位图`（见 `ColumnVector`）
- 谓词对一个批次产出 `byte[]` 三值向量（编码 `FALSE=0 / UNKNOWN=1 / TRUE=2`，见 `Ternary`）
- `AND/OR/NOT` 只在位图字节上做紧凑循环，不产生逐行对象（`TruthVector`）
- 列引用零拷贝；常量/参数按批广播；投影按选择位图压缩

### 两条执行路径（验收用对照）

- **向量执行器** `engine.VectorEngine`：位图/批量路径（生产路径）
- **逐行解释器** `engine.RowInterpreter`：每行一个 `Object`/`Ternary` 的标量路径（参考模型）
- 两者**共享同一套比较语义** `engine.CompareOps`，区别仅在执行方式
- 请求带 `"crossCheck": true` 时，每个批次同时跑两条路径并在响应里给出
  `crossCheck: "MATCH"` 或不匹配明细。**核心逻辑完全自研，未使用任何外部 SQL 引擎。**

---

## 2. 目录结构

```
.
├── src/main/java/com/tvl/
│   ├── Main.java                 # 启动入口
│   ├── core/        Ternary, TruthVector        # 3VL 与真值位图
│   ├── types/       DataType, Values, TypeCheckException
│   ├── columnar/    Schema, Batch, ColumnVector # 列批次与 NULL 位图
│   ├── sql/         Expr, Select, Lexer, Parser, SqlParseException
│   ├── engine/      Analyzer, VectorEngine, RowInterpreter,
│   │                CompareOps, QueryExecutor, QueryRequest, BatchResult
│   ├── json/        Json, JsonWriter, JsonException  # 零依赖 JSON
│   └── http/        QueryHttpServer, ApiCodec
├── src/test/java/com/tvl/test/  # 零依赖断言框架 + 37 个测试套件
├── examples/        6 个请求样例 + curl-demo.sh
├── build.sh / test.sh / run.sh
├── dependencies.lock
└── README.md
```

---

## 3. 环境与依赖

- **JDK 17+**（开发/验收实测：`openjdk 17.0.20.1`，Linux）
- **不需要** Maven/Gradle/任何 jar；不需要网络下载依赖
- 见 `dependencies.lock`（锁的是 JDK 大版本；第三方依赖列表为空）

确认环境：

```bash
java -version
javac -version
```

---

## 4. 构建、测试、启动

```bash
# 编译（输出到 build/classes）
./build.sh

# 编译并运行全部自动化测试（退出码 0=全过）
./test.sh

# 启动服务（默认 8080，可传端口或用环境变量 TVL_PORT）
./run.sh          # 或 ./run.sh 9090
```

启动后：

- `GET  /health` → `{"service":"tvl-query-executor","ok":true}`
- `POST /query`  → 执行查询
- `GET  /`       → 简要用法

---

## 5. HTTP 接口与请求样例

### 请求体（`POST /query`，JSON）

| 字段 | 类型 | 说明 |
|------|------|------|
| `sql` | string | 必填，受限 SELECT |
| `paramTypes` | string[] | 每个 `?` 的声明类型，按位置；无参数可省略 |
| `params` | array | 参数值，按位置；元素可为 `null`（SQL NULL 参数） |
| `batches` | array | 一批或多批；每批含 `schema` 与 `rows` |
| `crossCheck` | bool | true 时同时跑向量/逐行两条路径并对照 |

- `schema`：`[{"name":"a","type":"INTEGER"}, ...]`
- `rows`：对象数组，缺列或显式 `null` 都表示该格 SQL NULL
- 空批次：`"rows": []`（schema 仍须非空）
- 多个批次必须 schema 一致；参数只绑定一次并对所有批次生效

### 响应（200）

```jsonc
{
  "ok": true,
  "outputSchema": [{"name":"id","type":"INTEGER"}, ...],
  "totalSelectedRows": 4,
  "batches": [
    {
      "inputRows": 4,
      "truth": ["TRUE","FALSE","TRUE","UNKNOWN"], // 每行原始三值（含 UNKNOWN）
      "selectedRows": 3,                           // WHERE 放行（仅 TRUE）行数
      "crossCheck": "MATCH",                       // 或 null（未对照）
      "rows": [ { ... 投影后的行 ... } ]
    }
  ]
}
```

错误响应（`ok:false`）：

| HTTP | errorCode | 触发场景 |
|------|-----------|----------|
| 400 | `INVALID_JSON` | 请求体不是合法 JSON |
| 400 | `SQL_PARSE_ERROR` | SQL 语法错误 |
| 400 | `INVALID_REQUEST` | 空批次列表、批次 schema 不一致等 |
| 422 | `TYPE_ERROR` | 类型不兼容、字符串数值混转、参数个数/类型错误、未知列 |
| 405 | `METHOD_NOT_ALLOWED` | 错误的 HTTP 方法 |

### curl

```bash
curl -s -X POST http://127.0.0.1:8080/query \
  -H 'Content-Type: application/json' \
  --data-binary @examples/01-basic-3vl.json | python3 -m json.tool
```

一键跑全部 6 个样例（自动起停服务）：

```bash
./examples/curl-demo.sh 8080
```

样例文件：

| 文件 | 覆盖点 |
|------|--------|
| `01-basic-3vl.json` | NULL 比较、IS NULL、多批次（含空批次）、crossCheck |
| `02-null-bitmap-and-not.json` | NULL 位图 + AND/OR/NOT，输出含 UNKNOWN 明细 |
| `03-parentheses-precedence.json` | AND/OR 优先级与三值传播 |
| `04-parameter-binding.json` | 多参数、INTEGER/STRING 强类型绑定 |
| `05-type-error-string-to-int.json` | 预期 422：字符串 "1" 绑 INTEGER |
| `06-empty-batch.json` | 空批次 |

---

## 6. 三值逻辑枚举对照（验收方式）

测试（`src/test/...`）不是只断言最终 TRUE/FALSE，而是逐行比对**三值向量的每一个字节
（含 UNKNOWN）**：

- **真值表穷举**：`AND/OR` 3×3、`NOT` 3 种输入全部枚举，手工期望值逐格核对，并验证
  枚举实现与位图字节实现一致；附德摩根律在 3VL 下成立
- **9 种三值组合**：两列各取 `{非空值A, 非空值B, NULL}` 笛卡尔 9 行，对 12 种谓词形态
  （6 比较符、IS NULL、AND/OR/NOT、括号）跑向量 vs 逐行，逐行 MATCH
- **6 比较符 × 4 类型**：INTEGER / DOUBLE / STRING / BOOLEAN，NULL 行必须 UNKNOWN
- **3³=27 行三态括号矩阵**：三变量独立取 TRUE/FALSE/UNKNOWN，8 个括号/优先级 SQL 全部
  MATCH，并显式验证加括号确实改变结果（AND 优先级）
- **空批次 / 跨批次**：`rows=[]`、空批与非空批混合、5 批共享一次参数绑定、schema
  不一致拒绝
- **参数类型错误**：字符串→INTEGER、DOUBLE 1.5→INTEGER、个数不符、NULL 参数
- **HTTP 端到端**：进程内起真实服务器，200/400/405/422 状态码与响应体

最近一次实际运行结果（本环境）：

```
== 共 37 个套件，249 项断言，0 个套件失败 ==
ALL TESTS PASSED
```

---

## 7. 实际运行记录（如实）

- 构建：`./build.sh` —— 通过（`javac 17.0.20.1`，26 个主源文件，0 warning 阻断）
- 测试：`./test.sh` —— **37 套件 / 249 断言全部通过**
- 端到端：`./examples/curl-demo.sh 8091` —— 6 个样例的实际响应符合预期，
  含一个预期的 `HTTP 422 TYPE_ERROR`
- 开发过程中测试真实捕获到的缺陷（均已修复并回归）：
  1. INTEGER/DOUBLE 跨类型比较时按 `double[]` 强取 `long[]` → `ClassCastException`
  2. 参数值为 `null` 时 `List.copyOf` 抛 NPE
  3. **列式解码共享数组 bug**：多列同类型表共用一个值数组导致列间互相覆盖
     （该 bug 正是被"27 行三态括号矩阵 vs 逐行解释器"对照测试暴露）
  4. JSON 解析器接受非标准前导零数字（如 `01`）
  5. BOOLEAN 裸列/`WHERE TRUE` 未作为合法谓词处理

---

## 8. 未完成项 / 已知边界（如实声明）

1. **SQL 覆盖面刻意受限**：无 JOIN、聚合、GROUP BY、子查询、集合运算、算术/函数表达式、
   `LIKE/IN/BETWEEN`、`ORDER BY/LIMIT/OFFSET`、`CAST`、DDL/DML。
2. **投影仅支持标量表达式**（列/常量/参数）；`SELECT a > 2` 这类谓词投影在解析层拒绝。
3. **`FROM` 后的表名仅做语法解析不校验**：本服务数据随请求提供，没有目录/存储层。
4. 标量位置不支持括号（如 `SELECT (a)`），谓词位置括号完整支持。
5. 无数值字面量一元负号解析（`-1` 作字面量）；负号当前仅出现在 JSON 数值中。
   谓词中需要负值时请用参数绑定。
6. 服务为固定 8 线程的简单 HTTP 实现，面向功能验收而非高并发生产；无鉴权/限流/持久化。
7. 单条 SQL 的参数个数与批次大小仅受内存限制，未做硬性上限。
```
