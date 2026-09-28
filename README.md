# 组合 API：部分失败聚合（Composite Partial-Failure Aggregation）

TypeScript + Node.js + Fastify + SQLite 的后端服务：按**依赖图（DAG）**并发调用多个
**本地合成来源**，组装类型化响应；在必需调用失败、可选调用超时、来源版本不一致等情况下，
输出带**字段级原因**的部分结果，而不是整体 500。

所有“上游”都是本地夹具（`data/fixtures.json` + 故障注入计划），**没有任何真实网络或生产账号**。

---

## 1. 快速开始

```bash
npm install        # 安装依赖（better-sqlite3 有预编译二进制）
npm test           # 运行全部测试（终端 spec + logs/test.tap.log TAP）
npm run test:cover # 带覆盖率（当前约 95% 行 / 84% 分支）
npm run typecheck  # 严格类型检查
npm run build      # 编译到 dist/ 并拷贝夹具
npm run demo       # 真实启动服务，依次打 4 个场景，再打印可重放事件
npm run dev        # 以 tsx 直接启动服务（默认 127.0.0.1:3000）
# 或：npm run build && npm start
```

需要 Node.js ≥ 20。

环境变量（均有本地安全默认值）：`PORT`、`HOST`、`DB_PATH`、`LOG_PATH`、
`DEFAULT_TIMEOUT_MS`（300）、`MAX_TIMEOUT_MS`（5000）。

### 一次手动请求

```bash
npm run dev &
curl -s -X POST http://127.0.0.1:3000/v1/compose \
  -H 'content-type: application/json' \
  -d '{"contract":"orderSummary","input":{"userId":"u-1001","sku":"sku-1"}}' | head
```

---

## 2. 架构与工程边界

```
HTTP (Fastify)                 src/http/app.ts
  ├─ POST /v1/compose
  └─ 诊断接口                   src/diagnostics/routes.ts
应用门面（编排）               src/compose/service.ts
  ├─ 契约解析/校验             src/contract/validate.ts · src/contract/types.ts
  ├─ 执行内核（DAG 调度）      src/kernel/executor.ts · timing.ts · errors.ts · types.ts
  ├─ 来源 SPI + 注册表         src/sources/types.ts · registry.ts
  │    └─ 本地夹具来源/故障注入 src/sources/fixtures/* · src/sources/index.ts
  ├─ 状态适配（SQLite）        src/state/runStore.ts
  └─ 可观测性（结构化日志）    src/observability/runLog.ts
```

模块间的数据与错误契约：

- **契约**：每个输出字段显式声明 `source/method`、依赖、`requirement: required|optional`、
  取值 `path`、可选 `defaultOnMissing`。契约非法（重复节点/字段、未知依赖、环、
  缺少必需性声明）在调用任何来源**之前**就报 `INPUT_ERROR`。
- **内核**：只依赖 `Source` 接口与 `CallContext`，不认识 HTTP / SQLite；
  输出不可变的 `CompositeResult`。
- **来源**：实现 `call(method, request, ctx)`，必须观察 `ctx.signal`/`ctx.deadline`，
  可通过 `consistency(ctx)` 上报本次读取的快照一致性与数据版本。
- **状态层**：持久化运行信封与事件流，处理幂等键。
- 跨模块抛出的错误统一为 `CompositeError`（`src/kernel/errors.ts`），携带稳定的
  `category` 与 `reason`。

### 依赖图（`orderSummary`，菱形）

```
        profile(users)            product(catalog)
       /   |    \                  /   |   \
 contact  stock  quote/upsell ◇───┘   promo
   (contacts) (inventory)(pricing/recommendations 同时依赖两支 = 菱形)
```

`quote`（报价）、`stock`（库存）、`upsell`（推荐）都同时需要 **profile 与 product**，
这正是测试核验的菱形汇聚点。

---

## 3. 响应契约

`POST /v1/compose` 请求体：

| 字段 | 说明 |
|---|---|
| `contract` | `orderSummary` |
| `input` | `{ userId: string, sku: string }` |
| `scenario` | `healthy` / `required-failure` / `optional-timeout` / `version-mismatch` |
| `timeoutMs` | 本次截止期；上限 `MAX_TIMEOUT_MS` |
| `snapshotToken` | 不传则服务端生成；同一请求的令牌传给**所有**来源 |
| `idempotencyKey` | 同键同载荷重放；同键异载荷返回 409 |

响应（`200`，无论 `status` 是 complete/partial/failed；失败类见下）：

