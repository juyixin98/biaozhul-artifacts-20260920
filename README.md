# 资源版本 API（强/弱 ETag 与条件请求）

一个本地自包含的资源版本化服务：通过 **ETag** 与 **If-Match / If-None-Match /
If-Modified-Since / If-Unmodified-Since** 实现乐观并发控制，支持响应丢失后的
**幂等重放**。技术栈：TypeScript · Node.js · Fastify · SQLite（better-sqlite3）。

所有数据均为本地合成夹具，无任何生产账号或真实业务数据依赖。

---

## 1. 它保证什么

- **条件校验与写入在同一事务内**：每个 PUT/DELETE 在一个 `BEGIN IMMEDIATE`
  事务中读取当前快照、判定条件、执行写入。并发写者在写锁上串行化，后到者基于
  **已提交的新状态**重新判定，因此**不可能发生丢失更新**。
- **比较模式按请求方法区分**（RFC 9110 §13.1.1）：
  - `If-Match` 对 **PUT/DELETE 用强比较**（弱校验子 `W/` 不能授权字节级覆盖），
    对 GET/HEAD 用弱比较；
  - `If-None-Match` 始终用弱比较。
- **条件优先级固定**（RFC 9110 §13.2.2）：`If-Match` → `If-Unmodified-Since` →
  `If-None-Match` → `If-Modified-Since`。前一步命中失败即短路；If-Match 成功时
  跳过其余校验；If-None-Match 存在时 If-Modified-Since 被忽略。
- **不存在资源与版本不匹配是不同语义**：无条件读取不存在资源 → `404 not-found`；
  针对不存在资源的 `If-Match` 失败 → `412 if-match-mismatch`。
- **响应体与 ETag 来自同一提交快照**：写入提交后，响应载荷在自动提交模式下
  **重新读取**，并校验其版本/ETag 与写入记录一致后才返回。
- **异常/未知状态不会被当作成功**：畸形条件头返回明确分类的 `400`，锁竞争返回
  `503`，幂等键复用不同请求体返回 `409`，传输层 4xx 不被改写成 500。

---

## 2. 工程结构（分层，非单文件 / 非调用壳 / 非固定返回）

```
src/
  config.ts                 配置层：环境变量解析 + 启动期校验
  contract/                 契约解析层（纯函数，无 IO）
    model.ts                领域类型、失败类别、可追踪判定步骤
    etag.ts                 ETag 语法解析 / 强弱比较 / 强校验子生成
    httpDate.ts             IMF-fixdate（及两种废弃格式）解析与序列化
    methodPolicy.ts         方法 → 比较模式策略
    preconditions.ts        条件求值器（固定优先级，纯函数）
  kernel/
    kernel.ts               执行内核：事务编排、幂等重放、结果→状态码
  state/
    store.ts                状态端口（ResourceStore / StoreTransaction）
    sqliteStore.ts          SQLite 适配器（WAL，BEGIN IMMEDIATE）
  http/
    app.ts                  Fastify 传输适配 + 诊断接口
  server.ts                 启动装配
scripts/
  replay.ts                 JSON 夹具重放运行器（真实 HTTP）
fixtures/replay/*.json      手写夹具：弱标签 / 通配匹配 / 响应丢失
test/
  contract/                 契约单测 + 对独立预言机的交叉校验
  kernel/                   内核测试（版本、语义、快照、幂等）
  state/                    worker_threads 真实并发测试
  http/                     Fastify 端到端 + 夹具重放测试
  oracle/                   独立参考预言机（不 import 任何 src 代码）
  helpers/                  测试夹具工厂、结构化日志、并发 worker
  fixtures.ts               静态合成夹具（哈希由 coreutils 独立生成）
```

**为什么参考答案不由被测核心自己生成**：`test/oracle/preconditionOracle.ts`
是依据 RFC 文本另写的第二份判定实现，不共享 `src/` 任何代码；28 个场景同时跑
被测求值器与预言机并断言一致。夹具里的期望 ETag 哈希用系统外的
`printf '%s' <body> | sha256sum` 独立生成（见 `test/fixtures.ts` 注释）。

---

## 3. 配置与依赖版本

需要 **Node.js ≥ 20**（开发验证于 v22.23.3）、npm 10。

| 依赖 | 版本 | 作用 |
|---|---|---|
| fastify | ^5.12.5 | HTTP 框架 |
| better-sqlite3 | ^13.0.3 | 同步 SQLite（WAL，原生绑定） |
| typescript | ^5.9.2 | 类型检查/编译 |
| tsx | ^4.23.15 | 开发期直接运行 TS |
| vitest | ^3.2.4 | 测试框架 |
| @vitest/coverage-v8 | ^3.2.7 | 覆盖率 |
| @types/node / @types/better-sqlite3 | ^22 / ^7.6 | 类型 |

配置全部来自环境变量（`src/config.ts`，启动即校验）：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DATABASE_PATH` | `data/app.db` | SQLite 文件路径；`:memory:` 为内存库 |
| `PORT` | `3000` | 监听端口 |
| `HOST` | `127.0.0.1` | 监听地址 |
| `ETAG_STRENGTH` | `strong` | 发射强度：`strong` 或 `weak`（存储始终为强标签） |
| `DIAGNOSTICS` | `0` | `1`/`true` 打开结构化诊断日志 |
| `BUSY_TIMEOUT_MS` | `5000` | 写锁等待上限，超时返回 `503 write-lock-busy` |

---

## 4. 从干净目录复现

```bash
# 1) 安装
npm install

