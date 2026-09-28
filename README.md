# JSON-RPC 2.0 本地批次服务

一个**本地、自包含**的 JSON-RPC 2.0 服务，支持批次请求、通知（无响应）、异步任务（立即返回、稍后查询）以及可查询的操作记录与诊断事件。

- **技术栈**：TypeScript（strict）· Node.js 18+ · Fastify 5 · SQLite（better-sqlite3）· Vitest
- **数据**：全部为本地合成夹具（进程内内存实现 + 本地 SQLite 文件），无任何外部账号、真实业务数据或网络依赖。

---

## 1. 快速开始

```bash
npm install        # 依赖已通过 package-lock.json 锁定
npm test           # 运行 50 个测试（单元 / 适配 / 真实 HTTP 集成）
npm run typecheck  # 严格类型检查
npm start          # 启动服务，默认 http://127.0.0.1:8545
```

环境变量（均有默认值，见 `src/config.ts`）：

| 变量 | 默认 | 说明 |
|------|------|------|
| `HOST` | `127.0.0.1` | 监听地址（仅本地） |
| `PORT` | `8545` | 监听端口 |
| `DB_PATH` | `./data/rpc.sqlite` | SQLite 文件；`:memory:` 为纯内存 |
| `TASK_MAX_DELAY_MS` | `5000` | 任务可请求的最大延迟（输入边界） |
| `LOG_LEVEL` | `info` | `silent` / `info` / `debug` |

健康检查：`GET /healthz` → `{ "ok": true, "liveTasks": 0 }`。

---

## 2. 模块划分（各自有真实职责）

```
src/
  contract/            契约解析层：字节 → 结构化消息
    protocol.ts        JSON-RPC 类型、错误码
    parse.ts           信封/批次/元素解析；-32700 与 -32600 分层
  kernel/              执行内核：与 HTTP、SQLite 无关
    kernel.ts          批次调度、位置归属、通知、重复 ID、幂等、中断
    method.ts          方法处理器接口、MethodError、分离式任务协议
    methods/           合成业务方法（ping/calc/kv/tasks/operations/diagnostics）
      validate.ts      严格参数校验（拒绝隐式类型转换）
    ids.ts             独立 operationId / batchId / connectionId、重复 ID 键
    task-registry.ts   进程内分离任务注册（取消、优雅关停 drain）
  state/               状态适配层：内核只依赖接口
    store.ts           StateStore 接口 + 数据模型
    memory-store.ts    内存实现（单元测试用，语义与 SQLite 对齐）
    sqlite-store.ts    持久实现（WAL，唯一约束保证幂等）
    domain-store.ts    业务侧效果存储接口（KV 夹具）
    memory-/sqlite-domain-store.ts
  diagnostics/         诊断
    logger.ts          接受/拒绝/无法判定 三类事件 + 关联标识
    redact.ts          敏感字段脱敏
  transport/
    server.ts          Fastify：原始 body、连接中断信号、204 语义
  config.ts · app.ts（装配根）· main.ts（入口）

tests/                 独立组织；期望值为手写字面量，不由被测核心生成
  helpers/harness.ts   内存测试夹具 / 临时 SQLite / 中断工具
  parse · batch · idempotency-async · sqlite-store · diagnostics · http-integration
```

---

## 3. 四个边界语义（本项目重点）

### 3.1 通知无响应，但失败仍留痕；`[]` 与单对象区分

- 通知（无 `id`）**永不**出现在响应数组中；全通知批次返回 **HTTP 204 / 空体**。
- 但通知的副作用与失败都会落库：失败的副作用通知在 `operations` 中为 `failed`；
  未知方法等拒绝在 `diagnostics.events` 中为 `rejected`。
- `[]`（空批次）→ 单个 `-32600` 对象（非法请求），**不**等同于含一个元素的批次，
  也**不**等同于全通知批次（后者 204）。
- 顶层 `null`、数字、字符串 → 单对象 `-32600`；**完全无法解析的字节** → `-32700`。

### 3.2 请求 ID 重复：策略明确，绝不串结果

- 同一批次内重复的 RPC `id`：**第一次正常执行，其后每次都返回 `-32001`**，
  响应按**输入位置**归位（即使方法异步、完成乱序，也不会把 A 的结果发给 B）。
- 数字 `1` 与字符串 `"1"` 是两个不同 id（带类型前缀的内部键），不会相互碰撞。
- **不同批次之间相同 id 完全合法、不冲突**——因为 RPC id 从不作为存储键。

### 3.3 解析错误与业务错误分层

