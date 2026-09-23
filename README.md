# 窗口聚合算子（单机内存查询引擎）

纯 Java 实现的**单机、内存、无外部依赖**窗口算子，通过 JSON 请求驱动，
**不调用任何现成 SQL 引擎**完成核心运算。支持：

- 分区 + 排序后的 `ROW_NUMBER()`、`RANK()`；
- 滑动 **`ROWS`** 物理窗口求和 `SUM(...)`（含越界截断、偏移帧、前向空帧）；
- 明确的并列（tie）与 `NULL` 排序语义；
- 聚合值使用 **64 位有符号整数（`long`）并可检查溢出**；
- 输入数据与执行计划可导出为 JSON；
- 与一份**独立编写的朴素逐行参考实现**做差分对拍。

> 纯后端项目：没有 HTTP 服务、没有前端。入口是命令行程序 `engine.Main`，
> 从文件或标准输入读取一个 JSON 请求，向标准输出写一个 JSON 响应。

---

## 1. 目录结构

```
.
├── src/engine/
│   ├── Main.java                 # JSON 请求入口（CLI / stdin）
│   ├── json/                     # 手写 JSON 解析器 / 序列化器 / 值模型
│   ├── model/                    # Relation / Row / ColumnType
│   ├── window/                   # 窗口核心：引擎、规格、帧、溢出检查
│   └── executor/                 # JSON 请求 → Relation + WindowSpec → JSON 响应
├── test/
│   ├── testutil/
│   │   ├── TestHarness.java      # 零依赖微型断言框架
│   │   ├── Rows.java             # 行规范化/比较工具
│   │   └── NaiveWindowReference.java  # 朴素逐行 O(n²) 参考实现（对拍基准）
│   └── test/
│       ├── JsonTest.java
│       ├── WindowEngineTest.java
│       ├── DifferentialTest.java     # 引擎 vs 朴素参考（300 组随机 + 120 组溢出）
│       ├── ExecutorTest.java
│       └── RunAllTests.java
├── examples/                     # JSON 请求样例
├── out/exports/                  # 样例运行后导出的响应（可重新生成）
├── build.sh                      # 仅用 javac 编译主代码与测试
├── run_tests.sh                  # 编译并运行全部测试
└── bin/run.sh                    # 运行引擎
```

---

## 2. 环境与构建

需要 **JDK 17+**（开发实测 OpenJDK 17.0.20；21 亦可），**无需 Maven/Gradle/网络**。

若 `java/javac` 不在 `PATH`，通过 `JAVA_HOME` 指定：

```bash
export JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64
./build.sh          # 编译到 out/classes 与 out/test-classes
```

运行全部自动化测试：

```bash
./run_tests.sh
```

---

## 3. 运行方式

```bash
# 1) 从文件读请求，结果打印到标准输出
bin/run.sh examples/01_basic_partitioned.json

# 2) 从文件读请求，并把响应导出（数据/计划）到第二个文件
bin/run.sh examples/01_basic_partitioned.json out/exports/01_output.json

# 3) 从标准输入读取（管道/重定向）
echo '{"input":{...},"window":{...}}' | bin/run.sh
```

进程退出码：

| 退出码 | 含义 |
|---:|---|
| `0` | 成功 |
| `1` | 窗口计算错误（典型：整数求和溢出，响应 `error.type = WINDOW_ERROR`） |
| `2` | 请求错误（JSON 语法错误 `JSON_PARSE_ERROR`，或结构/语义错误 `INVALID_REQUEST`） |
| `3` | IO 错误（文件读写失败） |

---

## 4. 请求 / 响应格式

### 4.1 请求

顶层字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `input` | 对象，必填 | `columns`（`{name,type}` 列表，`type ∈ {LONG, STRING}`）与 `rows`（数组的数组；单元为整数/字符串/`null`） |
| `window` | 对象，必填 | 见下 |
| `includePlan` | 布尔，默认 `false` | 是否在响应中导出执行计划 |
| `includeData` | 布尔，默认 `true` | 是否在响应中导出结果数据 |

`window`：

