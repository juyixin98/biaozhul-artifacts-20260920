# streamback — 流响应背压演示服务

纯后端 Go 项目：本地 HTTP 分块结果流服务 + 故障注入客户端。所有"上游"数据来自
**本进程内的假服务**（`internal/upstream`)，不接任何生产系统。时钟可注入
（`internal/clock`)，客户端输出结构化 JSON 测试结果。

## 功能

- **分块结果流**:`GET /stream` 以 NDJSON（每行一条 JSON 记录）流式返回结果项。
- **有界缓冲 + 上游背压**：每个请求一个生产者 goroutine 从上游拉取数据，进入
  双重有界缓冲（槽位数 `BufferSlots` + 字节预算 `BufferBytes`)。慢客户端使缓冲
  填满后，生产者阻塞，从而停止从上游拉取——背压传导到数据源。每个在途请求的
  内存上界为 `(BufferSlots+2)` 项且不超过 `BufferBytes` + 1 项（写出中）。
- **单项过大明确拒绝**：超过 `MaxItemBytes` 的请求在开始流式输出前返回
  `413` 和 JSON 错误体。
- **协议尾记录表达中途错误**：已发出部分结果后若上游失败或服务关闭，以
  `{"type":"error",...}` 尾记录结束流（此时 HTTP 状态码已无法修改）。
- **可控时钟**：上游延迟全部通过 `clock.Clock` 接口；测试使用手动时钟，无需
  真实等待。
- **结构化测试结果**：客户端每次运行输出 JSON（收到项、序列、尾记录、错误码、
  耗时等），可直接用于断言。

## 协议

响应 `Content-Type: application/x-ndjson`，每行一条记录：

```json
{"type":"item","seq":1,"bytes":8,"payload":"aaaaaaaa"}
{"type":"end","sent":3}
{"type":"error","sent":2,"code":"UPSTREAM_FAILURE","message":"boom"}
```

尾记录错误码：`UPSTREAM_FAILURE`（上游失败）、`SERVER_SHUTDOWN`（服务关闭）、
`ITEM_TOO_LARGE`（单项过大，流前以 413 拒绝）。

## 构建与运行

```sh
go build ./...
go build -o /tmp/streamback-server ./cmd/server
go build -o /tmp/streamback-client ./cmd/client

# 启动服务（参数均有默认值）
/tmp/streamback-server -addr 127.0.0.1:8080 \
  -buffer-slots 4 -buffer-bytes 262144 -max-item-bytes 131072
```

## 请求样例

`/stream` 查询参数（即故障注入点）:`count`、`itemBytes`、`itemDelayMs`、
`failAt`、`failMsg`。

```sh
# 正常流
curl -N 'http://127.0.0.1:8080/stream?count=3&itemBytes=8'
# {"type":"item","seq":1,"bytes":8,"payload":"aaaaaaaa"}
# {"type":"item","seq":2,"bytes":8,"payload":"bbbbbbbb"}
# {"type":"item","seq":3,"bytes":8,"payload":"cccccccc"}
# {"type":"end","sent":3}

# 上游在第 3 项处注入失败 -> 已发 2 项 + 错误尾记录
curl -N 'http://127.0.0.1:8080/stream?count=5&itemBytes=8&failAt=3&failMsg=boom'
# {"type":"item","seq":1,"bytes":8,"payload":"aaaaaaaa"}
# {"type":"item","seq":2,"bytes":8,"payload":"bbbbbbbb"}
# {"type":"error","sent":2,"code":"UPSTREAM_FAILURE","message":"boom"}

# 单项过大 -> 413
curl -i 'http://127.0.0.1:8080/stream?count=3&itemBytes=200000'
# HTTP/1.1 413 Request Entity Too Large
# {"error":"ITEM_TOO_LARGE","message":"itemBytes 200000 exceeds max 131072"}

# 内存/活动流观测
curl -s 'http://127.0.0.1:8080/stats'
# {"bufferedBytes":0,"highWaterBytes":24,"activeStreams":0}
```

## 故障注入客户端

```sh
# 快客户端：尽快消费
/tmp/streamback-client -mode fast  -url 'http://127.0.0.1:8080/stream?count=10&itemBytes=1024'

# 慢客户端：限制 socket 读取速率，触发服务端有界缓冲与背压
/tmp/streamback-client -mode slow -read-delay 20ms -url 'http://127.0.0.1:8080/stream?count=10&itemBytes=1024'

# 断连客户端：读 3 条记录后断开
/tmp/streamback-client -mode disconnect -disconnect-after 3 \
  -url 'http://127.0.0.1:8080/stream?count=1000&itemBytes=64&itemDelayMs=5'
```

每次运行输出结构化 JSON，例如上游错误时：

```json
{
  "mode": "fast",
  "statusCode": 200,
  "itemsReceived": 2,
  "seqs": [1, 2],
  "trailer": {"type":"error","sent":2,"code":"UPSTREAM_FAILURE","message":"boom"},
  "completed": false,
  "errorCode": "UPSTREAM_FAILURE"
}
```

## 测试

```sh
go test ./...            # 单元 + 集成测试
go test -race ./...      # 竞态检测
go test -cover ./...     # 覆盖率
```

集成测试（`internal/server/server_test.go`）覆盖验收项：

- `TestFastAndSlowClientsConcurrent` — 快慢客户端并发，序列一致（1..N 连续），
  内存高水位 ≤ 上界，结束后缓冲归零。
- `TestSlowClientTriggersBackpressure` — 慢客户端使缓冲填满但严格有界
  （通过缩小客户端 `SO_RCVBUF` 与服务端 `SO_SNDBUF`，在回环上制造真实 TCP
  背压）。
- `TestOversizedItemRejected` — 413 明确拒绝。
- `TestUpstreamErrorAfterPartialResults` — 部分结果后错误以尾记录表达。
- `TestClientDisconnect` — 断连后服务端释放全部资源。
- `TestGracefulShutdownSendsTrailer` — 关闭时在途流收到 `SERVER_SHUTDOWN` 尾记录。
- `TestSequenceConsistencyAcrossRuns` — 多次运行序列一致。

实际运行记录见 [RUNLOG.md](RUNLOG.md)。

## 结构

```
cmd/server          HTTP 服务入口（信号处理、优雅关闭）
cmd/client          故障注入客户端入口（输出结构化 JSON）
internal/clock      可控时钟（Real / Manual)
internal/upstream   本进程假上游服务（延迟、尺寸、失败注入）
internal/stream     NDJSON 协议编解码（有界行宽）
internal/server     流式 HTTP 服务（有界缓冲、背压、统计、关闭）
internal/client     客户端库（fast / slow / disconnect 模式）
```
