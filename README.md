# sse-server — 持久化 SSE 事件服务（断点恢复）

纯 Go 标准库（`net/http`）实现的单流 Server-Sent Events 服务，支持事件落盘、
单调递增事件 ID、基于 `Last-Event-ID` 的断线补发、心跳、慢消费者强制断开、
日志保留窗口以及过旧游标的显式重置。无任何第三方依赖。

## 功能与协议要点

| 能力 | 实现 |
| --- | --- |
| 单调事件 ID | uint64，从 1 开始；重启后从 WAL 恢复 `nextID` |
| 持久化 | 每条事件以「8 字节大端长度前缀 + JSON」追加写入 `data/events.log`，重启回放；可选 `--fsync` |
| 断线补发 | 客户端 `Last-Event-ID: N`，服务端补发所有保留窗口内 `ID > N` 的事件，随后无缝衔接实时事件 |
| 接缝保证 | 补发快照与订阅注册在同一临界区完成，补发期间到达的事件进入 pending，激活后严格按序输出——不重不漏 |
| 心跳 | 定时发送 `: <comment>` 注释帧（默认 15s），SSE 解码器忽略，不产生事件 |
| 慢消费者限额 | 每订阅者有界缓冲（默认 64）。补发期 pending 或实时 channel 满时，**仅断开该连接**并发送无 id 的 `error` 帧，发布方永不阻塞 |
| 过旧游标 | `Last-Event-ID+1 < oldest` 或游标大于最新 ID 时，返回一个无 id 的 `event: reset` 控制帧（携带 `oldest/last/reason`）后关闭连接，要求客户端丢弃本地状态从 `oldest-1` 重连 |
| 日志保留 | 按事件条数（默认 10000）与年龄（默认 24h）淘汰；淘汰累积后原子压缩重写 WAL |
| 多行 data | 负载中的 `\n` 按 SSE 规范输出为多个 `data:` 行；`\r\n`/`\r` 统一规范化为 `\n`，客户端用 `\n` 拼回 |

控制帧（`reset` / `error`）**不携带 `id:` 字段**，因此不会污染浏览器/客户端的
last-event-ID 缓冲区，重连游标保持不变。

## 目录结构

```
cmd/sse-server/      服务端入口
cmd/sse-client/      演示客户端（自动重连、按 ID 去重、reset 处理）
internal/store/      WAL 持久化日志（回放、保留、压缩、torn-write 修复）
internal/broker/     订阅管理、补发/实时两阶段交接、慢消费者策略
internal/server/     net/http 路由与 SSE 帧写出
internal/sse/        SSE 帧编码与规范解码器（服务端与示例客户端共用）
```

## 依赖

- Go 1.23+（开发与验证版本：go1.23.4 linux/amd64）
- **零第三方依赖**：仅使用 Go 标准库（`net/http`、`encoding/json`、`os` 等）。
- 依赖锁定：`go.mod` 通过 `go 1.23` 指令锁定最低工具链；因不引用任何外部模块，
  无需 `go.sum`，`go mod verify` 通过，构建可离线复现。

## 构建

```bash
go build ./...
go test -race ./...
```

## 启动

```bash
# 默认 :8080，WAL 在 ./data，保留 10000 条 / 24h，心跳 15s
go run ./cmd/sse-server

# 测试/演示常用小配置
go run ./cmd/sse-server \
  -addr :18080 \
  -data-dir /tmp/sse-data \
  -retention-events 100 \
  -retention-age 1h \
  -heartbeat 2s \
  -sub-buffer 16
```

全部参数也支持环境变量：`SSE_ADDR`、`SSE_DATA_DIR`、`SSE_RETENTION_EVENTS`、
`SSE_RETENTION_AGE`（如 `1h`）、`SSE_HEARTBEAT`、`SSE_SUB_BUFFER`、`SSE_FSYNC`。

## HTTP 接口

### `POST /events` — 发布事件

```bash
# JSON
curl -s -X POST http://127.0.0.1:8080/events \
  -H 'Content-Type: application/json' \
  -d '{"data":"hello\nsecond line"}'
# => 201 {"id":1,"data":"hello\nsecond line","ts":"2026-09-24T...Z"}

# 或纯文本
curl -s -X POST http://127.0.0.1:8080/events --data-binary 'raw text payload'
```

