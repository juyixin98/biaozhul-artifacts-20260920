# sse-resume — SSE 断线续流（纯后端 / 纯标准库 Rust）

从零实现的 **Server-Sent Events 增量字节解析库**与**本地 TCP 断线续流测试服务**。
不使用任何现成的 SSE/HTTP 协议解析库或第三方 crate（`Cargo.toml` 零依赖，纯 `std`），
核心解析器为手写的逐字节状态机。

> 范围：**只有后端**。没有浏览器、没有前端、没有 HTTP/2、没有压缩。
> 目标是把「字节流 → 事件」的增量解析和「断线 → 带游标重连 → 续流/重置」
> 这两件事做对、测透。

---

## 1. 目录结构

```
src/
  error.rs              明确支持的错误类型 + 长度上限配置 Limits
  decode.rs             ★核心：手写增量字节解析状态机（客户端侧）
  encode.rs             服务端事件帧编码（与 decode 对偶，可往返）
  server.rs             本地 TCP SSE 服务：有限历史 / Last-Event-ID / reset
  lib.rs                库入口（#![forbid(unsafe_code)]）
  bin/
    sse-server.rs       可运行的服务端（自动 tick）
    sse-client.rs       可运行的客户端（小块读取、自动重连、识别 reset）
    demo-reconnect.rs   真实 TCP 上的三场景续流演示（无遗漏/重复边界/过期reset）
tests/
  chunking.rs           CR/LF/CRLF 跨块、逐字节、随机分块不变性
  roundtrip.rs          encode↔decode 往返、帧注入防护、模糊往返
  reconnect.rs          真实 TCP 端到端续流 / reset / HTTP 错误
examples/
  http/*.http           可直接阅读/用 nc 发送的请求样例
  sample-frames.sse     离线 SSE 帧格式样例
RUN.md                  实际运行记录（命令、输出、结论）
```

## 2. 构建与测试

需要 Rust（开发使用 1.98.1，纯标准库，2021 edition）。

```bash
cargo build --release
cargo test                 # 55 个测试（单元 + 集成，含真实 TCP）
cargo clippy --all-targets # 零警告（dev/release 两个 profile 均干净）
```

## 3. 快速上手

```bash
# 终端 A：起服务（历史窗口 64，每 1s 一个 tick 事件）
./target/release/sse-server --port 18080 --history 64 --tick-ms 1000

# 终端 B：订阅（无游标，只收实时事件）
curl -N http://127.0.0.1:18080/events

# 断线后续流（把 42 换成你收到的最后一个 id）
curl -N -H 'Last-Event-ID: 42' http://127.0.0.1:18080/events
```

跑可执行的三场景续流演示（自动起停，无需手工操作）：

```bash
./target/release/demo-reconnect
```

使用内置客户端（小块读取、读错自动重连、识别 reset）：

```bash
./target/release/sse-client --url http://127.0.0.1:18080/events \
    --max-events 10 --reconnects 3 --chunk 16
```

---

## 4. 解析器支持的 SSE 子集（明确范围）

依据 WHATWG HTML 标准的 *Server-sent events* 字节流解析算法：

| 规范元素 | 支持 |
|---|---|
| 行终止符 LF / CR / **CRLF**，以及它们**跨 TCP 块**被切开 | ✅ |
| 空行派发事件；EOF 时未终止的尾巴也按规范派发 | ✅ |
| 注释行（`:` 开头，含纯 `:`、冒号后无空格），忽略不派发 | ✅ |
| `data`：多行用 `\n` 拼接；派发时去掉最后一个尾部 `\n` | ✅ |
| `event`：设置类型，派发后复位为 `message` | ✅ |
| `id`：更新持久 last event id；含 U+0000 忽略整行 | ✅ |
| **空 id**（裸 `id` / `id:`）：游标置为**空串**（清空，而非保留旧值） | ✅ |
| `retry`：仅全 ASCII 数字时作为重连毫秒数，否则忽略整行 | ✅ |
| 未知字段：忽略；`data` 裸字段（无冒号）= 空值 | ✅ |
| 值只去掉**一个**前导空格；流首 UTF-8 BOM（可跨三块到达） | ✅ |
| 长度上限（行 / data / id）与明确错误类型 | ✅ |

**刻意不做：** 不做非法 UTF-8 的“替换字符”容错（直接报错，后端场景早暴露更有用）；
不实现 EventSource DOM / 自动重连策略（那是上层策略，本库只做字节→事件）。

### 增量性

`Decoder::push(&[u8])` 接受任意长度的字节片——一个 TCP 段、一次 `read()` 的结果、
甚至逐字节喂入都可以。行尾只出现一半（`\r` 在块尾、`\n` 在块首）由内部
`pending_cr` 状态挂起，**不会**重复断行或多派发空事件。一旦报错，解码器进入
**毒化（poisoned）**状态，之后所有调用返回 `DecodeError::Poisoned`，正确做法是
断开重连并带上游标，避免“半截事件”混入正常流。

