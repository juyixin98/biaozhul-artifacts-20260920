# 三值逻辑表达式查询引擎（3VL Query Engine）

一个**纯后端、单机内存**的查询引擎与表达式求值器。用 Java 17 实现，
**零外部依赖**（不使用任何现成 SQL 引擎 / 解析器生成器 / JSON 库 / 测试框架），
核心运算（词法、语法、类型检查、三值逻辑求值、溢出检测、JSON 处理）全部手写。
不包含任何前端代码。

表达式通过 **JSON 请求**提交，引擎返回选中行、每行的**三值判定**、
**AST** 与可导出的**执行计划**；表数据与计划均可导出为 JSON。

---

## 1. 它实现了什么

- 带**类型检查**的表达式解析器：
  - 比较：`=  <>  !=  <  <=  >  >=`
  - 逻辑：`AND  OR  NOT`
  - 判空：`x IS NULL`、`x IS NOT NULL`
  - 整数算术：`+  -  *  /`（一元负号、整除向零截断）
  - 字面量：整数、单引号字符串、`TRUE / FALSE / NULL`
  - 括号、SQL 风格注释（`-- 行注释`、`/* 块注释 */`），关键字大小写不敏感
- **SQL 三值逻辑（3VL）**：`TRUE / FALSE / UNKNOWN`，采用 Kleene 真值表；
  `NULL` 参与任何比较结果为 `UNKNOWN`，算术为 `NULL` 传播。
- **短路求值**：`TRUE OR x`、`FALSE AND x` 不求值 `x`；
  被跳过分支中的除零 / 溢出不会触发。
- **整数溢出 / 除零**返回**明确错误码**（`INTEGER_OVERFLOW` /
  `DIVISION_BY_ZERO`），绝不静默回绕。
- WHERE 过滤语义：**仅结果为 TRUE 的行被选中**，FALSE 与 UNKNOWN 都排除。
- 错误带**源码位置**（offset / line / column / 出错 token 长度）。

---

## 2. 环境与构建

要求 JDK 17+（仅用 `javac` / `java`，无需 Maven/Gradle，无需联网）。

```bash
bash build.sh        # 编译主代码与测试代码到 build/
bash run-tests.sh    # 运行全部自动化测试（失败时退出码非 0）
```

---

## 3. 快速开始

### 3.1 执行一个 JSON 查询（从文件）

```bash
java -cp build/classes tvl.cli.Main query samples/query-basic.json
```

### 3.2 从标准输入读取

```bash
echo '{"expression":"a = 1 OR a IS NULL",
       "table":{"name":"t","columns":[{"name":"a","type":"INTEGER"}],
                "rows":[[1],[2],[null]]}}' \
  | java -cp build/classes tvl.cli.Main query
```

### 3.3 穷举三值真值表

```bash
java -cp build/classes tvl.cli.Main truth-table
```

### 3.4 只看表达式 AST（语法解析）

```bash
java -cp build/classes tvl.cli.Main parse "x > 1 AND NOT y IS NULL"
```

退出码：`0` 成功；`2` 请求或表达式错误（错误体为 JSON）；`1` 用法 / IO 错误。

---

## 4. 表达式语法与优先级

优先级从**低到高**：

```
OR
AND
NOT                      （一元前缀）
IS [NOT] NULL
比较  = <> != < <= > >=   （非结合，不允许 a = b = c）
+  -
*  /
一元 -
原子  整数 | '字符串' | TRUE | FALSE | NULL | 列名 | ( 表达式 )
```

> 说明：比较紧于 `NOT`（`NOT 1 = 2` 等价 `NOT (1 = 2)`，标准 SQL 行为）；
> `AND` 紧于 `OR`（`TRUE OR FALSE AND FALSE == TRUE`）。

---

## 5. 三值逻辑语义

NULL 表示“未知”。布尔表达式的 NULL 被解释为 `UNKNOWN`。

| a | NOT a |
|---|---|
| TRUE | FALSE |
| UNKNOWN | UNKNOWN |
| FALSE | TRUE |

| AND | T | U | F |     | OR | T | U | F |
|---|---|---|---|---|---|---|---|---|
| **T** | T | U | F |     | **T** | T | T | T |
| **U** | U | U | F |     | **U** | T | U | U |
| **F** | F | F | F |     | **F** | T | U | F |

