# 流响应背压（streambp）

纯后端 Go 项目：本地 HTTP 分块结果流服务 + 故障注入客户端。外部依赖全部用
**进程内假服务**（`internal/upstream`）替代，不接任何生产系统；时钟可注入
（`internal/clock`），客户端输出结构化 JSON 测试结果。

## 功能

- **分块结果流**：`/stream` 以 NDJSON（每行一条 JSON 记录）逐条下发结果，
  每条立即 flush。
- **有界缓冲 + 上游背压**：每条连接一个容量固定的缓冲 channel（默认 8 项），
  另有一个全局限字节信号量（`Limiter`）约束所有连接的在途缓冲总量。慢客户
  端读得慢 → 缓冲填满 → 生产者阻塞 → 上游假服务停止 生产，内存有硬上限。
- **单项过大明确拒绝**：`item_size` 超过 `MaxItemBytes` 时，未发数据前返回
  HTTP 413；流式中途出现超大项时发送 `ITEM_TOO_LARGE` 尾记录。
- **部分结果后的错误用协议尾记录表达**：已发送 N 条数据后上游失败，连接
  不直接断开，而是发一条 `{"type":"error","code":...,"sent":N}` 尾记录，
  客户端可据此对齐已收到的前缀。
- **断连回收**：客户端断开 → 请求 context 取消 → 生产者退出 → 缓冲配额
  全部释放（`/stats` 可观测 `bufferedBytes` 归零）。
- **优雅关闭**：SIGINT/SIGTERM 或 `Close()` 通知在途流，发送
  `SHUTTING_DOWN` 尾记录后退出，不静默丢连接。

## 协议（NDJSON，每行一条记录）

```json
{"type":"data","seq":1,"payload":"item-000000:aaaa..."}
{"type":"data","seq":2,"payload":"item-000001:bbbb..."}
{"type":"end","status":"ok","sent":2}
```

出错时（可能已发过部分数据）以尾记录收尾：

```json
{"type":"error","code":"UPSTREAM_ERROR","message":"upstream: injected failure","sent":2}
```

`code` 取值：`UPSTREAM_ERROR` / `ITEM_TOO_LARGE` / `SHUTTING_DOWN`。
`sent` 为终止前已发送的数据条数，用于客户端校验已收到序列的一致性。

## 接口

| 端点 | 说明 |
|---|---|
| `GET /stream` | NDJSON 结果流。查询参数：`items`、`item_size`、`item_delay_ms`、`fail_after`（故障注入：生产 N 项后失败） |
| `GET /stats` | 结构化观测：活跃连接、在途缓冲字节/上限、已生产/已发送计数 |
| `GET /healthz` | 存活探针 |

## 构建与运行

```bash
go build ./...
go build -o bin/server ./cmd/server
go build -o bin/client ./cmd/client

# 启动服务（有界缓冲 4 项，单项上限 1MiB，全局缓冲上限 16MiB）
./bin/server -addr 127.0.0.1:8080 -buffer 4 -max-item-bytes 1048576

# 快客户端
./bin/client -url "http://127.0.0.1:8080/stream?items=10" -mode fast

# 慢客户端（每条记录前停顿，触发背压）
./bin/client -url "http://127.0.0.1:8080/stream?items=10" -mode slow -read-delay-ms 25

# 故障注入：上游第 3 项后失败 -> 尾记录
./bin/client -url "http://127.0.0.1:8080/stream?items=50&fail_after=3"

# 故障注入：读 4 条后主动断连
./bin/client -url "http://127.0.0.1:8080/stream?items=10000&item_delay_ms=2" -disconnect-after 4
```

客户端每次运行输出一行结构化 JSON 结果，例如：

```json
{"url":"...","httpStatus":200,"received":10,"endStatus":"ok","serverSent":10,"sequenceOk":true,"disconnected":false,"durationMs":229}
```

curl 请求样例见 [samples/requests.sh](samples/requests.sh)。

## 测试

```bash
go test ./... -count=1          # 单元 + 集成测试
go test ./... -count=1 -race    # 竞态检测
```

