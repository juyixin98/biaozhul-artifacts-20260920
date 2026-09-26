# canceltree — 请求取消传播（纯后端）

一个完全本地、自包含的 Go HTTP 服务，用于演示和验证**请求取消树**：

- 一个 HTTP 请求扇出（fan-out）为多个**兄弟子任务**；
- **首个致命失败（fatal）取消其余所有子任务**，但仍**等待每个子任务的有界资源清理完成**后才返回；
- **客户端断开连接**会沿同一棵取消树终止剩余计算（包括在途 HTTP 调用与本地等待），清理同样必须完成；
- 每个子任务支持独立的**超时**（由可注入时钟驱动，可在测试中确定性地制造“响应 vs 取消”竞争）。

所有“外部依赖”都是**本进程内的假服务**（可阻塞、可注入失败、可重置连接），不连接任何生产系统。无前端。

## 为什么这样设计

取消传播只用 Go 标准库原语实现，没有引入第三方框架：

- 取消信号 = `context.Context` 派生树。根 context 由 `net/http` 管理，客户端 TCP 断开时自动取消。
- 兄弟任务挂在一个 `context.WithCancel(root)` 的子 context 上；首个 fatal 失败调用其 `cancel()`，所有兄弟的在途 HTTP 请求（`http.NewRequestWithContext`）被真正拆除——服务端能观察到断连。
- 清理动作使用 `context.WithoutCancel(root)` **脱离已取消的请求 context**，再套一个**独立的有界 deadline**，所以“取消不丢清理”，同时“卡死的清理也不会泄漏请求”。
- 时间来自一个极小的 `clock.Clock` 接口：生产用真实时钟，测试用 `clock.Fake`（手动推进），从而确定性地复现超时/取消/清理之间的竞争。

## 目录结构

```
cmd/canceltree/         程序入口（信号驱动的优雅关停）
internal/clock/         时钟接口：Real + Fake（可手动推进、可 Stop）
internal/upstream/      进程内可控假服务：阻塞/释放/失败/连接重置/计数
internal/client/        故障注入 HTTP 客户端（latency/stall/error/reset，有界重试，连接计数）
internal/tree/          取消树引擎：扇出、fatal 取消兄弟、断开传播、有界清理
internal/server/        HTTP API、嵌入式假上游（/upstream）、诊断端点、关停
internal/testutil/      异步断言与 goroutine/堆基线（泄漏检测）
examples/               请求样例（可直接 curl）
RUN_LOG.md              实际运行的命令、结果与如实记录的问题
```

## 构建与运行

需要 Go 1.22+（开发环境：go1.22.2 linux/amd64）。

```bash
go build ./...
go run ./cmd/canceltree -addr 127.0.0.1:8080
# 或
go build -o bin/canceltree ./cmd/canceltree
./bin/canceltree -addr 127.0.0.1:8080
```

参数：`-addr`（监听地址，默认 `127.0.0.1:8080`）、`-attempts`（客户端默认尝试次数，默认 1）。
`Ctrl+C` / `SIGTERM` 或 `POST /shutdown` 触发优雅关停。

## API

### `POST /api/v1/process`

执行一棵取消树。相对 URL 按请求自身 Host 解析（因此可跑在任意端口）。

请求体字段（时长均为**毫秒**）：

| 字段 | 说明 |
|---|---|
| `request_id` | 可选，缺省自动生成 |
| `tasks[].id` | 必填，唯一 |
| `tasks[].fatal` | 该任务失败是否为“致命”，默认 false |
| `tasks[].timeout_ms` | 子任务超时；超时取消该任务且整树判失败 |
| `tasks[].call.method` | 默认 GET |
| `tasks[].call.url` | 必填；相对路径指向内置假上游 `/upstream/...` |
| `tasks[].call.attempts` | 传输错误时的尝试次数（受 ctx 约束，取消即停） |
| `tasks[].call.fault.kind` | `""` / `latency` / `stall` / `error` / `reset` |
| `tasks[].call.fault.delay_ms` | `latency` 的等待时长 |
| `tasks[].cleanup.url` | 子任务结束后调用的清理地址（脱离请求 ctx） |
| `tasks[].cleanup.timeout_ms` | 清理的独立 deadline（缺省 2s） |

HTTP 状态：全成功 `200`；有失败/超时 `503`；客户端断开时响应无法送达（客户端收到断连错误）。

### 内置假上游 `/upstream`

