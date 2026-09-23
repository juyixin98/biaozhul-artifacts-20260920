# 批处理聚合调度（Batch Aggregation Scheduler）

一个纯后端项目：用 Go 实现可测试的**批处理聚合调度库**与**本地 HTTP 接口**，
模拟“按兼容键合批的推理请求”场景。调度时钟与执行器均可替换；
每次状态变更输出结构化事件；同时受**条数、字节数、等待时间**三重约束；
超大单项明确拒绝；取消只影响对应项。

- 无前端、无第三方 Go 依赖（仅标准库，Go 1.22+）。
- 所有验收场景均以虚拟时钟（`FakeClock`）做确定性自动化测试，不依赖真实睡眠时序。

## 目录结构

```
.
├── go.mod
├── main.go                 # HTTP 服务入口（flag / 环境变量配置）
├── batch/                  # 核心调度库（不绑定推理语义）
│   ├── clock.go            # Clock 接口 + 真实墙钟 SystemClock
│   ├── fake_clock.go       # 可手动推进的虚拟时钟 FakeClock
│   ├── events.go           # 结构化事件、事件类型、EventSink 接收器
│   ├── errors.go           # ErrItemTooLarge / ErrShuttingDown 等
│   ├── batcher.go          # 泛型 Batcher：按 Key 合批 + 三重约束 + 取消 + 优雅关闭
│   ├── batcher_test.go     # 三个验收场景 + 拒绝/取消/字节/关闭/配置测试
│   ├── fake_clock_test.go  # 虚拟时钟单元测试
│   └── stress_test.go      # 高并发提交/取消守恒压测（-race）
├── inference/              # 推理请求模拟器
│   ├── executor.go         # Request（model 为合批键）+ FakeExecutor（确定性结果/失败）
│   └── executor_test.go
├── server/                 # 本地 HTTP 接口
│   ├── server.go           # POST /v1/infer、SSE、指标、健康检查
│   ├── broadcaster.go      # 事件 -> 多 SSE 订阅者扇出
│   └── server_test.go      # HTTP 端到端测试（httptest 真实服务器）
└── examples/
    ├── request_basic.json
    ├── request_partial_failure.json
    ├── request_rate_limited.json
    ├── request_auto_id.json
    ├── demo.sh             # 一键端到端演示
    └── load.sh             # 简易并发负载脚本
```

## 核心设计

### 合批模型

- 请求实现泛型接口 `batch.Request[T]`：
  - `Key() string`：**合批兼容键**。只有相同 Key（本项目中为模型名 `model`）
    的请求才会进入同一批次；不同 Key 拥有彼此隔离的缓冲、等待窗口与调度循环。
  - `Size() int`：逻辑字节数。
- 每个 Key 一个 goroutine（`runKey`）+ 一个有界缓冲通道，天然避免跨键争用。
- 一个打开的批次满足任一条件立即发批（事件 `batch.flushed` 中 `reason` 标注）：
  1. **条数**达到 `MaxCount`（`max_count`）；
  2. **累计字节**再加入下一项会超过 `MaxBatchBytes`（`max_bytes`，
     旧批先发、新项另开一批）；
  3. 自首批入内起 **等待时间**达到 `MaxWait`（`max_wait`）；
  4. 优雅关闭时排空残留批（`drain`）。

### 超大单项

`Size() > MaxItemBytes` 的请求在进入任何批次之前被拒绝，返回
`*batch.ItemTooLargeError`（包装 `ErrItemTooLarge`，含 size/max），HTTP 层映射为
**413**。它不会入队、不影响同键其它请求、也不会拖垮字节上限。

### 取消语义（只影响对应项）

- 请求在“等待结果”阶段被 `ctx` 取消时，向该 Key 的通道投递一条只含自己 ID 的
  取消消息；调度循环把该条目从当前批中摘除，其余条目照常凑批/执行。
- 批一旦移交给执行器（`closed`），后到的取消一律忽略；执行使用
  `context.Background()`，**单项取消绝不中止整批**。
- 每个条目有且仅有一次终态：`entry.settle()` 用互斥 + 标志保证“取消摘条”与
  “批次交付”竞争时只有一方生效，不会重复交付或覆盖结果。

### 可替换时钟与执行器

