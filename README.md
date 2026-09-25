# brpc — 二进制 RPC 复用（纯后端，std-only Rust）

一个从零手写的**增量字节解析库** + **带版本/请求ID/长度/CRC 的 RPC 帧协议** +
**单 TCP 连接多路复用**的本地客户端/服务端。无任何第三方依赖（仅 Rust 标准库），
核心解析不借助任何现成协议解析器。

- 库：`brpc`（[src/lib.rs](src/lib.rs)）
- 服务端二进制：`brpc-server`（[src/bin/server.rs](src/bin/server.rs)）
- 客户端/演示二进制：`brpc`（[src/bin/client.rs](src/bin/client.rs)）

---

## 1. 帧格式（wire format）

全部整数大端。固定 24 字节头部 + 变长 payload：

| 偏移 | 长度 | 字段 | 说明 |
|---|---|---|---|
| 0 | 4 | magic | `0x42 52 50 43` = `"BRPC"` |
| 4 | 1 | version | 当前 = `1`，不匹配报 `UnsupportedVersion` |
| 5 | 1 | kind | `1=Request` `2=Response` `3=Cancel` |
| 6 | 1 | flags | v1 掩码为 `0x00`，未知位置位报 `BadFlags`（预留扩展） |
| 7 | 1 | reserved | 必须为 0 |
| 8 | 8 | request_id | 多路复用键（见下） |
| 16 | 4 | payload_len | 负载字节数；受配置上限约束 |
| 20 | 4 | crc32 | 对 **version..length（字节 4..20）+ payload** 的 CRC-32（IEEE，多项式 0xEDB88320，自实现） |
| 24 | N | payload | 应用层请求/响应 |

**CRC 不覆盖 magic，也不覆盖 CRC 字段自身。** 头部与 payload 分别喂给流式
`Crc32` hasher，无需拼接到一块内存。

> 长度前缀 + CRC 意味着流一旦在「帧边界之外」损坏就无法重新同步，因此
> **成帧级（framing）错误是致命的：立即关闭连接**；而**应用级错误**（未知方法、
> 参数错误、取消）始终装在合法响应帧里返回，**不影响连接**。这两类错误在代码里
> 被严格区分。

### 应用层 payload（演示用，帧内）

- 请求：`method:u16` + 方法参数
- 响应：`code:u16`（0=OK）+ 响应体
- 方法：`Echo=1`、`Slow=2`（前 4 字节为延迟毫秒，用于制造乱序/超时/取消）、
  `Add=3`（两个 i64）、`Stats=4`
- 错误码：`UnknownMethod / BadArgs / Cancelled / TooManyInFlight / Internal`

---

## 2. 增量字节解析库（核心，手写）

见 [src/decode.rs](src/decode.rs) 的 `FrameDecoder`，在一个**有界** `Vec<u8>`
累积区上手写，明确支持：

- **半包（half packet）**：字节可以一个个到，`next_frame()` 返回 `Ok(None)` 表示
  还需更多数据（`NeedMore` 不是错误）。
- **粘包（sticky packet）**：一次读可含多个帧，循环 `next_frame()` 直到 `None`。
- **子集解析（subset parse）**：`Frame::parse_header()` 只解析路由所需字段
  （kind/id/len/crc），不必先吃下整个 payload；`payload::decode_method()` 同理只
  取方法号。
- **长度上限 / 内存有界**：**仅凭头部**就拒绝 `payload_len > max_payload`，绝不会
  先按声明长度分配/读入。累积区随时 `drain` 已消费前缀。
- **显式错误类型**：`DecodeError` 逐个命名，见下表；致命错误会使解码器进入
  `Poisoned` 状态，之后拒绝继续解析。

| 错误 | 含义 | 致命？ |
|---|---|---|
| `NeedMore` | 数据未到齐，继续喂字节 | 否 |
| `BadMagic` | 魔数错误 | 是 |
| `UnsupportedVersion(v)` | 版本不支持 | 是 |
| `UnknownKind(k)` | 未知帧类型 | 是 |
| `BadFlags(f)` | 出现 v1 不认识的 flag | 是 |
| `PayloadTooLarge{declared,max}` | 声明长度超上限 | 是 |
| `CrcMismatch{expected,actual}` | CRC 校验失败 | 是 |
| `Poisoned` | 已发生致命错误后的再使用 | 是 |

`is_fatal()` 判定是否必须断开连接。

---

## 3. 多路复用语义

见 [src/client.rs](src/client.rs) 与 [src/server.rs](src/server.rs)。

- **同连接并发**：客户端一个写线程独占发送半部分（帧绝不交错），一个读线程按
  `request_id` 分发；服务端每连接一个读线程 + 每请求一个工作线程，慢请求不阻塞
  快请求，**响应可乱序返回**。
- **请求 ID 未释放前不得复用**：ID 是打包进 u64 的 `(slot, generation)`
  （高 32 位代次、低 32 位槽位）。槽位可回收，但每次释放都令 `generation += 1`，
  因此**同一个 64 位 ID 永不再发放**；首代代次从 1 起，`0` 保留为「无 ID」。
- **迟到响应可识别**：响应到达时，若该 id 无在途属主，或其代次已过期，则计为
  *late response* 并丢弃（`MuxClient::late_responses()` 可观测），绝不串到复用了
  槽位的新请求上。
- **取消与竞争**：`Call::cancel()` 发 `Cancel(kind=3)`，然后等待终止帧。慢处理器
  协作式每 10ms 检查取消标志。竞争只有一个胜出：取消先到 → 服务端回
  `AppCode::Cancelled`；响应先到 → `cancel()` 直接返回该响应。绝不重复、绝不挂死。
