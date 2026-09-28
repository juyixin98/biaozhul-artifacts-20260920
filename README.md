# 组合 API 部分失败聚合（Composite API Partial-Failure Aggregation）

TypeScript + Node.js + Fastify + SQLite 的后端组合查询服务：按**依赖图**调用多个
本地数据源，组装类型化响应，并在部分来源失败/超时/版本不一致时**聚合**结果而不是
整单失败。所有数据与参与者均为本地合成夹具（内存表 + 本地 SQLite），无生产账号、
无网络依赖。

---

## 1. 它解决什么问题

一个 `orderDetails` 组合查询要聚合 5 个来源，依赖图是一个真实的**菱形**：

```
                     customers (A)
                    /              \
            inventory (B)      promotions (C, 可选, 80ms 预算)
                    \              /
                 recommendations (D = B ⋈ C)          pricing（独立分支）
```

系统必须保证：

- 菱形汇聚节点 `recommendations` 在三条入边下**只调用一次**；
- **必需**来源失败与**可选**来源缺省被区别对待（缺省走 fallback，不拖垮整单）；
- 同一个**快照令牌**传给所有来源，不支持快照的来源被标注为一致性限制；
- **截止期沿依赖图传播**，超时即取消，禁止后台无限执行；
- 返回的每个字段都带**字段级状态、来源（provenance）和失败原因**。

---

## 2. 快速开始

环境要求：Node.js ≥ 22.13（使用内置的实验性 `node:sqlite`，无需额外编译）。

```bash
npm install

npm test            # 运行全部 40 个测试（真实执行并报告结果）
npm run coverage    # 覆盖率（v8）
npm run typecheck   # src + scripts + tests 全量类型检查
npm run build       # 编译到 dist/

npm run demo        # 本地端到端演示（5 个内核场景 + HTTP 服务）
npm start           # 启动 HTTP 服务（默认 127.0.0.1:8080）
# 或开发模式：npm run dev
```

演示脚本会依次运行：菱形正常路径、**必需失败**、**可选超时**、
**来源版本不一致**、**运行级截止取消**，并在最后启动一个临时 HTTP 服务演示
查询接口与诊断接口。

### HTTP 用法

```bash
# 正常查询（200）
curl -s -X POST http://127.0.0.1:8080/query/orderDetails \
  -H 'content-type: application/json' \
  -d '{"params":{"customerId":"C1","sku":"SKU-1"},
       "snapshotToken":"snap-v1-20260928T000000Z",
       "timeoutMs":1000}'

# 必需来源失败 → 202（partial），响应里带字段级原因
curl -s -X POST http://127.0.0.1:8080/query/orderDetails \
  -H 'content-type: application/json' \
  -d '{"params":{"customerId":"C1","sku":"SKU-BROKEN"},"snapshotToken":"snap-v1-x"}'

# 诊断：按运行编号重放
curl -s http://127.0.0.1:8080/runs                # 运行索引
curl -s http://127.0.0.1:8080/runs/<runId>        # 完整响应
curl -s http://127.0.0.1:8080/runs/<runId>/events # 有序内核事件轨迹
curl -s http://127.0.0.1:8080/runs/<runId>/nodes  # 每节点执行行
```

| 方法/路径 | 说明 |
|---|---|
| `POST /query/:name` | 执行组合查询，返回类型化响应 |
| `GET /contracts` | 已注册契约（节点数/字段数） |
| `GET /runs` · `/runs/:id` · `/runs/:id/events` · `/runs/:id/nodes` | 诊断与重放 |
| `GET /health` | 健康检查 |

---

## 3. 工程边界（模块划分）

不是单文件实现，也没有空壳拆分；每个模块有明确的数据/错误契约：

```
src/
  errors.ts                 # 统一错误分类法（DomainError + FailureDetail）
  types.ts                  # 契约 / 来源 / 内核运行时的全部类型契约
  contract/parser.ts        # 契约解析：校验、环检测、拓扑排序、可选性继承
  kernel/
    executor.ts             # 执行内核：图调度、截止传播、取消、字段组装
    semaphore.ts            # 有界并发信号量（队列满→资源耗尽；可中断排队）
    events.ts               # 内核事件流（可重放的状态轨迹）
  sources/
    fixtureSource.ts        # 内存合成表来源（可注入延迟/失败/挂起/快照能力）
    sqliteSource.ts         # 本地 SQLite 来源（应用自播种的夹具数据）
    timing.ts               # 协作式可取消计时（超时不留下后台工作）
  state/runStore.ts         # 状态适配：SQLite 运行日志（runs/run_nodes/run_events）
  service/
    registry.ts             # 契约注册表
    app.ts                  # Fastify 路由 + 统一错误信封
  fixtures/scenario.ts      # 本地合成场景：orderDetails 契约 + 夹具来源装配
  index.ts                  # 可运行服务入口
scripts/demo.ts             # 本地演示脚本
tests/                      # 8 个测试文件，40 个断言具体结果的用例
```

