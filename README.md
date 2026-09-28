# JSON-RPC 2.0 本地批次服务

基于 **TypeScript + Node.js + Fastify + SQLite（`node:sqlite` 内置驱动）** 的本地 JSON-RPC 2.0 服务，
支持批次请求、通知（无响应）、异步副作用操作以及可查询的操作台账。所有业务数据均为进程内合成夹具，
无任何生产账号或外部网络依赖。

## 1. 支持范围

### JSON-RPC 2.0 契约

- 单对象请求与数组批次请求，二者严格区分（`[]` 不是“全是通知”，而是无效请求）。
- 通知（无 `id` 成员）：**永不返回响应**，批次中若全为通知则返回 `204 No Content`。
  注意 `id: null` 是合法请求 ID，不是通知。
- 批次元素并发执行、允许乱序完成，**响应按请求位置归位**；每个元素独立记账（独立 `callCorr`），
  因此重复的请求 ID 不可能串结果。
- 错误分层（`error.data.category`）：
  - `parse_error`（-32700）：body 不是合法 JSON；
  - `invalid_request`（-32600）：能解析但不是合法报文（顶层标量、空批次、批内非法元素、错误 `jsonrpc`/`method`/`id`/`params`）；
  - `method_not_found`（-32601）、`invalid_params`（-32602，方法参数契约层）；
  - `business_rule`（-32xxx，业务规则层，如余额不足）、`not_found`（-32010）、`internal`（-32603）；
  - `interrupted`：连接中断、结果无法判定。
- 每个错误对象都带 `data.correlationId`（顶层错误为 `requestCorr`，元素错误为 `callCorr`），
  可据此在诊断接口中定位。

### 方法

| 方法 | 类型 | 说明 |
| --- | --- | --- |
| `math.add` | 纯查询 | `{a,b}` 求和 |
| `echo` | 纯查询 | 原样回显，用于验证响应归属（接受任意 params） |
| `accounts.list` / `accounts.balance` | 纯查询 | 合成账户夹具 |
| `accounts.transfer` | 同步副作用（独立操作号） | 整数“分”转账；显式 `idempotencyKey` 去重 |
| `secrets.put` | 同步副作用 | 台账只保存脱敏输入 |
| `tasks.schedule` | **异步副作用** | 立即返回 `pending + opSeq`，后台完成后轮询查询 |
| `operations.get` / `operations.list` | 查询 | 查询操作台账（含 pending/failed/interrupted） |
| `debug.sleep` / `debug.sideEffect` | 调试 | 慢调用/慢副作用，用于乱序与中断测试 |

### 诊断接口

- `GET /diag/requests?limit=` — 近期请求及裁决（accepted / rejected / undecidable）。
- `GET /diag/requests/:requestCorr` — 请求详情：计数、裁决层级与理由、每个调用、副作用操作。
- `GET /diag/operations?status=&kind=&limit=` / `GET /diag/operations/:opSeq`。
- 服务同时向 stdout 输出结构化 JSON 诊断行（含关联 ID、裁决与关键状态）。
- **脱敏**：敏感键（secret/password/token/apiKey/privateKey 等，大小写不敏感）在落库与日志前
  统一替换为 `***redacted***`；台账从不保存原始 params。

## 2. 模块划分（各自有真实职责，非单文件脚本）

```
src/
  config.ts                 启动配置：env 解析与失败快速校验
  protocol/
    types.ts                JSON-RPC 线类型、失败类别、通知判定
    errors.ts               错误码常量与异常层级（契约异常 / 业务异常 / not-found）
    parser.ts               契约解析：parse 层与 contract 层分离；空批次/单对象/批内元素
  kernel/
    fixtures.ts             合成账户、密钥保险库夹具（纯本地）
    ids.ts                  request/call 关联 ID；RPC id 的无碰撞 JSON 编码
    types.ts                内核对外类型与执行器共享的运行时接口
    support.ts              错误归一化、中断竞速、结果封装等执行支持
    methods.ts              方法注册表：validate（仅产出 invalid_params）/ execute 两段
    call-executor.ts        单调用执行：调用/操作落账、操作号、幂等、异步续体
    engine.ts               编排内核：并发调度、响应归属、通知、请求裁决、中断
  state/
    schema.ts               SQLite 表结构（rpc_requests / rpc_calls / operations）
    store.ts                状态适配器（LedgerStore 端口 + SQLite 实现，条件更新）
    serialize.ts            台账记录的对外投影（只暴露脱敏列）
  diagnostics/
    logger.ts               结构化诊断日志
    queries.ts              接受/拒绝/无法判定的理由解释视图
  transport/
    http.ts                 Fastify：原始文本解析、socket 中断信号、诊断路由
  server.ts                 启动引导
test/
  unit/                     parser / kernel / operations / state / redact 单测
  integration/              Fastify inject 与真实 TCP 连接中断测试
  support/                  测试夹具（内存 SQLite + 事件捕获日志）
```

## 3. 关键取舍

1. **RPC ID 只是传输关联值，绝不是全局幂等键。** 副作用拥有独立的、单调递增的 `opSeq`；
   幂等只接受客户端显式传入的 `idempotencyKey`（按 `kind + key` 作用域）。重复 RPC ID 合法，
   每个位置独立应答，诊断日志记录 `duplicate_rpc_id_accepted` 但不拒绝。
2. **幂等只回放“成功”的原始结果**；失败尝试不毒化重试。同 key 并发在进程内串行化，
   保证恰好一次副作用（测试有并发用例）。
3. **参数校验先于操作号分配**：`invalid_params` 不会留下副作用行；业务错误（如余额不足）
   才会产生 `failed` 操作。
