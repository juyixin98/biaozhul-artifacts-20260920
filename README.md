# wsframe —— WebSocket 帧重组（纯后端，Rust）

从零实现的 WebSocket（[RFC 6455](https://www.rfc-editor.org/rfc/rfc6455)）
**增量字节解析库** + 本地 TCP 测试服务。**零第三方依赖**，解析器是逐字节驱动的
状态机；不使用任何现成 WebSocket 协议解析库（SHA-1 / Base64 也均为手写）。

## 明确支持的子集

| 项 | 支持情况 |
|---|---|
| 握手 | `Upgrade: websocket` / `Connection: Upgrade` / 16 字节 Key / Version 13；手写 SHA-1+Base64 计算 Accept |
| 数据操作码 | Text(0x1)、Binary(0x2)、Continuation(0x0) |
| 控制操作码 | Close(0x8)、Ping(0x9)、Pong(0xA) |
| 分片 | 完整实现 §5.4 序列状态机；控制帧可插入数据分片之间（§5.5） |
| UTF-8 | Text 消息与 Close 原因**跨分片**增量校验（拒绝过长编码、代理区、>U+10FFFF、末尾半个字符） |
| 掩码 | 严格校验：客户端帧必须掩码、服务端帧必须不掩码 |
| 长度 | 7 / 16 / 64 位三种长度字段均可解析；声明超长在读到长度时立即拒绝，不缓冲载荷 |
| 长度上限 | 单帧默认 1 MiB、单消息默认 1 MiB，均可通过 CLI/配置修改 |

**明确不支持**：任何扩展（RSV 位必须全 0）、保留操作码（3-7、B-F）、
WebSocket over HTTP/2、客户端角色的连接管理（库能解析服务端帧，测试服务只处理客户端）。

## 错误类型与关闭码（RFC 6455 §7.4）

| 场景 | 错误类型 (`error::Error`) | 关闭码 |
|---|---|---|
| 客户端帧未掩码 | `UnmaskedClientFrame` | 1002 |
| 服务端帧带掩码 | `MaskedServerFrame` | 1002 |
| RSV 位置位 | `RsvBitsSet` | 1002 |
| 保留操作码 | `ReservedOpcode` | 1002 |
| 控制帧 FIN=0 | `FragmentedControlFrame` | 1002 |
| 控制帧 > 125 字节 | `ControlFrameTooLong` | 1002 |
| 孤立 continuation | `UnexpectedContinuation` | 1002 |
| 分片中又来起始帧 | `NestedMessageStart` | 1002 |
| Close 体格式错（1 字节/非法码） | `InvalidCloseFrame` | 1002 |
| 分片未结束即 Close | `ClosingDuringFragment` | 1002 |
| 非法 UTF-8（可跨分片） | `InvalidUtf8` | 1007 |
| 单帧超长 | `FrameTooLarge` | 1009 |
| 消息重组超长 | `MessageTooLarge` | 1009 |
| 对端断开 / I/O | `ConnectionClosed` / `Io` | 不发 Close |

Close 帧允许的关闭码：1000/1001/1002/1003/1007/1008/1009/1010-1014 及
3000-4999；**1004、1005、1006、1015 禁止出现在 Close 帧中**（收到 → 1002）。

## 目录结构

```
src/
  error.rs       错误类型 + 关闭码
  utf8.rs        流式 UTF-8 状态机（跨分片）
  frame.rs       帧解析状态机（逐字节）+ 帧编码
  message.rs     消息重组：分片序列 / 控制帧插入 / Close 校验
  handshake.rs   HTTP 升级握手 + 手写 SHA-1 / Base64
  server.rs      TCP echo 服务（连接循环）
  bin/server.rs  可执行入口
tests/
  acceptance.rs  30 个真实 TCP 端到端验收测试
  common/mod.rs  测试客户端（手工握手、逐字节发送）
examples/
  requests/      握手与帧字节样例（含中文注释）
  python/        零依赖 Python 演示客户端
RUNLOG.md        实际运行命令与结果记录
```

## 快速开始

```bash
cargo build --release
./target/release/wsframe-server --addr 127.0.0.1:9001
# 可选：--max-frame 1048576 --max-message 1048576
```

服务是 echo 语义：Text/Binary 原样回显，Ping→Pong，Close 原样回显后做关闭握手。

```bash
# 自动化测试（45 个单元 + 30 个 TCP 端到端）
cargo test

# 手工演示（逐字节发送、ping 插入半个字符、1002/1007 检查）
python3 examples/python/client_demo.py 127.0.0.1 9001
```

## 核心 API 速览

```rust
use wsframe::{FrameReader, PeerRole, Assembler};

// 逐字节解析
let mut reader = FrameReader::new(PeerRole::Client, 1 << 20);
if let Some(frame) = reader.feed_byte(b)? { /* 完整一帧 */ }

// 消息重组（内部维护分片状态机与跨分片 UTF-8 校验器）
let mut asm = Assembler::new(1 << 20);
match asm.handle(frame)? {
    Event::Message(m)   => { /* 完整消息 */ }
    Event::Ping(p)      => { /* 回 Pong，不影响分片状态 */ }
    Event::Close{code,..} => { /* 回显关闭帧 */ }
    Event::StreamProgress => {}
}
```

## 验收点对照

- **逐字节输入**：`bytewise_input_is_reassembled`、`bytewise_fragmented_message_with_two_pings`
- **Ping 插入**：`ping_interleaves_fragmented_utf8_character`
- **非法连续帧**：`continuation_without_start_is_1002`、`nested_message_start_is_1002`
- **半个 UTF-8**：`dangling_half_utf8_at_message_end_is_1007`、
  `surrogate_split_across_fragments_is_1007`、`utf8_character_split_across_fragments_is_valid`
- **超长消息**：`oversized_declared_length_is_rejected_without_buffering`、
  `oversized_accumulated_message_is_1009`
- **关闭码检查**：`close_*` 共 8 个用例（合法回显 / 1005→1002 / 非 UTF-8 原因→1007 /
  分片中关闭→1002 / 3000 私有码回显 / 空体回显）
