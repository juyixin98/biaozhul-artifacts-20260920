# Restricted GraphQL Backend

一个**受限子集**的 GraphQL 执行后端，从零实现契约解析（lexer/parser/AST）、执行内核（非空冒泡、查询并发 / mutation 串行）、状态适配（SQLite + 合成夹具）与诊断接口。不依赖 `graphql-js` 等参考实现，核心语义按 GraphQL 规范（§6.6 Executing Operations）独立实现并以**手写固定样例**对照核验。

技术栈：**TypeScript（ESM, strict）· Node.js ≥20 · Fastify 5 · better-sqlite3 · Vitest**。

---

## 1. 目录结构

```
.
├── src/
│   ├── graphql/                # ① 契约解析 + ② 执行内核
│   │   ├── ast.ts              # AST 节点类型
│   │   ├── lexer.ts            # 词法分析（含 block string、转义）
│   │   ├── parser.ts           # 递归下降语法分析
│   │   ├── schema.ts           # 极简类型系统（SCALAR/ENUM/OBJECT + !/[]）
│   │   ├── collect.ts          # 字段收集（片段展开）+ 片段循环染色检测
│   │   ├── validate.ts         # 执行前校验（变量类型、合并冲突等）
│   │   ├── values.ts           # 变量/实参强制转换
│   │   ├── execute.ts          # ★ 执行内核：非空冒泡、列表、并发/串行
│   │   ├── error.ts            # GraphQLError / NonNullViolation / 类别
│   │   └── run.ts              # 编排：parse→选操作→validate→转换→execute
│   ├── state/                  # ③ 状态适配
│   │   ├── db.ts               # SQLite 连接 / DDL / 播种 / 行映射
│   │   └── schema.ts           # 业务类型 + resolver 映射 + 失败夹具
│   ├── diagnostics/            # ④ 诊断接口
│   │   ├── redact.ts           # 邮箱/敏感键脱敏
│   │   └── collector.ts        # 请求标识、决策记录、原因码
│   ├── server/
│   │   ├── config.ts           # 环境变量配置
│   │   └── app.ts              # Fastify：/graphql /healthz /diagnostics
│   └── main.ts                 # 启动入口
├── fixtures/
│   ├── seed-data.json          # 合成业务数据（含失败标记）
│   └── spec-cases.json         # ★ 手写固定规范答案（golden answers）
├── tests/
│   ├── golden.spec-cases.test.ts  # 逐条对照手写答案
│   ├── unit/                      # 解析/校验/转换/冒泡/顺序/脱敏
│   ├── integration/               # HTTP + 诊断
│   └── helpers/harness.ts         # 每用例独立内存库
└── package.json
```

---

## 2. 本地启动

```bash
npm install        # 已提交 package-lock.json，安装可复现
npm run build      # tsc -> dist/
npm start          # 等价于 node dist/main.js
```

开发模式（免构建、文件监听）：

```bash
npm run dev        # tsx watch src/main.ts
```

启动后监听 `http://127.0.0.1:4000`，首次启动自动建表并把 `fixtures/seed-data.json` 播种到 `data/app.db`。

环境变量（均有默认值）：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `HOST` | `127.0.0.1` | 监听地址 |
| `PORT` | `4000` | 监听端口 |
| `DATABASE_FILE` | `data/app.db` | SQLite 文件（测试用 `:memory:`） |
| `LOG_REQUESTS` | `true` | 是否向 stderr 输出结构化诊断行 |

---

## 3. 示例请求

见 [`examples/requests.http`](examples/requests.http)（可直接用 curl 或 REST Client 执行）。

```bash
# 成功查询
curl -s -X POST localhost:4000/graphql -H 'content-type: application/json' \
  -d '{"query":"{ users(limit: 2) { id name tags posts { id title } } }"}'

# 变量 + 片段
curl -s -X POST localhost:4000/graphql -H 'content-type: application/json' -d '{
  "query": "query($id: ID!){ user(id:$id){ ...U } } fragment U on User { id name }",
  "variables": { "id": "u1" }
}'

# mutation（顶层字段按序执行）
curl -s -X POST localhost:4000/graphql -H 'content-type: application/json' \
  -d '{"query":"mutation { a: bump { value } b: bump { value } }"}'
```

健康检查与诊断：

```bash
curl localhost:4000/healthz
curl localhost:4000/diagnostics            # 最近请求（倒序，?limit=N）
curl 'localhost:4000/diagnostics?requestId=<x-request-id>'
```

每个 `/graphql` 响应头都带 `x-request-id`，可用它在 `/diagnostics` 取该请求的决策记录。

---

## 4. 支持范围

**语法**

- `query` / `mutation`（匿名或具名；`subscription` 明确拒绝）；多操作文档用 `operationName` 选择。
- 字段、**别名**、参数、**变量定义与默认值**、嵌套选择集。
- **具名片段** `fragment F on T`、**内联片段** `... on T` / `... { }`、片段展开。
- 全部输入字面量：Int/Float/String（含 block string 与转义）/Boolean/Null/Enum/List/InputObject、变量引用。
- 类型：`Int Float String Boolean ID` 内建标量 + 自定义标量（`Tag`）+ 枚举；`T`、`T!`、`[T]`、任意嵌套与非空组合。
- 指令：能解析（避免语法报错），但**不执行**（见取舍）。`__typename` 元字段支持。

**执行语义**

