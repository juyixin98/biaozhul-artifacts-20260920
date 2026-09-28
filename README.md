# OpenAPI 3.1 子集 · 接口契约差异服务

给定旧、新两份 OpenAPI 3.1 契约，从**客户端请求**与**服务端响应**两个方向
判断演进是否破坏兼容，并为每个破坏性结论给出**最小、具体、可双向验收的见证**
（witness）：

- 请求方向：旧客户端**能发出**的请求，被新服务端**拒绝**；
- 响应方向：新服务端**能返回**的响应，旧客户端**无法消费**。

破坏性判断不是 JSON 字段增删的文本 diff——参数位置、必填性、HTTP 状态码都按
各自的线上语义比较；`$ref` 循环有界解析；`x-*` 未知扩展标保留但**不参与判定**。

---

## 1. 本地验证命令

前置：Node.js ≥ 20.19、Python ≥ 3.10（仅用于**独立参考预言机**，非运行时依赖）。

```bash
npm install

# 1) 类型检查
npm run typecheck

# 2) 全部测试（6 个文件，63 个用例）
npm test

# 3) 离线 CLI：退出码 0 兼容 / 1 有破坏 / 2 解析失败
npm run diff -- examples/old.json examples/new.json

# 4) 启动 HTTP 服务（默认 127.0.0.1:3000，SQLite 落 ./data/diffs.db）
npm start
PORT=3210 DB_PATH=./data/demo.db npm start

# 5) 真实 HTTP 调用
curl -s -X POST http://127.0.0.1:3210/api/v1/diffs \
  -H 'content-type: application/json' \
  -H 'x-request-id: demo-001' \
  -d "{\"old\":$(cat examples/old.json),\"new\":$(cat examples/new.json)}"
curl -s http://127.0.0.1:3210/api/v1/diffs/demo-001   # 按请求 id 回读
```

### 如何判断结果

- 响应体 `data.result.compatible === false` 即存在破坏；
- `findings[]` 中 `severity="BREAKING"` 的每项都带 `witness`：
  - `witness.location`：具体请求位置或 `操作 -> 状态码 body.路径`；
  - `witness.example`：最小具体值（如被删枚举字面量 `"sold"`、`null`、缺参请求、
    新增的 `429`）；
  - `witness.rationale`：两侧为何分歧；
- `uncertainties[]` 与失败原因**分开列出**（循环引用、无法解析的指针、子集外
  关键字等），绝不含糊成"兼容"；
- `steps[]` 是有序处理轨迹；每个响应与日志行都带同一个 `requestId`
  （可用 `x-request-id` 头指定，否则自动生成 UUID）。

---

## 2. 测试为什么能回答具体问题

测试不满足于"接口能调用"。`test/` 中：

| 测试文件 | 断言内容 |
|---|---|
| `matrix.test.ts` | 19 组手写旧/新矩阵（可空收紧/放宽、枚举收窄/扩展、默认值变更/删除、同名异位参数、同名第二位置、必填翻转、新增必填字段、状态码新增、响应字段消失等），逐组断言 `compatible` 与**确切失败码集合** |
| `witnesses.test.ts` | 对每个 BREAKING 结论断言见证存在且值具体（`null`、`"sold"`、`429`…），再交给**独立预言机**对原始契约双向验收 |
| `directionality.test.ts` | 同一结构变化在请求/响应方向结论相反（必填翻转、加字段、枚举扩展） |
| `parser.test.ts` | `$ref` 解析、循环/超深/悬空指针、`x-*` 不参与判定、`oneOf` 标为不确定 |
| `store.test.ts` | SQLite 落盘、见证与不确定性回读、按请求 id 关联 |
| `api.test.ts` | Fastify 注入：信封、关联 id、400/404/422、回读、不确定性单列 |

**参考答案不是被测核心自己生成的。** `test/oracle/contract_oracle.py` 是一份
纯标准库 Python 的独立实现（独立的类型格、枚举/必填/位置/状态码规则、独立的
`$ref` 处理与一个独立的值验收器）。TypeScript 测试经
`test/helpers/oracle-bridge.ts` 用 stdio JSON 调用它：

1. 预言机独立推导"应当出现的 (direction, severity, code, operation, path)
   多元组集合"，与引擎结果逐项比对（`matrix.test.ts`）；
2. 预言机拿引擎产出的每个见证值，回到**原始**旧/新契约上独立判定
   "生产者侧接受 ∧ 消费者侧拒绝"（`witnesses.test.ts`）。伪造或两侧都接受的
   见证会让测试失败。

> 覆盖率数字：未运行 `@vitest/coverage-v8`（未安装该依赖），因此不声称达到
> 80% 数值；核心判定路径（参数、请求体、响应、类型格、解析器边界）均有直接断言。

---

## 3. 算法假设（判定语义）

比较的基本问题是："**生产者侧允许的每个值，消费者侧是否都接受？**"
方向决定谁是生产者：

- 请求：生产者 = 旧契约（旧客户端会发什么），消费者 = 新契约（新服务端收什么）；
- 响应：生产者 = 新契约（新服务端会回什么），消费者 = 旧契约（旧客户端按什么解析）。

