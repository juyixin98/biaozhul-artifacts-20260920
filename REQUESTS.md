# HTTP 接口与请求样例

本服务为纯后端，不提供页面。所有 HTTP 端点如下。

## 1. HTTP 端点一览

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/ws` | WebSocket 升级端点（RFC 6455），回显每条完整文本/二进制消息 |
| `GET` | `/health` | 健康检查，返回 `{"status":"ok"}` |
| `GET` | `/` | 纯文本使用说明 |

### 1.1 普通 HTTP 请求（curl）

```bash
# 健康检查
curl -s http://127.0.0.1:8080/health
# {"status":"ok"}

# 根路径说明
curl -s http://127.0.0.1:8080/

# 未携带升级头 -> 400
curl -s -i http://127.0.0.1:8080/ws
# HTTP/1.1 400 Bad Request
# expected Connection: Upgrade

# 错误的方法 -> 405
curl -s -i -X POST http://127.0.0.1:8080/ws
# HTTP/1.1 405 Method Not Allowed
```

### 1.2 WebSocket 握手

WebSocket 不是普通 HTTP 请求，握手请求必须带以下头：

```http
GET /ws HTTP/1.1
Host: 127.0.0.1:8080
Upgrade: websocket
Connection: Upgrade
Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==
Sec-WebSocket-Version: 13
```

服务端返回：

```http
HTTP/1.1 101 Switching Protocols
Upgrade: websocket
Connection: Upgrade
Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=
```

`Sec-WebSocket-Accept = base64(sha1(Sec-WebSocket-Key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"))`。

curl 无法完成 WebSocket 交互，推荐用仓库自带的演示客户端：

```bash
go run ./cmd/client -url ws://127.0.0.1:8080/ws \
  -max-frame 1048576 -max-message 4194304
```

## 2. 用 bash 手工发送 WebSocket 帧

了解帧字节布局后，也可以用 bash + `/dev/tcp` 发送掩码帧。
客户端帧格式（RFC 6455 §5.2）：

```
 0                   1                   2                   3
 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
+-+-+-+-+-------+-+-------------+-------------------------------+
|F|R|R|R| opcode|M| Payload len |    Extended length (0/2/8)    |
|I|S|S|S|  (4)  |A|     (7)     |             ...               |
|N|V|V|V|       |S|             |   Masking-key (4, if MASK=1)  |
| |1|2|3|       |K|             |   Masked payload data ...     |
+-+-+-+-+-------+-+-------------+-------------------------------+
```

- `FIN=1, opcode=1` 表示单帧文本。
- 客户端必须 `MASK=1`，payload[i] 与 maskKey[i%4] 异或。
- 控制帧 opcode=0x8(close)/0x9(ping)/0xA(pong)，载荷 ≤125 字节且禁止分片。

下面脚本发送一条掩码文本帧 `hello`（掩码键取全 0，故密文=明文），服务端会原样回显；
随后发送 close 帧（状态码 1000 = `0x03E8`）：

```bash
exec 3<>/dev/tcp/127.0.0.1/8080
printf 'GET /ws HTTP/1.1\r\nHost: 127.0.0.1:8080\r\nUpgrade: websocket\r\n' \
  'Connection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n' \
  'Sec-WebSocket-Version: 13\r\n\r\n' >&3
# 读取 101 响应头（略：可用 head -c 读取）
# 文本帧 "hello"：81 85 | mask=00000000 | h e l l o
printf '\x81\x85\x00\x00\x00\x00hello' >&3
# close 帧，状态码 1000：88 82 | mask | 03 E8
printf '\x88\x82\x00\x00\x00\x00\x03\xe8' >&3
exec 3<&-; exec 3>&-
```

> 用十六进制工具（如 `xxd`）查看回显更方便：上面读到的回显帧为
> `81 05 68 65 6c 6c 6f`（服务端不掩码）。

## 3. 演示客户端覆盖的 11 个场景

| # | 场景 | 预期服务端行为 |
|---|---|---|
| 1 | 单条文本消息 | 回显 |
| 2 | 文本分片，分片之间穿插 ping，多字节字符跨帧切开 | 先回 pong，再重组回显 |
| 3 | 二进制消息分片 | 重组回显（opcode=binary） |
| 4 | 半个多字节字符跨两个分片，后续补全 | 合法 UTF-8，回显 |
| 5 | 续帧内出现 `FF FE` 非法字节 | 关闭码 **1007** |
| 6 | 消息结束时只发出一个多字节字符的前 2 字节（悬空） | 关闭码 **1007** |
| 7 | 没有起始帧却发 continuation | 关闭码 **1002** |
| 8 | FIN=0 的 ping（控制帧分片） | 关闭码 **1002** |
| 9 | 单帧载荷超过 `--max-frame` | 关闭码 **1009** |
| 10 | 多个合法分片累加后超过 `--max-message` | 关闭码 **1009** |
| 11 | 客户端发 close(1000) | 回显 close(1000)，完成关闭握手 |

## 4. 关闭码含义（RFC 6455 §7.4.1）

- `1000` 正常关闭
- `1002` 协议错误（非法续帧、控制帧分片、未掩码、RSV 非零等）
- `1007` 数据内容非法（文本不是合法 UTF-8、close 载荷非法）
- `1009` 消息/帧过大
- `1006` 保留值，本库在 TCP 直接断开（没有 close 帧）时报告
