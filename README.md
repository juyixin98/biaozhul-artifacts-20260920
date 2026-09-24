# sse-resume — 持久化 SSE 事件服务（断点恢复）

纯 Go 标准库（`net/http`）实现的服务端事件服务：事件 ID 单调递增、持久化到磁盘、
支持 `Last-Event-ID` 断点补发、心跳保活、慢消费者限额摘除；游标超出保留窗口时返回
**明确的重置要求**；多行 `data` 按 SSE 规范编码。纯后端，无前端页面。

## 1. 依赖

- Go ≥ 1.23（开发环境为 go1.23.4 linux/amd64）
- **仅标准库**，无任何第三方依赖；`go.mod` 中没有 require 段，因此不需要 `go.sum`
  （这就是本项目的"锁定依赖"：工具链版本锁定在 `go.mod`，依赖集合为空、可复现）。

```text
module sse-resume
go 1.23
```

## 2. 目录结构

```text
.
├── go.mod
├── cmd/
│   ├── server/main.go          # HTTP 服务入口
│   └── demo-client/main.go     # 参考客户端：自动重连/按ID去重/缺口检测/reset全量重同步
├── internal/
│   ├── broker/broker.go        # 持久化事件日志（JSONL + fsync + 压缩）与订阅扇出
│   ├── broker/broker_test.go
│   ├── sse/sse.go              # SSE 线格式编码（多行 data / 心跳 / reset）
│   ├── sse/sse_test.go
│   ├── server/server.go        # net/http 处理器
│   └── server/server_test.go   # 真实 httptest.Server 集成测试（含全部验收场景）
└── README.md
```

## 3. 启动

```bash
go build ./...
go run ./cmd/server \
  -addr :8080 \
  -data ./data \
  -max-events 10000 \
  -queue 128 \
  -heartbeat 15s \
  -write-timeout 5s
```

| 参数 | 默认值 | 说明 |
|---|---|---|
| `-addr` | `:8080` | 监听地址 |
| `-data` | `./data` | 持久化日志目录（自动创建） |
| `-max-events` | `10000` | 保留事件条数上限；超过后压缩日志、滚动窗口；`0` = 不限 |
| `-queue` | `128` | 每个订阅连接的实时事件缓冲；满即摘除该慢消费者 |
| `-heartbeat` | `15s` | SSE 心跳（`: ping` 注释帧）间隔 |
| `-write-timeout` | `5s` | 单帧写截止时间，防止卡死对端长期占用 goroutine |
| `-nosync` | false | 跳过每条事件 fsync（仅演示/测试用，生产勿开） |

`Ctrl-C` / `SIGTERM` 触发 5 秒优雅关闭。

## 4. HTTP 接口

### 4.1 发布事件 `POST /v1/events`

```bash
curl -s -X POST http://127.0.0.1:8080/v1/events \
  -H 'Content-Type: application/json' \
  -d '{"event":"order.created","data":"订单 #1001 已创建"}'
# 201 -> {"id":1,"timestamp":"2026-09-24T01:20:00Z"}
```

多行 data（`\n` 会按规范展开成多条 `data:` 指令）：

```bash
curl -s -X POST http://127.0.0.1:8080/v1/events \
  -H 'Content-Type: application/json' \
  -d '{"event":"doc","data":"第一行\n第二行\n\n空行后的一行"}'
```

请求体上限 1 MiB；`data` 必填且非空；`event` 不得含 CR/LF/NUL（违例返回 400）。

### 4.2 SSE 订阅 `GET /v1/events/stream`

```bash
# 全新连接：从最旧的保留事件开始
curl -N http://127.0.0.1:8080/v1/events/stream

# 断点续传（标准请求头，浏览器 EventSource 重连时自动携带）
curl -N -H 'Last-Event-ID: 42' http://127.0.0.1:8080/v1/events/stream

# 也可用查询参数显式指定（优先级高于请求头）
curl -N 'http://127.0.0.1:8080/v1/events/stream?after=42'
```

连接后的帧序列：

1. `event: ready`（无 `id:` 行）——实时尾巴已挂上；
2. 严格补发 `id > cursor` 的保留事件（每帧带 `id:`）；
3. 进入实时阶段，持续推送新事件；空闲时每 15s 一帧 `: ping` 心跳；
4. 慢消费者被摘除时下发 `event: slow_consumer`（无 `id:`）并关闭连接；
5. 游标过旧/超前时下发 `event: reset`（无 `id:`）并**立即关闭**。

帧示例（多行 data）：

```text
id: 7
event: doc
data: 第一行
data: 第二行
data:
data: 空行后的一行

```

### 4.3 重置协议（游标过旧）

当 `Last-Event-ID < oldest_id-1`（保留窗口已滚过该点），服务端无法保证无缺口补发：

```text
event: reset
data: {"reason":"cursor_expired","oldest_id":9905,"last_id":12000,"at":"..."}
data: resume impossible from the requested cursor; perform a full resync

```