| 字段 | 说明 |
|---|---|
| `partitionBy` | 分区列名数组，可省略（= 单一分区） |
| `orderBy` | 排序键数组：`{column, ascending, nullOrder}`；`ascending` 默认 `true`；`nullOrder` 省略时按 **SQL/PostgreSQL 默认**：ASC→`NULLS LAST`，DESC→`NULLS FIRST` |
| `functions` | 非空函数数组 |

函数规格：

```jsonc
{ "type": "ROW_NUMBER", "outputColumn": "rn" }
{ "type": "RANK",       "outputColumn": "rk" }
{ "type": "SUM", "outputColumn": "s", "argument": "v",
  "frame": { "mode": "ROWS",
             "start": {"kind": "UNBOUNDED_PRECEDING"},
             "end":   {"kind": "CURRENT_ROW"} } }
```

帧边界 `kind`：`UNBOUNDED_PRECEDING` / `PRECEDING`（需 `offset`≥0）/
`CURRENT_ROW` / `FOLLOWING`（需 `offset`≥0）/ `UNBOUNDED_FOLLOWING`。
`SUM` 省略 `frame` 时默认 `ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW`
（即累计和）。仅支持 `ROWS`（物理行偏移），不支持 RANGE/GROUPS。

> 顶层与对象中允许出现无法识别的额外字段（如 `_comment`），解析时忽略。

### 4.2 响应

成功：

```json
{
  "ok": true,
  "plan": { ... },                 // includePlan=true 时
  "output": { "columns": [...], "rowCount": 8, "rows": [...] }
}
```

失败：

```json
{ "ok": false, "error": { "type": "WINDOW_ERROR", "message": "..." } }
```

---

## 5. 语义约定（验收口径）

### 5.1 排序、并列与确定性

- 输出按「分区键顺序 + 分区内排序键顺序」排列；
- **并列键**（所有 ORDER BY 键相等）时，以**输入原始行号** `sourceIndex`
  作为最终决胜键做**稳定排序**，因此结果完全确定、可复现；
- `ROW_NUMBER()`：分区内从 1 开始的连续编号，**永不重复**，并列键按输入次序区分；
- `RANK()`：并列行同名次，之后**跳跃**（如 `1,1,1,4,4,6`）；
  无 `ORDER BY` 时所有行名次为 1。

### 5.2 NULL 顺序

- `NULL` 排序位置由每个排序键的 `nullOrder` 显式决定；
- 缺省遵循 SQL/PostgreSQL：**ASC → NULLS LAST，DESC → NULLS FIRST**；
- 多个 `NULL` 互相并列（参与 RANK 的同名次）；
- **分区键为 NULL 的行归入同一分区**。

### 5.3 ROWS 帧与越界

- 帧端点是相对当前行的**物理行号**；帧在分区内越界时按 SQL 语义
  **截断到分区边界**，不报错；
- 当整个帧都落在分区之外（例如分区最后一行使用
  `BETWEEN 1 FOLLOWING AND 2 FOLLOWING`），帧为空 → 聚合结果为 `null`；
- `SUM` 忽略帧内的 `NULL` 单元；若帧内没有任何非空值，结果为 `null`。

### 5.4 整数与溢出

- 输入数值列仅接受 **JSON 整数且在 64 位有符号范围内**（`LONG`）；
  小数/字符串写入 LONG 列会被拒绝；
- 求和按帧用 `BigInteger` 前缀和计算**数学上的精确和**，再用
  `longValueExact()` 做一次性范围检查：只要窗口内非空整数之和超出
  `[-2^63, 2^63-1]` 即抛错，**绝不静默回绕**；
- 因此「中间累加会溢出、但窗口最终和合法」的**正负抵消**场景不会误报
  （例如 `MIN_VALUE + MAX_VALUE = -1`）；
- 另有逐操作的 `CheckedLong.addExact`（`src/engine/window/CheckedLong.java`），
  带独立溢出判定与单测，用于交叉验证；
- 溢出错误携带函数、帧定义、源行号、分区内位置与数学和，便于定位。

---

## 6. 请求样例