```jsonc
{
  "replayed": false,
  "run": {
    "runId": "run-...",
    "contract": "orderSummary",
    "status": "complete | partial | failed",
    "snapshotToken": "snap-...",
    "startedAt": 1790524145411, "endedAt": 1790524145423, "deadlineMs": 300,
    "data": { /* 成功取到的类型化字段；失败的必需字段缺失 */ },
    "fields": [
      { "field": "finalPrice", "source": "pricing", "call": "quote",
        "requirement": "required", "state": "present" },
      { "field": "promotionCode", "source": "promotions", "call": "promo",
        "requirement": "optional", "state": "failed",
        "reason": { "category": "RESOURCE_EXHAUSTED", "reason": "DEADLINE_EXCEEDED",
                    "message": "...", "retryable": true, "context": {} } }
    ],
    "nodes": [ /* 每个图节点：state/attempts/durationMs/snapshotHonored/sourceVersion/... */ ],
    "limitations": [ /* SNAPSHOT_UNSUPPORTED / VERSION_MISMATCH */ ],
    "errors": [ /* 运行级错误；partial 通常为空，failed 非空 */ ]
  }
}
```

节点状态：`pending / running / resolved / failed / timeout / cancelled / skipped`。
每个节点 `attempts` 至多为 1（无重试，测试据此断言调用次数）。

---

## 4. 错误语义（四类必须可区分）

| 类别 `category` | 含义 | 典型 `reason` | HTTP |
|---|---|---|---|
| `INPUT_ERROR` | 调用方输入/契约问题，不可重试 | `INPUT_MISSING_FIELD`、`INPUT_TYPE_ERROR`、`CONTRACT_CYCLE`、`UNKNOWN_CONTRACT`、`TIMEOUT_OUT_OF_BOUNDS` | 400 |
| `STATE_CONFLICT` | 持久化状态与请求冲突 | `IDEMPOTENCY_KEY_MISMATCH` | 409 |
| `RESOURCE_EXHAUSTED` | 截止期/取消/预算耗尽，**可重试** | `DEADLINE_EXCEEDED`、`SOURCE_TIMEOUT`、`CANCELLED` | 504（单字段失败时仍随 200 信封返回） |
| `COMPUTATION_FAILED` | 来源调用失败或数据异常 | `CATALOG_UNAVAILABLE`、`SOURCE_NOT_FOUND`、`REQUIRED_VALUE_MISSING`、`DEPENDENCY_UNAVAILABLE` | 502（单字段失败时仍随 200 信封返回） |

注意：组合**部分失败是正常业务结果**，走 `200` + `run.status=partial/failed`，
原因放在字段/节点上；只有请求在进入内核前就非法、状态冲突等才用 4xx。

### 必需 vs 可选

- **必需字段来源失败**：`run.status=failed`，该字段 `state=failed` 带原因；
  依赖该节点的下游**不会被调用**（`state=skipped`，原因 `DEPENDENCY_UNAVAILABLE`）。
  与之**无关的分支照常执行并收获数据**（最大化可用部分结果）。
- **可选字段来源失败/超时**：`run.status=partial`，字段 `state=failed` 带字段级原因，
  其余字段不受影响，`run.errors` 为空。
- **可选字段合法缺省**：来源成功但值为 `null/undefined` 且无默认值 →
  `state=missing`，**不会**把响应降级为 partial；声明了 `defaultOnMissing` 则补默认值并标
  `defaulted:true`。
- 来源成功但**必需值缺失** → `REQUIRED_VALUE_MISSING`，运行失败。

### 快照令牌与一致性

- 每次请求生成（或使用客户端给定的）**同一个** `snapshotToken`，连同同一个绝对
  `deadline` 传给**每一个**来源；测试断言所有来源收到的令牌/截止期一致。
- 来源不支持快照固定（夹具里的旧版 `contacts`）→ 不伪装成功，而是在 `limitations`
  标记 `SNAPSHOT_UNSUPPORTED`（一致性限制，不是错误）。
- 多个**支持快照**的来源在同一令牌下返回不同数据版本 → 标记 `VERSION_MISMATCH`，
  并列出每个来源实际版本；不支持快照的旧来源不参与版本比较。

### 截止期与取消

- 截止期沿依赖图传播：所有来源拿到同一绝对时间与同一个 `AbortSignal`。
- 来源必须协作式响应中止（夹具用 `cancellableDelay`）；超时节点记为 `timeout`，
  原因 `DEADLINE_EXCEEDED`。
- **截止期后不允许后台无限执行**：中止即清除定时器，测试用探针来源证明
  `postAbortWorkObserved === false`，并断言整次运行在截止期附近结束。
- 必需失败时不再启动被其阻塞的节点（最终 `skipped`），但不强杀已在途的独立分支。

---

## 5. 四个场景的复现步骤