- 帧**不含 `id:` 行**，符合 SSE 规范——客户端的 lastEventId 不会被污染，也不会拿
  reset 帧的内容当游标反复重连；
- 客户端收到后必须**丢弃旧游标、做全量重同步**（不带 `Last-Event-ID` 重开流，
  或走业务侧快照接口）；
- 游标超前（`>last_id`）时 reason 为 `cursor_ahead`；
- 非 SSE 的补发查询接口对同类情况返回 `409 Conflict` + JSON 处置说明；
- 游标格式非法在输出任何 SSE 帧之前返回 `400`。

### 4.4 分页补发查询 `GET /v1/events`（非流式）

```bash
curl -s 'http://127.0.0.1:8080/v1/events?after=100&limit=50'
# {"events":[...],"oldest_id":1,"last_id":123,"has_more":true}

curl -s -i 'http://127.0.0.1:8080/v1/events?after=2'   # 游标过旧 -> 409
```

### 4.5 可观测性

```bash
curl -s http://127.0.0.1:8080/v1/stats
# {"last_id":123,"oldest_id":24,"retained":100,"subscribers":2,"dropped":0,"max_events":10000}

curl -s http://127.0.0.1:8080/healthz   # {"status":"ok"}
```

## 5. 关键设计：补发/实时交界处为什么无缺口

处理程序严格按以下顺序（`internal/server/server.go` 的 `handleStream`）：

1. **先订阅实时通道**，拿到订阅时刻的日志头 `head`；
2. 校验游标；
3. **再回放** `(cursor, head]` 的保留事件；
4. 进入实时 select。

在步骤 1 之后发布的任何实时事件必然 `id > head`，而步骤 3 已覆盖到 `head`，
两段首尾相接——**交界处不可能出现缺口**。极端时序下，回放与实时可能重复投递
同一个 id（at-least-once），因此客户端必须按 `id` 去重，去重后即精确一次、无缺口。
这正是浏览器 EventSource 的标准重连语义。

其他保证：

- ID 在持久化追加**成功之后**才消费；追加失败不产生 ID 空洞；
- 事件以 JSONL 逐条追加并 `fsync`（含 compaction 后的目录 fsync），重启后恢复事件
  与 `lastID`，新 ID 继续单调递增；
- 压缩采用"临时文件重写 → fsync → rename → 重开"，发布不因压缩失败而丢失；
- 扇出为**非阻塞发送**：单个慢消费者不会拖慢发布者和其他连接；其有界队列溢出即
  摘除（关闭通道 → 通知帧 → 关连接），事件仍留在持久日志里，重连可补；
- `cursor == 0` 表示"无客户端状态"，永远合法（从当前最旧保留事件开始），
  不会因为窗口滑动被误判为过期。

## 6. 参考客户端（演示断点恢复/去重/reset）

`cmd/demo-client` 是一个独立的 SSE 消费者，断线自动带 `Last-Event-ID` 重连，
按 ID 去重、统计连接内缺口，收到 `reset` 即清空游标做全量重同步，退出时打印报告。

```bash
go run ./cmd/demo-client -url http://127.0.0.1:8080 -duration 30s
```

## 7. 自动化测试

```bash
go test -race -count=1 ./...
```

测试覆盖（共 20 个用例，均使用真实 `httptest.Server` / 真实文件）：

**broker 层**：ID 单调与回放；重启持久化与 ID 续增；损坏日志/ID 空洞拒绝；
保留窗口压缩及重启后收紧；慢消费者队列溢出摘除；"先订阅后回放"交界不变量。

**SSE 编码层**：单行/多行 data、尾随换行、CR/CRLF 归一化；事件名校验；
reset 帧不含 `id:`；心跳为注释帧。

**HTTP 验收场景**（`internal/server/server_test.go`）：

1. `TestBoundaryDisconnectAndReconnect` —— 在补发/实时交界断线、离线期间继续发布、
   重复重连，断言去重后 id 集合 1..N 连续无缺口且确实发生过交界重复投递；
2. `TestRepeatedReconnectsUnderLoad` —— 发布压力下反复断连/重连 60 个事件，
   最终无缺口；
3. `TestRetentionReset` —— 窗口滑过游标：SSE 收到 `reset(cursor_expired)` 且连接
   被关闭；超前游标收到 `cursor_ahead`；补发接口返回 409；非法游标 400；
4. `TestSlowConsumerEviction` —— 用测试专用 flush 闸门确定性地模拟"对端停止读"，
   队列满后连接被摘除并收到 `slow_consumer`，重连后从持久日志补齐、无缺口；
5. 心跳、多行 data 过 HTTP 往返、单客户端断开不影响他人、服务重启后续传。

> 说明：慢消费者场景的真实触发条件是"对端 TCP 窗口耗尽导致写阻塞"，loopback 上
> 内核缓冲很大、难以稳定复现；因此在 `server` 包内设了一个**未导出的** flushGate
> 测试钩子（仅同包测试可访问，不进入公开 API），把写阻塞变为确定性条件。生产代码
> 路径（有界队列 + 非阻塞发送 + 写截止时间）不依赖该钩子。