- `GET /upstream/work?delay=20ms` 工作后返回 200
- `GET /upstream/work?hold=1&hold_id=X` 阻塞，直到释放
- `GET /upstream/work?fail=1&status=503` 注入失败响应
- `GET /upstream/reset` 劫持并直接关闭 TCP 连接（传输级错误）
- `POST /upstream/cleanup` 资源清理占位，返回 204
- `GET /upstream/stats` 假服务计数
- `POST /api/v1/release?id=X`（或 `id=all`）释放阻塞调用

### `GET /api/v1/diagnostics`

返回 goroutine 数、`runtime.MemStats`、客户端连接计数（累计建立/当前打开/**在途调用**）、假上游计数（启动/完成/失败/**被取消**/**清理次数**/在途/峰值/活跃 hold）、请求计数。用于泄漏观测。

## 请求样例

```bash
curl -s -X POST localhost:8080/api/v1/process \
  -H 'Content-Type: application/json' -d @examples/01-success.json

# 致命失败：两个 hold 兄弟会被取消，且各自 cleanup 仍执行
curl -s -X POST localhost:8080/api/v1/process \
  -H 'Content-Type: application/json' -d @examples/02-fatal-cancels-others.json

# 客户端断开终止剩余计算（stall 永不自行返回，只能靠断开）
curl -s -X POST localhost:8080/api/v1/process \
  -H 'Content-Type: application/json' -d @examples/05-client-stall-fault.json --max-time 0.3
```

样例清单见 `examples/`：成功、fatal 取消兄弟、非 fatal 失败不取消、任务超时、客户端 stall 故障、延迟+连接重置。

## 取消树语义（结构化结果）

响应中的每个 `outcomes[]` 给出：`status`（`succeeded`/`failed`/`canceled`）、`canceled_by`
（`fatal-sibling` / `client-disconnect` / `task-timeout`）、`result`（状态码、尝试次数、
延迟、故障、错误文本）、以及 `cleanup_ran` / `cleanup_error` / `cleanup_ms`。顶层
`status` 为 `succeeded` / `failed` / `canceled`，fatal 时附 `fatal_task_id`。

竞争规则：fatal 失败与客户端断开同一窗口发生时，**根断开优先**做最终分类（fatal 任务
仍在 `outcomes` 中可见）；无论谁先发生，所有任务恰好结算一次，所有清理都执行。

## 测试

```bash
go test ./...                      # 全量
go test -race ./...                # 竞态检测（验收默认方式）
go test -race -count=10 ./internal/tree   # 重复运行取消/竞争用例
go test -cover ./...               # 覆盖率
go vet ./... && gofmt -l .         # 静态检查与格式
```

测试分层覆盖：假时钟触发顺序与 Stop、假服务阻塞/释放/取消计数/连接重置、客户端各故障与
有界重试、引擎的 fatal 取消/非 fatal 不取消/断开传播/超时/清理有界/重复断开泄漏、以及通过
`httptest.Server` 的端到端响应与取消竞争。泄漏用例在强制 GC 前后比较 goroutine 数与堆，并
断言在途调用、上游在途请求、活跃 hold 归零，TCP 连接不持续增长。

覆盖率（`go test -cover ./...`，内部逻辑包）：client 90.6%、clock 82.0%、server 82.8%、
tree 93.0%、upstream 82.7%。命令入口另有启停/端口占用冒烟测试。

## 泄漏口径（重要）

- **在途 HTTP 调用、上游在途请求、活跃 hold、goroutine**：请求结束后必须归零/回落，测试强断言。
- **TCP 连接**：生产客户端默认使用有界 keep-alive 连接池，完成的连接会作为**空闲连接**短暂留在
  池中（`diagnostics.connections.open`），受 `MaxIdleConnsPerHost` 限制、随空闲超时回收，
  **不随请求数持续增长**（实测 10 轮断开后稳定在池上限）。测试通过 `DisableKeepAlives`
  选项让每次调用独立连接，从而断言连接数严格归零。这是有意区分的两种口径。

## 边界与非目标

- 纯后端：无任何前端资源。
- 假服务是同进程 HTTP（生产形态的忠实近似：取消真的会拆 TCP、对端真的能看到断连），但不是
  跨进程/跨主机部署；不接生产系统、不做服务发现/鉴权。
- 清理是单次、有界的 HTTP 调用，不实现清理队列/持久化重试。
