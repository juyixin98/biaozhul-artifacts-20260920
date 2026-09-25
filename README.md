# 批处理聚合调度（batchagg）

纯后端的推理请求批处理聚合调度库 + 本地 HTTP 接口。它把按**兼容键**（compatibility key）
划分的请求聚合成批，在「条数、字节数、等待时间」三个约束任一满足时触发下发；
调度时钟与执行器均可替换，所有状态变更输出结构化事件。后端是一个**推理请求模拟器**，
支持批内单项独立失败，用于验证聚合行为。

> 本项目不包含任何前端代码。

## 目录结构

```
.
├── go.mod
├── clock.go            # Clock/Timer 抽象；SystemClock 与可确定性推进的 VirtualClock
├── events.go           # Event 结构、EventSink、内存/JSON-lines/slog 等事件记录器
├── scheduler.go        # 核心调度器：按 key 聚合、三种刷批条件、取消、关闭排空、Future
├── simulator.go        # Executor 实现：推理请求模拟器（单项独立失败）
├── httpapi/
│   └── server.go       # 本地 HTTP 接口：POST /infer、GET /events(SSE)、/healthz
├── cmd/batchagg/
│   └── main.go         # 可运行服务（参数化阈值 + 优雅关停）
├── examples/           # 请求样例 JSON 与 curl 脚本
└── *_test.go           # 自动化测试（虚拟时钟验收用例 + HTTP 测试 + 并发压力测试）
```

## 编译与运行

需要 Go 1.22+，无第三方依赖。

```bash
go build ./...
go run ./cmd/batchagg \
  --addr 127.0.0.1:8080 \
  --max-items 8 \
  --max-bytes 65536 \
  --max-wait 50ms \
  --event-log -            # 可选：'-' 表示 stdout，或给一个文件路径输出 JSON Lines
```

参数：

| 参数 | 默认值 | 含义 |
|---|---|---|
| `--max-items` | `8` | 批次达到该条数立即下发 |
| `--max-bytes` | `65536` | 批次累计 payload 字节达到该值立即下发；**单个请求超过该值直接拒绝（413）** |
| `--max-wait` | `50ms` | 自建批起的最长等待，超时强制下发（即使只有 1 条） |
| `--event-log` | 空 | 结构化事件额外落盘（JSON Lines） |

## 核心语义

### 兼容键合批

- 每个请求带一个兼容键（HTTP 层取 `key`，缺省用 `model`）。
- **相同键**的请求才可能进入同一批；**不同键永不混批**（每个键一条独立 runner 协程）。
- 一个键的批次严格按到达顺序串行执行；不同键之间完全并行。

### 三个下发条件（满足其一即触发）

1. `items`：批次条数达到 `MaxItems`；
2. `bytes`：批次累计 `len(payload)` 达到 `MaxBytes`；
3. `wait`：距该批第一条请求的等待时间达到 `MaxWait`。

关闭时对尚未下发的批次额外触发一次 `shutdown` 下发，排空该键队列中**已接收**的请求。

### 超大单项明确拒绝

`len(payload) > MaxBytes` 的单项**永远不可能进批**，在 `Submit` 处同步拒绝
（哨兵错误 `ErrOversizeItem`，HTTP `413` + `code:"oversize_item"`），并记录
`rejected` 事件。它不会被拆分、不会进队、不会影响其它请求。

### 取消只影响对应项

- 请求靠 `context.Context` 关联。入队后、下发前 ctx 结束：只从待发批次中摘除
  **该 ID 的这一项**，发出 `item_canceled` 事件，其 Future 返回 `context.Canceled`；
  同批其它项照常聚合/下发。
- 批次**一旦下发**（已交给 Executor），取消不再影响它：真实执行结果仍然返回
  （HTTP 客户端断开只断开 HTTP 等待，不会撤下执行中的项）。
- 摘光某批全部项时，该空批以原因 `cancel` 关闭，不调用执行器。

### 批内部分失败 / 独立结果

执行器契约（`Executor.Execute`）：

- 返回整批 `error`：批内**每一项**独立收到该错误（整批失败）。
- 返回与入参等长、同序的 `[]*ItemResult`：每个结果自带自己的 `Err`；
  一个结果 `Err != nil` **只让这一项失败**，兄弟项照常成功（**部分失败**）。
- 返回结果数与请求数不符等协议违背，按整批失败处理，错误信息里带批次号。

模拟器（`Simulator`）逐项解码 JSON：坏 JSON / 空 prompt / `"fail": true`
都只让对应项失败（`ErrSimulatedInference`），其余项正常产出。**每个请求都能
独立读到自己的结果、批内序号 `index` 与 `batch_id`。**

`Future.Get` 的错误与单项执行结果刻意分开：执行结果（含单项失败）在
`ItemResult.Err`；Go 的 error 只用于「没有结果」的情况（入队前被取消等）。

## 结构化事件

每次状态迁移恰好一条 `Event`（JSON 可序列化）：

| `type` | 触发时机 | 关键字段 |
|---|---|---|
| `submitted` | 单项进入某键的当前批次 | `key,item_id,batch_id,items,bytes` |
| `rejected` | 超大项/空 key/关停后提交 | `reason=oversize|empty_key|closed,bytes,max_bytes` |
| `batch_open` | 为某键新建批次（启动等待计时） | `batch_id,max_items,max_bytes` |
| `batch_flush` | 批次交给执行器 | `reason=items|bytes|wait|shutdown|cancel,items,bytes` |
| `item_canceled` | 单项在下发前被取消 | `item_id,batch_id,err` |
| `item_result` | 单项得到独立结果 | `item_id,batch_id,success,err` |

