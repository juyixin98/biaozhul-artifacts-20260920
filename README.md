# 三值逻辑表达式查询引擎（TVL Query Engine）

纯 Java 实现的**单机内存查询引擎 + JSON 请求入口**。核心表达式运算（词法、语法、
类型检查、三值逻辑求值、执行计划）全部自行实现，**不调用任何现成 SQL/关系引擎**，
除 JDK 外无第三方依赖（无需 Maven/Gradle、可离线构建）。

> 仅后端：提供 CLI 与内置 HTTP 接口，不含任何前端页面。

---

## 1. 它能做什么

- 解析带**类型检查**的布尔表达式：比较、`AND`、`OR`、`NOT`、`IS [NOT] NULL`、
  整数算术（`+ - * /`、一元负号）与括号。
- 实现 SQL 式 **三值逻辑（3VL）**：`TRUE` / `FALSE` / `UNKNOWN`（由 `NULL` 产生）。
- **短路求值**：`FALSE AND x`、`TRUE OR x` 不求值右侧。
- **整数溢出与除零返回明确错误**，错误精确定位到字符偏移 / 行 / 列，并给出指示符。
- 在内存表上执行 `SCAN -> FILTER -> PROJECT -> LIMIT`，返回**数据行、统计与执行计划**。
- 支持数据与计划**导出（export）**，导出内容可直接重新装载（round-trip）。

---

## 2. 目录结构

```
src/tvl/
  json/Json.java           自研 JSON 解析/书写器（零依赖）
  lexer/                   词法分析（Token、关键字、整数/字符串字面量）
  expr/                    AST（sealed 层级）、Tri 三值、表达式异常基类
  parser/Parser.java       递归下降解析器（优先级见下）
  analyzer/                DataType / Schema / TypeChecker 类型检查
  evaluator/Evaluator.java 三值逻辑求值、短路、溢出与除零
  engine/                  Table/Row/Catalog、QueryEngine、执行计划节点
  api/JsonApi.java         JSON 请求分发（load/query/export）与错误定位
  server/ApiServer.java    JDK com.sun.net.httpserver 极简 HTTP 入口
  Main.java                CLI：serve / run / stdin
test/tvl/test/             零依赖测试框架 + 7 个测试类（274 条断言）
samples/                   JSON 请求样例
build.sh / test.sh / run.sh
```

---

## 3. 构建与运行

环境：Java 21（语法使用 `sealed`/`switch 模式匹配`；更低版本需调整语法）。

```bash
./build.sh          # 编译到 out/
./test.sh           # 运行全部自动化测试
./run.sh run samples/09_batch.json   # 执行一个 JSON 文件（对象 / 数组 / 多对象拼接）
cat req.json | ./run.sh stdin        # 从标准输入执行
./run.sh serve --port 8080           # 启动 HTTP 服务
```

HTTP：`POST /query`，请求体为一个 JSON 对象；`GET /health` 健康检查。
错误响应 HTTP 状态码为 400，成功为 200。

---

## 4. 表达式语法与优先级

优先级从低到高：

```
orExpr    := andExpr ( OR andExpr )*
andExpr   := notExpr ( AND notExpr )*
notExpr   := NOT notExpr
predicate := comparison ( IS [NOT] NULL )?
comparison:= additive ( (=|==|<>|!=|<|<=|>|>=) additive )?     # 不允许链式比较
additive  := term ((+ | -) term)*
term      := unary ((* | /) unary)*
unary     := -unary | primary
primary   := INTEGER | '字符串' | NULL | TRUE | FALSE | UNKNOWN | 列名 | '(' orExpr ')'
```

- 关键字大小写不敏感（`and`/`And`/`AND` 等价），列名大小写敏感。
- 字符串用 SQL 单引号，`''` 表示一个单引号。整数为 64 位有符号整数。

---

## 5. 三值逻辑语义（SQL）

真值表（穷举验证见 `TruthTablesTest`）：

| AND     | TRUE    | FALSE | UNKNOWN |
|---------|---------|-------|---------|
| TRUE    | TRUE    | FALSE | UNKNOWN |
| FALSE   | FALSE   | FALSE | **FALSE** |
| UNKNOWN | UNKNOWN | FALSE | UNKNOWN |