请求体上限默认 1 MiB；多行内容直接放在 `data` 中（JSON 转义），服务端按规范拆行。

### `GET /events` — 订阅 / 断点续传

```bash
# 新客户端：建立实时流（不带 Last-Event-ID 表示从当前时刻开始）
curl -N http://127.0.0.1:8080/events

# 断线重连：带上最后收到的事件 ID，服务端先补发后实时
curl -N -H 'Last-Event-ID: 42' http://127.0.0.1:8080/events
```

普通事件帧：

```
id: 43
event: message
data: hello
data: second line

```

心跳（默认每 15s）：

```
: heartbeat 2026-09-24T10:00:00.000Z

```

游标过旧/超前（HTTP 仍为 200，随后服务端关闭连接）：

```
event: reset
data: {"reason":"expired","oldest":16,"last":120,"note":"Last-Event-ID is outside the retained window; discard local state and reconnect with Last-Event-ID: <oldest-1>"}

```

慢消费者被断开：

```
event: error
data: {"reason":"slow_consumer"}

```

> 客户端正确做法：收到 `reset` 后清空本地去重表，以 `oldest-1` 作为
> `Last-Event-ID` 重连；收到 `error: slow_consumer` 后用原游标立即重连补发。
> 参考客户端 `cmd/sse-client` 已实现这两条路径。

### `GET /stats`、`GET /healthz`

```bash
curl -s http://127.0.0.1:8080/stats
# {"oldest_id":1,"last_id":120,"retained_events":100,"slow_drops":0,"subscribers":1}
curl -s http://127.0.0.1:8080/healthz   # ok
```

## 示例客户端

```bash
go run ./cmd/sse-client publish "first event"
go run ./cmd/sse-client publish "second event"

# 订阅 10s；每收到 3 条事件强制断一次线并以 Last-Event-ID 重连
go run ./cmd/sse-client stream -duration 10s -reset-every 3 -v
```

客户端维护「最大 ID 游标 + 去重集合」，断线自动重连，退出时打印汇总：

```
summary: connections=4 unique_events=12 range=[1,12] duplicates_dropped=3 missing=[] gap_alerts=0
```

`missing` 非空或出现 gap 告警时进程以非零码退出，可直接用于验收脚本。

## 端到端手动验收步骤

终端 1：

```bash
go run ./cmd/sse-server -addr :18080 -retention-events 5 -heartbeat 1s -sub-buffer 4 -data-dir /tmp/sse-demo
```

终端 2（观察端，每 2 条断一次线）：

```bash
go run ./cmd/sse-client stream -addr http://127.0.0.1:18080 -duration 30s -reset-every 2 -v
```

终端 3（持续发布）：

```bash
for i in $(seq 1 20); do
  go run ./cmd/sse-client publish -addr http://127.0.0.1:18080 "msg-$i"
  sleep 0.3
done
```

观察点：

1. **补发/实时交界断线**：终端 2 反复在补发与实时交界处断线重连，汇总 `missing=[]`。
2. **重复重连去重**：日志可见重复 ID 被丢弃（`duplicates_dropped>0`），最终区间连续无缺口。
3. **保留边界 reset**：保留仅 5 条，用一个很旧的游标连接：
   ```bash
   curl -N -H 'Last-Event-ID: 1' http://127.0.0.1:18080/events
   ```
   立即收到 `event: reset`（reason=expired，含当前 oldest/last）后连接关闭；
   按提示以 `oldest-1` 重连可拿到完整保留窗口。
4. **慢消费者**：将 `-sub-buffer` 设为 2，用一个不读取的客户端挂着，再发布
   足够多/足够大的事件填满对端 TCP 接收缓冲，该连接收到
   `event: error {"reason":"slow_consumer"}` 后断开，`/stats` 的 `slow_drops`
   增加，其他订阅者与发布不受影响。注意：loopback 内核接收缓冲较大，用小消息
   短时间未必触发——有界缓冲的强制断开在 broker 层测试中被严格覆盖
   （`TestSlowConsumerLiveDropped`、`TestSlowConsumerDuringReplayDropped`），
   HTTP 层 error 帧契约由 `TestAcceptance_SlowConsumerDisconnected` 覆盖。
