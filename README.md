# Restricted GraphQL Execution Backend

一个**受限 GraphQL 子集**的执行后端，从零实现契约解析、执行内核、状态适配与诊断接口。
技术栈：TypeScript · Node.js（内置 `node:sqlite`）· Fastify。全部数据为本地确定性合成夹具，
无需任何外部账号、云服务或真实业务数据。

- 支持：对象、列表（含嵌套/非空）、别名、命名片段、内联片段、变量、`@skip`/`@include`、query/mutation
- 重点保证：执行前变量类型校验、片段循环检测、字段按响应键合并的兼容性判定、
  非空失败沿**类型树**冒泡、query 并发 / mutation 顶层按序、错误路径与部分数据
- 错误按**类别**区分 HTTP 状态，而不是统一 500
- 每个请求留下带**请求标识**的结构化决策记录，敏感信息只记录脱敏形态

---

## 1. 目录结构

```
src/
  graphql/            # 契约解析 + 执行内核（不认识 HTTP，也不直接碰 SQLite）
    ast.ts            #   受限子集 AST
    lexer.ts          #   词法
    parser.ts         #   语法
    types.ts          #   内部类型表示（NAMED/LIST + nonNull）
    coercion.ts       #   变量/实参/标量输出强制
    schema.ts         #   类型结构声明 + 解析器注册表（makeSchema/buildSchema）
    collect.ts        #   片段展开、@skip/@include、按响应键合并字段
    validator.ts      #   执行前校验（片段循环、合并冲突、变量/参数/指令）
    executor.ts       #   执行内核（并发/顺序、非空冒泡、错误路径、部分数据）
    errors.ts         #   统一错误模型与类别
    index.ts          #   内核公共出口
  data/               # 状态适配
    db.ts             #   SQLite 连接 + 迁移（node:sqlite）
    fixtures.ts       #   确定性合成夹具（含 p-fp-* 故障夹具）
    repository.ts     #   仓储：内核与 SQLite 的唯一边界
  diagnostics/        # 诊断
    redact.ts         #   脱敏（键名规则 + 循环引用安全）
    logger.ts         #   有界环形缓冲 + 可选落盘
  http/               # Fastify 适配
    gqlHandler.ts         # /graphql：信封、错误类别->HTTP、诊断装配
    diagnosticRoutes.ts   # /diagnostics 查询
  resolvers.ts        # 解析器装配（仓储行 -> GraphQL 字段，植入故障夹具）
  app.ts              # 依赖装配
  server.ts           # 进程入口
  config.ts           # 环境配置（均有本地默认值）
  scripts/seed.ts     # 本地播种脚本
tests/
  unit/               # 内核/词法/强制/脱敏的独立单元测试
  integration/        # 真实 SQLite + 夹具 + 完整 HTTP 装配
  http/               # 信封、状态码、诊断接口
  fixtures/           # 测试辅助
examples/             # 可直接 curl 的示例请求
```

模块各有真实职责：**解析器**只产出 AST；**校验器**决定能否执行；**执行内核**负责调度与冒泡；
**仓储**是唯一接触 SQL 的地方；**诊断**独立记录决策。不是单文件脚本，也不是只有接口的空工程。

---

## 2. 本地启动

要求 Node.js ≥ 22.5（使用内置 `node:sqlite`，无需原生编译）。

```bash
npm install        # 已提交 package-lock.json，安装即锁定
npm run build      # tsc -> dist/
npm start          # 打开/迁移 data/app.db，首次自动播种，监听 127.0.0.1:8787
```

开发模式（免构建，文件改动自动重启）：

```bash
npm run dev
```

重新播种（删除本地库后重建，仅本地开发）：

```bash
npm run seed
```

### 环境变量（全部可选，均有默认值）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `HOST` | `127.0.0.1` | 监听地址，默认仅本机 |
| `PORT` | `8787` | 监听端口 |
| `DB_PATH` | `data/app.db` | SQLite 文件；`:memory:` 为内存库 |
| `DATA_DIR` | `./data` | 数据目录（`DB_PATH` 未指定时使用） |
| `LOG_FILE` | 空 | 设置后诊断记录额外以 JSONL 落盘 |
| `DIAG_RING_SIZE` | `200` | 诊断环形缓冲容量 |
| `LOG_LEVEL` | `info` | Fastify 日志级别 |

---

## 3. 示例请求

服务启动后：

