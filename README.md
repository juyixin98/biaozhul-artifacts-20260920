# cancelprop — 请求取消传播（Request Cancellation Propagation）

一个**纯后端**的 Go 项目，演示并验证「HTTP 入口 → 多路子任务」的取消树语义：

- 入口请求的 `context` 是整棵取消树的根，**客户端断开**即取消全部子任务；
- **第一个致命（fatal）子任务失败**会取消其余所有子任务，但**等待它们完成资源清理**后才返回；
- 非致命（best-effort）失败只记录、不取消；
- 下游全部是**本进程内的假服务**（in-process loopback HTTP），可阻塞、可放行、可注入故障，**不连接任何生产/外部系统**；
- 提供**可控时钟**（真实时钟 + 手动推进的假时钟）与**结构化测试结果**（`go test -json` 汇总报告）；
- 验收测试专门覆盖**响应与取消的竞争**，并检查 goroutine、连接、内存不持续泄漏。

## 目录结构

```
cmd/server/            HTTP 服务入口（可测的 run() + main 信号处理）
cmd/testreport/        把 `go test -json` 事件汇总成单个结构化 JSON 报告
internal/clock/        时钟抽象：RealClock 与可手动推进的 FakeClock
internal/fakesvc/      进程内可阻塞假下游服务（gate/hold/失败注入/连接与在途计数）
internal/faultclient/  故障注入客户端（强制错误/500/延迟/时钟驱动超时）
internal/canceltree/   取消树编排器（首致命取消、等待清理、panic 兜底）
internal/resguard/     请求级独占资源注册表（证明清理必释放）
internal/serverapi/    HTTP 入口：POST /process 映射为取消树
internal/leakcheck/    goroutine/连接/堆内存收敛与泄漏断言工具
examples/              请求样例 JSON
scripts/demo.sh        本地端到端演示
results/               测试报告、覆盖率、真实运行记录（运行后生成/更新）
```

## 取消语义（canceltree）

每个叶子（`Leaf`）有 `Run(ctx)` 和可选的 `Cleanup(ctx)`：

1. 所有叶子在同一个派生 context 下并发启动。
2. 任一 **fatal** 叶子返回**自身错误**（或任一叶⼦ panic）→ 取消整棵树。
3. 根 context 取消（客户端断连/父超时）→ 取消整棵树；报告标记 `canceled:true`。
4. 非 fatal 叶子失败只在报告中记录（`outcome:"failed"`），整树仍 `ok:true`。
5. **Run 必定等待所有叶子返回，并且等待每个 Cleanup 完成后才返回**——即便走致命快速失败路径。
6. 叶子的结果区分为 `succeeded / canceled / deadline_exceeded / failed / panicked`；
   因树被取消而返回的错误记为 `canceled`，不会被误判为新的致命触发者。
7. Cleanup 收到的是可能已取消的 context；cleanup 失败被记录（`cleanup:"failed"`）但不掩盖主错误。

关键实现点：根取消监听 goroutine 在被 `WaitGroup` join 之后才读取触发字段，
避免与读取方产生数据竞争（由 `-race` 守护）。

## 快速开始

需要 Go 1.22+，无第三方依赖（仅标准库）。

```bash
go build ./...
go test -race -count=1 ./...           # 竞态检测下运行全部测试
go test -cover ./...                   # 覆盖率
```

启动服务：

```bash
go run ./cmd/server -addr 127.0.0.1:8080
```

## HTTP 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET  | `/healthz` | 健康检查 |
| POST | `/process` | 提交取消树，body 见下 |
| GET  | `/stats` | goroutine/堆/在途请求/资源/各下游连接与计数 |
| GET  | `/admin/release?gate=<name>` | 放行两个假服务上名为 gate 的阻塞请求 |
| GET  | `/admin/close-idle-connections` | 关闭客户端连接池空闲连接 |

### POST /process

```json
{
  "leaves": [
    {"name": "fetch-a", "service": "a", "fatal": true},
    {"name": "fetch-b", "service": "b", "fatal": true,
     "gate": "g1", "resource": "lock-b"}
  ]
}
```

叶子字段：

| 字段 | 含义 |
|------|------|
| `name` | 叶子名（缺省 `leaf-N`） |
| `service` | 假下游 `"a"` / `"b"`（缺省 `a`） |
| `fatal` | 自身失败是否取消整树 |
| `gate` | 在假服务上阻塞直到该 gate 被放行 |
| `hold_ms` | 假服务在响应前保持的毫秒数（真实时钟） |
| `fail` | 令假服务返回 500 |
| `fail_request` | 客户端直接注入传输错误（不发请求） |
| `delay_ms` | 发请求前通过注入时钟睡眠（可被取消） |
| `timeout_ms` | 用注入时钟为该次调用设置超时 |
| `resource` | 叶子持有、并在 Cleanup 中释放的独占资源名 |

成功返回 `200`；致命失败返回 `502`；客户端断连时不写响应体（对端已离开），
但服务端所有叶子与清理仍会执行完毕。

## 请求样例

`examples/` 下：

