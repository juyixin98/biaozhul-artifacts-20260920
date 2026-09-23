# WebSocket 消息重组（Go / net/http，纯标准库）

从零实现的本地 RFC 6455 WebSocket **帧状态机**：不依赖 gorilla/websocket 等任何
第三方库，仅使用 Go 标准库（`net/http`、`net`、`crypto/sha1` 等）。
纯后端项目，无前端界面，提供 HTTP/WebSocket 接口与多种请求样例。

## 功能覆盖

- **客户端掩码**：服务端强制校验客户端帧必须带掩码（RFC 6455 §5.1），自动去掩码；
  同时支持反向配置（客户端解码器要求服务端帧不掩码）。
- **文本/二进制分片重组**：`text/binary` 起始帧 + 任意数量 `continuation`，
  帧长度支持 7 位 / 16 位 / 64 位三档。
- **控制帧穿插**：`Ping`/`Pong` 可以出现在分片消息中间；控制帧必须未分片且
  载荷 ≤ 125 字节，违例返回 1002。
- **关闭握手**：解析/校验 Close 帧（状态码合法性、原因必须为 UTF-8），
  收到后回发对等关闭码；Close 之后再到数据判为协议错误。
- **大小限制**：单帧上限（默认 1 MiB）与重组消息上限（默认 4 MiB）独立可配，
  超限返回 1009。
- **跨分片 UTF-8 校验**：显式字节级 DFA，支持多字节字符跨帧挂起，
  正确拒绝 overlong 编码、UTF-16 代理项、超 U+10FFFF、帧边界处的半个多字节字符
  （终结时返回 1007）。
- **分块无关性**：解码器与 I/O 解耦，任意大小分块 `Feed` 的结果与一次性
  喂入完全一致（有随机分块等价性测试保证）。

## 目录结构

```
.
├── go.mod                         # 模块定义；零外部依赖，go.mod 即依赖锁（无 go.sum）
├── ws/
│   ├── frame.go                   # 操作码/关闭码/限制常量、Event 类型
│   ├── errors.go                  # FrameError（携带建议关闭码）
│   ├── utf8.go                    # 跨分片增量 UTF-8 状态机
│   ├── decoder.go                 # 帧状态机：Feed([]byte) -> []Event
│   ├── writer.go                  # 帧编码器（服务端不掩码/客户端掩码）
│   ├── server.go                  # net/http Hijack 握手升级 + Conn
│   ├── decoder_test.go            # 状态机单元测试（全部验收场景 + 随机分块等价性）
│   └── integration_test.go        # 真实 net/http 端到端测试 + 编码器往返测试
├── cmd/
│   ├── wsserver/main.go           # echo 服务端
│   └── wsclient/main.go           # 裸 TCP 演示客户端（可分片/穿插 ping）
└── scripts/raw_client.py          # 裸 socket 手工发帧，触发各错误场景
```

## 依赖

- Go 1.23+（仅用标准库；1.21+ 理论可用，未在更旧版本验证）。
- 运行时依赖：**无**。`go list -m all` 只输出本模块；没有 `go.sum`，
  构建不访问网络、不下载模块。
- 可选：python3（仅 `scripts/raw_client.py` 错误场景演示需要）。

## 启动命令

```bash
# 在项目根目录
go run ./cmd/wsserver                         # 默认监听 :8080
go run ./cmd/wsserver -addr :18080            # 指定端口
go run ./cmd/wsserver -maxframe 4096 -maxmsg 65536   # 自定义大小限制
go run ./cmd/wsserver -idle 30s               # 空闲读超时（默认 120s，0=不限）
```

或先编译：

```bash
go build -o wsserver ./cmd/wsserver
./wsserver -addr :8080
```

## HTTP / WebSocket 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET  | `http://host:8080/healthz` | 健康检查，返回 200 `ok` |
| GET  | `ws://host:8080/ws` | WebSocket 升级端点（RFC 6455 握手） |

`/ws` 握手要求（来自 net/http 层的校验）：

- `GET` 方法、HTTP/1.1；
- `Connection: Upgrade`（允许逗号列表中的 token）、`Upgrade: websocket`；
- `Sec-WebSocket-Version: 13`；
- `Sec-WebSocket-Key`：16 字节随机数的 base64；
- 服务端按 §4.2.2 回 `Sec-WebSocket-Accept`。

升级后是 echo 语义：收到完整文本/二进制消息原样回显；`Ping` 自动回同载荷
`Pong`；`Close` 回对等关闭码（空码则回 1000）；协议错误回对应关闭码后断开。

## 请求样例

### 1. 健康检查

```bash
curl -i http://127.0.0.1:8080/healthz
# HTTP/1.1 200 OK
# ok
```