```bash
# 基础：对象/列表/别名/片段/变量
curl -s -X POST http://127.0.0.1:8787/graphql \
  -H 'content-type: application/json' \
  --data @examples/01-basic.json | python3 -m json.tool

# 非空冒泡（错误路径 post.related.1，兄弟根 other 保留）
curl -s -X POST http://127.0.0.1:8787/graphql \
  -H 'content-type: application/json' \
  --data @examples/03-non-null-element.json

# 片段循环 -> 400 FRAGMENT_CYCLE
curl -s -X POST http://127.0.0.1:8787/graphql \
  -H 'content-type: application/json' \
  --data @examples/04-fragment-cycle.json

# mutation 顶层按序
curl -s -X POST http://127.0.0.1:8787/graphql \
  -H 'content-type: application/json' \
  --data @examples/05-mutation-order.json
```

也支持 GET（`variables` 需为 JSON 字符串）：

```bash
curl -s 'http://127.0.0.1:8787/graphql?query=query%20Q(%24id%3AID!)%7Bpost(id%3A%24id)%7Bid%7D%7D&variables=%7B%22id%22%3A%22p-01%22%7D'
```

诊断接口：

```bash
curl -s http://127.0.0.1:8787/diagnostics?limit=20        # 最近决策记录
curl -s http://127.0.0.1:8787/diagnostics/<requestId>     # 按响应头 x-request-id 关联
curl -s http://127.0.0.1:8787/health
```

---

## 4. 支持范围与关键取舍

### 4.1 支持的语法

- 操作：`query`、`mutation`；支持操作名、多操作 + `operationName` 选择、匿名操作
- 选择：字段、别名、嵌套选择集、命名片段（`fragment F on T`）、内联片段（`... on T {}` / `... {}`）
- 变量：变量定义、默认值、变量实参；类型 `T` / `T!` / `[T]` / 任意嵌套
- 字面量：Int、String、Boolean、null、Enum、List（本工程刻意不引入 Object 输入值）
- 指令：仅 `@skip(if:)`、`@include(if:)`（实参可为布尔字面量或布尔变量）
- 标量：`Int` `String` `ID` `Boolean` `DateTime`；枚举 `PostStatus`

### 4.2 非空冒泡（核心语义）

冒泡规则集中在 `executor.completeValue`，逐位置按声明可空性判定：

- 标量/枚举字段失败（解析器抛错、返回 null、输出强制失败）：
  字段可空则**停在字段**（值为 null，兄弟字段保留）；字段非空则**向上冒泡**
- 对象：任一非空子字段冒泡 → 整个对象置 null；对象自身非空则继续向上
- **列表元素非空**（`[T!]`）：某元素失败 → **整个列表置 null**；列表可空则停在列表，
  列表非空（`[T!]!`）则继续冒泡
- **列表本身非空**（`[T]!` / `[T!]!`）：解析器对列表位置返回 null → 在**字段位置**直接冒泡
- **可空元素**（`[T]!`）：元素内部失败只令**该元素为 null**，列表与其它元素保留
- 冒泡到根仍未被可空位置吸收 → `data: null`；每个原点错误都带 `path` 与 `locations`

错误 `path` 对列表使用数字索引，例如 `post.related.1` 或 `post.comments.1.body`。

### 4.3 并发与顺序

- **query**：顶层字段 `Promise.all` 并发；对象内部字段同样并发
- **mutation**：顶层字段严格按文档顺序逐个 `await`；字段内部仍并发
- 顺序用 `recordPulse(label, delayMs)` 自增序号夹具验证（延迟逆序时编号仍按文档序递增）

### 4.4 执行前校验（任一失败即拒绝执行，`data: null`）

- 变量：使用前定义、必填存在性、按声明类型强制、变量类型与参数位置兼容
- 片段：重名、目标类型存在且为对象、展开目标存在（`FRAGMENT_NOT_FOUND`）、
  三色 DFS 检测直接/间接环（`FRAGMENT_CYCLE`，错误信息含环路径）
- 字段合并：同**响应键**的字段必须——同底层字段名、同名同值实参、同为叶或同为对象；
  否则 `FIELD_CONFLICT`
- 字段/参数：字段存在、叶/对象选择集规则、未知/必填参数、字面量类型
- 指令：仅 `@skip`/`@include`，必须带布尔 `if`，不可重复

### 4.5 刻意的取舍（受限边界）

