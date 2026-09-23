# 三值逻辑列批次查询执行器（Java / 纯 JDK）

纯后端服务：对列式批次数据执行受限 SQL 查询，严格实现 **SQL 三值逻辑**
（TRUE / FALSE / UNKNOWN）、静态类型检查、`?` 参数绑定，以及基于 **NULL 位图**
的向量化执行。HTTP 层使用 JDK 自带 `com.sun.net.httpserver.HttpServer`，
JSON 解析/序列化为自写极简实现，**核心逻辑不借助任何外部 SQL 引擎**，
运行期与编译期均**零第三方依赖**。

## 1. 依赖（已锁定）

| 依赖 | 锁定版本 | 说明 |
| --- | --- | --- |
| JDK | **OpenJDK 17**（开发/实测：Temurin 17.0.20.1+1） | 用到 sealed types、record、switch 模式匹配，需要 17+ |
| 第三方库 | **无** | 不引用 Maven Central 上的任何构件；无 `pom.xml`/`build.gradle` |
| 构建工具 | 仅 `javac` | `build.sh` 直接调用，无需 Maven/Gradle |

> 因为没有任何外部构件，"锁定依赖"即锁定 JDK 大版本（17）。
> 验证环境下载地址（实际使用的二进制）：
> `https://api.adoptium.net/v3/binary/latest/17/ga/linux/x64/jdk/hotspot/normal/eclipse`
> （Temurin 17.0.20.1+1，sha 以 Adoptium 发布页为准）。

## 2. 目录结构

```
src/com/example/tvl/
  sql/        词法、递归下降解析器、AST、DataType
  engine/     三值逻辑、模式、参数绑定、列式批次、计划、向量执行器、逐行参考解释器、服务编排
  json/       零依赖 JSON 解析/序列化
  http/       JDK HttpServer 入口
test/com/example/tvl/tests/
              迷你测试框架 + 五个测试套件（51 个用例）
examples/     HTTP 请求样例（正常/括号优先级/两类类型错误）
build.sh run.sh test.sh
```

## 3. 构建与启动

```bash
# 指定 JDK 17（若 java/javac 已是 17 可跳过）
export JAVA_HOME="$HOME/.local/jdk-17"
export PATH="$JAVA_HOME/bin:$PATH"

./build.sh           # 编译到 out/classes 与 out/test-classes
PORT=8080 ./run.sh   # 启动 HTTP 服务（默认 8080；-Dserver.port 同效）
```

启动后：

- `POST http://localhost:8080/query` —— 执行查询
- `GET  http://localhost:8080/` —— 接口说明与内置请求样例
- `GET  http://localhost:8080/health` —— 健康检查

## 4. 运行自动化测试

```bash
./test.sh
```

五个套件共 **51 个用例**：

1. **三值逻辑穷举**：独立实现的 Kleene 真值表，枚举 NOT(3)、AND(3×3)、OR(3×3)、
   三原子混合 3³=27 组合与括号变体、NOT/德摩根 9 组合，
   **逐行解释器结果与向量结果逐组合对照**；另含 IS NULL 与比较 NULL 传播矩阵。
2. **类型检查/参数绑定（19）**：字符串↔数值混转在列比较、字面量、参数、数据写入
   各路径被拒绝；参数数量不符、INTEGER 绑定小数、TEXT 绑定数字、非法类型等。
3. **空批次/跨批次（5）**：0 行批次、无 WHERE 空批次、同一计划跨多个批次互不污染、
   批次顺序交换、40 组随机数据（>500 行）下向量与逐行解释器逐行对照。
4. **解析器（15）**：AND 优先于 OR 的 AST 结构、括号改变优先级、嵌套 NOT、
   IS [NOT] NULL、参数编号、错误输入。
5. **HTTP 端到端（5）**：真实起服，验证 200 响应内容、400 错误类别、健康检查。

## 5. HTTP 接口

### 请求 `POST /query`（`application/json`）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `sql` | string | 必填。`SELECT ('*' \| 列(,列)*) FROM 表 [WHERE 表达式]` |
| `schema.columns` | 数组 | `{name,type}`，`type ∈ INTEGER \| FLOAT \| TEXT`，列名大小写不敏感 |
| `params` | 数组 | 可选。按 `?` 出现顺序绑定，元素 `{type,value}`；`value: null` 表示该类型的 NULL |
| `batches` | 数组 | 可选。元素 `{rows:[[...]]}`，每行按 schema 顺序排列；允许空批次 `{"rows":[]}` |
| `includeTvl` | bool | 可选，默认 true。返回每行 `TRUE/FALSE/UNKNOWN` 诊断数组 |

