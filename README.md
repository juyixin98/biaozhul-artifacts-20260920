# 断路器半开竞争（Circuit Breaker Half-Open Contention）

纯后端练习项目：用 Go 实现一个按**滑动调用样本**跳闸的断路器，重点正确处理
半开（HALF_OPEN）阶段的三类难题：

1. **旧代请求迟到** —— 在 CLOSED 时代发出、跨越跳闸的请求，其失败/成功结果
   在新的 HALF_OPEN/OPEN 代际才回来时，**绝不能改变新代际的状态与计数**。
2. **半开探测名额有限** —— HALF_OPEN 最多放行 `MaxProbeCalls` 个并发探测；
   名额满时其他调用直接拒绝（不触达下游）；名额释放后可放入替补探测。
3. **取消不是失败** —— 调用方取消或超时记为 `canceled`，既不会在 CLOSED
   累计失败跳闸，也不会作为探测结论关闭或重开断路器。

所有外部依赖均为**本进程假服务**（fake upstream），时间使用**可控虚拟时钟**，
不连接任何生产系统。每个验收场景输出结构化 JSON（逐步记录调用、时钟推进、
断路器快照与断言结果）。

## 目录结构

```
cmd/server/          本地 HTTP 演示服务（仅监听 127.0.0.1）
cmd/runscenarios/    运行内置验收场景并写出 JSON 报告
internal/clock/      可控虚拟时钟 + 时钟驱动的 context.WithTimeout
internal/breaker/    断路器核心（状态机、滑动窗口、代际隔离、探测名额）
internal/upstream/   进程内假下游（脚本化延迟/失败/挂起、动态故障注入）
internal/appclient/  故障注入客户端（断路器 × 假下游；取消≠失败的分类）
internal/scenario/   虚拟时钟场景引擎 + 3 个验收场景 + 结构化报告
internal/httpapi/    HTTP API 层
examples/            curl 端到端演示脚本与请求样例
reports/             场景运行产出的结构化 JSON（已含一次实际运行结果）
```

## 核心设计

### 状态机与代际（generation）

```
CLOSED ──(最近 WindowSize 个样本中失败 ≥ FailureThreshold)──▶ OPEN
OPEN   ──(冷却 OpenCoolDown 结束，惰性转换)──────────────▶ HALF_OPEN
HALF_OPEN ──(连续探测成功 ≥ HalfOpenSuccessThreshold)────▶ CLOSED
HALF_OPEN ──(任一探测失败)───────────────────────────────▶ OPEN
```

每次状态转换 `generation++`。`Allow()` 返回的每个 `Permit` 都记下获取时的
代际；`Permit.Done()` 汇报结果时若发现代际已过期，则：

- **不**释放当代探测名额；
- **不**写入驱动状态转换的任何计数；
- 仅计入生命周期累计观测值（total_successes/failures/canceled）。

这就从根上杜绝了"旧失败迟到污染新代状态"。

### 滑动调用样本窗口

CLOSED 状态用定长环形缓冲保存最近 `WindowSize` 个样本（success/failure/
canceled）。每次写入失败后只统计窗口内失败数，达到 `FailureThreshold` 跳闸。
取消样本进窗口但不算失败；窗口滑出后旧失败自然失效（有单测固定该行为）。

### 半开探测名额

HALF_OPEN 下 `probesInFlight` 严格不超过 `MaxProbeCalls`；超额 `Allow()`
立即返回 `ErrOpen`。探测完成（含取消，取消只是释放名额、不下结论）后名额归还。
连续成功计数达到阈值才 CLOSED；任一失败立即 OPEN 并把仍在飞的探测全部置为
过期代。

### 可控虚拟时钟

`clock.Virtual` 不会自己走，只有显式 `Advance()` 才推进；到期的等待者按时长
顺序被唤醒。关键细节：**定时器在标记调用 active 之前同步注册**，因此测试
"等待调用挂起 → 推进时钟"不存在定时器漏注册的竞态。另提供
`clock.WithTimeout`（虚拟时钟驱动的 context 截止时间），使"超时"也可被
确定性复现。

## 快速开始

需要 Go 1.22+。

```bash
# 构建
go build ./...

# 跑全部自动化测试（带竞态检测）
go test -race ./...

# 运行三个验收场景，结构化报告写入 reports/
go run ./cmd/runscenarios -out reports

# 启动本地 HTTP 演示（默认 127.0.0.1:8080）
go run ./cmd/server -addr 127.0.0.1:18080
# 另一个终端执行端到端 curl 演示
./examples/demo_late_failure.sh http://127.0.0.1:18080
```

断路器参数可调（`cmd/server` flag）：
`-window 5 -fail-threshold 3 -cooldown 10s -max-probes 2 -probe-success 2`。

## 验收场景

| 场景 | 复现内容 | 关键校验 |
|---|---|---|
| `late_failure` | CLOSED 的慢调用挂起 → 后续 3 个失败跳闸 → 冷却进 HALF_OPEN → 旧调用此时才失败 | HALF_OPEN 状态/代际/探测计数不变；随后两个探测仍正常恢复 |
| `probe_contention` | 两个探测占满名额；第 3 个被拒；释放 1 个名额后替补探测进入；再次满额又拒绝 | 被拒调用不到达假下游；最终恰好 6 次调用到达下游；2 成功后恢复 |
| `cancel_is_not_failure` | CLOSED 下调用方取消、虚拟时钟超时；HALF_OPEN 下取消探测、失败探测重开；第二轮半开 | 4 次取消不跳闸；取消探测不关闭/重开；第二轮干净恢复 |

## HTTP API 摘要

| 方法/路径 | 说明 |
|---|---|
| `GET  /api/state` | 断路器快照 + 虚拟当前时间 |
| `GET  /api/history` | 客户端每次尝试 + 假下游调用记录 |
| `POST /api/call` | 发起一次受保护调用，body 可带 `{"timeout_ms":100}`；被断路器拒绝返回 **503** |
| `POST /api/clock/advance` | `{"duration_ms":10000}` 推进虚拟时间 |
| `POST /api/upstream/behavior` | `{"fail":true}` / `{"stall":true}` / `{"dynamic":false}` 注入或解除故障 |
| `POST /api/upstream/release` | 放行所有挂起的假下游调用 |
| `GET  /api/upstream` | active/stalled 调用数、当前行为、历史记录 |
| `POST /api/reset` | 重置时钟、断路器与假下游 |
| `GET  /api/scenarios` | 列出内置场景 |
| `POST /api/scenarios/{name}/run` | 运行场景，返回结构化报告（失败时 HTTP 422） |

统一响应包络：`{"success":bool,"data":...,"error":...}`。请求样例见
[examples/requests.http](examples/requests.http)。

## 测试结果与如实记录

实际执行的命令、结果以及开发中出现过的失败/修复过程，见
[RUN_LOG.md](RUN_LOG.md)。最近一次全量结果：

```text
go test -race ./...
ok  breakerhalfopen/internal/appclient
ok  breakerhalfopen/internal/breaker
ok  breakerhalfopen/internal/clock
ok  breakerhalfopen/internal/httpapi
ok  breakerhalfopen/internal/scenario
ok  breakerhalfopen/internal/upstream
3/3 验收场景 PASS（reports/*.json）
```

## 边界与非目标

- 仅本地回环监听，无鉴权、无持久化，**不接入任何真实下游**。
- 不含前端；仅提供 JSON API、curl 脚本与机器可读报告。
- 状态转换在 `Allow/State/Snapshot` 观察时惰性发生（虚拟时钟无后台定时器），
  因此读取代际前先观察状态是预期用法，测试与场景均遵循此约定。