```bash
npm run dev   # 另一个终端执行：

curl -s -X POST localhost:3000/v1/compose -H 'content-type: application/json' \
  -d '{"contract":"orderSummary","input":{"userId":"u-1001","sku":"sku-1"}}'
#   -> status=complete；7 个节点各调用 1 次；contacts 报 SNAPSHOT_UNSUPPORTED

curl -s -X POST localhost:3000/v1/compose -H 'content-type: application/json' \
  -d '{"contract":"orderSummary","input":{"userId":"u-1001","sku":"sku-1"},"scenario":"required-failure"}'
#   -> status=failed；product=failed；quote/stock/upsell/promo=skipped（attempts=0）；
#      profile/contact 独立分支仍成功；errors[0]=COMPUTATION_FAILED/CATALOG_UNAVAILABLE

curl -s -X POST localhost:3000/v1/compose -H 'content-type: application/json' \
  -d '{"contract":"orderSummary","input":{"userId":"u-1001","sku":"sku-1"},"scenario":"optional-timeout","timeoutMs":80}'
#   -> status=partial；promo=timeout（约 80ms 处被中止，而非 5s）；
#      promotionCode 字段 failed/RESOURCE_EXHAUSTED；finalPrice 等必需字段仍在

curl -s -X POST localhost:3000/v1/compose -H 'content-type: application/json' \
  -d '{"contract":"orderSummary","input":{"userId":"u-1001","sku":"sku-1"},"scenario":"version-mismatch"}'
#   -> status=complete，但 limitations 含 VERSION_MISMATCH（pricing=pricing-hot-2026-09-27
#      与其余 catalog-epoch-2026-09-01 不一致）
```

或直接 `npm run demo` 一次性跑完，并打印最后一次运行的事件流与诊断索引。

### 可重放日志（保留运行编号 / 中间状态 / 判断理由）

每次运行都会同时写入 SQLite（`runs`、`run_events` 表）和 `logs/runs.jsonl`：

```bash
RUNID=$(curl -s -X POST localhost:3000/v1/compose -H 'content-type: application/json' \
  -d '{"contract":"orderSummary","input":{"userId":"u-1001","sku":"sku-1"},"scenario":"optional-timeout","timeoutMs":80}' \
  | sed -E 's/.*"runId":"([^"]+)".*/\1/')

curl -s localhost:3000/runs/$RUNID          # 完整响应信封
curl -s localhost:3000/runs/$RUNID/events   # 有序事件：RUN_START/NODE_START/.../DECISION/RUN_END
curl -s localhost:3000/runs?limit=10        # 运行索引
```

`DECISION` 事件带 `data.rule` 与 `because`，例如 `DEADLINE_ABORT`、
`OPTIONAL_FIELD_DEGRADED`、`DEPENDENCY_SKIP`、`REQUIRED_FAILURE_FAILS_RUN`、
`SNAPSHOT_LIMITATION`、`VERSION_SKEW_LIMITATION`——记录“为什么这样判定”，便于重放问题。

---

## 6. 测试与“参考答案独立性”

`npm test` 运行 47 个测试（node:test + tsx，真实 Fastify 注入 + 临时 SQLite），
覆盖：菱形依赖、必需失败、可选超时、版本不一致、字段语义、契约校验、错误分类、
状态/幂等/事件重放、HTTP 端到端。断言的是**具体结果与失败类别**（具体值、
节点状态、调用次数、取消行为、字段来源、响应契约），不是“接口能调通”。

独立性保证：

- 期望值由测试层从夹具事实/独立公式推导（如金牌 15% 折扣：
  `1299 × (1−0.15) = 1104.15`），不是读被测内核的输出。
- 故障由**独立的 `FaultPlan`** 声明（“pricing:getQuote 会超时”），测试再独立核验
  内核是否把该节点正确归类为 `timeout`。
- 取消行为用独立探针来源（`ProbeSlowPromotions`）证明截止期后没有遗留后台工作。

---

## 7. 目录

```
data/fixtures.json          本地合成数据（唯一事实来源）
src/contract/               契约类型与解析校验
src/kernel/                 执行内核、截止期/取消、错误分类、结果类型
src/sources/                来源 SPI、注册表、夹具来源、故障注入
src/state/                  SQLite 运行/事件/幂等
src/observability/          结构化运行日志（内存 + JSONL sink）
src/diagnostics/            诊断 HTTP 接口
src/compose/                契约定义、场景表、应用门面
src/http/ src/server.ts     Fastify 应用与可运行入口
scripts/demo.mjs(.sh)       本地端到端演示
test/                       单元 + 集成 + HTTP e2e
```
