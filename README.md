# dagscheduler — 带依赖失败传播的 DAG 作业调度库（Go）

纯后端的可测试 DAG 调度引擎 + 本地 HTTP 接口。调度时钟与执行器（executor）
均可替换；每次状态变更产出结构化事件；支持节点重试预算、失败后区分
**跳过（skipped）**与**失败（failed）**、两种依赖策略，并在提交前做环检测。

无前端、无第三方依赖，仅用 Go 1.22 标准库。

---

## 1. 目录结构

```
.
├── go.mod
├── scheduler/            # 核心调度库（可被任意 Go 程序嵌入）
│   ├── types.go          # 状态、策略、事件类型、Executor 接口
│   ├── dag.go            # DAG 规格、归一化与校验（含环检测）
│   ├── clock.go          # Clock 接口：RealClock + 可控 FakeClock
│   ├── executor.go       # Registry 与内置执行器（noop/fail/flaky/sleep）
│   ├── engine.go         # 调度引擎（单 goroutine 状态机 + 事件日志）
│   └── engine_test.go    # 库级自动化测试
├── server/               # 本地 HTTP 适配层
│   ├── server.go
│   └── server_test.go
├── cmd/server/main.go    # HTTP 服务入口（含结构化事件日志输出）
├── examples/             # 请求样例（JSON 负载）
└── scripts/demo.sh       # 端到端冒烟脚本
```

## 2. 快速开始

```bash
# 构建
go build ./...

# 运行测试（含竞态检测）
go test -race ./...

# 启动 HTTP 服务（默认 :8080，可用 -addr 或环境变量覆盖）
go run ./cmd/server -addr 127.0.0.1:8080
# 另开一个终端，跑端到端演示：
./scripts/demo.sh http://127.0.0.1:8080
```

## 3. 核心模型

### 节点状态机

```
pending ──launch──▶ running ──成功──────────────▶ succeeded
   ▲                  │  │
   │                  │  └─失败且还有预算──▶ waiting ──backoff 到点──┘
   │                  │
   │                  ├─失败且预算耗尽────▶ failed
   │                  └─运行中被取消──────▶ canceled
   │
   ├─上游 failed/skipped 且 policy=all_success ─▶ skipped（从不执行）
   └─运行被取消 / 上游 canceled ────────────────▶ canceled
```

- **failed 与 skipped 严格区分**：`failed` 是节点自己执行并耗尽重试预算；
  `skipped` 是节点一次都没跑，因为 all_success 策略下上游未成功。跳过原因
  记录在 `skip_reason` 字段里（指明是哪个依赖、什么策略）。
- **run 终态规则**：任一节点 failed → run 为 `failed`；否则，只要发生过
  取消（运行被取消或任一节点 canceled）→ `canceled`；全部 succeeded →
  `succeeded`。skipped 节点本身不单独决定 run 状态，它们通常是某个 failed
  节点的下游，此时 run 已因 failed 而为 failed。

### 依赖策略（每个节点单独配置 `policy`）

| 策略 | 含义 | 上游失败时 |
|---|---|---|
| `all_success`（默认） | 所有依赖都成功才运行 | failed/skipped 依赖 → 本节点 **skipped**；canceled 依赖 → **canceled** |
| `all_finished` | 所有依赖到达终态即可运行（无论成败） | 照常运行 |

跳过/取消沿 DAG 做**不动点传播**，因此传递性下游也会被正确标记。

### 重试预算

- `max_attempts`：总尝试次数（含首次），默认 1（不重试）。
- `backoff`：两次尝试之间的等待，接受 `"200ms"` / `"5s"` 等字符串。
- 等待期间节点状态为 `waiting`；此时取消会**停掉 backoff 定时器**，节点直接
  canceled，不会再产生第 N+1 次尝试。
- backoff 由可替换的 `Clock` 计时；测试用 `FakeClock` 手动推进，重试测试
  完全确定性、零真实等待。

### 环检测

`Submit` / `DAG.Validate()` 在启动前执行：空 ID、重复 ID、未知依赖、自依赖、
未知 task_type、非法策略/预算，以及基于 WHITE/GRAY/BLACK 着色的迭代式 DFS
环检测。检测到环会返回形如
`dependency cycle detected: a -> b -> c -> a` 的错误，HTTP 层映射为 400。

## 4. “节点不会重复并发运行”是如何保证的

引擎只有**一个 goroutine 拥有全部 Run/Node 状态**（engine loop）：

1. 节点只有在状态为 `pending` 时才可能被 launch；launch 的同一个持锁步骤里
   立即把状态翻成 `running`、`attempts++`、`inflight=true`，之后才启动执行
   goroutine。执行体永远不直接改引擎状态，只通过带缓冲结果通道回传。