表达式文法：`AND` 优先级高于 `OR`，括号可改变结合；支持 `NOT`、
比较符 `= <> < <= > >=`、`IS [NOT] NULL`、`?` 参数占位、整数/浮点/字符串/NULL 字面量。

### 响应

成功（200）：

```json
{
  "ok": true,
  "batchCount": 2,
  "totalSelected": 2,
  "results": [
    {
      "rowCount": 3, "selected": 1,
      "trueRows": 1, "falseRows": 1, "unknownRows": 1,
      "rows": [[1, "alice"]],
      "tvl": ["TRUE", "UNKNOWN", "FALSE"]
    }
  ]
}
```

错误（400）：`{"ok": false, "errorKind": "...", "error": "..."}`，
`errorKind ∈ JSON_ERROR | SQL_PARSE_ERROR | SEMANTIC_ERROR | BAD_REQUEST`。

### 请求样例

```bash
curl -s localhost:8080/query -H 'Content-Type: application/json' \
  --data @examples/basic.json
curl -s localhost:8080/query -H 'Content-Type: application/json' \
  --data @examples/parentheses.json
# 参数把字符串 "8.0" 声明为 FLOAT → 400 SEMANTIC_ERROR
curl -s localhost:8080/query -H 'Content-Type: application/json' \
  --data @examples/bad-param-type.json
# TEXT 列与整数比较 → 400 SEMANTIC_ERROR
curl -s localhost:8080/query -H 'Content-Type: application/json' \
  --data @examples/bad-type-mix.json
```

## 6. 三值逻辑语义

- 比较：任一操作数为 NULL → **UNKNOWN**；NULL 之间相等比较也是 UNKNOWN
  （不是 TRUE）。
- `x IS NULL`：结果恒为 TRUE/FALSE，绝不产生 UNKNOWN。
- `NOT UNKNOWN = UNKNOWN`；AND 遇 FALSE 即 FALSE，否则遇 UNKNOWN 为 UNKNOWN；
  OR 遇 TRUE 即 TRUE，否则遇 UNKNOWN 为 UNKNOWN。
- WHERE 只保留 **TRUE** 行；FALSE 与 UNKNOWN 都被排除。

类型规则（拒绝隐式混转）：

- INTEGER 与 INTEGER 按 `long` 精确比较；INTEGER 与 FLOAT 提升为 `double`；
  TEXT 与 TEXT 按字典序。
- TEXT 与数值之间的比较、赋值、参数绑定一律报错，不做 `"3" = 3` 这类隐式转换。
- 裸 `NULL` 关键字无类型：`col = NULL` 合法但对所有行恒为 UNKNOWN。

## 7. 向量执行实现要点

- 列数据存于原生数组 `long[] / double[] / String[]`，每列独立 `boolean[]`
  NULL 位图；比较循环无装箱。
- 每个表达式产出一个 `byte[]` 三值编码（TRUE=1/FALSE=0/UNKNOWN=2），
  AND/OR/NOT 按行合并编码。
- `QueryService` 在每次请求时把 SQL 编译一次，同一计划复用于该请求的全部批次。
- 生产路径（`VectorExecutor`）与参考路径（`RowInterpreter`，逐行直白解释）
  在服务内逐行互校；不一致直接抛内部错误。测试中另有独立真值表与随机模糊对照。

## 8. 实测结果与未完成项

见同目录 `RUN_NOTES.md`（如实记录构建、测试与 curl 示例的真实输出）。

已知边界/未做：

- 表名仅做语法消费，不维护目录/不校验表是否存在。
- 不支持 JOIN、GROUP BY、ORDER BY、聚合、算术表达式、IN/LIKE/BETWEEN、布尔列。
- 单进程内存执行，无持久化、无鉴权；线程池固定 8 个守护线程。
- JSON 解析器只覆盖本接口所需类型，不保证对任意 JSON 扩展（注释等）容错。