关键规则：

- `NULL = NULL` → **UNKNOWN**（不是 TRUE；判空必须用 `IS NULL`）
- `NULL <> NULL` → **UNKNOWN**
- 任何比较只要一侧为 NULL → **UNKNOWN**
- `NULL` 参与算术 → `NULL`（如 `NULL + 1 = NULL`）
- `U AND F = F`、`U OR T = T`（UNKNOWN 不总是传播到底）
- WHERE 只保留 TRUE：UNKNOWN 与 FALSE 一样被过滤掉

短路（关键验收点）：

- `TRUE OR  (1/0 = 1)` → TRUE，**不除零**
- `FALSE AND (1/0 = 1)` → FALSE，**不除零**
- `(1 = NULL) OR (1/0 = 1)` → UNKNOWN 不能定结果，**右侧仍要求值 → 抛除零错误**

---

## 6. JSON 请求 / 响应

### 6.1 请求

```json
{
  "expression": "x > 1 AND y IS NOT NULL",
  "includeTable": false,
  "table": {
    "name": "nums",
    "columns": [
      { "name": "x", "type": "INTEGER" },
      { "name": "y", "type": "INTEGER" }
    ],
    "rows": [
      [2, 10],
      [5, null]
    ]
  }
}
```

- `expression`（必填，字符串）：过滤表达式。
- `table`（可选）：
  - `columns[].type` 仅支持 `INTEGER` / `BOOLEAN` / `STRING`；
  - `rows` 为二维数组，单元值用 JSON `null` 表示 SQL NULL；
  - INTEGER 列只接受 JSON 整数（小数会被拒绝）。
- `includeTable`（可选，布尔）：为 true 时响应回带完整表数据。
- 不提供 `table` 时为**纯表达式模式**：编译通过后对空行求值一次，
  返回表达式自身标量值（布尔表达式另附 `triValue`）。

### 6.2 成功响应

```json
{
  "ok": true,
  "expression": "x > 1 AND y IS NOT NULL",
  "ast": { "...": "带节点类型/位置/resolvedType 的语法树" },
  "plan": {
    "operator": "Filter",
    "predicate": { "...": "AST" },
    "input": { "operator": "TableScan", "table": "nums",
               "columns": ["x", "y"], "estimatedRows": 5 }
  },
  "result": {
    "selectedCount": 2,
    "selectedRows": [[2, 10]],
    "evaluatedRows": [
      { "rowIndex": 0, "triValue": "TRUE", "selected": true, "row": [2, 10] }
    ],
    "triStats": { "TRUE": 2, "FALSE": 2, "UNKNOWN": 1 }
  },
  "table": { "...": "includeTable=true 时出现，含全部列与行" }
}
```

### 6.3 错误响应（HTTP 无关，用退出码 2 表达）

```json
{
  "ok": false,
  "error": {
    "code": "TYPE_MISMATCH",
    "message": "算术运算 '+' 要求 INTEGER 操作数，右侧为 STRING",
    "position": { "offset": 2, "line": 1, "column": 3 },
    "length": 1
  }
}
```

错误码：

| code | 阶段 | 含义 |
|---|---|---|
| `INVALID_JSON` | 请求解析 | 请求文本不是合法 JSON |
| `BAD_REQUEST` | 请求装载 | 结构/列类型/行列数/单元值不合法 |
| `SYNTAX_ERROR` | 语法 | 缺操作数、括号不匹配、多余内容等 |
| `UNEXPECTED_CHAR` | 词法 | 非法字符 |
| `UNTERMINATED_STRING` / `UNTERMINATED_COMMENT` | 词法 | 字符串 / 块注释未闭合 |
| `NON_ASSOCIATIVE_COMPARISON` | 语法 | 比较运算符连写（如 `a = b = c`） |
| `UNKNOWN_COLUMN` | 语义 | 列名不存在（位置指向该标识符） |
| `TYPE_MISMATCH` | 语义 | 操作数类型不匹配（位置指向操作符/操作数） |
| `INTEGER_OUT_OF_RANGE` | 语法 | 整数字面量超出 64 位范围 |
| `INTEGER_OVERFLOW` | 求值 | 算术/一元负号/除法溢出 |
| `DIVISION_BY_ZERO` | 求值 | 整数除以零 |

