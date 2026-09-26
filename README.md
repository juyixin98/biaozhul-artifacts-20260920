# 优雅停机阶段控制（Graceful Shutdown Phases）

纯后端、零第三方依赖（仅 Go 标准库）的本地 HTTP 服务，演示并可验收**四阶段优雅停机**。
所有外部依赖均为本进程内的假服务（fault-injected fake），只绑定 `127.0.0.1`，**不连接任何生产系统**，也不包含任何前端。

## 它解决什么问题

进程收到停机信号后，严格按顺序执行四个阶段：

| 阶段 | 动作 | 关键保证 |
|------|------|----------|
| 1. `STOP_ACCEPT` | 就绪探针立即失败；流量监听进入“拒绝窗口”，新请求得到显式 `503` | 摘流量；**不再新增任何后台任务** |
| 2. `DRAINING` | 在排空预算内等待已接收的在途请求自行完成 | 存活探针仍为 `200`，探针与就绪分离 |
| 3. `CANCELLING` | 排空预算耗尽后，取消仍在途请求的 `context` | 每个请求得到**完成**或**明确取消**的结果 |
| 4. `CLOSING` | 按资源注册的**逆序**逐个关闭（带每资源超时） | 关闭顺序在结构化报告中可审计 |
| —    | `CLOSED` | 生成结构化停机报告并写回触发方，然后退出 |

重复停机信号（连续 Ctrl-C / 多次 SIGTERM / `signals=N`）被计数并“尽快推进”当前等待，
不会重启状态机、不会 panic，停机最终仍以 `CLOSED` 结束。

## 目录结构

```
cmd/server/main.go          服务端入口（OS 信号 + HTTP 触发两种停机方式）
cmd/client/main.go          故障注入客户端：4 个验收场景，输出结构化 JSON
internal/clock/             可控时钟：Real 与 Fake（测试中确定性地推进超时）
internal/lifecycle/         阶段常量、记录与结构化报告（Report）模型
internal/shutdown/          四阶段状态机（协调器），核心逻辑
internal/fakedep/           进程内假外部依赖 HTTP 服务（延迟 / 500 / 挂起 可注入）
internal/app/               接线：双监听器、长请求/流/后台任务/探针处理器
internal/client/            客户端库：请求、SSE 解析、探针轮询、触发停机
examples/requests.md        curl 请求样例
examples/run-demo.sh        一键构建并跑全部 4 个场景
results/                    实际运行产物（服务日志 + 客户端结构化结果）
```

## 快速开始

要求 Go 1.22+。

```bash
# 1. 构建
go build -o bin/server ./cmd/server
go build -o bin/client ./cmd/client

# 2. 一键跑全部 4 个验收场景（每场景独立起停服务）
bash examples/run-demo.sh
# 可用环境变量改端口/预算：
#   PUBLIC=127.0.0.1:28080 ADMIN=127.0.0.1:28081 \
#   DRAIN=3s CANCEL=2s REJECT=400ms bash examples/run-demo.sh
```

或手动两个终端（请求样例见 [`examples/requests.md`](examples/requests.md)）：

```bash
# 终端 A：起服务
go run ./cmd/server -drain 5s -cancel 2s -reject-window 1s

# 终端 B：注入挂起故障 + 制造在途请求，然后触发停机
curl -s -X POST http://127.0.0.1:18081/fault -d '{"hang":true}'
curl -N "http://127.0.0.1:18080/stream?duration=60s" &
curl -s  http://127.0.0.1:18080/work &
curl -s -X POST http://127.0.0.1:18081/trigger-shutdown | jq .
```

也可以用真实 OS 信号：`kill -TERM <pid>`，连发两次即“重复信号”。

## 端点

流量监听器（默认 `127.0.0.1:18080`）：
- `GET /work`：长请求，同步调用假外部依赖
- `GET /stream?duration=`：SSE 流式请求，停机取消时发 `event: cancelled`
- `POST /bg?duration=`：受理一个**脱离请求生命周期**的后台任务

管理/探针监听器（默认 `127.0.0.1:18081`，与流量端口分离）：
- `GET /readyz`：就绪探针，阶段 1 起立即 `503`
- `GET /livez`：存活探针，排空期间仍 `200`，进入 `CLOSING` 才 `503`
- `GET /report`：停机过程中的实时结构化快照
- `POST /trigger-shutdown?signals=N`：触发停机（可重复信号），响应体是最终报告
- `POST /fault`、`POST /fault/release`：对假外部依赖注入/解除故障

## 四个验收场景

`cmd/client` 用 `-scenario` 选择，输出结构化 JSON 到 `-out`：

1. **drain**：依赖有短延迟，3 个长请求在 `DRAINING` 内全部 `completed`，无取消。
2. **cancel**：依赖挂起；长请求、流、后台任务在排空预算后全部被**明确取消**。
3. **repeat**：一次发 3 个停机信号；报告 `signalsReceived=3`，仍走到 `CLOSED`。
4. **reject**：停机后新的长请求与后台 spawn 都得到显式 `503 rejected`，无后台任务被创建。

## 实际运行结果（已在本机如实执行）

> Go 1.22.2 / linux-amd64。完整产物在 `results/`（`server-*.log` 与 `client-*.json`）。