5. **重启持久化**：Ctrl-C 后用同一 `-data-dir` 重新启动，`/stats` 的
   `oldest_id/last_id` 保持，新事件 ID 从 `last+1` 继续单调递增。

## 实际验证结果（go1.23.4 linux/amd64）

以下为本次交付时的真实运行记录（非模拟）：

- `go vet ./...` 通过；`go mod verify` → `all modules verified`；无 `go.sum`（零外部依赖）。
- `go test -race -count=5 ./...` 连续 5 轮全部通过（sse / store / broker / server 四个包）。
- 真实 HTTP 端到端：
  - 单调 ID：连续 POST 返回 id 1..8；多行负载 `a\nb\nc` 在线上编码为 3 个 `data:` 行，
    按规范解码拼回为 `a\nb\nc`；CRLF 输入规范化为 LF。
  - 保留边界：`-retention-events 5` 发布 8 条后 stats 为 `oldest_id=4 last_id=8 retained=5`；
    `Last-Event-ID: 1` 收到 `event: reset reason=expired oldest=4 last=8` 后连接关闭；
    改用 `Last-Event-ID: 3`（=oldest−1）完整补发 4..8。
  - 重复补发：对同一游标 `Last-Event-ID: 65` 连续请求 3 次，服务端每次都重发 66..70（各 5 条）。
  - 接缝断线 + 重复重连：示例客户端 `-reset-every 1 -backoff 80ms`，在 21..70 发布期间
    建立 51 次连接，汇总 `unique_events=50 range=[21,70] missing=[] gap_alerts=0`。
  - 心跳：`: heartbeat <RFC3339>` 注释帧被解码器忽略，空闲后首个真实事件 id 仍连续。
  - 持久化：向数据目录写入 1..70 后重启，stats 保持 `last_id=70`，再发布得到 id=71。
- 慢消费者的"真实网络 stall"在 loopback 上用小数据包不稳定（内核/transport 缓冲会吸收），
  因此该限额的强制断开以 broker 层确定性测试为准，HTTP 帧契约单独测试——此处如实说明，
  未在 README 中夸大为已通过端到端真实断流复现。

## 设计说明：补发/实时接缝为何不重不漏

- `Subscribe` 在 broker 锁内完成「读历史快照 + 注册订阅者」；`Publish` 在同一把
  锁内完成「落盘 + fanout」。因此任一事件要么在补发快照里，要么走实时通道，不会两处都出现。
- 快照在锁外写网络（避免磁盘/网络 I/O 持有全局锁）。补发期间订阅者处于
  `replaying` 状态，实时事件追加到有界 `pending`；补发写完后 `Activate`
  原子切换：先输出 pending 再消费实时 channel，严格按 ID 升序。
- 补发快照与 pending 是两份独立切片，handler 锁外读快照与 broker 锁内追加
  pending 之间无数据竞争。

## 自动化测试

```bash
go test -race -count=1 ./...
```

- `internal/sse`：多行编码、无 id 控制帧、CRLF、id 缓冲持久化、截断帧不派发、行尾规范化
- `internal/store`：单调 ID、重启恢复与 ID 延续、条数/年龄保留与压缩、torn-write 修复
- `internal/broker`：接缝无重无漏、补发期并发发布、过期/未来游标 reset、实时与补发期慢消费者断开
- `internal/server`：HTTP 级验收——接缝断线+重复重连去重无缺口、保留边界 reset、
  心跳不产生事件、慢消费者 error 帧、多行 data、非法游标 400、stats/healthz

## 已知限制 / 未完成项

- 单进程单事件流（单主题）。多主题/分区需要扩展 store 与 broker 的 key 维度。
- 单实例，无副本复制；持久化靠本地 WAL（`--fsync` 关闭时崩溃可能丢失最后若干条未落盘事件）。
- 补发无上限窗口大小保护：保留窗口本身是补发上限（条数受 `-retention-events` 约束），超大窗口一次性补发会占用带宽，未做分页。
- 事件负载上限 1 MiB；无鉴权/TLS（建议放在反向代理后）。
