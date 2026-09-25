# RUNLOG — 实际运行记录

日期：2026-09-25，环境：Linux 6.8.0-90-generic,Go 1.22.2 linux/amd64。
以下命令均在仓库根目录实际执行，结果如实记录，含开发过程中的未通过项。

## 最终验证（全部通过）

```
$ go vet ./...
(无输出，通过)

$ go test ./...
ok  streamback/internal/clock
ok  streamback/internal/server
ok  streamback/internal/stream
ok  streamback/internal/upstream

$ go test -race -count=10 ./...
ok  streamback/internal/clock   1.021s
ok  streamback/internal/server  10.104s
ok  streamback/internal/stream  1.024s
ok  streamback/internal/upstream 1.634s

$ go test -cover ./...
ok  streamback/internal/clock    coverage: 92.3%
ok  streamback/internal/server   coverage: 83.1%
ok  streamback/internal/stream   coverage: 91.3%
ok  streamback/internal/upstream coverage: 88.9%
```

覆盖率说明：`internal/client` 单包覆盖率显示 0.0%，因为它被
`internal/server` 的集成测试跨包驱动；`go test -coverpkg=./... ./...` 下
server 测试二进制对整个模块的覆盖为 65.2%。`cmd/*` 为薄入口，无单测。

## 实机演示（真实进程，非测试）

服务器：`/tmp/streamback-server -addr 127.0.0.1:<port> -buffer-slots 4`
（首次使用 18080/18099 端口均因被本机其他进程占用而 bind 失败，改用空闲端口；
另有并行进程写同一 `/tmp/server.log` 造成一行串行日志，与本服务行为无关。)

1. 快客户端：5 项，seqs [1..5],`{"type":"end","sent":5}`,completed=true,1ms。
2. 慢客户端（read-delay 20ms)：同样收齐 5 项，耗时 20ms > 快客户端。
3. 单项 999999B > maxItemBytes 131072B:HTTP 413,
   `{"error":"ITEM_TOO_LARGE","message":"itemBytes 999999 exceeds max 131072"}`,
   客户端退出码 1。
4. `failAt=3&failMsg="disk on fire"`：收到 seqs [1,2] 后收到
   `{"type":"error","sent":2,"code":"UPSTREAM_FAILURE","message":"disk on fire"}`。
5. 断连客户端（读 3 条后断开）:`disconnected=true`；随后 `GET /stats` 返回
   `{"bufferedBytes":0,"highWaterBytes":320,"activeStreams":0}` —— 服务端
   检测到断连并释放全部缓冲。
6. 优雅关闭：慢流进行中向服务进程发 SIGTERM，客户端收到 25 项后收到
   `{"type":"error","sent":25,"code":"SERVER_SHUTDOWN"}`(`sent` 与实收一致）,
   服务日志 `received terminated, shutting down` / `shutdown complete`，正常退出。
7. curl 样例输出与 README 中"请求样例"一节一致（NDJSON 记录、413 响应头、
   /stats JSON)。

## 开发过程中的未通过项及修复（如实记录）

1. **`q.Get undefined`**（编译失败）:`r.URL.Query` 是方法，改为 `r.URL.Query()`。
2. **高水位超界（2048 > 1024)**：初版 `BufferedBytes` 在入队前计数、写出后
   扣减，口径含"通道+消费者处理中+生产者待发送"。改为入队前计数、出队即扣减，
   语义为"已生产但写协程未取走"，上界精确为 slots+2 项（含消费交接窗口）。
3. **慢客户端看不到背压（水位=1 项）**：两个内核因素 —— (a) 客户端
   `bufio.Scanner` 贪婪读空内核缓冲，应用层"慢"传不到 TCP，改为对 socket
   读取本身限流（`throttleReader`);(b) 回环发送缓冲自动调优可达数 MB，测试
   中通过 `ConnState` 钩子将服务端 `SO_SNDBUF` 限为 16KB、客户端
   `SO_RCVBUF` 限为 16KB，制造真实 TCP 背压（已用原始 TCP 探针验证：128KB
   记录写入阻塞约 69ms/条）。
4. **`bufio.Scanner` 行宽限制失效**：有效上限是 `max(max, cap(初始缓冲))`,
   初始 64KB 缓冲盖过了测试上限，改为初始缓冲不超过 maxLineBytes。
5. **竞态：`ts.Config.ConnState` 在 `httptest.NewServer` 启动后赋值** ——
   改用 `NewUnstartedServer` + `Start()`。
6. **竞态：`WaitGroup.Add`（每请求）与 `Wait`（关闭时）并发** —— 关闭等待
   改为基于原子计数器 `ActiveStreams` 的轮询。
7. **`-race` 下水位恒为 0**：字节预算信号量逐字节做 channel 操作（128KB 项 =
   13 万次插桩 channel 操作），记账开销压垮生产者。改为 O(1) 互斥锁+广播
   实现。
8. **并发测试水位 3584 > 3072**：消费者"已取走未扣减"的交接窗口使瞬时值
   多 1 项，每流精确上界修正为 `(BufferSlots+2)` 项，测试与文档同步更新。

## 已知限制

- `cmd/server`、`cmd/client` 主入口无单元测试（薄壳，逻辑均在 internal 包）。
- 背压集成测试依赖缩小内核 socket 缓冲来穿透回环优化；在极端内核配置下
  （如强制巨大 wmem 下限）可能需调整测试参数。
- 协议为单向流（服务端→客户端），无客户端→服务端应用层确认；背压依赖
  TCP 流控，与真实部署一致。