- 字段按**响应键**（`alias ?? name`）分组合并。
- **query** 同一选择集内字段并发解析；**mutation 顶层字段严格按文档顺序逐个 `await`**，其嵌套对象内部恢复并发。
- **非空冒泡**：字段/元素返回 null 或 resolver 抛错命中 `!` 边界时，错误沿**类型树**上抛至最近的可空边界落入 `errors`（原始错误只记录一次），到达根则 `data: null`。
- **列表元素非空（`[T!]`）与列表本身非空（`[T]!`）严格区分**；错误 `path` 携带响应键与元素下标（支持嵌套列表多下标）。
- 返回规范的部分数据：`{ data, errors }`，错误带 `path` / `locations` / `extensions.category`。

**错误类别**（`extensions.category`，测试据此断言而非看 HTTP 状态码）：

`PARSE` · `VALIDATION` · `COERCION` · `RESOLVER` · `INTERNAL`

**执行前校验**

- 变量：唯一定义、必须为输入类型、使用必须已声明、声明类型在参数位置可用（非空变量可用于可空位，反之不可）、执行前按类型强制转换（缺失/为 null/标量拒绝/越界 Int 等）。
- 片段：目标类型存在且为对象、展开目标存在、**循环检测（WHITE/GRAY/BLACK DFS，报告完整展开链）**、类型条件不重叠时安全跳过。
- 字段合并：同一响应键的字段必须**同名且参数一致**，否则拒绝；子选择合并后递归检查。
- 结构：未知字段/参数、leaf 字段带选择集、对象字段缺选择集等。

---

## 5. 关键取舍（明确说明）

1. **HTTP 状态码**：只要请求被处理，解析/校验/执行错误一律 **200**，错误在响应体 `errors` 中表达（GraphQL-over-HTTP 的部分数据语义）；只有请求体缺 `query` 这类**传输层**格式错误返回 404/400。这正是“核验错误冒泡位置而非统一 500”的落点。
2. **不实现 interface / union**：因此片段的类型判定简化为“类型条件名 == 当前对象类型名”。`... on DifferentType` 会被安全跳过。
3. **指令仅解析不执行**：`@skip` / `@include` 不改变执行结果；写指令不会导致语法错误，但也不生效。
4. **无 DataLoader / 查询批处理**：N+1 不做合并（YAGNI）；resolver 直接走 better-sqlite3 同步 API，但包在 `async` 执行模型中，并发语义由内核控制。
5. **自定义标量 `Tag`**：值为 `"__THROW__"` 时序列化抛错，作为确定性的合成失败夹具驱动非空冒泡，不依赖任何外部服务。
6. **解析/校验失败不带 `data` 字段**（规范允许，与 `data: null` 区分“未执行”与“执行但根冒泡为 null”）。
7. **诊断内存环形缓冲**（默认 200 条）非持久化，仅用于本地观察；变量只记录**类型形状**，`password/token/secret/email` 等键与正文中的邮箱一律脱敏。

---

## 6. 测试

```bash
npm test                 # vitest run（87 个用例）
npm run coverage         # v8 覆盖率
npm run typecheck        # 含测试目录的 tsc 校验
```

测试金字塔：

- **golden 固定样例**（`fixtures/spec-cases.json`，21 条）：参考答案**人工按规范推导、独立于被测核心**；逐条深比较完整 `data`、`error.path`、`extensions.category` 与决策原因。覆盖嵌套非空列表（`[T!]!` / `[T!]` / `[[T!]!]!` / 外层可空）、别名冲突、同键异参、片段循环、变量类型/缺失/null/位置不兼容、解析器部分数据、mutation 串行、query 并发等。
- **内核单测**：用测试内手工构造的最小 schema/resolver 验证冒泡在对象树与列表上的精确停止位置、并发/串行时序，期望值不依赖业务夹具。
- **解析/校验/转换单测**：PARSE 类别、循环染色、响应键冲突、标量/列表强制转换与 `[index]` 路径。
- **HTTP 集成**：部分数据 200、`x-request-id` 联动、400 请求体、诊断脱敏。

覆盖率（v8，已排除纯类型声明 `ast.ts` 与启动入口 `main.ts`）：语句 **83.8%** / 分支 **80.8%** / 函数 **91.8%**。

### 验收夹具如何对应到需求

| 需求 | 夹具/字段 | 期望 |
|------|-----------|------|
| 嵌套非空列表冒泡 | `strictTags: [Tag!]!`（含 `__THROW__`） | `data=null`, path `strictTags.2` |
| 元素非空但列表可空 | `looseTags: [Tag!]` | `{looseTags:null}`, path `looseTags.1` |
| 嵌套两层列表 | `nestedTags: [[Tag!]!]!` | `data=null`, path `nestedTags.1.1` |
| 外层可空的嵌套列表 | `nestedLoose: [[Tag!]!]` | `{nestedLoose:null}` |
| 别名冲突 | `{ id id: name }` | VALIDATION，无 data |
| 片段循环 | `A→B→A` | VALIDATION，消息含展开链 |
| 解析器失败 + 部分数据 | `Post.title`（p4 失败，可空） | 仅 `posts.3.title=null` |
| 对象树冒泡 | `User.pet: Pet` 的 `name!` 失败（u3） | `pet=null`，兄弟 `id` 保留 |
| mutation 顶层串行 | `a:bump b:bump` | value 分别 1、2 |
| query 并发 | `a/b: snapshotCounter` | 两者读到同一快照 |

---

## 7. 失败 / 未执行项

见 [`TEST_RUN.md`](TEST_RUN.md)，记录最近一次真实 `npm test` / `npm run coverage` / 冒烟结果，以及明确不在本工程范围内的事项。