模块间只通过 `types.ts` 中的契约通信；来源统一实现 `DataSource` 接口
（`fetch(SourceRequest) → Promise<SourceResult>`），内核不感知数据来自内存还是
SQLite。

---

## 4. 契约：字段来源与必需性显式声明

字段来源在契约里显式声明为三类：`source`（来源字段）、`constant`（常量）、
`der`（由前序字段派生，支持 `concat` / `add`）。

```jsonc
{ "path": "price", "required": true,
  "ref": { "from": "pricing", "property": "price" } }

{ "path": "discount", "required": false,
  "ref": { "from": "promotions", "property": "discount", "fallback": 0 } }

{ "path": "total", "required": true,
  "ref": { "derive": { "op": "add", "fields": ["price", "fee"] } } }
```

节点入参可为字面量 `{value}`、请求入参 `{request}`、上游引用
`{from, property}`。对 `necessity:"optional"` 节点的引用自动继承**非阻塞**
语义；可选上游不可用时参数取其 `default`（未声明则为 `undefined`）。

### 字段状态（`fields[].status`，字段级原因）

| status | 含义 |
|---|---|
| `present` | 成功取到值 |
| `missing-required-failed` | 来源被实际调用但失败，且字段必需 |
| `missing-upstream-skipped` | 来源因必需上游失败而未被调用 |
| `missing-optional-default` | 可选来源失败，**已应用 fallback** |
| `missing-optional-skipped` | 可选来源失败且无 fallback（值为 null） |
| `derivation-failed` | 派生计算失败或缺操作数 |

每个缺失字段都在 `reasons[]` 里附带具体的 `category/code/at/causedBy`。

---

## 5. 错误语义（可区分的失败类别）

所有失败归入互不混淆的 `category`，HTTP 层据此映射状态码：

| category | 含义 | HTTP | retryable |
|---|---|---:|---|
| `MISSING_INPUT` | 请求输入错误（缺参数、超时值非法等） | 400 | false |
| `NOT_FOUND` | 契约 / 运行不存在 | 404 | false |
| `INVALID_CONTRACT` | 契约结构非法（注册期暴露，不会出现在逐请求路径） | 500 | false |
| `SOURCE_FAILURE` | 来源返回错误 / 上游属性缺失等**计算/参与者失败** | 502 | 视情况 |
| `SOURCE_TIMEOUT` | 来源在**传播后的截止期**前未完成 | 504 | true |
| `CANCELLED` | 运行级截止传播导致的取消 | 504 | true |
| `STATE_CONFLICT` | 快照状态冲突（exact 快照无法满足 / 版本漂移） | 409 | false |
| `RESOURCE_EXHAUSTED` | 本地有界并发槽/队列耗尽 | 503 | true |
| `COMPUTATION_FAILED` | 组装/派生计算失败、来源未注册 | 500 | false |
| `CONSISTENCY_LIMITED` | 非硬失败：快照未能完全保证（写入一致性备注） | 200 | — |

**整单结果 `outcome`：**

- `complete`：所有必需字段就位（HTTP 200）；
- `partial`：存在缺失的必需字段，但独立分支仍被组装（HTTP 202），
  失败细节在字段与 `failures[]` 中，而非整体 5xx；
- `failed`：请求级失败（调度前/调度异常）。

### 节点状态（`nodeResults[].status`）

`succeeded` / `failed` / `timed-out` / `cancelled` / `skipped` /
`running` / `pending`。另有两个可断言的计数：

- `attempts`：来源被实际调用次数（取消与跳过时为 **0**）；
- `didNotInvoke`：内核是否在调用来源前就拒绝了该节点。

### 快照一致性

- 请求带 `snapshotToken`（格式 `snap-<version>-<id>`）时，**同一令牌**传给全部来源；
- 来源声明 `supportsSnapshot` 与 `dataVersion`；
- 不支持快照的 **best-effort** 节点 → `consistency[]` 追加
  `snapshot-unsupported`（一致性受限，但照常服务）；
- 版本与令牌不符 → `snapshot-mismatch`，并汇总 `dataVersion.consistent=false`；
- 声明 `snapshot:"exact"` 的节点无法满足时 → 硬失败
  `STATE_CONFLICT`（`SNAPSHOT_NOT_SUPPORTED` / `SNAPSHOT_VERSION_DRIFT`），
  其传递性下游会被标注。

### 截止期与取消（禁止后台无限执行）