- `batch.Clock`：`Now()` 与 `NewTimer()`。生产用 `SystemClock`；
  测试用 `FakeClock`——时间只在显式 `Advance(d)` 时前进，到期定时器同步触发，
  因此“低流量等满 MaxWait 再发批”可以零 flake 地确定性验证。
- `batch.Executor[T]`：`Execute(ctx, items) []ItemResult`，逐项返回成功载荷或
  单项错误（部分失败）。结果数必须与入批条数一致，否则按执行器契约错误处理。
  本项目的实现是 `inference.FakeExecutor`：确定性哈希输出、可注入延迟与单项失败码。

### 结构化事件

每次状态转换调用 `EventSink.OnEvent(Event)`：

| 事件 | 含义 |
| --- | --- |
| `item.rejected` | 单项超 MaxItemBytes，被拒绝 |
| `batch.opened` | 某 Key 新开一批 |
| `item.admitted` | 请求被纳入批（带累计 count/bytes） |
| `item.canceled` | 请求在执行前取消，仅自身被摘除 |
| `batch.flushed` | 批次关闭并提交执行器（带 reason/count/bytes） |
| `item.succeeded` / `item.failed` | 单项执行结果（部分失败可区分到请求 ID） |
| `batch.completed` | 一批执行结束 |
| `drain.started` / `drain.completed` | 优雅关闭起止 |

事件可同时输出到多个接收器（`MultiSink`）：服务默认一份写结构化日志，
一份通过 SSE `GET /v1/events` 实时广播。

### 优雅关闭

`Shutdown(ctx)`：停止接受新请求（新提交返回 `ErrShuttingDown`）→ 各 Key
非阻塞排空已入队消息并把残留批交给执行器 → 等待全部在途批次执行并交付 →
等待所有等待中的 `Submit` 落定。关闭决策与提交入队通过同一把互斥串行化，
保证不丢已入队消息。

## HTTP 接口

| 方法与路径 | 说明 |
| --- | --- |
| `POST /v1/infer` | 提交单个推理请求，阻塞至其所属批次执行完，返回该请求自己的结果 |
| `GET  /v1/events` | SSE 实时事件流（`event: <类型>` + `data: <JSON>`） |
| `GET  /v1/metrics` | 调度器计数、发批原因、执行器批大小序列 |
| `GET  /healthz` | 存活检查 |

### 请求体

```json
{
  "id": "req-basic-1",
  "model": "llm-demo-a",
  "prompt": "你好",
  "simulate_error": "model_error"
}
```

- `model`（必填）：合批兼容键。
- `prompt`（必填）：UTF-8 字节计入 `Size`。
- `id`（可选）：缺省自动分配 `auto-N`；提供时直接作为事件中的 `request_id`。
- `simulate_error`（可选）：`model_error` / `rate_limited` / `content_policy`，
  使该请求在批内单独失败，用于演示部分失败。

### 响应

成功：`200 {"request_id","status":"ok","result":{...}}`
部分失败（单项失败不影响同批其它项，故仍是 200）：

```json
{"request_id":"req-partial-fail","status":"item_error",
 "error":{"code":"model_error","message":"simulated model-side failure"}}
```

超大单项：`413 {"status":"error","error":{"code":"item_too_large",...}}`。
`result` 中含 `batch_seq` / `batch_size`，可用来核对哪些请求被合进了同一批。

## 运行

```bash
# 构建
go build ./...

# 启动（默认 :8080；全部参数同时支持 flag 与 BATCHAGG_ 前缀环境变量）
go run . \
  -addr 127.0.0.1:8080 \
  -max-count 8 \
  -max-batch-bytes 4096 \
  -max-item-bytes 2048 \
  -max-wait 50ms \
  -exec-latency 20ms
```

另开终端：

```bash
# 单请求
curl -X POST http://127.0.0.1:8080/v1/infer \
  -H 'Content-Type: application/json' \
  -d @examples/request_basic.json

# 实时观察事件
curl -N http://127.0.0.1:8080/v1/events

# 一键演示（自动起服务、并发提交、部分失败、413、指标、优雅关闭）
bash examples/demo.sh

# 并发负载（先自行起好服务）
N=50 bash examples/load.sh
```