### 长度上限与错误类型

`Limits { max_line_bytes, max_data_bytes, max_id_bytes }` 可逐连接配置，
默认 8 KiB / 64 KiB / 256 B。明确的错误枚举（实现 `std::error::Error`）：

```rust
pub enum DecodeError {
    LineTooLong  { len, limit },   // 单行超限（逐字节即时报出）
    DataTooLong  { len, limit },   // 单事件 data 累积超限
    IdTooLong    { len, limit },   // id 超限
    InvalidUtf8  { field },        // 字段值非 UTF-8
    Poisoned,                      // 已因前一个错误毒化
}
pub enum EncodeError {
    ValueContainsNewline { field },// 帧注入防护：字段值不允许裸 CR/LF
    Io(String),
}
```

> 多行 data 必须通过传入多个 data 值表达；编码器拒绝在字段值里放裸换行，
> 否则一个订阅者的内容可以伪造出 `id:`/`event:` 行（帧注入）。

---

## 5. 续流语义（server）

服务端给每个事件分配**单调递增 u64 序号**作为 SSE `id`，并保留最近
`--history N` 个已编码帧（**有限历史**）。

| 重连请求 | 行为 |
|---|---|
| 无 `Last-Event-ID` | 只收连接之后的实时事件，**不**重放历史 |
| `id: n` 且历史中存在 `>n` 的事件 | 按序重放这些帧，然后无缝转实时；游标对应事件本身不重发 |
| `n` 早于历史最旧 id（**过期**） | 先投递 `event: reset`（reason=`cursor-expired`），再实时 |
| `n` 非数字 / 空 | reset，reason=`cursor-invalid` |
| `n` 比最新 id 还大 | reset，reason=`cursor-ahead` |

reset 事件自带可对齐的游标，形如：

```text
id: 33
event: reset
data: {"reason":"cursor-expired","yourLastEventId":"2","earliestId":14,"latestId":33,"resumeAfter":33}
```

客户端看到 reset 即知：`n → resumeAfter` 之间的事件因服务端只保留有限历史而
**永久丢失**，必须做全量再同步，不能假装没有缺口；随后把本地游标对齐到帧 id。

### “允许的重复边界”（at-least-once）

事件在“客户端已收到、但还没来得及更新持久游标”的窗口期内崩溃/断网时，
重连会用**旧游标**，服务端会把**游标之后的第一个事件再投一次**。本服务不去重，
幂等/去重是消费方责任。`tests/reconnect.rs::duplicate_boundary_when_cursor_lags_one`
与 `demo-reconnect` 场景 2 都显式断言了“恰好重复一个边界事件、其后无遗漏”。

### HTTP 子集（最小）

`GET /events` → `200 text/event-stream`；其他路径 `404`、其他方法 `405`、
畸形请求行/超大请求头 `400`；固定 `Connection: close`，无 keep-alive/分块。

---

## 6. 作为库使用

```toml
# 本项目零依赖；在项目内直接用模块即可
[dependencies]
sse-resume = { path = "." }
```

```rust
use sse_resume::{Decoder, OutEvent};

// 解码（增量）
let mut d = Decoder::new();
let evs = d.push(b"data: hello\n\n").unwrap(); // 任意大小的字节块
assert_eq!(evs[0].data, "hello");
let cursor = d.last_id(); // 断线时作为 Last-Event-ID

// 编码
let frame = sse_resume::encode::encode_to_vec(
    &OutEvent::data_line("a\nb".into())  // 也可用 data: vec!["a".into(),"b".into()]
        .with_id("7"),
).unwrap();
```

---

## 7. 设计取舍

* **逐字节状态机**而非“按块 split 换行”：只有逐字节才能在零拷贝最少拷贝下
  正确处理 CR/LF/CRLF 跨块，并即时执行行长上限（不把无限长行先攒进内存）。
* **字段值延迟到行结束才校验 UTF-8**：按字节做行切分，避免半个 UTF-8 字符
  跨块时误判。
* **有限历史 + 显式 reset**：不承诺“无限回放”，过期即明确告知，比静默缺口安全。
* **纯 `std::net` 阻塞 I/O + 每连接一线程 + Condvar 广播**：测试服务够用、
  可读性优先；非阻塞 listener 仅用于优雅退出，accept 后立即把连接切回阻塞。

## 8. 已知边界 / 非目标

* 服务端事件源是进程内 `publish`，没有持久化，进程重启即历史清空（会触发 reset）。
* 不做鉴权、压缩、HTTP/2、CORS 凭据；心跳注释固定 15s（`server::PING_INTERVAL`）。
* 高并发生产场景应替换为异步运行时；本服务的定位是**本地正确性测试床**。