## 8. 实际运行记录（2026-09-24，go1.23.4 linux/amd64）

原始日志随仓库归档：`docs/test-run.log`（完整测试输出）、`docs/demo-run.log`
（端到端脚本输出）。可随时用 `bash scripts/demo.sh` 复现。

### 8.1 自动化测试

命令：`go vet ./... && go test -race -count=1 -v ./...`

结果：**全部通过，25/25 用例 PASS，`-race` 无数据竞争报告**。

- `internal/broker`：8 个（含 `TestSubscribeHeadSeamNoGap` 交界不变量、
  `TestSubscribeDeliversLiveAndDropSlow` 队列溢出摘除、
  `TestRetentionCompaction`/`TestRetentionTightenedOnRestart` 保留边界）
- `internal/sse`：6 个（多行 data、CRLF、reset 无 id、心跳等）
- `internal/server`：11 个真实 HTTP 集成测试，验收场景对应关系：
  - 补发/实时交界断线 + 重复重连 + 去重无缺口：
    `TestBoundaryDisconnectAndReconnect`、`TestRepeatedReconnectsUnderLoad`
  - 保留窗口边界与重置：`TestRetentionReset`（SSE reset 帧 + 409 + 400）
  - 慢消费者限额：`TestSlowConsumerEviction`（flushGate 确定性模拟写阻塞）
  - 其余：心跳、多行 data 往返、客户端隔离、服务重启续传

另用 `go test -count=5 ./internal/server/` 连跑 5 轮确认无偶发抖动。

### 8.2 端到端手动演示（`scripts/demo.sh`）

按脚本顺序实测到的关键结果（节选自 `docs/demo-run.log`）：

1. 连发 22 条、`-max-events 20` 后 `GET /v1/stats` →
   `last_id=22, oldest_id=3, retained=20`，窗口正确滚动；
2. `Last-Event-ID: 1`（已在窗口外）打开 SSE →
   `event: reset` + `reason=cursor_expired`（无 `id:` 行）后连接关闭；
3. 全新流补发从 `id: 3` 开始；
4. 含空行的多行 data 在链路上编码为多条 `data:` 指令（`data: ` 空行保留）；
5. 过旧游标查询 `GET /v1/events?after=1` → `HTTP 409` + JSON 处置说明；
6. **崩溃重连验收**：服务端收到 SIGKILL（硬崩溃、所有 SSE 连接立即断），
   从同一数据目录重启，期间继续发布。参考客户端日志：
   ```
   connected (sent Last-Event-ID=0, high-water=0)
   connection ended: unexpected EOF; reconnecting with Last-Event-ID: 24 in 200ms
   connected (sent Last-Event-ID=21, high-water=24)
   dup  id=22   (redelivered at replay/live seam; discarded)
   dup  id=23   (redelivered at replay/live seam; discarded)
   dup  id=24   (redelivered at replay/live seam; discarded)
   unique events : 27
   duplicates    : 3 (dropped after id-based dedup)
   reconnects    : 1
   resets/resync : 0
   RESULT        : PASS - after dedup the observed id set is contiguous (no gaps)
   ```
   （脚本用 `demo-client -lag 3` 让重连游标故意落后 3 条，稳定制造交界重复，
   以演示按 ID 去重；真实崩溃重连中该重复是否出现取决于断线瞬间的时序，
   两种情况都正确——at-least-once + 客户端去重。）
7. 重启后新事件 ID 继续单调递增（`last_id=30`），日志从磁盘完整恢复；
8. 非法输入：空 data → 400，非法 `Last-Event-ID` → 400。

心跳（`: ping` 注释帧）在空闲连接上按 `-heartbeat` 间隔收到（演示中 4 帧）。

### 8.3 未完成项 / 已知边界（如实列出）

- **单实例、单机存储**：JSONL 追加日志 + 内存全量索引，未做多副本复制/集群选主；
  水平扩展需引入共享存储或副本协议，不在本次范围内。
- **保留策略只按条数**：没有按时间 TTL 或按字节容量保留（接口上容易扩展）。
- **内存索引为全量事件**：`-max-events` 同时约束内存与磁盘；超大窗口（百万级以上）
  未做专门优化。单事件默认上限 1 MiB、扫描行上限 16 MiB。
- **慢消费者的"写阻塞"触发依赖对端 TCP 反压**：自动化测试用包内未导出的
  flushGate 做确定性模拟；生产实网中行为为"队列满即摘除 + 写截止时间兜底"，
  未在真实跨网慢链路上压测。
- **无鉴权/TLS**：接口默认裸 HTTP、无认证，仅适用于受信内网；生产前置网关加 TLS
  与鉴权即可（SSE 不支持浏览器端自定义请求头，鉴权通常走 Cookie 或短期令牌查询参数）。
- **无 Web UI**：按需求仅提供 HTTP 接口与 `curl`/参考客户端示例。
- 优雅关闭等待最多 5s，到点后在途 SSE 连接被强制关闭（客户端会自动重连补发）。

