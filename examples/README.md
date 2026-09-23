# 请求样例

本目录的 `send_sample.py` 是**纯标准库 Python** 的报文合成/发送器。它不调用任何
TLS 库、不完成握手，而是手工拼装 record 层与 handshake 层字节，专门用来对观察器
注入正常与畸形输入。

## 运行

```bash
# 1) 启动观察器
./target/release/tls-observer-server --port 8443

# 2) 发送样例（默认 127.0.0.1:8443，每 7 字节一个 TCP 写）
python3 examples/send_sample.py all
python3 examples/send_sample.py valid
python3 examples/send_sample.py nested-bad --port 8443 --chunk 3
```

参数：`[scenario] [--host H] [--port P] [--chunk N]`。脚本发送完会 **shutdown(SHUT_WR)**
半关闭连接，服务端据此判断客户端方向结束并回写 JSON。

## 场景清单与预期

| 场景 | 构造 | 预期观察 |
|---|---|---|
| `valid` | TLS1.2 ClientHello：SNI `example.com`、ALPN `h2,http/1.1` | `ok=true`，SNI/ALPN 正确 |
| `fragment` | 同一握手消息切成 3 条 Handshake 记录 | `ok=true`，跨记录重组成功 |
| `duplicate` | 两个 SNI + 两个 ALPN 扩展 | `ok=true`，首个生效 + duplicate 警告 |
| `nested-bad` | extensions 长度声明 100，实际 4 字节 | `ok=false`，`length_mismatch(nested)` |
| `grease` | GREASE 密码套件 `0x0a0a` + GREASE 扩展 `0x2a2a` | GREASE 被分类，未知扩展仅 `0x00ab` |
| `trunc-record` | 流在第 9 字节切断（记录分片未到齐） | `ok=false`，`truncated/record` |
| `trunc-hs` | 握手消息只发一半 | `ok=false`，`truncated/reassembly` |
| `ciphertext` | ClientHello 后跟 ApplicationData + CCS + ApplicationData | CH 正常，密文记录只计数不解 |
| `ciphertext-only` | 流直接以 ApplicationData 开始（内含貌似握手的字节） | `client_hello=null`，无臆造 |
| `bad-ctype` | content_type = 99 | `ok=false`，`bad_content_type` |
| `garbage` | 以 `0x16` 开头但长度无意义/截断 | `ok=false`，不 panic、不臆造握手 |

`outputs/` 保存了 `all` 的实际服务端回写 JSON，以及用 OpenSSL 真实 ClientHello 的
观察结果，便于离线比对。

## 用真实客户端

```bash
openssl s_client -connect 127.0.0.1:8443 \
    -servername real.example.org -alpn h2,http/1.1 -tls1_2 < /dev/null
```

openssl 最终会因为对端（观察器）不回应握手而失败/超时——**这是预期**；观察器只读
客户端方向，真实明文 ClientHello 已被解析。
