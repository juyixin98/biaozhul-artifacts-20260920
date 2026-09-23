# 窗口聚合算子（单机内存查询引擎）

纯 Java 实现的单机、内存、窗口聚合查询引擎。核心窗口运算**不依赖任何 SQL 引擎或第三方库**
（JSON 解析/序列化均为手写），通过 JSON 文件 / 标准输入作为请求入口，并可把数据与执行计划
导出为 JSON。

实现的窗口能力：

- 分区（`PARTITION BY`，支持单列 / 多列 / 无分区 / NULL 分区键）；
- 分区内排序后的 **`ROW_NUMBER`** 与 **`RANK`**（明确的并列与跳号语义）；
- **滑动 `ROWS` 窗口求和 `SUM`**（物理行偏移帧、分区边界裁剪、空帧、NULL 忽略、负值）；
- 聚合值为**可检查溢出的 64 位有符号整数**：中间累加用 `BigInteger`，结果超出 `long` 范围
  时以错误码 `OVERFLOW` 明确报错，绝不静默回绕。

本项目只有后端，不含任何前端或 HTTP 服务。

---

## 1. 目录结构

```
.
├── src/windowengine/            # 主源码
│   ├── Value.java               # 值类型：LONG / STRING / NULL
│   ├── Row / Schema / Relation  # 内存数据模型
│   ├── Json.java                # 手写 JSON 解析器 + 输出器（仅接受整数数字）
│   ├── RequestParser.java       # JSON 请求 -> 结构化 QueryRequest（含全部语义校验）
│   ├── QueryRequest.java
│   ├── Exporter.java            # 数据 / 计划 / 规范化请求导出
│   ├── JsonCodec.java           # 关系 & 计划 -> JSON
│   ├── Main.java                # JSON 请求入口（文件参数 / stdin）
│   ├── plan/                    # 执行计划模型（窗口规格、排序键、帧、函数）
│   └── engine/
│       ├── Partitioner.java     # 分区
│       ├── RowComparator.java   # 排序（ASC/DESC、NULLS FIRST/LAST、稳定兜底）
│       ├── WindowOperator.java  # ROW_NUMBER / RANK / ROWS SUM
│       └── WindowEngine.java    # 分区 -> 排序 -> 窗口 -> 按原行序收集
├── tests/windowengine/          # 测试（零框架，自带迷你 Runner）
│   ├── NaiveReferenceWindow.java# 朴素逐行 O(n²) 参考实现（独立代码路径）
│   ├── WindowEngineTest.java    # 19 个固定用例 + 420 组随机差分
│   ├── RequestParserTest.java
│   ├── SerializationTest.java
│   ├── EndToEndTest.java        # 真实子进程跑 CLI
│   ├── TestData / Asserts / TestRunner
├── examples/                    # 请求样例
├── scripts/build.sh             # 仅编译主源码
├── scripts/run_tests.sh         # 编译 + 跑全部测试
└── scripts/run.sh               # 运行请求文件（或管道 stdin）
```

---

## 2. 环境要求

- JDK 17+（开发与验收实测使用 **OpenJDK 21.0.12**）；
- 无任何第三方依赖、无 Maven/Gradle 要求，只用 `javac` / `java`。

确认环境：

```bash
javac -version   # javac 21.x（17+ 即可）
```

---

## 3. 快速开始

### 3.1 编译

```bash
bash scripts/build.sh
# 产物在 build/classes
```

### 3.2 运行请求样例

```bash
# 文件参数方式
bash scripts/run.sh examples/01_rank.json

# 标准输入方式
cat examples/02_sliding_sum.json | bash scripts/run.sh

# 一次跑多个请求（输出之间带分隔注释）
bash scripts/run.sh examples/01_rank.json examples/02_sliding_sum.json
```

也可以直接用 java：

```bash
java -cp build/classes windowengine.Main examples/01_rank.json
```

成功输出形如：

```json
{
  "ok" : true,
  "source" : "examples/01_rank.json",
  "rowCount" : 6,
  "result" : { "columns" : [ ... ], "rows" : [ ... ] }
}
```

出错时（列不存在、非法帧、溢出等）：

```json
{ "ok" : false, "error" : { "code" : "OVERFLOW", "message" : "..." } }
```

并以退出码 `2` 结束（用法 / IO 级错误为 `1`）。

### 3.3 导出数据与执行计划

请求里加 `"export": "目录"`（见 `examples/03_cumulative_export.json`），执行后会写入：

- `data.json`：输出关系（原列 + 窗口结果列），可作为新查询的 `data.file` 数据源回读；
- `plan.json`：规范化执行计划，默认帧、默认 NULL 顺序全部显式展开，帧同时给结构与
  `sqlText`（如 `ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW`）；