---

## 7. 数据与执行计划导出

- AST：每个节点含 `node`、`pos`、`length`，类型检查后含 `resolvedType`。
- 执行计划：物理算子树 `Filter -> TableScan`，`predicate` 内嵌完整 AST。
- 表数据：`includeTable: true` 时响应中的 `table` 含列定义、`rowCount`、全部行。
- `docs/output/` 下保存了对 `samples/` 每个样例的**真实运行响应**。

---

## 8. 自动化测试

```bash
bash run-tests.sh
```

零依赖手写迷你断言框架（`src/test/java/tvl/TF.java`）。测试分组：

| 套件 | 覆盖 |
|---|---|
| `TruthTableTest` | **穷举** NOT(3) + AND(3×3) + OR(3×3)，用独立数值编码 oracle（min/max/取负）验证，操作数走真实布尔列 |
| `NullComparisonTest` | NULL 参与 6 种比较的 3×3 穷举（INTEGER 与 STRING）、`NULL = NULL`、NULL 算术/逻辑传播、IS NULL、WHERE 三值过滤 |
| `PrecedenceTest` | 全部优先级层级、左结合、括号、一元负号、整除取整、关键字大小写、注释 |
| `ShortCircuitTest` | OR/AND 短路、被跳过分支除零/溢出不触发、UNKNOWN 不短路、嵌套短路、列驱动短路、对照组 |
| `OverflowTest` | 加减乘/一元负号/除法溢出、除零、边界值、字面量越界、列值触发、NULL/0 传播 |
| `ErrorPositionTest` | 错误码与 offset/line/column/length，含跨行定位、非结合比较、未闭合字面量 |
| `TypeCheckTest` | 合法形式、结果类型回填、NULL 未定型兼容性、非法形式 |
| `JsonParserTest` | JSON 解析/序列化往返、数字、转义、畸形输入 |
| `JsonRequestTest` | 端到端：选择结果、三值统计、短路跨多行、错误定位、数据/计划导出、纯表达式模式 |

最近一次完整运行：**474 个断言全部通过，0 失败**（见 `docs/RUNLOG.md`）。

---

## 9. 目录结构

```
.
├── build.sh / run-tests.sh      # 构建 / 测试脚本（仅依赖 JDK）
├── src/main/java/tvl/
│   ├── core/      # DataType / Value / TriBool / Pos / 异常
│   ├── parser/    # Lexer / Token / Parser(递归下降) / AST / TypeChecker
│   ├── eval/      # Evaluator（三值逻辑 + 短路 + 溢出检测）
│   ├── engine/    # Table / QueryEngine / RequestHandler(JSON 入口)
│   ├── json/      # 手写 JSON 解析器与序列化器
│   └── cli/       # Main（query / parse / truth-table）
├── src/test/java/tvl/           # 迷你测试框架 + 9 个测试套件 + TestRunner
├── samples/                     # JSON 请求样例（含成功与错误样例）
└── docs/
    ├── output/                  # 样例的真实运行响应
    └── RUNLOG.md                # 实际运行命令与结果记录
```

---

## 10. 设计取舍与已知限制

- 整数统一为 64 位有符号 `long`；整除向零截断（与 Java `/`、SQL 常规一致）。
- 源码中直接书写 `-9223372036854775808` 会在词法期以 `INTEGER_OUT_OF_RANGE`
  明确拒绝（数字部分超出正数范围，一元负号是独立节点）。该最小值可经列值或
  等价表达式 `(-9223372036854775807 - 1)` 参与运算，溢出仍被正确检测。
- 比较运算符刻意设计为**非结合**：`a = b = c` 报错，需用括号明确。
- 布尔类型只支持 `= / <>`，不支持排序比较（与多数 SQL 一致）。
- 字符串为字典序（Java UTF-16 序），不做 locale/排序规则。
- 纯内存单机引擎，无持久化、无网络服务；JSON 入口为“请求文本 → 响应文本”，
  方便接入任意传输层（HTTP/WebSocket/文件）而不耦合。
