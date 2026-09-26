# cbhalfopen — 断路器半开竞争实验场

一个**纯后端**的 Go 项目，用于确定性地复现和验证断路器（circuit breaker）在
半开（half-open）阶段的三类棘手问题：

1. **旧代请求迟到** —— 请求在旧的断路器代际（generation）发出，结果在断路器
   已经断开、探测、甚至完全恢复后才返回。旧结果**不能**改变新代际的状态与计数。
2. **半开探测名额竞争** —— 半开时只允许有限数量的探测请求在飞；名额满后新
   请求快速失败而不是排队；完成的探测释放名额；断路器在仍有探测在飞时关闭，
   这些探测随后变成旧代际结果并被丢弃。
3. **取消不是服务失败** —— 调用方主动取消（context canceled）只释放许可/探测
   名额，记为 `canceled`，**绝不**记为 failure，因此既不会触发跳闸，也不会
   判负一次探测；超时（deadline exceeded）才算服务失败。

所有外部依赖都是**本进程内假服务**（in-process fake），时钟是**手动推进的
虚拟时钟**，不连接任何生产系统，也没有前端。

---

## 它如何保证确定性

| 机制 | 位置 | 作用 |
|---|---|---|
| 虚拟时钟 | `internal/vclock` | 时间只在调用 `Advance` 时前进；定时器按虚拟时间触发，零真实等待 |
| 虚拟截止时间 context | `vclock.ContextWithTimeout` | “100ms 超时”用虚拟时间衡量，goroutine 真实阻塞但由 `Advance` 唤醒 |
| 进程内假上游 | `internal/fakeupstream` | 可控立即成功 / 500 / 传输错误 / **挂起（hang）直到显式释放** |
| 故障注入客户端 | `internal/faultclient` | 虚拟超时、接下来 N 次失败、每 N 次失败；区分取消/超时/失败 |
| 代际标记许可 | `internal/breaker` | 每个 permit 带签发时的 generation；旧代结果只计 `stale_results` |
| 有限探测名额 | `internal/breaker` | 半开时 `HalfOpenMaxProbes` 个在飞名额，满了返回 `ErrProbesExhausted` |
| 滑动样本窗口 | `internal/breaker` | 保留最近 N 个已完成（非取消）样本，失败率达到阈值才跳闸 |

挂起的调用跑在真实 goroutine 上、park 在 channel 上；**逐次启动并等待其在
假服务中注册**，使假服务分配的调用 ID 与启动顺序一致，因此“释放最旧的挂起
调用”是确定的——场景脚本重复运行结果完全相同。

## 目录结构

```
cmd/demo/                HTTP 服务 / 场景报告 入口
internal/vclock/          虚拟时钟 + 虚拟截止时间 context
internal/breaker/         断路器核心（滑动窗口/状态机/代际/名额）
internal/fakeupstream/    进程内可控假上游（Call + http.Handler）
internal/faultclient/     故障注入客户端与结果分类
internal/demoapp/        本地 HTTP 服务（接线 + 管理端 API）
internal/scenario/        4 个验收场景脚本与结构化报告
```

## 快速开始

需要 Go 1.22+。

```bash
# 跑自动化测试（含竞态检测）
go test -race ./...

# 直接运行 4 个验收场景（文本报告）
go run ./cmd/demo scenarios

# 结构化 JSON 报告
go run ./cmd/demo scenarios --json > report.json

# 启动本地 HTTP 服务（默认 :8080）
go run ./cmd/demo serve
```

## 验收场景

| 名称 | 验证内容 |
|---|---|
| `late_failure_from_old_generation` | gen0 调用挂起 → 断路器被其他失败流量打开 → 冷却 → 两次探测成功恢复 closed(gen3) → 旧调用此时才失败：状态仍 closed、gen3，失败计数不动，`stale_results=1` |
| `half_open_probe_competition` | 3 个探测占满名额；第 4 个被快速拒绝（`probes_rejected=1`）；探测成功释放名额后竞争者复用；断路器关闭后仍在飞的探测变 stale |
| `full_recovery_lifecycle` | closed→open（冷却期内 4 次拒绝）→half_open（2 次探测）→closed，窗口重置，健康流量恢复 |
| `canceled_is_not_failure` | closed 与 half_open 两种状态下取消调用：释放名额但 `failures` 不增加、不跳闸、不改变连续成功计数；随后上游对该调用的迟到失败也不改变任何计数 |