| OR      | TRUE | FALSE   | UNKNOWN |
|---------|------|---------|---------|
| TRUE    | TRUE | TRUE    | **TRUE** |
| FALSE   | TRUE | FALSE   | UNKNOWN |
| UNKNOWN | TRUE | UNKNOWN | UNKNOWN |

| NOT | 值 |
|-----|----|
| TRUE | FALSE |
| FALSE | TRUE |
| UNKNOWN | UNKNOWN |

其他规则：

- `NULL` 参与**任何比较**结果均为 `UNKNOWN`，包括 `NULL = NULL`（永不为 TRUE）。
  判空只能用 `x IS NULL` / `x IS NOT NULL`，其结果恒为 TRUE/FALSE。
- `NULL` 参与算术结果为 `NULL`。
- WHERE 过滤：仅谓词结果为 **TRUE** 的行通过；FALSE 与 UNKNOWN 一律剔除。
- 短路：`AND` 左值为 FALSE、`OR` 左值为 TRUE 时不求值右侧；
  左值为 **UNKNOWN** 时结果仍取决于右侧，因此**必须继续求值**（右侧的除零等错误照常抛出）。
- 整数算术用 `Math.*Exact` 实现：加/减/乘/一元负号溢出、除零、`MIN/-1` 溢出
  都抛 `EVAL_ERROR`。

---

## 6. JSON 请求与响应

### 6.1 load —— 装载/替换内存表

```json
{
  "action": "load",
  "table": "employees",
  "columns": [
    {"name": "id",   "type": "INTEGER"},
    {"name": "name", "type": "STRING"},
    {"name": "active","type": "BOOLEAN"}
  ],
  "rows": [
    {"id": 1, "name": "ada",   "active": true},
    {"id": 2, "name": null,    "active": null}
  ]
}
```

类型名兼容 `INTEGER/INT/BIGINT`、`STRING/TEXT/VARCHAR`、`BOOLEAN/BOOL`。
装载时逐格校验：值类型与声明不符、缺列、多出列、均返回 `TYPE_MISMATCH`。

### 6.2 query —— 查询

```json
{
  "action": "query",
  "table": "employees",
  "where": "dept = 'eng' AND (age >= 30 OR mgr_id IS NULL)",
  "select": ["id", "age"],
  "limit": 10
}
```

`where` / `select` / `limit` 均可省略；`select` 省略或为 `["*"]` 表示全部列。
响应包含 `columns`、`rows`、`rowCount`、`stats{scanned,matched,returned}` 与
`plan`（执行计划树，每个节点带 `rowsIn/rowsOut`）。

### 6.3 export —— 导出

```json
{ "action": "export" }
```

或 `{"action":"export","table":"employees"}` 导出单表。返回所有表的列定义与数据行
（NULL 序列化为 JSON `null`），结构与 `load` 兼容，可直接重新导入。

### 6.4 错误信封

```json
{
  "ok": false,
  "error": "TYPE_ERROR",
  "message": "type error in comparison '>': incompatible types INTEGER and STRING",
  "detail": {
    "message": "...",
    "offset": 4,
    "line": 1,
    "column": 5,
    "snippet": "age > '30'\n    ^"
  }
}
```

错误码：`INVALID_JSON`、`INVALID_REQUEST`、`LEX_ERROR`、`PARSE_ERROR`、
`TYPE_ERROR`、`EVAL_ERROR`、`UNKNOWN_TABLE`、`ENGINE_ERROR`、`TYPE_MISMATCH`。

### 6.5 批量 / 多请求

一次输入可以是：单个 JSON 对象、JSON 数组（每个元素一个请求）、或多个 JSON 对象
首尾拼接（支持美化多行 / JSON Lines）。批量内请求**顺序执行、共享内存表**，
因此可先 `load` 再多次 `query`/`export`。任一响应 `ok=false` 时 CLI 退出码为 1。

---

## 7. 自动化测试覆盖

`./test.sh` 运行 7 个套件、**274 条断言**：