- **超时后迟到**：`Call::wait_timeout()` 超时只表示「不再等」，**不**释放 ID、**不**
  杀服务端工作；随后 `drop(call)` 释放 ID，服务端真正完成时的响应即被识别为迟到。
- **内存有界**：
  - 单帧 payload：解码器上限（服务端默认 4096，可配）；
  - 在途请求数：客户端 `max_inflight`（满则 `start_request` 返回
    `TooManyInFlight` 背压，不无限缓冲）；服务端每连接 `max_inflight`（满则回
    `TooManyInFlight` 应用帧）；
  - 发送队列：有界 `sync_channel`，满则回压/失败而非增长；
  - 长期运行的突发结束后在途表回到 0（有专门回归测试）。

### 错误帧之后的连接处理

- 魔数/版本/类型/flag/超长/CRC 错误 → 计 `framing_errors`，**立即关闭连接**
  （对端读到 EOF），解码器 `Poisoned`。
- 应用错误（BadArgs、UnknownMethod…）→ 合法响应帧，连接继续可用。
- 服务端在同连接上收到 `Response`（客户端才该发的）→ 协议违例，关闭连接。
- 对未知 id 的 CANCEL → 回一个 `Cancelled` 应用帧并保持连接（解除竞争客户端的阻塞）。

---

## 4. 构建与运行

需要 Rust（开发用 1.98.1，edition 2021，无外部依赖）。

```bash
cargo build --release

# 终端 A：启动服务端（默认 127.0.0.1:9000；可传地址）
./target/release/brpc-server 127.0.0.1:9000

# 终端 B：演示乱序 / 超时迟到 / 取消竞争 / 应用错误不影响连接
./target/release/brpc demo 127.0.0.1:9000

# 单请求
./target/release/brpc echo "hello"
./target/release/brpc slow 150
./target/release/brpc add 40 2
./target/release/brpc stats

# 生成原始请求帧样例（不联网）
./target/release/brpc sample-gen samples
```

后台/受管运行服务端可设 `BRPC_NO_STDIN=1`（不读 stdin，运行至被终止）。

### 用裸 TCP 发送样例帧

样例就是可直接写入 TCP 的完整帧。例如用 python（见 [samples/README.md](samples/README.md)）：

```python
import socket
d = open("samples/request_echo.bin","rb").read()
s = socket.create_connection(("127.0.0.1",9000)); s.sendall(d)
print(s.recv(4096)[:24])   # b'BRPC\x01\x02...'
```

`echo` 帧头部示例（payload=`hello-rpc`，11 字节）：

```
42 52 50 43  magic "BRPC"
01           version 1
01           kind=Request
00 00        flags, reserved
00 00 00 01 00 00 00 01   request_id = (gen=1, slot=1)
00 00 00 0b  payload_len = 11
6f ef 8c 04  crc32
68 65 6c 6c 6f 2d 72 70 63   "hello-rpc"
```

---

## 5. 自动化测试

```bash
cargo test
```

- **单元测试（17）**：CRC 已知向量、帧往返、子集头部解析、逐字节半包、粘包、
  跨 push 切帧、随机 1..7 字节切分重组 200 帧、各类错误类型、长度上限仅凭头部拒绝、
  CRC 翻位检测、poison 后拒绝再用。
- **集成测试（20）**（真实本机 TCP）：
  - [tests/wire.rs](tests/wire.rs)：逐字节半包、三帧粘一包、坏魔数/超长/坏CRC 后
    服务端关闭连接并计数、半头后续传、未知 id 的 CANCEL 应答且连接存活；
  - [tests/concurrency.rs](tests/concurrency.rs)：单连接 60 并发按 id 正确路由、
    慢后快造成的乱序、应用错误后连接仍可用；
  - [tests/cancel.rs](tests/cancel.rs)：超时后迟到响应被识别丢弃、ID 不复用且代次
    递增、长任务取消胜出、40 线程取消/响应竞争恰好一次结局、响应已到再取消返回原响应；
  - [tests/bounds.rs](tests/bounds.rs)：客户端在途上限背压、服务端在途上限回应用帧、
    8×20 突发后在途表归零无泄漏。

实际运行命令与结果（含一次真实抓出的 CRC 覆盖范围 bug 的修复）记录在
[TESTING.md](TESTING.md)。

---

## 6. 目录结构

```
Cargo.toml
src/
  lib.rs          模块汇总
  crc32.rs        手写表驱动 CRC-32（含流式 hasher）
  frame.rs        帧布局 / 编码 / 头部子集解析 / CRC 校验
  decode.rs       ★ 增量、有界、显式错误类型的解析器 FrameDecoder
  payload.rs      演示应用层（方法 / 错误码 / BE 读写）
  client.rs       MuxClient + Connection + Call（并发/取消/迟到/背压）
  server.rs       Server：连接线程 + worker + 取消注册表 + Stats
  bin/server.rs   服务端可执行
  bin/client.rs   客户端/演示/样例生成可执行
tests/            真实 TCP 集成测试 + common 辅助
samples/          生成的原始请求帧样例
README.md  TESTING.md
```

## 7. 明确的非目标 / 边界

- 纯后端：无任何前端/UI。
- 未实现 TLS、认证、分片（flags 已预留）、自动重连；聚焦在「字节解析 + 单连接
  多路复用」这一核心。
- 应用层 payload 是刻意保持极小的演示格式，不是某种现成 RPC 协议。