4. **异步方法**立即接受（`{accepted:true, opSeq, status:"pending"}`），后台续体只更新台账，
   终态通过 `operations.get` 查询；异步通知失败同样落账但没有响应。
5. **连接中断 = undecidable（无法判定）**，而不是成功或失败：请求标记为 `undecidable/interrupted`，
   仍在 pending 的调用与操作被终态标记为 `interrupted`；已完成的结果不回滚、不被晚到的续体覆盖
   （finish 语句带 `status='pending'` 条件）。传输层监听 **socket** 的 `close`
   （Node 中 request 的 `close` 在 body 读完时就会触发，不能用于判活），中断后销毁套接字而非回送状态码。
6. **HTTP 状态码**：parse error 返回 500（JSON-RPC 规范建议），契约层无效请求 400，
   正常批次与单请求 200，纯通知 204；批内元素错误始终体现在报文体中而非整体 HTTP 状态。
7. JSON-RPC 允许的批内乱序：本服务并发执行（乱序完成真实存在，测试用独立观察者记录完成顺序），
   但按规范建议按请求顺序返回响应数组。
8. 顶层标量 JSON（如 `4`、`"x"`）归类为 `invalid_request` 契约失败，而非 parse error：
   语法合法但不是 Request 对象。

## 4. 本地启动

要求 Node.js ≥ 22.5（使用内置 `node:sqlite`，无需原生编译）。

```bash
npm ci              # 依据 package-lock.json 安装锁定依赖
npm run typecheck   # tsc --noEmit
npm test            # vitest run（44 个测试）
npm run coverage    # 覆盖率（门槛：语句/分支/函数/行均 80%）
npm run build       # 输出 dist/
npm start           # 默认 127.0.0.1:3000，SQLite 在 data/service.db
```

开发模式：`npm run dev`（tsx watch）。

环境变量：`HOST`（默认 127.0.0.1）、`PORT`（默认 3000）、
`DB_PATH`（默认 `data/service.db`，测试用 `:memory:`）、`LOG_LEVEL`。

## 5. 示例请求

另见 `examples/example.sh`（可直接执行：`bash examples/example.sh`）。

```bash
# 单请求
curl -s -X POST http://127.0.0.1:3000/ -H 'content-type: application/json' -d '{
  "jsonrpc":"2.0","method":"math.add","params":{"a":2,"b":3},"id":1
}'

# 混合批次：请求 + 通知 + 非法元素（通知无响应，非法元素单独报错且 id 为 null）
curl -s -X POST http://127.0.0.1:3000/ -H 'content-type: application/json' -d '[
  {"jsonrpc":"2.0","method":"math.add","params":{"a":1,"b":1},"id":"a"},
  {"jsonrpc":"2.0","method":"secrets.put","params":{"name":"k","secret":"abc"}},
  7
]'

# 空批次 → 400 + -32600（区别于全通知的 204）
curl -i -X POST http://127.0.0.1:3000/ -H 'content-type: application/json' -d '[]'

# 重复 ID：两个响应都带同一个 id，但结果各自归属、互不串用
curl -s -X POST http://127.0.0.1:3000/ -H 'content-type: application/json' -d '[
  {"jsonrpc":"2.0","method":"math.add","params":{"a":1,"b":2},"id":"dup"},
  {"jsonrpc":"2.0","method":"math.add","params":{"a":100,"b":200},"id":"dup"}
]'

# 显式幂等键：重复提交只产生一次副作用，第二次 replayed=true 且复用同一 opSeq
curl -s -X POST http://127.0.0.1:3000/ -H 'content-type: application/json' -d '{
  "jsonrpc":"2.0","method":"accounts.transfer",
  "params":{"from":"acc-1","to":"acc-2","amount":300,"idempotencyKey":"tx-001"},"id":10}'

# 异步操作：先接受，再轮询
curl -s -X POST http://127.0.0.1:3000/ -H 'content-type: application/json' -d '{
  "jsonrpc":"2.0","method":"tasks.schedule","params":{"name":"job","delayMs":100},"id":11}'
curl -s -X POST http://127.0.0.1:3000/ -H 'content-type: application/json' -d '{
  "jsonrpc":"2.0","method":"operations.get","params":{"opSeq":1},"id":12}'

# 诊断
curl -s http://127.0.0.1:3000/diag/requests | head
```

## 6. 测试与运行记录

- 测试框架：Vitest（单元 + Fastify inject 集成 + 真实 TCP 套接字中断）。
- 断言均为**具体结果值与失败类别**，期望值为测试内字面量，不由被测核心生成；
  乱序测试通过测试侧独立观察者记录真实完成顺序；中断测试使用原始 `node:net` 套接字。
- 最终运行结果（Node v22.23.3）：**5 个测试文件、44 个测试全部通过，0 失败、0 跳过**；
  覆盖率：语句 89.26% / 分支 86.96% / 函数 82.75% / 行 89.26%（均高于 80% 门槛）。
  明细见 `test-report.md`。
- 开发过程中出现并已修复的真实问题（保留记录）：
  1. 误把 Node `IncomingMessage` 的 `close`（body 读完即触发）当作断连，导致正常请求被记为
     interrupted —— 改为监听 socket close 并加 `writableFinished` 守卫；
  2. 初版中断后回送 HTTP 499，客户端会收到“响应” —— 改为销毁套接字；
  3. 两处测试期望写错（转账余额方向、并发用例漏了 `id` 退化成通知），已按真实契约修正；
  4. request 行缺少 snake_case→camelCase 映射，已补 `mapRequest`。
- 当前无未执行/未覆盖的已知项；`node:sqlite` 在 Node 22 仍带 ExperimentalWarning，
  启动脚本以 `--disable-warning=ExperimentalWarning` 抑制，属预期现象。