- **不实现**接口/联合/抽象类型，因此类型条件只做对象类型相等判定
- 不实现 subscription、`@defer`/`@stream`、自定义指令、schema introspection（`__schema`/`__typename`）
- 不实现 Object 输入值与 Float（夹具与业务不需要；`Int` 做安全整数校验）
- 解析器异常被捕获并归类为 `RESOLVER_FAILURE`；非 GraphQLError 的未知异常不吞，
  在内核边界转 `INTERNAL`（500），表示这是实现缺陷而非可预期业务失败
- 不引入第三方 GraphQL 库：解析、校验、执行均为本工程实现，便于精确控制冒泡与错误类别；
  唯一运行时依赖是 Fastify，SQLite 使用 Node 内置模块

---

## 5. 错误类别与 HTTP 状态

| 类别 | 含义 | HTTP |
| --- | --- | --- |
| `SYNTAX` | 词法/语法错误 | 400 |
| `VALIDATION` / `FIELD_NOT_FOUND` / `ARGUMENT` / `DIRECTIVE` | 语义校验 | 400 |
| `VARIABLE_TYPE` | 变量缺失/类型不兼容/强制失败 | 400 |
| `FRAGMENT_CYCLE` / `FRAGMENT_NOT_FOUND` | 片段环 / 未定义片段 | 400 |
| `FIELD_CONFLICT` | 同响应键字段不兼容（别名冲突等） | 400 |
| `AMBIGUOUS_OPERATION` / `BAD_REQUEST` | 多操作未指名 / 信封非法 | 400 |
| `UNKNOWN_OPERATION` | 指定的 operationName 不存在 | 404 |
| `RESOLVER_FAILURE` / `NON_NULL_VIOLATION` / `COERCION_FAILURE` | 执行期字段错误 | 200（随部分数据） |
| `INTERNAL` | 内核未预期缺陷 | 500 |

执行期错误始终以 **200 + `errors` + 部分 `data`** 返回（符合 GraphQL over HTTP 的部分数据约定）；
只有执行前拒绝才用 4xx。响应中每个错误形如：

```json
{
  "message": "Cannot return null for non-nullable position at path \"post.related.1\" ...",
  "path": ["post", "related", 1],
  "locations": [{ "line": 1, "column": 31 }],
  "extensions": { "category": "NON_NULL_VIOLATION" }
}
```

### 固定故障夹具（验收用，ID 稳定）

| 夹具 | 行为 |
| --- | --- |
| `p-fp-element` | `related: [Post!]!` 的结果在索引 1 注入 null 元素 |
| `p-fp-listnull` | `archivedComments: [Comment!]!` 解析器直接返回 null |
| `p-fp-throw` | `title!` 解析器抛错；可空字段 `faultyNote` 也抛错；`badCount: Int` 返回字符串 |
| `c-fp-nullbody` | 通过 `comments(includeFault:true)` 让 `body: String!` 返回 null |

---

## 6. 诊断与脱敏

- 每请求一条记录：`requestId`、时间戳、耗时、`decision`（`ACCEPTED`/`REJECTED`/`UNDECIDABLE`）、
  `phase`、机器可读 `reasons`（错误类别集合或 `OK`）、`summary`、操作类型、
  **变量形态**（如 `ids -> "list[1]"`）、字段错误数、HTTP 状态
- “为什么接受/拒绝/无法判定”：接受进入 `execute`；语法/校验/变量失败记 `parse`/`validate`/
  `coerce-variables` 并 `REJECTED`；信封本身无法解析记 `envelope` 并 `UNDECIDABLE`
- 敏感键（email/password/token/secret/apiKey/credential 及 camelCase/分隔符变体）值一律掩码；
  变量只记录类型形态不记录值；诊断输出不含查询正文
- 响应头始终带 `x-request-id`，可用 `/diagnostics/<id>` 精确关联

---

## 7. 测试

```bash
npm test          # node --test 跑 unit + integration + http
npm run typecheck # src 与 tests 一起做严格类型检查
```

- 单元测试中的执行内核用**独立自建的 Box/Item 最小 schema** 与手写期望值，
  不通过生产 schema 的解析器生成“参考答案”
- 集成测试跑真实内存 SQLite 与合成夹具，断言**具体结果、错误路径、失败类别与 HTTP 状态**，
  而非仅“接口能调用”
- 最近一次完整运行结果与开发过程中发现并修复的真实缺陷见 [TEST_REPORT.md](./TEST_REPORT.md)
