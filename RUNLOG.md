# 运行记录（RUNLOG）

- 日期：2026-09-23
- 环境：Linux 6.8.0-90-generic x86_64，Rust 1.98.1（edition 2021），Python 3.12.3
- 依赖：**零第三方 crate**（`Cargo.toml` 中 `[dependencies]` 为空）

## 1. 单元 + 集成测试：`cargo test`

命令：

```bash
cargo test
```

结果（最终一次）：

```text
running 45 tests          # src 内单元测试
test result: ok. 45 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out

running 0 tests           # bin/server 无单测
test result: ok. 0 passed; 0 failed

running 30 tests          # tests/acceptance.rs 真实 TCP 端到端
test result: ok. 30 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out

running 1 test            # doc 示例（ignore 的骨架代码）
test result: ok. 0 passed; 0 failed; 1 ignored
```

合计 **75 个可执行测试全部通过，0 失败**。测试覆盖：

- 单元（45）：UTF-8 状态机 7、帧解析/编码 14、消息重组与关闭帧 14、
  SHA-1/Base64/握手 10
- 端到端（30）：逐字节输入、ping 插入分片、非法分片序列、跨分片 UTF-8、
  超长（声明超长不缓冲 + 累积超长 + 帧上限）、关闭码 8 个场景、握手 2 个

`cargo clippy --all-targets`：**0 警告**。

## 2. release 构建与服务启动

```bash
$ cargo build --release
   Compiling wsframe v0.1.0
    Finished `release` profile [optimized] target(s) in 0.71s

$ ./target/release/wsframe-server --addr 127.0.0.1:9001 --max-message 65536
wsframe echo server listening on ws://127.0.0.1:9001
limits: max_frame=1048576 bytes, max_message=65536 bytes
```

## 3. 手工 Python 客户端实测（真实进程，非 mock）

```bash
$ python3 examples/python/client_demo.py 127.0.0.1 9001
[ok] handshake, accept=CkDwLMWIrD2MoLkaAfns+Zxxrxw=
[..] sending one frame byte-by-byte (20 TCP segments)
[ok] bytewise echo: 你好 wsframe
[ok] pong arrived while message was half a UTF-8 char
[ok] cross-fragment UTF-8 message: 中
[ok] handshake, accept=HKFXF7XJlpOr8zd9GpTbMwm0yPU=
[check] stray continuation -> close code 1002
[ok] handshake, accept=zz6wzGoZfajW2bzSWYDdqXI+3b0=
[check] dangling half UTF-8 char -> close code 1007
[ok] handshake, accept=naBcb4ueJVPWvBFXZSbEMuFlrJU=
[check] unmasked client frame -> close code 1002

ALL DEMO CHECKS PASSED
demo_exit=0
```

## 4. curl 握手探测

合法握手（curl 升级后等帧会超时，101 响应头已正确返回，符合预期）：

```text
HTTP/1.1 101 Switching Protocols
Upgrade: websocket
Connection: Upgrade
Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=
curl: (28) Operation timed out after 3002 milliseconds   # 预期：协议升级后 curl 不懂帧
```

非法版本：

```text
HTTP/1.1 400 Bad Request
Content-Type: text/plain; charset=utf-8
Content-Length: 43
Connection: close

unsupported Sec-WebSocket-Version (need 13)
```

## 5. 超长消息实测（--max-message 65536 的服务）

独立 Python 脚本对运行中的服务验证：

```text
oversized single message -> opcode 0x8 close code 1009
fragmented oversized message -> opcode 0x8 close code 1009
OVERSIZE CHECKS PASSED
```

## 6. 服务端日志（如实记录错误路径被触发）

```text
wsframe echo server listening on ws://127.0.0.1:9001
limits: max_frame=1048576 bytes, max_message=65536 bytes
protocol error: continuation frame without a started message
protocol error: payload is not valid UTF-8 (may span fragments)
protocol error: client frame is not masked (RFC 6455 5.1)
handshake rejected: unsupported Sec-WebSocket-Version (need 13)
connection ended: I/O error: bad handshake: unsupported Sec-WebSocket-Version (need 13)
protocol error: reassembled message exceeds configured limit
protocol error: reassembled message exceeds configured limit
```

## 未通过项 / 已知限制（如实列出）

- **无失败测试**：75 个测试全部通过；演示脚本全部断言通过。
- curl 合法握手后的 `(28) timed out` 不是缺陷——HTTP 客户端在 101 后不会
  继续 WebSocket 帧协议，属于预期现象。
- 已知范围限制（设计如此，非缺陷）：
  - 不实现任何扩展（收到 RSV 位直接 1002）；
  - 不做来源校验（Origin/子协议/鉴权），纯本地测试服务；
  - 服务端不主动发起 Ping 心跳；
  - 每连接一个线程（测试用途足够，未做异步运行时）；
  - 超长帧在"声明长度"阶段即拒绝（1009），不会先把载荷收完。