- `01-all-succeed.json` — 全部成功
- `02-first-fatal-cancels.json` — 两个 gated 叶子 + 300ms 后注入致命失败
- `03-non-fatal-tolerated.json` — best-effort 500 被容忍
- `04-client-disconnect.json` — 两个叶子阻塞，客户端主动断开
- `05-fake-clock-timeout.json` — 100ms 时钟超时取消兄弟任务

### 手工 curl

```bash
# 终端 A
go run ./cmd/server -addr 127.0.0.1:8080

# 终端 B
curl -sS localhost:8080/process -H 'Content-Type: application/json' \
  --data @examples/02-first-fatal-cancels.json | jq

# 客户端断开：让 curl 0.3s 后放弃
curl --max-time 0.3 -sS localhost:8080/process -H 'Content-Type: application/json' \
  --data @examples/04-client-disconnect.json
curl -sS localhost:8080/admin/release?gate=hold   # 放行
curl -sS localhost:8080/stats | jq                # 检查是否全部排空
```

### 一键演示

```bash
go build -o bin/cancelprop-server ./cmd/server
bin/cancelprop-server -addr 127.0.0.1:8090 &     # 先启动服务
./scripts/demo.sh http://127.0.0.1:8090          # 再跑演示
```

## 结构化测试报告

```bash
go test -json -race -count=1 ./... > results/raw-test-events.jsonl
go run ./cmd/testreport -in results/raw-test-events.jsonl -out results/test-report.json
```

报告汇总每个包/每个用例的结果与耗时、失败输出尾部，并在有失败时以非零码退出。

### 实测结果（本机，Go 1.22.2 linux/amd64）

`results/` 目录保留了一次真实运行的完整记录（`acceptance.log`、
`test-report.json`、`coverage.out`、`coverage-summary.txt`、`demo-run.log`、
`demo/`）。最近一次：

- `go build ./...`、`go vet ./...`、`gofmt -l .`：全部通过/无输出；
- `go test -race -count=1 ./...`：**9 个包、67 个用例全部 PASS，exit 0**；
- 总语句覆盖率 **91.7%**；各 `internal/` 包与 `cmd/testreport` 均 ≥ 84%
  （fakesvc 98.8%、faultclient 96.0%、resguard 96.3%、serverapi 92.6%、
  clock 92.9%、canceltree 90.9%、leakcheck 84.6%、testreport 91.5%）。
- 唯一低于 80% 门槛的是 `cmd/server`（73.2%）：未覆盖部分是 `main()` 这个
  仅做 flag 解析与信号转发的薄封装（Go 惯例不对 `main` 做单测）；其可测核心
  `run()` 覆盖率 85.7%，含「正常关停」与「监听端口被占立即返回错误」两条路径。
- `gosec` 静态扫描**未执行**：环境未安装该工具，临时联网 `go run ...@latest`
  下载未获授权。本项目仅依赖 Go 标准库、监听 loopback、无密钥/SQL/模板/认证面，
  输入仅做 JSON 解码与校验；如需可在授权后运行 `gosec ./...`。
- 开发过程中出现过并已修复的问题（均有对应回归测试）：假服务无 gate/hold 时
  select 永久阻塞；`FailRequest` 短路了 `DelayBefore`；取消树根监听 goroutine
  与结果读取间的数据竞争（`-race` 捕获，已用 WaitGroup join 建立 happens-before）；
  gate「先释放后注册」丢失；跨迭代陈旧 in-flight 计数导致的假汇合；资源注册表
  最初按全局名而非按请求命名空间隔离。

## 泄漏验收怎么做

- **goroutine**：`TestE2E_NoSustainedLeakage` 预热后做三轮各 50 个请求，
  断言 `runtime.NumGoroutine()` 不随轮次增长。
- **连接**：假服务用 `ConnState` 统计活跃连接；每轮后关闭空闲连接，
  断言两个服务 `active_connections` 收敛到 0。
- **内存**：每轮强制 GC 采样堆，断言堆不单调爬升（允许 GC 噪声带）。
- **资源**：`resguard` 对每个 acquire 计数，断言累计 acquire == release，
  且任何时刻 `held_resources` 最终为 0（覆盖致命失败与断连路径）。
- **响应/取消竞争**：`TestE2E_ResponseVsCancelRace` 交替驱动「先放行→响应成功」
  与「先取消→调用出错」两种结局，每种 20 次，并在结尾统一断言无泄漏。

## 设计边界与说明

- 假服务的 `hold_ms` 用真实时钟（它是真实 HTTP server）；**客户端**的
  `delay_ms` / `timeout_ms` 走注入时钟，因此单测可用 FakeClock 零等待驱动超时。
- gate 必须在 handler 注册后释放才有效；handler 在计入 in-flight 前先注册 gate，
  所以测试中观察到 `in_flight==1` 即可安全放行（避免释放被“丢失”）。
- 生产语义仅到本进程 loopback 为止；不包含前端、服务发现、TLS、鉴权等。
- 已知小语义：客户端 `timeout_ms` 触发时返回的错误文本可能是 `context canceled`
  （内部取消 cause 未单独映射为 `DeadlineExceeded`），但它仍被正确判为该叶子的
  自身致命失败并取消整树；行为正确，仅文案如此。