| 场景 | 信号数 | completed | cancelled | rejected | 拒绝后台 | 在途遗留 | 关闭顺序 |
|------|------|------|------|------|------|------|------|
| drain  | 1 | 3 | 0 | 0 | 0 | 0 | admin-listener → http-client → fake-dep |
| cancel | 1 | 0 | **3** | 0 | 0 | 0 | 同上 |
| repeat | 3 | 0 | 1 | 0 | 0 | 0 | 同上 |
| reject | 2 | 0 | 1 | **1** | **1** | 0 | 同上 |

- cancel 场景探针采样：`readyz` 有 61 次 `503`，同时 `livez` 在 `DRAINING` 阶段有 60 次 `200`
  —— **就绪先摘、存活仍在**得到实证。
- 流请求最后一个 SSE 事件为 `event: cancelled`，载荷 `{"reason":"shutdown phase CANCELLING",...}`。
- OS 信号路径单独验证：连续两次 `SIGTERM` → 报告 `signalsReceived=2`、升级排空、挂起请求
  返回 `{"outcome":"cancelled",...}`、进程退出码 `0`。

复现命令：

```bash
go test -race -count=1 ./...          # 全部通过
bash examples/run-demo.sh             # 4 场景实跑，产物写入 results/
```

## 结构化报告长什么样

`POST /trigger-shutdown` 的响应（以及服务端日志）是一份 `Report`，包含：
时间线（每阶段进入时刻与备注）、完成/取消/拒绝计数、后台任务计数
（`backgroundSpawnedAfterStopAccept` 恒为 0 是核心不变量）、每个请求的开始/结束/结果、
`inFlightAtClose`、以及带序号的 `closeOrder`（含每资源关闭耗时与错误）。

## 测试

```bash
go test -race ./...                  # 竞态检测
go test -cover ./internal/...        # 覆盖率
go test -race -count=2 ./...         # 并发稳定性多轮
```

最近一次覆盖率：

| 包 | 覆盖率 |
|----|--------|
| internal/shutdown | 84.9% |
| internal/clock | 89.3% |
| internal/lifecycle | 100% |
| internal/app | 80.7% |
| internal/fakedep | 76.3% |
| internal/client | 74.6% |

测试策略：
- **虚拟时钟**：协调器的排空/取消/关闭超时全部走 `clock.Clock` 接口；单测用 `Fake`
  在“计时器装弹后”显式 `Advance`，确定性地触发超时，不依赖真实睡眠。
- **真实 HTTP 栈端到端**：`internal/app` 与 `internal/client` 的测试在随机空闲端口
  起真实监听器与假依赖，验证长请求、SSE、后台任务、探针分离、重复信号、关闭顺序。

## 开发过程中实际遇到并修复的问题（如实记录）

1. **触发请求与自身监听器关闭的循环等待**：`/trigger-shutdown` 挂在 admin 监听器上，
   若该监听器在阶段 4 被 `Shutdown` 优雅关闭，会等待触发请求结束，而触发请求又在等停机报告。
   修复：触发处理器在**发信号之前** `Hijack` 接管连接——被劫持的连接既不被 `Shutdown`
   等待，也不被 `Close` 关闭，报告随后在劫持连接上原样写回。
2. **进程在报告写回前退出（客户端偶发 EOF）**：快速 drain 场景下，`Done` 关闭即触发 `main`
   退出，与处理器 flush 劫持连接产生竞态。修复：协调器增加 `ExpectFinalizeAck/FinalizeAck`，
   在发布最终报告后、关闭 `Done` 前等待触发方确认已刷出响应。
3. **HTTP 触发后 main 不退出**：早期 `main` 只 `<-sigs` 等待 OS 信号，HTTP 触发停机后
   状态机已 `CLOSED` 但进程仍阻塞。修复：`main` 同时等待 OS 信号或协调器 `Done`。
4. **重复信号升级丢失/穿透**：初版用缓冲 channel 传递升级，残留 token 会错误穿透到下一阶段，
   接收方换 channel 又会吞掉信号。修复：单一持久“闭锁”（latch），首个重复信号关闭一次，
   当前及后续等待都立即推进，语义即“尽快完成停机”，且不会重复 close 导致 panic。
5. **admin 优雅关闭被探针 keep-alive 拖到满超时**：客户端轮询探针的长连接会反复回到 idle 集。
   修复：admin 监听器给 250ms 优雅宽限，随后 `Close()` 强制丢弃空闲探针连接（劫持连接不受影响）。

## 设计说明与边界

- 拒绝窗口（`-reject-window`，默认 500ms）让监听器在阶段 1 短暂保留，使迟到请求拿到
  **显式 503** 而不是裸的“连接被拒”；窗口结束后才真正停止 accept。这与负载均衡器
  preStop + 摘流量的真实模式一致。
- “忽略取消、卡死到最后的工作”由流量监听器在排空+取消总预算后强制断开来兜底；
  协调器报告中的 `inFlightAtClose` 记录此时仍在途的数量（本项目验收场景下均为 0）。
- 仅用于教学/验收的本地实验程序，不包含鉴权、限流、TLS 等生产能力。