| 套件 | 覆盖点 |
|------|--------|
| `TruthTablesTest` | **穷举** AND/OR 各 3×3、NOT 3 格；IS NULL 真值表；六个比较符 × NULL 在左/右/两侧；短路分支除零（FALSE-AND、TRUE-OR 短路；UNKNOWN 不短路）；整数比较小表穷举；`UNKNOWN` 字面量 |
| `ParserPrecedenceTest` | AND/OR/NOT/比较/算术/一元负号优先级与结合性、括号、`IS NULL` 绑定位置、链式比较与悬空运算符等语法错误（用 AST 的 S-表达式结构断言） |
| `ErrorPositionTest` | 词法/语法/类型/求值错误的字符偏移、行列换算与 caret 片段；溢出/除零指向具体运算符 |
| `TypeCheckerTest` | 算术仅限整数、比较须同类型族、布尔不可排序、逻辑运算须布尔、未知列；NULL/UNKNOWN 组合 |
| `EvaluatorSemanticsTest` | NULL 算术传播、截断除法、四则/一元溢出边界、除零与 `MIN/-1`、字符串/布尔比较、复合 3VL 分支、NULL 除数得 NULL 而非除零 |
| `EngineApiTest` | 端到端 JSON：装载类型校验、3VL 过滤行计数、IS NULL 过滤、投影/limit、计划统计、短路除零经 API、全部错误信封、export round-trip、批量状态共享 |
| `JsonTest` | JSON 往返、键顺序、转义/`\u`、数字类型、非法输入（前导零、尾逗号等） |

---

## 8. 实际运行记录（如实摘录）

构建与测试（本机 OpenJDK 21）：

```
$ ./test.sh
== Exhaustive 3VL truth tables      passed=112 failed=0
== Parser precedence / grammar      passed=24  failed=0
== Error positions (offset/...)     passed=19  failed=0
== Type checker                     passed=24  failed=0
== Evaluator semantics              passed=36  failed=0
== Engine + JSON API end-to-end     passed=40  failed=0
== JSON parser/writer               passed=19  failed=0
TOTAL: 274 passed, 0 failed, 274 assertions
```

批量样例 `samples/09_batch.json`（load → 两条查询 → export）：

- `x IS NOT NULL AND x >= 2` 在 4 行（含一个 NULL）上 matched=2，返回 x=2,3；
- `NOT (x < 3)` matched=1，返回 x=3；
- export 回 4 行，NULL 保持为 JSON `null`。

员工表串联演练（load 后依次查询，输出汇总）：

```
0 LOAD ok rows = 5
1 QUERY ok ids = [1, 5]   stats = {scanned:5, matched:2, returned:2}   # eng AND (age>=30 OR mgr IS NULL)
2 QUERY ok ids = [4, 5]   ...                                           # age IS NULL OR active IS NULL
3 QUERY ok ids = []        matched=0                                    # id=999 AND 1/0：FALSE 短路，无除零
4 EVAL_ERROR | division by zero in '/' | offset 14 line 1 col 15        # id=1 AND 1/0：左 TRUE 强制求值右侧
5 TYPE_ERROR | comparison '>': INTEGER and STRING | offset 4 col 5      # age > '30'
6 EVAL_ERROR | integer overflow in '+' | offset 20 col 21               # MAX + id
```

HTTP 入口（`./run.sh serve --port 8099`，curl 实测）：

- `GET /health` → `{"ok":true,...}`；
- load/query/export 状态码均为 200，查询返回计划根节点 `LIMIT`；
- 类型错误返回 HTTP **400**，响应 `error=TYPE_ERROR` 且带 `snippet` 指示符。

### 已知边界 / 设计取舍

- 只支持 INTEGER / STRING / BOOLEAN 三种标量；不支持浮点与日期（load 时遇到 JSON
  小数报类型错误，避免隐式精度语义）。
- 单次进程的内存状态不跨进程持久化；需要留存时用 `export` 导出 JSON。
- HTTP 服务面向本地/单机演示，无鉴权与连接管理。
- CLI 在响应含任何 `ok:false` 时退出码为 1（便于脚本判断），HTTP 对错误返回 400。
