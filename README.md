# retrybudget — 重试预算传播（纯后端演示）

多层调用链上的**统一重试预算**与**截止时间传播**。根客户端为整棵调用树分配一个重试预算，
每一层的每次尝试（含首次）都从同一个计数器中扣减；任一层收到下游故障时，只有在操作被
显式标记为**可安全重试**（幂等）且预算与截止时间都允许时才重试。外部依赖全部为本进程内的
假服务，不接触任何生产系统。

调用链：`client -> layer1 -> layer2 -> fake 外部服务（进程内）`

## 设计要点

- **统一预算**：根预算通过 `X-Retry-Budget-Id` 关联到进程内 `BudgetStore` 中的共享原子计数器
  （多进程部署时它代表 Redis 一类的共享存储）。每层每次出站尝试 `TryAcquire` 一次，
  因此整棵树的尝试总数严格 ≤ 根预算——避免了"每层各自计数"导致的重试放大
  （本仓库测试曾复现：预算 5 时总尝试数膨胀到 25）。
- **截止时间传播**：`X-Deadline-Unix-Milli` 携带绝对截止时间；若 当前时间+退避等待 ≥ 截止时间，
  则不再重试，返回 `deadline_exceeded`。
- **仅安全操作可重试**：`X-Idempotent: true` 才会重试；缺省/为 false 时，503/传输错误都直接终局返回。
- **指数退避 + 可注入随机源**：`retry.Backoff{Base, Max, Multiplier, Rand}`，
  `Rand` 为可注入的 `*rand.Rand`（等量抖动，落在 [d/2, d]），测试用种子确定性复现。
- **可控时钟**：`clock.Clock` 接口（`Now`/`Sleep`），`clock.Fake` 手动推进，
  单元测试零真实等待；`Sleep` 响应 context 取消。
- **服务端 Retry-After**：503/429 响应的 `Retry-After`（delta-seconds 或 HTTP-date）优先于本地退避。
- **故障注入**：假服务识别 `X-Fault-*` 头，按 `X-Fault-Id` 维度记录有状态故障计划。

## 传播头

| 头 | 方向 | 含义 |
|---|---|---|
| `X-Retry-Budget-Id` | 下行 | 调用树共享预算的 ID（查 `BudgetStore`） |
| `X-Retry-Budget-Remaining` | 下行 | 剩余尝试数快照（无共享存储时的回退） |
| `X-Deadline-Unix-Milli` | 下行 | 端到端绝对截止时间 |
| `X-Idempotent` | 下行 | 操作是否可安全重试 |
| `Retry-After` | 上行 | 服务端指定的重试等待 |
| `X-Fault-Id` / `X-Fault-Fail-Times` / `X-Fault-Status` / `X-Fault-Retry-After` / `X-Fault-Hang-Ms` | 下行 | 假服务故障注入 |

## 结构

```
cmd/server/        本地 HTTP 服务（POST /call 进入三层链）
cmd/client/        故障注入场景运行器，输出结构化 JSON 结果
internal/clock/    可控时钟（Real / Fake）
internal/retry/    预算、退避、重试循环、传播头
internal/chain/    层处理器、进程内 RoundTripper、尝试计数器
internal/fakesvc/  进程内假外部服务（有状态故障注入）
internal/scenario/ 三层故障夹具 + 结构化结果
examples/          curl 请求样例
```

## 运行

```bash
go test ./...          # 自动化测试（含 -race 通过）
go run ./cmd/client    # 运行 6 个验收场景，输出 JSON；任一失败退出码为 1
go run ./cmd/server -addr 127.0.0.1:8080 -budget 8
```

请求样例见 [examples/requests.sh](examples/requests.sh)。核心示例：

```bash
# 正常链路
curl -X POST localhost:8080/call -H 'X-Idempotent: true'
# 注入"失败 2 次后恢复"：重试后 200
curl -X POST localhost:8080/call -H 'X-Idempotent: true' -H 'X-Fault-Id: a' -H 'X-Fault-Fail-Times: 2'
# 持续故障：预算耗尽 -> 429 {"error":"budget_exhausted"}
curl -X POST localhost:8080/call -H 'X-Idempotent: true' -H 'X-Fault-Id: b' -H 'X-Fault-Fail-Times: 100'
# 非幂等：不重试，直接 503
curl -X POST localhost:8080/call -H 'X-Idempotent: false' -H 'X-Fault-Id: c' -H 'X-Fault-Fail-Times: 100'
# 服务端 Retry-After: 1 被遵守（总耗时 ~1s）
curl -X POST localhost:8080/call -H 'X-Idempotent: true' -H 'X-Fault-Id: d' -H 'X-Fault-Fail-Times: 1' -H 'X-Fault-Retry-After: 1'
```

## 验收场景（cmd/client 输出结构化 JSON）

| 场景 | 验证点 |
|---|---|
| `happy_path` | 无故障，3 跳各 1 次尝试，200 |
| `transient_failure` | 假服务失败 2 次后恢复，重试成功，总数 ≤ 预算 |
| `retry_after` | 503 + Retry-After 被解析并作为等待时间 |
| `budget_exhaustion` | 持续 503，根预算 5 → 总尝试数恰好 5，结局 `retry budget exhausted` |
| `cancellation` | 假服务挂起，客户端取消 → 不再产生新尝试，结局 `context canceled` |
| `non_idempotent_no_retry` | 未标幂等，503 直接终局，每跳仅 1 次尝试 |

另有 `TestThreeLayerBudgetBound` 对预算 1..6 扫描验证：持续故障下三层总尝试数恒等于根预算。

## 实际运行记录（2026-09-25，go1.22.2 linux/amd64）

- `go vet ./...` — 通过。
- `go test -race -count=1 ./...` — 全部通过（chain/clock/retry/scenario 四包 ok）。
- 覆盖率：chain 82.7%、clock 78.8%、retry 82.9%、scenario 94.5%；
  fakesvc 无包内测试（由 scenario 集成覆盖，包内覆盖率显示 0.0%）。
- `go run ./cmd/client` — 6/6 场景通过，退出码 0。
- 服务器 curl 实测：正常链路 200；瞬时故障重试后 200；持续故障 429 `budget_exhausted`；
  非幂等 503 不重试；`Retry-After: 1` 时总耗时 1.0007s。
- 开发中由测试发现并已修复的问题：① 预算逐层复制导致重试放大（25 > 5），改为
  `BudgetStore` 共享计数器；② 进程内直传时请求体为 nil 导致 panic；③ 假时钟测试竞态；
  ④ 截止时间测试混用假时钟与真实 context 截止时间。
- 未通过项：无。

## 限制

- `BudgetStore` 为进程内 map，多进程部署需替换为共享存储；`X-Retry-Budget-Remaining`
  头部作为无共享存储时的回退（此时只能逐层近似约束，无法严格全局封顶）。
- 预算语义为"总尝试数上限"，不含并发超额保护之外的速率限制。