库提供 `EventRecorder`（测试用内存记录）、`JSONLinesSink`、`SlogSink`、
`MultiSink`；实现 `EventSink` 接口即可接入自己的记录后端。事件在对应键的
runner 协程内同步发出，`Sink` 实现需并发安全、不得回堵调度器。

## HTTP 接口

| 方法与路径 | 说明 |
|---|---|
| `POST /infer` | 提交一条推理请求；连接挂起到该项得到独立结果后返回 |
| `GET /events` | SSE 实时推送结构化事件（`event: <type>` + `data: <json>`，15s keep-alive） |
| `GET /healthz` | 存活探针 |
| `GET /` | 服务与端点描述 |

`POST /infer` 请求体（`key` 与 `model` 至少给一个用于确定兼容键）：

```json
{
  "key": "model-a",
  "model": "sim-llm-1",
  "prompt": "hello",
  "max_tokens": 16,
  "temperature": 0.7,
  "fail": false,
  "failure_code": ""
}
```

成功响应 `200`：

```json
{
  "request_id": "req-1",
  "item_id": "req-1-item",
  "batch_id": "batch-3",
  "index": 0,
  "output": {
    "model": "sim-llm-1",
    "prompt_chars": 5,
    "echo": "hello",
    "tokens_out": 16,
    "batch_id": "batch-3",
    "index": 0
  }
}
```

状态码约定：`200` 成功；`400` JSON 非法/缺少 key；`413` 单项超 `MaxBytes`
（或超出 HTTP body 上限，`code` 区分）；`502` 该项执行失败（单项失败同样如此，
兄弟项可能仍是 `200`）；`503` 调度器已关闭；`499` 客户端在等待结果期间断开；
`490` 项在下发前被取消。所有错误体形如 `{"error": ..., "code": ..., "request_id": ...}`。

### curl 示例

```bash
# 一条普通请求
curl -sS localhost:8080/infer -H 'Content-Type: application/json' \
  --data-binary @examples/requests/01_basic.json

# 三个同键请求并发，触发满批（配合 --max-items 3 观察）
for f in examples/requests/0{1,2,3}_*.json; do
  curl -sS localhost:8080/infer -H 'Content-Type: application/json' --data-binary @"$f" &
done; wait

# 另开终端实时观察结构化事件
curl -sS localhost:8080/events
```

`examples/curl_samples.sh` 封装了上述调用（`--concurrent` 并发发送同键请求）。

## 可替换的时钟与执行器

- `Clock` / `Timer` 是接口。生产用 `SystemClock`；测试注入 `VirtualClock`，
  通过 `vc.Advance(d)` 确定性推进时间——定时器只在推进到点时触发，测试里没有
  任何 `sleep` 等待业务时间。
- `Executor` 是接口。生产可接真实推理后端；本仓库交付 `Simulator`，测试里另有
  可配置延迟/整批错误的 fake 执行器。

## 测试

```bash
go test ./...            # 全量
go test -race ./...      # 竞态检测
go test -v -run TestLowTraffic_WaitFlush .
```

关键自动化用例：

- `TestLowTraffic_WaitFlush`：**虚拟时钟**，低流量下条数/字节都不满足，
  仅靠 `wait` 超时发批；逐项核对独立结果、顺序与 `batch_id`。
- `TestHighTraffic_ItemsFlush`：高流量满批**无需推进时钟**立即下发（原因 `items`），
  随后的零头再走 `wait`。
- `TestBytesLimit_FlushesAndSplits`：字节阈值触发与跨批拆分。
- `TestPartialBatchFailure`：同一批内中间一项失败，前后两项成功，三项共享 `batch_id`。
- `TestWholeBatchErrorFailsEachItem`：执行器整批错误时逐项独立收到同一错误。
- `TestOversizeItem_Rejected`：超大项被拒、不入执行器、不影响后续正常项、有拒绝事件。
- `TestCancellation_AffectsOnlyOwnItem` / `..._AfterDispatchDoesNotChangeResult`：
  取消只摘自己一项；下发后取消不改变真实结果。
- `TestDifferentKeys_NeverMixed`、`TestClose_FlushesPending`、
  `TestExecutorResultCountMismatch`：键隔离、关停排空、结果数协议校验。
- `httpapi` 包：真实 HTTP（httptest）覆盖聚合共享 `batch_id`、键隔离、413、
  部分失败隔离、SSE 事件流、坏请求等。
- `TestStress_ConcurrentKeysAndCancellations`：16×100 并发 + 随机取消，
  `-race` 下核对每个请求恰好一种结局（成功/取消）且结果事件一一对应。

## 设计说明与边界

- 队列模型：每键一个带缓冲 channel（1024）+ 一个 runner；队列满时 `Submit`
  施加背压而不是无界增长。
- 计数口径：`MaxBytes` 按 `len(payload)` 原始字节计；HTTP body 上限独立配置，
  建议 ≥ `MaxBytes`，让超大项能到达调度器并拿到语义化 413。
- 模拟器不做真实推理：输出为回显（echo）与元数据，仅用于驱动调度与结果核对。