### 2. 握手失败的普通 GET（返回 400）

```bash
curl -i http://127.0.0.1:8080/ws
# HTTP/1.1 400 Bad Request
```

### 3. Go 演示客户端（掩码、分片、控制帧穿插）

```bash
go run ./cmd/wsclient -url ws://127.0.0.1:8080/ws -msg "你好, WebSocket!"
go run ./cmd/wsclient -url ws://127.0.0.1:8080/ws -msg "你好世界分片重组测试OK" -frag 3 -ping
go run ./cmd/wsclient -url ws://127.0.0.1:8080/ws -binary -msg "binary-data"
```

`-frag N` 按 N 字节切分消息（N=3 发送中文时必然切在 3 字节汉字中间，
用于验证跨分片重组）；`-ping` 在分片间隙插入 Ping 验证控制帧穿插。

### 4. 手工构造错误帧（python3，验证服务端拒绝路径）

```bash
python3 scripts/raw_client.py unmasked            # 未掩码帧        -> 1002
python3 scripts/raw_client.py oversize           # 超 4MiB 消息     -> 1009
python3 scripts/raw_client.py badutf8            # 非法 UTF-8 字节  -> 1007
python3 scripts/raw_client.py half-rune          # 半个多字节字符   -> 1007
python3 scripts/raw_client.py fragmented-control # 分片 Ping        -> 1002
python3 scripts/raw_client.py echo               # 正常 echo + 关闭握手
```

## 测试

```bash
go test ./...                  # 全部测试
go test ./... -race            # 带竞态检测
go test ./ws/ -cover           # 覆盖率
go test ./ws/ -run TestChunkedEquivalence -v   # 随机分块等价性（300 个随机种子）
```

### 验收点对应

| 验收要求 | 对应测试 |
|----------|----------|
| 控制帧分片穿插 | `TestControlFrameInterleaved`、`TestEndToEndFragmentedWithPing` |
| 控制帧自身分片被拒 | `TestFragmentedControlFrameRejected`、`TestOversizedControlFrameRejected` |
| 错误延续帧 | `TestContinuationWithoutStart`、`TestNewStartBeforeFinish`、`TestUnknownOpcode` |
| 半个多字节字符 | `TestHalfMultibyteAcrossFragments`、`TestInvalidUTF8InFragment`、`TestOverlongAndSurrogateRejected` |
| 超长消息 | `TestOversizedMessage`、`TestOversizedMessageAcrossFragments`、`TestOversizedFrame`、`TestEndToEndOversizedMessageRejected` |
| 随机分块 == 一次性解析 | `TestChunkedEquivalence`（300 个种子、1..7 字节随机切分）、`TestChunkedEquivalenceInvalid`、`TestPartialHeaderAcrossFeeds` |
| 关闭握手 | `TestCloseFrameParsing`、`TestDataAfterCloseRejected`、`TestEndToEndFragmentedWithPing` |

## 实际运行结果（本机记录）

环境：`go version go1.23.1 linux/amd64`，Linux 6.8。

- `go test ./... -count=1 -race`：全部通过（ws 包 31 个测试函数，另含表驱动子测试），
  连续多轮无 flake；语句覆盖率 86.8%。
- `go vet ./...`：无告警；`gofmt -l .`：干净。
- 真实双进程实测：
  - `-frag 3 -ping` 发送 32 字节 UTF-8 文本：收到 1 个 `pong`，
    随后收到完整 echo `你好世界分片重组测试OK`，关闭握手 1000；
  - `-frag 1` 单字节切分：echo 一致；
  - raw_client 五种错误场景分别收到关闭码 `1002/1009/1007/1007/1002`，
    与预期一致；正常 echo + close 1000 正常。

## 未完成项 / 已知限制（如实列出）

1. **不支持扩展与压缩**：RSV 位非零一律 1002（未实现 permessage-deflate）。
2. **不支持 wss://（TLS）与 HTTP/2 扩展握手**（RFC 8441）；演示客户端同样
   只支持明文 `ws://`。
3. **未实现客户端侧主动 Ping 心跳调度与自动重连**（编码器和 Pong 解析已具备，
   只是没有后台调度器）。
4. **消息整段缓冲**：重组在内存中累积完整消息，不支持流式/分块交付给应用；
   有 4 MiB 上限兜底，但超大消息场景应改用流式 API。
5. 服务端为演示用途**默认不校验 Origin**（`Upgrader.CheckOrigin` 可注入），
   浏览器跨域部署时必须自行配置。
6. 单连接读循环为同步模型（适合演示状态机）；高并发生产场景需要每连接
   goroutine + 写锁/写队列，本项目未展开。