### 一次真实运行（本仓库实际执行）

命令：`go run ./cmd/demo scenarios`

```
[PASS] late_failure_from_old_generation
  counters: map[allowed:8 canceled:0 failures:5 probes_granted:2 probes_rejected:0 rejected:0 stale_results:1 successes:2]
[PASS] half_open_probe_competition
  counters: map[allowed:9 canceled:0 failures:5 probes_granted:4 probes_rejected:1 rejected:1 stale_results:1 successes:3]
[PASS] full_recovery_lifecycle
  counters: map[allowed:10 canceled:0 failures:5 probes_granted:2 probes_rejected:0 rejected:4 stale_results:0 successes:5]
[PASS] canceled_is_not_failure
  counters: map[allowed:9 canceled:2 failures:5 probes_granted:3 probes_rejected:0 rejected:0 stale_results:0 successes:2]
SCENARIOS: 4    PASS: 4    FAIL: 0
```

## HTTP API

服务启动后可用 curl 驱动完整的时间线（见 `examples/requests.md`）：

| 方法与路径 | 作用 |
|---|---|
| `GET /healthz` | 健康检查 |
| `POST /call` | 同步发起一次经断路器的调用（受虚拟时钟约束，hang 时会阻塞到被释放） |
| `POST /call/async` | 异步发起调用，返回 `id`（用于挂起/取消场景） |
| `POST /call/{id}/cancel` | 取消该异步调用 |
| `GET /calls` | 查看异步调用及其结果 |
| `GET /state` | 断路器完整快照（状态、代际、窗口、计数） |
| `GET /transitions` | 状态变迁历史 |
| `GET /clock` · `POST /clock/advance` | 查看/推进虚拟时钟（`{"ms":5000}`） |
| `POST /upstream/mode` | 设置假上游模式 `ok`/`fail`/`error`/`hang` |
| `GET /upstream/pending` | 当前挂起的调用 ID |
| `POST /upstream/release` | 释放挂起调用（`{"mode":"fail","id":1}` 或 `{"all":true}`） |
| `GET /upstream/attempts` | 假上游调用日志 |
| `GET/POST /client/config` | 故障注入配置（超时、FailNextN、FailEveryNth） |
| `POST /reset` | 重置断路器 |

## 断路器判定规则

- **滑动窗口**：保留最近 `SlidingWindowSize` 个已完成样本（取消的不入窗）。
  当样本数 ≥ `MinRequests` 且 `失败数 / 已完成数 ≥ FailureThreshold` 时跳闸。
  任何一次完成（成功或失败）都会重新评估，而不只在失败时评估。
- **open**：冷却结束前所有调用直接拒绝（`ErrOpen`）；冷却结束后**下一次
  Allow** 惰性进入 half_open，该次调用即第一个探测。
- **half_open**：至多 `HalfOpenMaxProbes` 个探测在飞，满了快速拒绝
  （`ErrProbesExhausted`）；连续 `RequiredSuccesses` 次成功 → closed；任一
  探测失败 → 重新 open（新冷却、新代际）。
- **代际隔离**：open/half_open/closed 每次切换 generation 加 1 并重置窗口与
  名额。permit 完成时若其 generation 与当前不同，仅累加 `stale_results`。
- **取消语义**：`RecordCanceled` 释放名额但不进窗口、不计数为失败、不改连续
  成功数。客户端层把 `context.Canceled`（调用方放弃）与
  `context.DeadlineExceeded`（超时）明确区分。

## 配置（`serve` 参数）

