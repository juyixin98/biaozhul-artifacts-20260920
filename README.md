# mpstream — multipart/form-data 增量流式解析（纯后端）

从零实现的 multipart 流式接收项目：**不依赖任何现成协议解析库**（仅使用 Rust 标准库），
核心是一个增量字节解析器，外加一个本地 TCP 测试服务。无前端。

## 组成

| 路径 | 说明 |
|---|---|
| `src/lib.rs` | 增量解析库 `mpstream`：状态机 + 事件流 |
| `src/main.rs` | `mpstream-server`：本地 TCP 测试服务 |
| `tests/parser_tests.rs` | 解析器测试（14 个） |
| `tests/server_tests.rs` | 端到端 TCP 测试（3 个） |
| `scripts/make_sample.py` | 生成含近似边界的二进制请求样例 |
| `scripts/send_sample.sh` | 把样例发给运行中的服务 |
| `samples/sample_request.bin` | 生成的请求样例（688 字节） |

## 支持的子集（明确范围）

* 消息 = 可选 preamble + 1..=N 个部件 + 结束边界 `--boundary--`；epilogue 忽略。
* 部件 = 0..N 个 `Name: value` 头（CRLF 分隔，不允许折行）+ 空行 + 不透明字节正文。
* 分隔符：首个为 `--boundary`，后续为 `\r\n--boundary`；结束为 `\r\n--boundary--`。
* **不支持**：嵌套 multipart、Content-Transfer-Encoding 解码、字符集转码、头折行。

## 流式保证

* `feed(&[u8])` 接受任意大小的输入块，边界可跨任意块边界（含逐字节喂入）。
* 内部只保留一个分隔符长度（`4 + boundary.len()` 字节）的回看窗口，
  正文尽快以 `Event::PartData` 吐出，**不缓存整个请求体**。
* 正文中的“近似边界”不会误判：`--boundary` 前面不是 CRLF、或后面跟的不是
  `\r\n` / `--` 时，一律按正文数据处理（见测试 `binary_body_with_boundary_prefixes`）。

## 限制（全部为硬上限，超限即报错）

`Limits` 结构体，默认值如下：

| 字段 | 默认 | 含义 |
|---|---|---|
| `max_parts` | 100 | 最大部件数 |
| `max_header_bytes` | 8 KiB | 单部件头部块最大字节数 |
| `max_total_bytes` | 16 MiB | 整个输入流最大字节数 |
| `max_part_bytes` | 8 MiB | 单部件正文最大字节数 |

## 错误类型

```rust
pub enum Error {
    InvalidBoundary(String),      // 构造时 boundary 不合法（空/超长/非法字符）
    TotalSizeExceeded { limit },  // 输入总字节数超限
    TooManyParts { limit },       // 部件数超限
    HeaderTooLarge { limit },     // 头部块超限
    PartTooLarge { limit },       // 单部件正文超限
    MalformedHeaders(String),     // 头部格式错误
    MissingFinalBoundary,         // 输入结束但未见到 --boundary--
    FeedAfterDone,                // Done 之后仍继续 feed
}
```

## 库用法

```rust
use mpstream::{Event, Limits, MultipartParser};

let mut p = MultipartParser::new("boundary", Limits::default())?;
for chunk in incoming_chunks {
    for ev in p.feed(&chunk)? {
        match ev {
            Event::PartStart { index, headers } => { /* ... */ }
            Event::PartData(bytes) => { /* 流式写出 */ }
            Event::PartEnd { index } => { /* ... */ }
            Event::Done => break,
        }
    }
}
p.finish()?; // 缺结束边界时返回 Err(MissingFinalBoundary)
```

## TCP 测试服务

自定义线协议（仅用于本地测试）：

1. 客户端发送一行 `BOUNDARY <boundary>\r\n`；
2. 随后原样发送 multipart 消息体（任意分片、可含二进制）；
3. 客户端关闭写方向（`shutdown(write)`）表示输入结束；
4. 服务端回送文本报告（`OK ...` 或 `ERROR ...`）后关闭连接。

```bash
cargo run --release --bin mpstream-server -- --addr 127.0.0.1:18091 \
    --max-total 16777216 --max-part 8388608 --max-parts 100 --max-header 8192
```

服务端按 4 KiB 块读取 socket 喂给解析器，不在内存中缓存整个请求。

## 运行测试

```bash
cargo test            # 全部 17 个测试
cargo test --test parser_tests   # 仅解析器
cargo test --test server_tests   # 仅端到端（自动起服务端进程）
```

## 请求样例

```bash
python3 scripts/make_sample.py        # 重新生成 samples/sample_request.bin
cargo build --release
./target/release/mpstream-server --addr 127.0.0.1:18091 &
./scripts/send_sample.sh 127.0.0.1:18091
```

样例的二进制正文故意包含：边界前缀（缺尾字符）、完整边界+非法后续字节、
行中间的边界、全字节值 0..=255、边界+`\rX`，以及一个空部件。

## 实测记录（2026-09-23，Linux 6.8，rustc 1.98.1）

### 自动化测试

```
$ cargo test
running 14 tests  (tests/parser_tests.rs)
test result: ok. 14 passed; 0 failed
running 3 tests   (tests/server_tests.rs)
test result: ok. 3 passed; 0 failed
```

覆盖：二进制正文含边界前缀/近似边界（8 种分片大小 + 逐字节，正文逐字节相等）、
空部件与无头部件、缺结束边界、截断的结束边界、部件数/头部/总量/单部件超限、
任意伪随机分片与整包喂入等价、512 KiB 正文只保留 18 字节回看窗口、
preamble 跳过、非法头部、Done 后继续喂入、非法 boundary。

### 手动 TCP 验证

```
$ ./target/release/mpstream-server --addr 127.0.0.1:18091 &
$ ./scripts/send_sample.sh 127.0.0.1:18091
OK
parts=3 total_bytes=661
part 0 name="title" filename="" bytes=22
part 1 name="blob" filename="blob.bin" bytes=347
part 2 name="empty" filename="" bytes=0
```

（part 1 的 347 字节与样例脚本构造的二进制正文长度一致，近似边界全部按正文处理。）

异常输入：

```
$ printf 'BOUNDARY t1\r\n--t1\r\n\r\nabc\r\n' | nc -N 127.0.0.1 18092
ERROR missing final boundary --<boundary>--

$ # 10 KiB 正文，服务端 --max-total 4096
ERROR total size exceeded limit 4096
```

### 未通过项 / 已知限制

* 当前无未通过的测试。
* 已知限制（子集之外，非缺陷）：不支持嵌套 multipart、Content-Transfer-Encoding、
  头折行；header 值按 UTF-8 有损转换；preamble 中形如 `--boundary` 后接非法字节的
  内容按垃圾跳过（不报错）。
