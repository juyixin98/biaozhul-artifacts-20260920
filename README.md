# resp-incr — RESP2 增量字节解析库 + 本地 TCP 测试服务

纯后端 Rust 项目：一个增量 RESP2 解析库（`resp_incr`）和一个基于它的本地
TCP 回显测试服务（`resp-server`）。核心解析器为手写实现，未使用任何现成
RESP/Redis 协议解析 crate（`Cargo.toml` 零依赖）。

## 支持的子集（RESP2）

| 类型 | 线格式 | 库内表示 |
|---|---|---|
| 简单字符串 | `+OK\r\n` | `Value::SimpleString(String)` |
| 错误 | `-ERR msg\r\n` | `Value::Error(String)` |
| 整数 | `:-123\r\n`（含 `i64::MIN/MAX`） | `Value::Integer(i64)` |
| 二进制 bulk 串 | `$5\r\nhello\r\n`（payload 可含任意字节，包括 `\r\n`） | `Value::BulkString(Option<Vec<u8>>)` |
| 数组（可嵌套） | `*2\r\n...` | `Value::Array(Option<Vec<Value>>)` |

**空值与空串严格区分**：`$-1\r\n` → `BulkString(None)`（null），
`$0\r\n\r\n` → `BulkString(Some(vec![]))`（空串）；数组同理
（`*-1` null vs `*0` 空数组）。

**不支持**（明确出范围）：RESP3 类型（map/set/double 等）、inline 命令、
流式/chunked 回复。

## 可配置上限（`Config`）

| 字段 | 默认 | 含义 |
|---|---|---|
| `max_depth` | 64 | 数组最大嵌套深度（顶层值为第 1 层） |
| `max_buffer_bytes` | 16 MiB | 未解析字节缓冲上限；不完整报文驻留缓冲，故也等于单条报文总字节预算 |
| `max_bulk_len` | 512 MiB | 单个 bulk payload 长度上限 |
| `max_array_len` | 1 Mi | 单个数组元素数上限 |

## 错误类型（`ParseError`）

`InvalidTypeByte` / `InvalidNumber` / `NegativeLength`（负长度且 ≠ -1，非法）/
`MissingCrlf` / `InvalidUtf8` / `DepthLimitExceeded` / `BufferBudgetExceeded` /
`BulkLengthLimitExceeded` / `ArrayLengthLimitExceeded`。
（`Incomplete` 仅内部使用，对外表现为 `Parser::next()` 返回 `Ok(None)`。）

## 库用法

```rust
use resp_incr::{Parser, Config};

let mut p = Parser::new(Config::default());
p.feed(b"$5\r\nhel")?;        // 任意分片喂入
assert_eq!(p.next().unwrap(), None);   // 字节不够，等待
p.feed(b"lo\r\n")?;
let msg = p.next().unwrap().unwrap();  // BulkString(Some(b"hello"))
```

硬错误后流位置未定义，复用前调用 `parser.reset()`。

## TCP 测试服务

对每条完整报文回写其规范重编码（round-trip 回显）；解析出错时回写
`-ERR <原因>\r\n` 并重置解析器，连接保持可用。

```bash
cargo run --release --bin resp-server -- \
    --addr 127.0.0.1:16380 \
    --max-depth 64 --max-buffer-bytes 16777216 \
    --max-bulk-len 536870912 --max-array-len 1048576
```

用样例请求文件（含 8 条连续报文：PING/ECHO 命令、简单串、整数、null bulk、
空 bulk、payload 内含 CRLF 的 bulk、嵌套数组+null）实测：

```bash
# 分 7 字节小片发送，验证增量解析
python3 - <<'EOF'
import socket
data = open('samples/requests.resp','rb').read()
s = socket.create_connection(('127.0.0.1', 16380))
for i in range(0, len(data), 7):
    s.sendall(data[i:i+7])
s.shutdown(socket.SHUT_WR)
out = b''
while (c := s.recv(4096)): out += c
assert out == data, "round-trip mismatch"
print("round-trip OK:", len(out), "bytes")
EOF
```

## 自动化测试

```bash
cargo test
```

- `tests/incremental.rs`（14 项）
  - **全切分点**：对 15 条样例报文，在每一个字节切分点切成两段喂入，
    结果必须与整体解析一致；另有逐字节喂入测试。
  - CRLF 出现在 bulk 内部（`$4\r\na\r\nb\r\n`）、二进制 payload、
    bulk 尾部非 CRLF 报错。
  - 负长度非法值：`$-2` / `$-100` / `*-2` / `*-99` → `NegativeLength`；
    `-1` 是合法 null 标记。
  - 深嵌套：10 层嵌套在 `max_depth=5` 下报 `DepthLimitExceeded`，
    默认上限下正常；边界值（5 层数组需深度 6）两侧都测。
  - 多报文连续输入：一次喂入 4 条、以及按 7 字节错位分片喂入 3 条。
  - 空值 vs 空串、缓冲字节预算、bulk/数组长度上限、非法类型字节/数字、
    编码 round-trip。
- `tests/server.rs`（1 项）：真实 TCP 集成测试——启动 `resp-server`
  二进制，逐字节发送分片命令、一次发送多条报文、非法输入返回
  `-ERR` 帧后连接继续可用。

## 实际运行记录（如实）

环境：cargo 1.98.1 / rustc 1.98.1，Linux 6.8。

1. `cargo build` — 通过。
2. 首次 `cargo test` — **10 项未通过**。原因：`find_crlf` 切片上界写错
   （`buf[from..buf.len()-1]` 漏掉最后一字节，行尾 CRLF 在缓冲末尾时找不到）。
   修复后复测。
3. 第二次 `cargo test` — **4 项未通过**，两类原因：
   - 库 bug：`parse_i64` 正向累加导致 `i64::MIN`（`-9223372036854775808`）
     溢出误报 `InvalidNumber`，改为 i128 累加后修复；
   - 测试数据 bug（非库问题）：`$5\r\na\r\nb\r\n` 中 `a\r\nb` 实为 4 字节，
     声明长度写错，改为 `$4`。
4. 第三次 `cargo test` — **全部通过：14 + 1 = 15 项，0 失败**。
5. 实测 TCP 服务（release 构建）：
   - 样例文件 104 字节按 7 字节分片发送，回显与原文逐字节一致
     （`reply == request: True (104/104 bytes)`）；
   - 首轮实测发现嵌套样例少一个元素（`*3` 只给了 2 个，解析器正确地
     持续等待），系样例文件笔误，补 `$-1\r\n` 后通过；
   - 错误帧实测：`$-2` → `-ERR invalid negative length -2`、
     `?hello` → `-ERR invalid type byte 0x3f`、`:12a` → `-ERR invalid number`；
     出错后同连接发 `+RECOVERED` 正常回显；
   - 限制实测（`--max-depth 3 --max-buffer-bytes 64`）：4 层嵌套 →
     `-ERR nesting depth limit exceeded`；100 字节不完整 bulk →
     `-ERR buffer byte budget exceeded`。

最终状态：**无未通过项**。

## 目录结构

```
Cargo.toml
src/lib.rs            库入口与文档
src/value.rs          Value / Config / encode
src/parser.rs         增量解析器（核心，手写）
src/bin/resp-server.rs  TCP 测试服务
tests/incremental.rs  解析器测试（含全切分点）
tests/server.rs       TCP 集成测试
samples/requests.resp 样例请求（8 条连续报文，真实 CRLF）
```