- `request.json`：规范化完整请求，`data` 为**原始输入数据**，可原样再次提交复现结果。

数据也可以放在外部文件里，用 `"data": { "file": "/path/to/data.json" }` 引用。

---

## 4. 请求格式

```jsonc
{
  "data": {
    "columns": [
      {"name": "dept", "type": "STRING"},   // type: LONG（默认）| STRING
      {"name": "v", "type": "LONG"}
      // 也可简写为裸列名 "v"（默认 LONG）
    ],
    "rows": [
      ["east", 5],                          // 整数 / 字符串 / null
      ["west", null]
    ]
  },
  "plan": {
    "window": {
      "partitionBy": ["dept"],              // 可省略 = 单一分区
      "orderBy": [
        {"column": "v", "direction": "ASC", "nullOrder": "FIRST"}
        // direction: ASC（默认）| DESC
        // nullOrder: FIRST | LAST；省略时按 SQL 标准：ASC->FIRST，DESC->LAST
        // 也可简写为裸列名 "v"（ASC + 默认 NULL 顺序）
      ],
      "frame": "ROWS BETWEEN 1 PRECEDING AND 1 FOLLOWING"
      // frame 可省略：有 ORDER BY 时默认 UNBOUNDED PRECEDING..CURRENT ROW，
      //                无 ORDER BY 时默认整个分区；
      // frame 也可以是结构化对象：
      // {"mode":"ROWS",
      //  "start":{"type":"UNBOUNDED_PRECEDING"},
      //  "end":  {"type":"CURRENT_ROW"}}
      // 边界 type: UNBOUNDED_PRECEDING | PRECEDING(offset) | CURRENT_ROW
      //          | FOLLOWING(offset) | UNBOUNDED_FOLLOWING
    },
    "functions": [
      {"function": "ROW_NUMBER", "alias": "rn"},
      {"function": "RANK",       "alias": "rk"},
      {"function": "SUM", "column": "v", "alias": "moving_sum"}
      // alias 省略时自动生成：row_number1 / sum_v ...
    ]
  },
  "export": "build/out"   // 可选

}
```

约束与约定：

- 数字域只支持整数（JSON 里写小数 / 指数会被拒绝，错误码 `INVALID_JSON`）；
- 窗口输出列别名不得与输入列或其他别名重名（大小写不敏感）；
- `SUM` 的参数列必须是 `LONG`；
- 仅实现 `ROWS` 帧；写 `RANGE` / `GROUPS` 报 `UNSUPPORTED`；
- 帧起点晚于终点（如 `ROWS BETWEEN 2 FOLLOWING AND 1 PRECEDING`）报 `INVALID_FRAME`。

---

## 5. 语义说明（并列、NULL、窗口、溢出）

### 5.1 ROW_NUMBER 与 RANK 的并列

- 排序按 `ORDER BY` 键从左到右比较；所有键值全等的两行为**并列行（peer）**。
  并列判定与 ASC/DESC、NULLS FIRST/LAST 无关；`NULL = NULL` 视为相等。
- `ROW_NUMBER`：分区内物理序号，从 1 开始，**即使并列也连续且不重复**；
  完全同键时用**原始输入行号**稳定打破并列（结果确定、可复现）。
- `RANK`：并列行得到相同名次，其后**跳号**。例如成绩 `90,90,80` 给出 `1,1,3`。
- 无 `ORDER BY` 时，整个分区互为并列：`RANK` 恒为 1。

### 5.2 NULL 排序顺序

NULL 不参与数值 / 字符串大小比较，其位置只由 `NULLS FIRST | LAST` 决定，
**不会被 ASC/DESC 翻转**（这是本实现刻意保证、并有测试锁定的语义）：

| direction | 省略 nullOrder 时的默认 |
| --------- | ----------------------- |
| ASC       | NULLS FIRST（SQL 标准） |
| DESC      | NULLS LAST（SQL 标准）  |

### 5.3 滑动 ROWS 窗口与 SUM

- 帧是相对当前行的**物理行闭区间** `[start, end]`（单位是行，不是值范围）。
- 帧先按分区边界裁剪；`PRECEDING/FOLLOWING` 偏移再大也只是空区间，不会跨行到其他分区。
- 帧与分区不相交（如 `ROWS BETWEEN 5 FOLLOWING AND 8 FOLLOWING` 在 3 行分区里）时为
  **空帧，结果为 NULL**。
- 帧内的 NULL 输入被**忽略**；帧内全部是 NULL（或空帧）时结果为 NULL。
- 负值正常参与求和。

### 5.4 整数溢出检查

