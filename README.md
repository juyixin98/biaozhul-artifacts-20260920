# WebSocket 消息重组（Go / net/http，纯后端）

用 Go **标准库**从零实现的本地 WebSocket（[RFC 6455](https://www.rfc-editor.org/rfc/rfc6455)）
帧状态机与分片消息重组服务。不使用 `gorilla/websocket`、`nhooyr.io/websocket` 等任何第三方库，
也没有前端界面；提供 HTTP 接口和可运行的原始 socket 演示客户端。

## 功能范围

- **流式帧解析状态机**（`ws.FrameParser`）：推送式 `Feed([]byte)`，按任意分块喂入都能增量解析。
  - 解析 FIN/RSV/opcode、7/16/64 位载荷长度（含非最小编码校验）、掩码键。
  - 支持客户端掩码（自动解掩码）、文本与二进制分片、125 字节上限的控制帧。
  - 帧大小上限：在读到载荷前就根据长度字段拒绝，避免为超大帧分配内存（关闭码 1009）。
- **分片消息重组状态机**（`ws.Reassembler`）：
  - 文本/二进制起始帧 + 任意数量 continuation 帧重组为完整消息。
  - 消息总大小上限（默认 4 MiB），在每个分片追加时检查。
  - **控制帧可穿插在分片之间**（ping/pong/close 不打断未完成的消息）。
  - 错误延续帧、嵌套起始帧、未掩码、RSV 非零、保留 opcode 等一律按 1002 拒绝。
  - close 帧载荷（状态码 + UTF-8 reason）合法性校验（关闭码白名单）。
- **跨分片 UTF-8 校验**：增量 DFA 校验器（见 `ws/utf8.go`），一个多字节字符可以在任意字节处被切开；
  消息结束时若多字节序列未完成（“半个字符”悬空）判 1007。
- **关闭握手**：服务端收到 close 回显同状态码/reason，再排空输入 5 秒后关 TCP；
  协议/策略错误时主动发对应关闭码并立即关闭。
- **HTTP 层**：基于 `net/http` + `http.Hijacker` 手工完成 101 升级，校验
  `Connection`/`Upgrade`/`Sec-WebSocket-Version`/`Sec-WebSocket-Key`，错误请求返回标准 4xx。

## 目录结构

```
.
├── go.mod                     # 模块定义；无第三方依赖（无 go.sum）
├── ws/                        # 核心库
│   ├── frame.go               # FrameParser 帧状态机（掩码、长度、控制帧）
│   ├── write.go               # 帧编码器（服务端不掩码 / 客户端掩码）
│   ├── utf8.go                # 增量 UTF-8 DFA 校验器（跨分片）
│   ├── reassemble.go          # Reassembler 分片消息状态机
│   ├── server.go              # HTTP 升级、Conn、默认回显 Serve
│   ├── frame_test.go          # 帧解析 + 随机分块等价性测试
│   ├── utf8_test.go           # UTF-8 DFA 与标准库交叉验证
│   ├── reassemble_test.go     # 重组/控制帧/UTF-8/大小限制 + 随机分块等价
│   └── server_test.go         # 真实 HTTP 服务器端到端测试
├── cmd/
│   ├── server/main.go         # HTTP 服务入口（/ws、/health、/）
│   └── client/main.go         # 原始 socket 演示客户端，跑 11 个验收场景
├── REQUESTS.md                # HTTP 接口说明 + 请求样例（含手工构造帧）
└── README.md                  # 本文件
```

## 环境与依赖

- Go **1.23+**（实测 go1.23.1 / linux-amd64；go.mod 声明 `go 1.23`）。
- 依赖：**仅 Go 标准库**（`net/http`、`crypto/sha1`、`encoding/base64`、`bufio` 等）。
  因此 `go.mod` 中没有 require，**没有 `go.sum`**——依赖即工具链本身，已由 go.mod 的 go 版本锁定。
- 构建不需要联网（不下载任何模块）。

## 启动与运行

```bash
# 1) 启动服务（默认 :8080）
go run ./cmd/server -addr :8080 -max-frame 1MiB -max-message 4MiB
# 或先编译
go build -o ws-server ./cmd/server
./ws-server -addr :8080

# 参数：
#   -addr         监听地址（默认 :8080）
#   -max-frame    单帧载荷上限：纯字节数(4096) 或 1K/1KiB/2M/2MiB/1G（默认 1MiB）
#   -max-message  重组消息上限，语法同上（默认 4MiB）

# 2) 另开终端：普通 HTTP 接口
curl -s http://127.0.0.1:8080/health      # {"status":"ok"}

# 3) 运行演示客户端（自动完成握手 + 11 个场景）
go run ./cmd/client -url ws://127.0.0.1:8080/ws \
  -max-frame 1048576 -max-message 4194304
```

客户端的 `-max-frame/-max-message` 必须与服务端一致，用于正确构造“超长”测试。

## 自动化测试

```bash
go test ./...            # 全部
go test ./ws/ -v         # 详细
go test -race ./...      # 竞态检测
go test ./... -count=30  # 重复 30 次（端到端走真实 TCP，验证稳定性）
```

测试覆盖的重点：

- `TestFrameParserRandomChunks` / `...Stress`：把同一批帧一次喂入 vs 按 1–64 字节随机分块
  （以及 1–3 字节极端分块）喂入，断言解析结果逐帧一致 —— 对应“随机分块喂入应与一次性解析相同”。
- `TestReassemblerRandomChunks`：重组层也做“整批直送 vs 编码后随机分块→解析→重组”的事件等价性。
- `TestEndToEndRandomChunks`：真实 TCP 上逐字节发送一条分片消息，验证回显完整。
- `TestUTF8ValidatorRandom`：2 万个随机字节串与标准库 `unicode/utf8.Valid` 结果交叉比对；
  `TestUTF8ValidatorIncremental` 在每个可能切点切分同一字符串。
- 控制帧分片、错误延续帧、半个多字节字符（悬空）、续帧内非法字节、单帧/消息超长、
  close 载荷非法、RSV/保留 opcode/未掩码、关闭握手等均有正反用例。

## 实测结果（2026-09-23，本机如实记录）

- `go vet ./...`：无输出（通过）。
- `go test ./... -count=1`：`ok wsreassemble/ws`，**30 个顶层测试函数全部通过**
  （另含若干子测试；`cmd/*` 无测试文件）。
- `go test -race ./... -count=2`：通过，无数据竞争。
- `go test ./ws/ -count=30`：30 次重复全部通过（端到端走真实 TCP，无偶发挂起）。
- 实跑服务器 + 演示客户端：**11 个场景全部 `PASS`，`ALL SCENARIOS PASSED`**。
  关键结果：非法 UTF-8 / 悬空半字符 → 服务端关闭码 1007；
  错误续帧 / 控制帧分片 → 1002；单帧超长 / 分片累加超长 → 1009；
  客户端发起 close(1000,"goodbye") → 服务端回显 1000 并完成握手。
  场景 9 中客户端继续写 1 MiB 载荷时会收到 TCP RST——这是服务端在读完超长帧头后
  立即关闭的预期表现，客户端已将该写错误作为预期情况处理。

> 开发过程中修复的一个真实竞态：一次 TCP 读取可能同时得到“pong + 回显”两个帧，
> 客户端若只取第一个事件会丢掉回显导致死等；已用事件队列修复，并用 30 次重复 + race 验证。

## 设计说明

- 两个状态机都**不持有 goroutine**：解析器/重组器由调用方用 `Feed` / `HandleFrame` 驱动，
  天然可逐字节喂入，便于测试“分块无关性”。
- `FrameParser` 内部维护残留缓冲区，长度字段读全后先与上限比较再分配载荷，
  且解析出的 Payload 总是拷贝 + 解掩码，调用方复用输入缓冲区不会污染已返回的帧。
- 文本消息在每个分片到来时用增量 DFA 实时校验，仅在 FIN 帧检查“序列已结束”，
  因此跨帧的半字符在中途合法、在结尾非法。
- 帧/消息大小上限是两层独立防御：帧层防单个恶意大帧，消息层防“小帧分片洪水”。

## 未完成项 / 已知限制（如实说明）

- **未实现 permessage-deflate 等扩展协商**：收到 RSV 非零帧直接 1002 拒绝。
- **未实现 Sec-WebSocket-Protocol / Sec-WebSocket-Extensions 子协议协商与 Cookie/鉴权**。
- 默认处理器是**原样回显**（text/binary 都回），没有房间/广播等业务逻辑；
  库本身提供 `ws.Upgrade` + `Conn.ReadMessage/WriteMessage`，可自行替换 `cmd/server` 的处理循环。
- 服务端回显不做分片（始终单 FIN 帧）；消息若远大于单次写缓冲，依赖 `net.Conn.Write` 的拆分。
- 关闭握手的“排空等待”固定为 5 秒；不支持对端半关闭时的额外优雅期配置。
- 演示客户端仅支持 `ws://`（明文），不支持 `wss://`（TLS）；生产部署应在前面放 TLS 终止代理。
- 1009 关闭帧可能在客户端写完超大载荷前就随 RST 到达，极端情况下客户端可能只读到连接重置
  而非关闭码（本机场景 9 能稳定读到 1009，但这依赖时序）；这符合 RFC 允许立即关闭的规定。