| 层 | 码 | 触发 |
|----|----|------|
| 解析错误 | `-32700` | body 不是 JSON（或超过 1 MiB） |
| 非法请求/元素 | `-32600` | 是 JSON 但不是合法 Request 对象；空批次 |
| 方法不存在 | `-32601` | `no-such-method` |
| 参数非法 | `-32602` | 严格校验失败（布尔当整数、整数溢出、超长、未知字段等） |
| 业务失败 | `-32603` | 处理器抛出的非预期错误 |
| 批次内重复 ID | `-32001` | 同批次重复 id |
| 幂等冲突 | `-32005` | 同键不同参数（指纹不符）/ 同键操作仍在途 |
| 未找到 | `-32006` | 查询不存在的 key/operation |
| 已取消 | `-32007` | 分离任务被 `tasks.cancel` |
| 连接中断 | `-32008` | 附着式执行期间客户端断开 |
| 合成业务失败 | `-32050` | 任务显式 `shouldFail` |

### 3.4 副作用拥有独立操作号；RPC ID 不是幂等键

- 每次副作用调用都获得独立 `operationId`（`op_…`），与 RPC `id` 无关；
  即便两个批次复用同一 RPC id，operationId 也不同。
- 幂等键是**显式的、按方法作用域**的（`kv.put` 默认用业务 key，`tasks.start` 用
  `params.idempotencyKey`）。无键的副作用每次都正常执行；**绝不用 RPC id 去重**。
- 重复提交：
  - 同键同参数 → 回放首次结果，**不再产生副作用**，也不新增操作行；
  - 同键**不同参数** → `-32005`（指纹不符），本次拒绝仍以**自己的 operationId**
    记录为 `failed` 审计行，并通过 `replayedFromOperationId` 指向原操作；
  - 同键首次操作仍 `pending` → `-32005`（in-progress），同样留审计行。

---

## 4. 异步执行与中断模型

- `tasks.start` 立即返回 `{ operationId, status: "pending" }`，后台工作继续；
  通过 `operations.get` / `operations.list` 查询最终 `succeeded | failed | cancelled`。
- **分离式任务在发起连接断开后仍继续运行**（传输层只在"响应尚未发出"时把连接关闭
  视为中断；响应一旦发出，之后的 socket 关闭不会取消后台工作）。可用 `tasks.cancel`
  显式取消，记录为 `-32007`。
- **附着式**执行（如 `kv.putDelayed`、只读 `debug.sleep`）在响应发出前连接断开，
  会被中止：副作用不落库，操作记录为 `failed / -32008`，诊断记 `indeterminate`
  （无法判定客户端是否收到结果）。
- 进程退出（SIGINT/SIGTERM）会用最多 5 秒等待在途任务落账；崩溃留下的 `pending`
  行可被查询到，是诚实的"未知/中断"状态，而不是被静默抹掉。

---

## 5. 诊断与脱敏

每个判定都持久化一条 `diagnostic_events`：含 `connectionId / batchId / rpcId /
operationId`、`decision`（accepted / rejected / indeterminate）、机器可读 `reason`
与脱敏后的 `detail`，回答"为什么接受、拒绝或无法判定"。

脱敏（`src/diagnostics/redact.ts`）先对键名做 camelCase/kebab/snake 归一化分词，
对 `password / secret / token / authorization / authentication / jwt / credential`
等词，以及 `api key / private key / secret key / access key` 等组合（任意层级、
含前后缀与复数变体，如 `accessToken`、`passwordHash`、`X-Auth-Token`、`client_secret`）
一律以 `***REDACTED***` 落库与打印；长字符串截断。此外，**解析失败只记录字节长度，
绝不回显或持久化原始报文片段**（畸形 JSON 里也可能含密钥字符串）。原始密钥既不进
操作记录，也不进诊断事件与日志。可用 `diagnostics.events` 查询验证。

---

## 6. 支持范围与关键取舍

**支持**：单请求与批次；通知；乱序并发但位置有序的批次响应；批次内重复 ID 检测；
解析/协议/业务三级错误；严格参数校验；显式按方法的幂等键与结果回放/冲突审计；
分离式异步任务、取消与存活查询；附着式执行的连接中断落账；持久化操作与诊断查询；
脱敏诊断；SQLite 持久与内存双适配（同一接口、同一语义）。

**刻意不做 / 取舍**：
- 分离任务的**进程内**状态不跨重启恢复（结果落 SQLite，但内存任务表不恢复）；
  崩溃后的在途行保持 `pending`，这是明确可观测的状态而非隐藏丢失。
- 幂等作用域为"方法 + 显式键"，不提供跨方法全局幂等，也不用 RPC id 充当幂等键。
- 批次内并发执行（顺序保留在响应归属上，而非串行执行），以暴露真实竞态。
  单批次元素数上限 10,000、body 上限 1 MiB，超限以单个 `-32600`/`-32700` 拒绝。
- 仅绑定本地地址、使用本地合成夹具，不含鉴权/多租户/TLS（本地服务定位）。

---

## 7. 示例请求

见 [`examples.md`](./examples.md)。可直接用 `curl` 复现。

## 8. 测试

`npm test` 运行 6 个测试文件、50 个用例，断言**具体结果与失败类别**（具体错误码、
状态、位置归属、脱敏后的字面值），而非仅"接口可调"。期望载荷全部为手写常量，
测试夹具独立于被测核心；真实 HTTP 用例经过 TCP 并用 `AbortController` 制造真中断。