2. 结果到达时还会校验 `inflight && attempts==result.attempt &&
   status==running`，过期/重复结果被丢弃。
3. 重试必须先等前一次 attempt 结束进入 `waiting`，backoff 到点后才回到
   `pending`，不存在两次 attempt 的时间窗重叠。

因此“同一节点永不并发执行”是**结构性保证**，不是靠执行器自律。该不变量由
以下测试主动证伪（任何违反都会失败）：

- `TestNodeNeverRunsConcurrently`：共享根节点 + 4 次零退避重试 + 50 个扇出
  叶子，执行器内置每节点并发计数，>1 即记一次违规；
- `TestConcurrentSubmitsNeverRunANodeTwice`：40 个 goroutine 并发提交同构
  DAG（根节点首败、零退避重试），在 `-race` 下跑多轮；
- 取消相关测试验证 backoff 取消后不再补发尝试。

## 5. HTTP API

基址默认 `http://127.0.0.1:8080`。所有请求/响应均为 JSON。

| 方法 & 路径 | 说明 |
|---|---|
| `GET  /health` | 健康检查 |
| `POST /runs` | 提交 DAG，校验通过立即启动，返回首个快照（201） |
| `GET  /runs` | 列出所有 run（按提交顺序） |
| `GET  /runs/{id}` | 查询单个 run 快照（含每个节点状态） |
| `GET  /runs/{id}/events` | 该 run 的结构化事件日志（按 seq 排序） |
| `POST /runs/{id}/cancel` | 取消 run；body 可选 `{"reason":"..."}` |

错误响应：`{"error":"..."}`；校验错误 400，run 不存在 404，取消已结束的
run 返回 409。

### 请求体（DAG）

```json
{
  "id": "optional-dag-name",
  "nodes": [
    {
      "id": "a",
      "task_type": "flaky",
      "params": {"fail_for": "2"},
      "depends_on": [],
      "policy": "all_success",
      "max_attempts": 3,
      "backoff": "200ms"
    }
  ]
}
```

### 内置 task_type

| task_type | 参数 | 行为 |
|---|---|---|
| `noop` | — | 立即成功 |
| `fail` | `message`（可选） | 恒失败 |
| `flaky` | `fail_for`（默认 1）、`message` | 前 N 次尝试失败，之后成功（演示重试） |
| `sleep` | `duration`（如 `"60s"`） | 睡眠；ctx 取消立即返回，用于演示取消 |

嵌入自己的执行器：实现 `scheduler.Executor` 接口并在 `Registry` 注册即可。

### 请求样例（curl）

```bash
# 提交菱形 DAG
curl -s -X POST localhost:8080/runs -H 'Content-Type: application/json' \
  -d @examples/diamond_success.json

# 查询事件流
curl -s localhost:8080/runs/$RUN_ID/events

# 取消
curl -s -X POST localhost:8080/runs/$RUN_ID/cancel \
  -H 'Content-Type: application/json' -d '{"reason":"manual stop"}'
```

更多负载见 [`examples/`](examples/)，一键演示见
[`scripts/demo.sh`](scripts/demo.sh)。

## 6. 结构化事件

每次状态变更追加一条不可变事件（内存日志 + 可插拔 `Sink`，服务端默认把
事件打印到 stdout）。字段包括全局递增 `seq`、时间戳、run/node id、事件类型、
attempt/max_attempts、状态、backoff、错误文本、跳过/取消原因、启动时各依赖
状态快照。事件类型：

`run_created / run_started / run_succeeded / run_failed / run_canceled`
`node_queued / node_started / node_retry_wait / node_retrying /
node_succeeded / node_failed / node_skipped / node_canceled`

## 7. 作为库嵌入

```go
eng := scheduler.New(registry,
    scheduler.WithClock(myClock),       // 可选：替换时钟
    scheduler.WithSink(mySink),         // 可选：外部事件订阅
)
defer eng.Close()

snap, err := eng.Submit(dag)
final, err := eng.Wait(ctx, snap.ID)    // 或轮询 eng.Get
events, _ := eng.Events(snap.ID)
```

## 8. 实际运行记录

以下命令与结果均在开发环境（Go 1.22.2, linux/amd64）真实执行。完整输出见
[RUNLOG.md](RUNLOG.md)。

- `go test -race -count=3 ./...`：scheduler 与 server 全部用例通过，
  竞态检测无报告；
- `go vet ./...`、`gofmt -l`：无问题；
- 实际启动 HTTP 服务并运行 `scripts/demo.sh`：菱形成功、重试成功（3 次
  尝试）、失败传播（skipped vs all_finished 对照）、环检测 400、取消
  （运行中节点 ctx 取消、未启动节点 canceled）全部符合预期。