# 2) 类型检查
npm run typecheck

# 3) 编译（产物在 dist/）
npm run build

# 4) 启动服务
npm start                         # 默认 127.0.0.1:3000, data/app.db
# 或：DATABASE_PATH=:memory: PORT=3000 ETAG_STRENGTH=weak DIAGNOSTICS=1 npm start

# 5) 测试
npm test                          # 全部 89 个测试
npm run test:coverage             # 含覆盖率门槛（语句/分支/函数/行 ≥ 80%）
npm run test:verbose              # 控制台打印可关联的判定日志

# 6) 重放手写夹具（真实 HTTP，独立命令）
npm run replay                    # 重放 fixtures/replay 下全部夹具
npx tsx scripts/replay.ts fixtures/replay/weak-tags.json
```

### 请求样例

```bash
# 创建（返回 201 + 强 ETag + 版本号）
curl -i -X PUT localhost:3000/resources/notes/1 \
  -H 'content-type: text/plain' --data 'alpha'
#   ETag: "v1-8ed3f6ad685b"
#   X-Resource-Version: 1

# 条件读取：当前标签命中 → 304
curl -i localhost:3000/resources/notes/1 -H 'If-None-Match: "v1-8ed3f6ad685b"'

# 乐观更新：携带观察到的 ETag
curl -i -X PUT localhost:3000/resources/notes/1 \
  -H 'content-type: text/plain' -H 'If-Match: "v1-8ed3f6ad685b"' --data 'beta'
# 过时标签 / 弱标签写 → 412 if-match-mismatch

# 仅当不存在时创建
curl -i -X PUT localhost:3000/resources/new/id -H 'If-None-Match: *' --data 'x'

# 存在才允许更新
curl -i -X PUT localhost:3000/resources/notes/1 -H 'If-Match: *' --data 'y'

# 响应丢失后的幂等重放（同 key + 同 body → 原结果，Idempotent-Replay: true）
curl -i -X PUT localhost:3000/resources/orders/9 -H 'Idempotency-Key: k-1' --data 'first-draft'
curl -i -X PUT localhost:3000/resources/orders/9 -H 'Idempotency-Key: k-1' --data 'first-draft'
# 同 key 不同 body → 409 idempotency-replay-conflict
# 同 key 但不同资源或不同方法同样 → 409（键绑定 method + 资源 + body）

# 诊断
curl 'localhost:3000/diagnostics/decisions?runId=<run>'
curl 'localhost:3000/diagnostics/history?resourceId=notes/1'
```

请求可携带 `X-Run-Id` / `X-Client-Id` / `X-Request-Id` 关联运行身份；
响应回 `X-Request-Id`。

### 状态码与失败类别

| 场景 | 状态码 | `error.category` |
|---|---|---|
| 创建 / 更新成功 | 201 / 200 | — |
| 删除成功 | 204 | — |
| 条件 GET 命中 | 304 | — |
| 资源不存在（无条件） | 404 | `not-found` |
| If-Match/INM/IUS 失败 | 412 | `if-match-mismatch` / `if-none-match-exists` / `if-unmodified-since-modified` |
| 畸形条件头 | 400 | `malformed-if-match` / `malformed-if-none-match` / `malformed-date` |
| 幂等键复用不同体 | 409 | `idempotency-replay-conflict` |
| 写锁忙 | 503 | `write-lock-busy` |

---

## 5. 验收场景如何被验证

- **两个客户端并发更改**：`test/state/concurrency.test.ts` 用 `worker_threads`
  让两个客户端用各自的连接打开**同一个磁盘 SQLite 文件**，在消息门控下同时发起
  携带相同旧 `If-Match` 的 PUT。断言：恰有一个 `200`、另一个 `412
  if-match-mismatch`；最终仅为 v2 且只含获胜方正文（无丢失更新）。另一个测试让
  失败方用新标签重试，断言两次写入分别落在 v2、v3，都不丢失。
- **弱标签**：`fixtures/replay/weak-tags.json` + 内核/HTTP 测试，断言弱发射
  `W/`、GET 弱比较 304、PUT 弱 If-Match 412、强 If-Match 成功。
- **通配匹配**：`fixtures/replay/wildcard.json` 断言 `If-None-Match: *`
  缺席可建/存在 412、`If-Match: *` 存在可改/缺席 412、无条件缺席 404。
- **响应丢失夹具可重放**：`fixtures/replay/lost-response.json` 断言同 key 同体重放
  返回原始 v1 快照且 `Idempotent-Replay: true`、不同体 409、状态不变。
- **测试日志可关联**：`test/helpers/testLog.ts` 每行带 `runId/clientId/requestId`，
  记录观察版本、逐步判定（stage/comparison/result）与结论；写入 `.test-output/all.log`，
  设置 `LOG_TESTS=1` 时同时输出到控制台。诊断接口暴露同样的步骤与依据。
- **断言具体结果与失败类别**：测试断言精确状态码、版本号、正文、ETag（含独立哈希）、
  `failureCategory` 与决策轨迹，而非“接口能调用”。

## 6. 执行结果

见 [RESULTS.md](./RESULTS.md)（记录最近一次干净环境下的类型检查、测试、覆盖率与
夹具重放的真实输出）。