`SUM` 的前缀累加使用任意精度 `BigInteger`，在产出每个结果值时用 `longValueExact()`
检查是否落在 64 位有符号整数区间；越界即抛 `OVERFLOW`（正向、负向都检测），
不会像 `long` 加法那样静默回绕。见 `examples/04_overflow_error.json`。

---

## 6. 自动化测试与正确性策略

```bash
bash scripts/run_tests.sh
```

测试不使用 JUnit（保持零依赖），自带迷你 Runner；端到端用例会**真实拉起子进程**执行
`java windowengine.Main`。

正确性的核心手段是**差分测试**：另有一份刻意与生产引擎走不同代码路径的
**朴素逐行参考实现** `NaiveReferenceWindow`（插入排序、逐行重算分区 / 排名 / 帧区间和、
不使用 plan 与 engine 包中的任何算法），在大量随机数据上与生产引擎逐格比较：

- 300 组随机场景：随机行数（1–24）、分区键（单列/两列/无）、ASC/DESC、
  NULLS FIRST/LAST、8 种帧（含越界空帧、无界帧、非法帧）、含 NULL 与负值数据；
- 120 组字符串排序键场景：验证 STRING 类型上的排序、并列、NULL 与窗口求和；
- 19 个固定边界用例，覆盖验收要求的：**并列键、单行分区、窗口越界、负值**，
  以及空表、多列排序键、NULL 分区键、全 NULL 帧、正/负溢出等。

测试分类：

| 测试类 | 内容 |
| ------ | ---- |
| `WindowEngineTest` | 固定用例 + 对朴素参考的随机差分 |
| `RequestParserTest` | 列/别名重名、列不存在、类型不符、非法帧、默认值展开、JSON 拒绝小数等 |
| `SerializationTest` | JSON 往返、中文/转义、导出文件可回读复现、输出列顺序 |
| `EndToEndTest` | stdin/文件入口、退出码、错误码透传、溢出、导出落盘（真实子进程） |

---

## 7. 实测记录（本仓库开发环境）

环境：Ubuntu 24.04，`javac/openjdk 21.0.12.1`，无第三方依赖。

构建与测试命令与结果：

```bash
bash scripts/build.sh        # 成功，编译主源码到 build/classes
bash scripts/run_tests.sh    # 成功：50 通过, 0 失败，共 50 个
```

其中随机差分包含 300 + 120 = **420 组**随机数据，均与朴素逐行参考逐格一致。

四个样例的实际运行：

```bash
bash scripts/run.sh examples/01_rank.json
#   ok=true, 6 行；华东 100,100,80 -> RANK 1,1,3；华北两个 90 并列 RANK 1，NULL 末尾 RANK 3

bash scripts/run.sh examples/02_sliding_sum.json
#   帧 1 PRECEDING..1 FOLLOWING，含负值/NULL：moving_sum = 70,0,-100,-45,25

bash scripts/run.sh examples/03_cumulative_export.json
#   ok=true，导出 build/sample-export/{data,plan,request}.json

bash scripts/run.sh examples/04_overflow_error.json
#   ok=false，error.code=OVERFLOW（9223372036854775808 超 long），退出码 2
```

开发过程中由测试实际捕获并修复的缺陷（如实记录）：

1. `RowComparator` 曾在 `DESC` 时把 NULL 的比较结果一并取反，导致
   `NULLS FIRST + DESC` 下 NULL 位置错误；已改为 NULL 位置只由 NULLS FIRST/LAST 决定，
   并由固定用例与 420 组随机差分锁定。
2. 帧边界偏移在程序化构造请求（`Integer`）时被误判为类型错误；已让整数读取兼容所有
   `Number`（JSON 解析进来仍只有 `Long`）。

无未通过项：当前 `50/50` 全部通过。

---

## 8. 错误码一览

| code | 触发场景 |
| ---- | -------- |
| `INVALID_JSON` | JSON 语法错误、数字为小数/指数、整数超出 long |
| `INVALID_REQUEST` | 缺字段、行列数不符、帧定义缺端点等 |
| `COLUMN_NOT_FOUND` | 分区/排序/SUM 引用了不存在的列 |
| `DUPLICATE_COLUMN` | 输入列重名、输出别名彼此或与输入列重名 |
| `AMBIGUOUS_COLUMN` | 大小写不敏感匹配到多列 |
| `TYPE_MISMATCH` | 跨类型比较、STRING 列收到整数、SUM 非 LONG 列等 |
| `INVALID_FRAME` | 帧起点晚于终点、位移为负/缺失、帧文本语法错误 |
| `UNSUPPORTED` | 非 ROWS 帧、未实现的窗口函数、不支持的列类型 |
| `OVERFLOW` | SUM 聚合结果超出 64 位有符号整数范围 |
| `IO_ERROR` | 数据/导出文件读写失败 |
