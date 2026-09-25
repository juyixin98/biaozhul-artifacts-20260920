# WebSocket 升级握手 —— 请求/响应样例（RFC 6455 §4.2.2）

> 本服务只做最小子集校验：`GET`、`HTTP/1.1`、`Upgrade: websocket`、
> `Connection` 包含 `upgrade` token、16 字节 Base64 的 `Sec-WebSocket-Key`、
> `Sec-WebSocket-Version: 13`。

## 合法请求

```http
GET /chat HTTP/1.1
Host: 127.0.0.1:9001
Upgrade: websocket
Connection: keep-alive, Upgrade
Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==
Sec-WebSocket-Version: 13
```

（头部以 `\r\n\r\n` 结束）

## 成功响应

`Sec-WebSocket-Accept = Base64(SHA1(key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"))`
SHA-1 与 Base64 均为本仓库手写实现。

```http
HTTP/1.1 101 Switching Protocols
Upgrade: websocket
Connection: Upgrade
Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=
```

## 失败响应（示例：版本号错误）

请求：

```http
GET / HTTP/1.1
Host: 127.0.0.1:9001
Upgrade: websocket
Connection: Upgrade
Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==
Sec-WebSocket-Version: 8
```

响应：

```http
HTTP/1.1 400 Bad Request
Content-Type: text/plain; charset=utf-8
Content-Length: 42
Connection: close

unsupported Sec-WebSocket-Version (need 13)
```