- 每个节点的有效截止 = `min(运行截止, now + 节点 timeoutMs)`；
- 截止通过 `AbortSignal` 传播；本地来源的模拟延迟是**协作式可取消**的，
  超时立即停止，测试通过夹具记录的 `aborted` 与 **`postAbortTicks === 0`**
  断言“超时后没有任何后续工作”；
- 只有节点自身预算**严格更紧**时才安装节点定时器并归类 `SOURCE_TIMEOUT`；
  有效截止等于运行截止时由运行定时器统一取消，归类 `CANCELLED`，
  避免同刻双定时器竞态吞掉运行级事件。

---

## 6. 诊断：可重放的运行日志

每次运行落 SQLite（默认 `./data/runs.sqlite`，`:memory:` 用于测试），三张表：

- `runs`：运行编号、契约、outcome、快照、入参、失败汇总、完整响应 JSON；
- `run_nodes`：每节点状态、尝试次数、延迟、所用令牌、是否未调用、失败 JSON；
- `run_events`：**有序**事件轨迹（`seq` 单调），含 `run-started →
  node-ready → node-invoked → deadline-fired → node-settled →
  late-settle-ignored → field-resolved → run-finished`。

测试还会把每个运行的轨迹写到 `test-results/traces/<runId>.jsonl`，并把
每条断言的判断理由追加到 `test-results/verdict.log`（运行编号 + 关键中间状态 +
判定理由），用于问题重放。

---

## 7. 第三阶段：验证用例与复现

测试不只检查“接口能调用”，而是断言**具体值与失败类别**，夹具期望（如
`price=100`、`stock=7`、`discount=15`）以字面量写在测试里，**不**由被测内核生成。

| 测试文件 | 关键断言 |
|---|---|
| `diamond.test.ts` | 汇聚节点三入边下**调用一次**；精确组装值；5 个来源收到**同一令牌**；边数据沿图流动（`stock=7`、`promoDiscount=15`）；字段 provenance；运行/事件持久化 |
| `requiredFailure.test.ts` | 必需来源失败→`partial`，`price/total` 带 `SOURCE_FAILURE` 原因，独立分支不受影响；失败必需节点的下游 `attempts=0` 被跳过且 `causedBy` 链保留 |
| `timeout.test.ts` | 可选节点 80ms 超时→字段 fallback、汇聚节点仍调用一次、`postAbortTicks=0`；10ms 运行截止取消在途节点、下游零调用、独立分支存活；节点/运行截止两个方向的取 min |
| `snapshot.test.ts` | best-effort 不支持→一致性限制；v2 对 v1 令牌→`consistent=false`；exact 不支持/漂移→`STATE_CONFLICT`；无令牌→逐节点标注 |
| `resourceAndInput.test.ts` | 队列溢出→恰好 2 个 `RESOURCE_EXHAUSTED`（与超时区分）；缺参→`MISSING_INPUT`；上游属性缺失→`SOURCE_FAILURE`；信号量可中断排队与槽位交接 |
| `computationFailure.test.ts` | 来源未注册→`COMPUTATION_FAILED`；`add` 对字符串→`DERIVED_TYPE_ERROR` 且只影响该字段 |
| `contract.parser.test.ts` | 环/重复 id/未知引用/非法必需性等被拒；可选性继承；拓扑序 |
| `http.test.ts` | 200/202/400/404 状态码与错误信封；诊断接口事件与节点数 |

复现四个指定场景：

```bash
npm test                 # 全量
npx vitest run tests/diamond.test.ts
npx vitest run tests/requiredFailure.test.ts
npx vitest run tests/timeout.test.ts
npx vitest run tests/snapshot.test.ts
npm run demo             # 同样四个场景的可读输出
```

最近一次全量结果：**8 个测试文件 / 40 个用例全部通过**；语句覆盖率约 **86%**
（未覆盖主体是纯启动引导 `src/index.ts`）。时序敏感的取消用例已在覆盖率插桩下
做过 300 次运行级截止 + 100 次可选超时的压测，无偶发失败。

---

## 8. 设计说明与边界

- 有界并发：默认 `maxConcurrent=4, maxQueue=16`，队列满立即失败而非无限排队；
  槽位释放采用**所有权直接交接**，不存在新请求插队窗口。
- 调度对必需依赖做记忆化（`ensureNode`），菱形汇聚天然只执行一次；可选依赖
  仅在已完成时被采纳其值，否则按缺省处理，慢的可选调用不会阻塞下游。
- SQLite 使用 Node 内置 `node:sqlite`（实验性），无需原生编译；生产替换只需
  实现 `DataSource` 与 `RunStore` 两个接口。
- 无任何硬编码密钥或网络出口；全部输入在系统边界（HTTP 路由、契约解析）校验。