`--addr`、`--window`、`--min-requests`、`--threshold`、`--cooldown`、
`--probes`、`--required`，见 `go run ./cmd/demo serve -h`。

## 不做什么

- 不连接任何真实外部系统或生产服务；上游是进程内假服务。
- 不做前端/UI；只有 HTTP/JSON 与 CLI 文本/JSON 报告。
- 不做真实网络重试、限流、指标上报等生产化能力。

---

## 实际运行记录（如实）

环境：Go 1.22.2，linux/amd64。以下命令均已实际执行。

### 单元/集成测试（含竞态检测）

```bash
go vet ./...                          # 通过，无输出
go test -race -count=10 ./...         # 通过
```

结果（10 轮）：

```
?   	cbhalfopen/cmd/demo	[no test files]
ok  	cbhalfopen/internal/breaker	1.088s
ok  	cbhalfopen/internal/demoapp	1.548s
ok  	cbhalfopen/internal/fakeupstream	1.111s
ok  	cbhalfopen/internal/faultclient	1.083s
ok  	cbhalfopen/internal/scenario	1.108s
ok  	cbhalfopen/internal/vclock	1.071s
```

覆盖率（`go test -cover ./...`）：breaker 97.0%、scenario 95.2%、
fakeupstream 92.9%、faultclient 87.0%、vclock 86.3%、demoapp 75.3%。

### 场景运行

```bash
go run ./cmd/demo scenarios           # 4 场景全部 PASS（见上文表格）
go run ./cmd/demo scenarios --json    # 结构化报告，样例见 examples/scenario-report.json
```

### HTTP 服务端到端验证（实际 curl 结果）

`go run ./cmd/demo serve --addr :18080` 后按 `examples/requests.md` 执行：

- **旧失败迟到**：挂起调用 id=1 → 5 次失败跳闸（`open gen 1 failures 5`）
  → 冷却 5s + 两次探测（`closed gen 3`）→ 释放 id=1 为失败 →
  终态 `state closed gen 3 failures 5 stale 1`。✔ 旧失败未改变新代际。
- **探测竞争**：3 探测占满（`half_open inflight 3`）→ 第 4 个被拒绝
  （`half-open probe slots exhausted`）→ 释放一个成功（`inflight 2 streak 1`）
  → 全部释放后 `closed gen 3 granted 3 rejected 1`。✔
- **取消**：取消挂起调用 → outcome `canceled`，随后上游迟到失败 →
  `state closed failures 0 canceled 1`。✔ 取消未记为失败。
- **虚拟超时**：`timeout_ms=100` + hang → `Advance(100ms)` 触发 1 个定时器 →
  outcome `failure`/`timeout`，虚拟耗时 100ms，`failures 1 canceled 0`。✔

### 开发中发现并修复的问题（如实记录）

1. **竞态（已修复）**：`Breaker.Allow` 曾在释放互斥锁后才读取 `b.generation`
   构造 permit，与并发 `openLocked` 写 generation 构成数据竞态（10 轮
   `-race` 压测暴露）。已改为锁内读取构造。
2. **虚拟时钟投递顺序（已修复）**：`Advance` 原先在更新 `now` 之前投递定时器
   事件，被唤醒的 goroutine 读 `Now()` 会看到旧时间。已改为先推进时间再投递。
3. **测试自身的竞态（已修复）**：`TestReleaseSpecificIDAndReleaseAll` 假设
   goroutine 启动顺序等于假服务 ID 分配顺序，实际由调度决定，导致按 ID 释放
   打到错误的调用而挂起。已改为逐个启动并等待注册。场景脚本中的探测启动
   存在同类问题，同样修复（`startHungProbe` 等待注册）。
4. **场景死锁（已修复）**：探测竞争场景初版并发启动 3 个探测后按“最旧 ID”
   释放，与启动顺序不一致时主流程永久等待。修复方式同上。

### 未通过项

无。最终状态：`go vet` 干净，`go test -race -count=10 ./...` 全部通过，
4 个验收场景全部 PASS，HTTP 端到端验证全部符合预期。