覆盖的验收场景（`internal/server/server_test.go`、`endpoints_test.go`）：

- 快客户端完整接收、序列一致（`seq` 连续、`sent` 对齐）
- 慢客户端触发背压：运行中轮询 `/stats`，断言 `produced - sent` 不超过
  缓冲容量（+在途 1 项）、`bufferedBytes` 不超过上界
- 快慢客户端并发（4 快 + 4 慢），全部完成且互不阻塞
- 客户端中途断连：连接数与缓冲字节最终归零（无泄漏）
- 上游错误：收到 5 条数据后收到 `UPSTREAM_ERROR` 尾记录
- 单项过大：前置 413；流中超大项 → `ITEM_TOO_LARGE` 尾记录
- 全局限流：全局缓冲字节上限压到小于两条连接的需求量，验证跨连接背压
  且 `bufferedBytes` 永不超限
- 优雅关闭：在途慢流收到 `SHUTTING_DOWN` 尾记录，服务进程干净退出
- 可控时钟：`clock.Fake` 手动推进时间驱动上游延迟（`internal/clock`）

## 实际运行记录（本仓库交付前真实执行）

环境：Go 1.22.2，linux/amd64。

```
$ go vet ./... && go build ./...
BUILD_OK

$ go test ./... -count=1
ok  streambp/internal/clock   0.061s
ok  streambp/internal/server   0.665s
ok  streambp/internal/stream   0.103s

$ go test ./... -count=1 -race
ok  streambp/internal/clock   1.072s
ok  streambp/internal/server   1.726s
ok  streambp/internal/stream   1.115s

$ go test ./internal/... -coverpkg=./internal/...   # 总覆盖率
total: (statements) 84.1%
```

真实进程联跑（server + client 二进制，非 httptest）：

```
# 快客户端：10/10 条，序列一致
{"httpStatus":200,"received":10,"endStatus":"ok","serverSent":10,"sequenceOk":true,"durationMs":52}
# 慢客户端（20ms/条）：同样完整，耗时反映消费速度
{"httpStatus":200,"received":10,"endStatus":"ok","serverSent":10,"sequenceOk":true,"durationMs":229}
# 上游第 3 项后失败：3 条数据 + UPSTREAM_ERROR 尾记录
{"httpStatus":200,"received":3,"endStatus":"error","errorCode":"UPSTREAM_ERROR","serverSent":3,"sequenceOk":true}
# 单项 2MiB 超过 1MiB 上限：HTTP 413，退出码 1
{"httpStatus":413,"endStatus":"none","failure":"http 413: {\"error\":\"item_too_large\",...}"}
# 读 4 条后断连：sequenceOk 仍为 true（已收前缀一致）
{"httpStatus":200,"received":4,"endStatus":"none","sequenceOk":true,"disconnected":true}
# 断连后 /stats：activeConnections=0, bufferedBytes=0（无泄漏）
{"activeConnections":0,"bufferedBytes":0,"producedTotal":27,"sentTotal":27,...}
# 慢流进行中 SIGTERM：收到 38 条后收到 SHUTTING_DOWN 尾记录，服务 exit=0
{"httpStatus":200,"received":38,"endStatus":"error","errorCode":"SHUTTING_DOWN","serverSent":38,"sequenceOk":true}
```

未通过项：无。曾出现的失败（假时钟测试的注册竞态、流水线测试未释放配额）
均为测试代码自身问题，已修复并复跑通过，见 git 历史前的工作记录。

## 结构

```
cmd/server        服务入口（flag 配置、信号处理、优雅关闭）
cmd/client        故障注入客户端入口（输出结构化 JSON 结果）
internal/clock    可控时钟：Real / Fake（测试手动推进）
internal/protocol NDJSON 记录编解码（data / end / error）
internal/upstream 进程内假上游：延迟、故障注入（fail_after）
internal/stream   有界缓冲流水线 + 全局字节信号量（背压核心）
internal/server   HTTP 服务：/stream /stats /healthz、优雅关闭
internal/client   客户端库：快慢模式、断连注入、序列校验
samples/          curl 请求样例
```