> 若本机配置了 HTTP 代理，脚本内的 curl 已带 `--noproxy "*"`；
> 手工 curl 访问 127.0.0.1 时请自行加 `--noproxy '*'` 或设置 NO_PROXY。

## 测试

```bash
go test ./...                 # 全部测试
go test -race ./...           # 竞态检测
go test -race -count=50 ./batch/   # 重复跑核心包排查时序 flake
```

### 验收场景 → 自动化用例映射

| 验收要求 | 测试 | 关键断言 |
| --- | --- | --- |
| 低流量超时发批 | `TestAcceptance_LowTrafficTimeoutFlush` | 半窗口不发；越过 MaxWait 后恰好 1 批，`reason=max_wait`，两个请求各自拿到自己的结果 |
| 高流量满批 | `TestAcceptance_HighTrafficFullBatch` | A 模型 8 条→2 个满批、B 模型 5 条→满批+超时尾批；不同 Key 绝不混批；13 个结果逐项按 ID 核对；原因计数 3×`max_count`+1×`max_wait` |
| 批内部分失败 | `TestAcceptance_PartialFailureInBatch` | 同批 2 成功 1 失败；成功项结果正常、失败项拿到自己的错误；事件计数 success=2/failed=1 |
| 超大单项明确拒绝 | `TestOversizedItemRejected` | 返回 `ErrItemTooLarge`（含 size/max），不入批；同键正常请求仍照常成批 |
| 取消只影响对应项 | `TestCancelOnlyAffectsOwnItem` | 被取消方得 `context.Canceled`；同批存活者仍正常成功，且以 1 条的批超时发出 |
| 字节约束 | `TestByteLimitFlushes` | 60+60（上限 100）拆成两批，首批发批 `reason=max_bytes` |
| 优雅关闭排空 | `TestShutdownDrains` | 在途请求在关闭时仍成功完成；关闭后新提交得 `ErrShuttingDown`；残留批以 `drain` 发出 |
| 并发守恒 | `TestStress_ConcurrentSubmitAndCancel` | 16×120 混合提交/取消下 `admitted == succeeded+failed+canceled`，每个请求恰一次终态 |
| 虚拟时钟 | `TestFakeClock_*` | 到期点、Stop、Reset、同刻多定时器顺序 |
| HTTP 端到端 | `server.TestInfer_*` / `TestEventsSSE` | 满批拆分、模型隔离、部分失败响应、413、参数校验、SSE 事件 |

## 实测记录（本仓库开发机上真实执行）

- 环境：Linux x86_64，Go 1.22.2。
- `go test -race -count=20 ./...`：`batch` / `inference` / `server` 三包均 **PASS**
  （核心包另以 `-count=50` 重复运行通过；压测在 `-race` 下守恒成立）。
- `bash examples/demo.sh`：**exit 0**。关键观测：
  - 并发 6 个同模型请求 → 批大小 `[4, 2]`，前 4 个 `batch_seq=1`（满批），
    后 2 个 `batch_seq=2`（超时）；
  - 部分失败请求返回 `status=item_error, code=model_error`；同批正常请求 `status=ok`；
  - 5000 字节提示词 → `HTTP 413`，`item_too_large: size=5074 max=2048`；
  - 指标：`submitted=8 admitted=8 rejected=1 flushed=4 succeeded=7 failed=1`，
    发批原因 `max_count=1, max_wait=3`；
  - SSE 依次可见 `ready / batch.opened / item.admitted / batch.flushed ...`；
  - SIGTERM 后 `drain.started → drain.completed`，最终 `keys=0`，无残留。
- `N=50 bash examples/load.sh`（`max-count=8`）：50 请求 122ms 内完成，
  聚合为 7 批，批大小 `[8,8,8,8,8,8,2]`，原因 `max_count=6, max_wait=1`，
  `succeeded=50 failed=0`。

开发过程中曾真实遇到并修复的问题（均已被回归测试覆盖）：
根路径路由模式在 Go 1.22 下应使用 `GET /{$}`（否则会遮蔽 `/v1/...` 导致 404）；
关闭协议中 WaitGroup 的 Add/Wait 竞态与“排空等待发送者退出”的互锁；
取消与批次交付并发时的重复交付；以及本机 HTTP 代理导致 curl 打到代理的 404
（脚本统一加 `--noproxy`）。