| 文件 | 覆盖点 |
|---|---|
| `examples/01_basic_partitioned.json` | 多分区、DESC、并列 RANK、累计和、居中三行滑动和、NULL、负值 |
| `examples/02_ties_and_nulls.json` | 并列键、RANK 跳跃、ASC 默认 NULLS LAST、单行分区 |
| `examples/03_frame_bounds.json` | 全部分区帧 / 回溯帧 / 前向帧，越界截断与空帧 |
| `examples/04_overflow_error.json` | 正向溢出，预期 `ok:false` / `WINDOW_ERROR` / 退出码 1 |
| `examples/05_negatives_single_partition.json` | 负值、单行分区、帧截断 |
| `examples/06_plan_only_empty.json` | 0 行输入、仅导出执行计划 |

每个样例对应的实际响应保存在 `out/exports/*.response.json`（可删除后重新生成）。

---

## 7. 测试与对拍策略

不使用 JUnit，自带零依赖断言框架，`./run_tests.sh` 一键运行四个套件：

1. **`JsonTest`**（26 项）：JSON 词法/语法、转义与代理对、往返、错误输入；
2. **`WindowEngineTest`**（62 项）：
   - 并列键决胜、RANK 跳跃、无 ORDER BY；
   - ASC/DESC 与三种 NULL 位置组合、NULL 参与分区；
   - 累计帧、居中滑动帧、前向帧的**越界截断/空帧**、SUM 忽略 NULL；
   - **单行分区**、**负值**、`MIN_VALUE/MAX_VALUE` 边界；
   - **正向溢出 / 负向下溢**被检出、抵消场景不误报；
   - 规格校验（未知列、重名列、SUM 字符串列、非法帧、负偏移）；
   - `CheckedLong.addExact` 的溢出单测；
3. **`DifferentialTest`**：
   - **300 组**固定随机种子的随机数据 + 随机窗口规格（小取值域制造大量并列与
     NULL、0~30 行覆盖空关系/单行/小分区、多列排序、多函数、各类 ROWS 帧），
     将生产引擎结果与 `NaiveWindowReference` **逐行规范化后对拍**；
   - **120 组**极端大值（含 `±MAX_VALUE`、成对抵消）对拍：验证两个实现在
     **“是否溢出抛错”**与成功时逐行结果上完全一致；
4. **`ExecutorTest`**（34 项）：端到端 JSON 请求、计划/数据导出开关、
   默认 NULL 顺序、默认帧、各类错误分类与退出语义。

### 朴素参考实现（对拍基准）

`test/testutil/NaiveWindowReference.java` 刻意与生产引擎**不同构**：

- 用 `HashMap` 按分区键值分组（生产引擎用一次全局稳定排序）；
- `SUM` 对每一行用**双重 for 循环**在帧内逐行累加（生产引擎用前缀和 O(n)）；
- `RANK` 从头数「排序键严格小于当前行」的行数 +1（生产引擎按相邻键变化跳跃）。

两条独立路径得到相同结果，为核心运算的正确性提供交叉验证。

---

## 8. 实际运行记录（本仓库开发环境）

环境：Ubuntu，OpenJDK `17.0.20`（`JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64`）。

编译与测试（命令与输出如实记录）：

```
$ ./run_tests.sh
>> 编译主源码...
>> 编译测试...
>> 编译完成：out/classes, out/test-classes
>> 运行测试...
[PASS] JSON 层（26 项断言）
[PASS] 窗口引擎功能（62 项断言）
[PASS] 差分对拍（引擎 vs 朴素参考）（7 项断言）
[PASS] JSON 请求入口（34 项断言）

全部测试套件通过。
```

差分对拍规模：300 组随机规格全部逐行一致；120 组极端值对拍中
**57 组两个实现均判定溢出、63 组均成功，0 组分歧**。

样例运行：

```
$ bin/run.sh examples/01_basic_partitioned.json     # 退出码 0
$ bin/run.sh examples/04_overflow_error.json        # 退出码 1（预期）
```

溢出样例响应（节选）：

```json
{
  "ok": false,
  "error": {
    "type": "WINDOW_ERROR",
    "message": "窗口求和溢出 long 范围：函数 SUM(v) AS s，帧 ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW，源行号 1，分区内第 2 行（共 2 行），帧内非空整数和 = 9223372036854775808"
  }
}
```

**未通过项：无。** 全部四个测试套件通过；样例 `04` 以退出码 1 结束是
被测试/设计明确预期的溢出错误行为，不是缺陷。