因此同一变化在两个方向结论可能相反（见 `directionality.test.ts`）：

| 变化 | 请求方向 | 响应方向 |
|---|---|---|
| 可选 → 必填 | 破坏（旧客户端可能漏发） | 非破坏（服务端总是给，旧方更宽松） |
| 必填 → 可选 | 非破坏 | 破坏（服务端可能省略旧客户端依赖的字段） |
| 新增字段 | 必填才破坏 | 非破坏（旧客户端忽略多余字段） |
| 删除字段 | 非破坏（旧客户端多发被忽略） | 旧方必填则破坏 |
| 枚举新增字面量 | 非破坏 | 破坏（可能回出旧客户端无法映射的值） |
| 枚举收窄 | 破坏 | 非破坏 |
| 新增状态码 | — | 破坏（旧客户端无对应分支）；移除状态码非破坏 |

其他假设：

- **类型格**：`integer` 是 `number` 的子类型；可空性按 3.1 的
  `type:["string","null"]` 数组成员精确处理；`null` 不会被隐式接受；
  `type` 缺省视为"不受限"（接受任意值）。
- **参数身份**为 `` `${in}:${name}` ``：仅当"新位置键在旧侧不存在 **且**
  旧同名键在新侧也消失"才判为 `PARAM_LOCATION_CHANGED`；旧键仍存活（如
  `path:id` 还在、新增 `query:id`）算新增，避免把共存参数误判成搬迁。
- **默认值**变化本身不破坏线上报文（默认值是文档/客户端补全行为），单列
  非破坏元数据结论；"缺省"与 `default: null` 严格区分。
- 仅判定支持子集：`type/enum/default/required/properties/items/format`、
  参数四位置、请求体、显式状态码（`default` 响应与 `x-*` 不判定）；
  `oneOf/anyOf/allOf/not/if/then/else` 等命中即记为 `UNSUPPORTED_KEYWORD`
  不确定性，不猜测。
- 仅处理本地 `#/...` 指针；链长上限 32 跳，循环立即截断。
- 媒体类型只比较 `application/json` 与 `*+json`；不一致或非 JSON 记
  `AMBIGUOUS_MEDIA_TYPE` 并跳过该处比较。
- 见证为"最小"：对象只填满足必填所需的键；优先用 `default`、再用枚举字面量、
  再按类型取样；嵌套深度有界（默认 6）。

---

## 4. 模块关系

```
HTTP/CLI
  │
  ├─ src/diagnostics/app.ts      Fastify 路由、请求 id 关联、结构化日志、信封
  │      │
  ├─ src/kernel/diff-engine.ts   编排：解析旧/新 → 双向比较 → 排序 → 统计
  │      ├─ src/kernel/schema-compare.ts   递归 schema 判定（方向由钩子参数化）
  │      ├─ src/kernel/type-lattice.ts     类型格 / 枚举验收 / JSON 相等
  │      └─ src/kernel/witness.ts          最小具体见证取样
  │
  ├─ src/parser/contract-parser.ts  OpenAPI 3.1 → 规范化契约（有界 $ref、扩展标）
  │
  ├─ src/state/diff-store.ts      SQLite：runs / findings / uncertainties
  │
  ├─ src/core/types.ts            跨模块领域类型与全部失败码词汇表
  └─ src/config.ts                集中配置（端口、DB、跳数上限、报文上限）
```

依赖方向单向向内：内核不导入状态层与 HTTP 层；CLI 与 HTTP 都只依赖内核。
测试位于独立 `test/` 目录，预言机位于独立 `test/oracle/`（Python 运行时）。

### HTTP 接口

| 方法/路径 | 说明 |
|---|---|
| `POST /api/v1/diffs` | 请求体 `{old, new}`，返回完整结论；失败 400/422 |
| `GET  /api/v1/diffs/:id` | 按请求 id 回读已持久化的分析 |
| `GET  /api/v1/runs` | 最近运行列表 |
| `GET  /health` | 存活探针 |

---

## 5. 依赖版本（实际安装）

运行时：

- [fastify](https://www.npmjs.com/package/fastify) **4.29.1** — HTTP/诊断层
- [better-sqlite3](https://www.npmjs.com/package/better-sqlite3) **11.10.0** — 本地 SQLite（同步 API，WAL）

开发时：

- TypeScript **5.9.3**（`strict` + `noUncheckedIndexedAccess` +
  `exactOptionalPropertyTypes`）
- Vitest **2.1.9**、tsx **4.23.15**、`@types/node` **22.x**、
  `@types/better-sqlite3` **7.6.x**

无任何生产账号或外部业务数据；`examples/` 为合成宠物商店契约。

---

## 6. 测试运行状态

最近一次本地运行（Node v22.23.3）：

- `npm run typecheck`：**通过**；
- `npm test`：**6 个文件 / 63 个用例全部通过**；
- 真实 HTTP 端到端（POST 分析 → 按 id 回读）：**已手工验证通过**；
- CLI 退出码（不兼容=1，相同契约=0）：**已验证**。
- 未运行项：覆盖率百分比工具未安装、未执行（见 §2 说明）。
