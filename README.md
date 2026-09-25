# sse-resume：SSE 断线续流（纯后端，Rust）

手写的 SSE（Server-Sent Events）增量字节解析库 + 本地 TCP 测试服务与自动重连客户端。
**零第三方依赖**，核心解析器逐字节手写，未借用任何现成协议解析库。

## 功能与协议子集

解析器（`src/parser.rs`）支持的 SSE 子集：

| 特性 | 行为 |
|---|---|
| `data:` | 可多行，派发时以 `\n` 连接 |
| `event:` | 事件类型，缺省为 `message` |
| `id:` | 更新 last-event-id；含 NUL 时按规范忽略；空值（`id:`）把游标重置为空串 |
| `retry:` | 纯数字生效（毫秒），否则忽略 |
| 注释行 | `:` 开头的行忽略（用作心跳） |
| 未知字段 | 忽略 |
| 行尾 | `\n`、`\r`、`\r\n` 均支持，**`\r\n` 可跨块拆分** |
| 无冒号的行 | 整行作为字段名，值为空串 |

### 长度上限（`Limits`，可调）

- 单行：8 KiB
- 单事件 data 累计：64 KiB
- 字段名：64 字节
- id：256 字节

### 错误类型（`ParseError`）

`LineTooLong` / `EventTooLarge` / `FieldNameTooLong` / `IdTooLong` / `InvalidUtf8`。
出错后解析器状态未定义，调用方应丢弃并重建（对应断线重连语义）。

## 断线续流设计

- 服务端保留**有限历史**（环形缓冲，容量 `history_cap`）。
- 客户端断线后以 `Last-Event-ID: <id>` 重连：
  - 游标在历史窗口内 → 服务端重放严格 `id > last_id` 的事件（**服务端不主动制造重复**）；
  - 游标过期（`last_id + 1 < 历史最旧 id`）→ 服务端先下发明确的重置事件
    `event: reset` + `data: cursor-expired`，再重放当前保留的全部事件；
    客户端收到 `reset` 后清空本地去重状态重新同步。
- **允许的重复边界**：客户端按 id 去重；由于服务端严格重放 `id > last_id`，
  重复只可能出现在"客户端已收到事件但尚未记录游标就断线"的边界，
  即至多重复断线前最后一个 id。本实现的客户端在派发时即记录游标，
  验收测试中重复数为 0。

## 构建与测试

```bash
cargo build
cargo test
```

## 运行

终端 1（服务端，`--drop-after 8` 表示每条连接发 8 个事件后模拟断线）：

```bash
cargo run --bin sse-server -- --addr 127.0.0.1:9100 --total 30 --interval-ms 20 --history 64 --drop-after 8
```

终端 2（客户端，断线后自动带 Last-Event-ID 重连）：

```bash
cargo run --bin sse-client -- --addr 127.0.0.1:9100 --expect-total 30
```

## 请求样例

握手是极简 HTTP/1.1 子集：读取到 `\r\n\r\n` 为止，仅识别 `Last-Event-ID` 头。

新连接：

```
GET /events HTTP/1.1
Host: 127.0.0.1:9100

```

断线续传：

```
GET /events HTTP/1.1
Host: 127.0.0.1:9100
Last-Event-ID: 8

```

用 `nc` 直接观察（history=3 时用过期游标 `Last-Event-ID: 2`）：

```bash
printf 'GET /events HTTP/1.1\r\nHost: x\r\nLast-Event-ID: 2\r\n\r\n' | nc 127.0.0.1 9101
```

实际输出（游标过期 → 明确 reset 事件 + 重放保留历史）：

```
HTTP/1.1 200 OK
Content-Type: text/event-stream
Cache-Control: no-cache
Connection: close

event: reset
data: cursor-expired

id: 8
event: message
data: event-8

id: 9
...
```

游标未过期（`Last-Event-ID: 8`）则只重放 `id: 9`、`id: 10`，无 reset。

## 实际运行记录（2026-09-23，rustc 1.98.1）

`cargo test`：**22 个测试全部通过，0 失败** ——

```
unittests src/lib.rs        ... 1 passed   (encoder::split_lines)
tests/parser_cases.rs       ... 18 passed  (CR/LF/CRLF 跨块、空 id、注释、多行 data、
                                            retry、NUL id、未知字段、无冒号字段、
                                            行/事件上限、非法 UTF-8、编码回环)
tests/reconnect.rs          ... 3 passed   (断线重连无遗漏、过期游标 reset、窗口内精确重放)
```

端到端（服务端 `--total 30 --drop-after 8`，客户端自动重连）：

```
done: received=30 duplicates=0 resets=0 reconnect=2 (dup ids: [])
```

即：30 个事件全部收到、无遗漏、无重复、无 reset，发生 2 次断线重连。

未通过项：无。

## 代码结构

```
src/parser.rs    增量字节解析器（子集、上限、错误类型）
src/encoder.rs   服务端事件编码（多行 data 拆行、注释/心跳）
src/history.rs   有限历史缓冲与 Last-Event-ID 续传判定（Replay / Expired）
src/server.rs    本地 TCP 测试服务（极简 HTTP 握手、事件生成器、断线模拟）
src/client.rs    客户端：单次连接消费 + 自动重连去重
src/bin/         sse-server / sse-client 命令行
tests/           解析器边界用例 + 断线重连集成测试
```
