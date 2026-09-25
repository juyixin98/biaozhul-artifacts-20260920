# minimultipart —— 手写的流式 multipart/form-data 解析器（纯 Rust / 纯 std）

一个从零实现的、**增量（流式）** `multipart/form-data` 字节解析库，外加一个用于本地验收的
极简 HTTP/1.1 TCP 服务。**不使用任何现成的 multipart/HTTP/mime 解析库**，核心分隔识别与
状态机全部手写，只依赖 Rust 标准库（连 SHA-256 都是自己实现的）。

## 它解决什么问题

接收 multipart 上传时，边界 `--boundary` 可能横跨任意两个 TCP 读取块，正文又是二进制的、
内部可以出现长得非常像边界的字节。解析器必须：

- **不缓存整个请求体**（大文件不能全进内存）；
- 边界可以在**任意字节位置**跨块到达；
- 正文里的 `--boundary`、`--boundary--`、`\r\n--boundary-x` 等**近似边界绝不能误判**；
- 对部件数量、头部、单部件、总大小四类资源都有明确上限，超限即错；
- 每种非法输入对应一个**明确**的错误类型。

## 仓库结构

```
Cargo.toml
src/
  lib.rs            库入口与子集/语义说明
  error.rs          Error：10 种明确错误（标签 + 是否“超限”）
  limits.rs         Limits：part 数 / 头 / 单 part / 总大小 四类上限
  event.rs          Event：PartBegin / Body / PartEnd / End
  mime.rs           手写 boundary 校验、Content-Type 与 part 头解析
  reader.rs         ★ 核心：增量字节状态机 MultipartReader
  sha256.rs         手写 SHA-256（FIPS 180-4，带标准向量自测）
  http.rs           极简 HTTP/1.1 请求头读取
  server.rs         本地 TCP 服务（逐块读取 → 解析 → JSON 报告）
  bin/server.rs     multipart-server 二进制入口
tests/
  integration.rs    14 个验收级解析器测试
  property.rs       随机化属性测试（随机分包 + 近似边界，确定性 PRNG）
  server.rs         6 个真实 TCP 回环的服务端到端测试
examples/
  request.http                请求报文样例（文本）
  gen_binary_payload.py       生成“含边界前缀”的二进制载荷
  binary-payload.bin          生成产物（96 字节，含 5 种近似边界）
scripts/
  acceptance.py               端到端验收脚本（启动服务、原始 socket 发包）
RUNLOG.md                     实际运行命令与结果记录
```

## 支持的子集（明确边界）

**支持：**

- Content-Type：`multipart/form-data; boundary=...`；boundary 可加引号、参数周围允许 OWS。
- boundary 规则（RFC 2046 bchar 的一个安全子集）：长度 1..=70，可见 ASCII（0x21..=0x7E），
  不含空格/控制字符，不全是 `-`。
- 每个 part 必须有 `Content-Disposition: form-data; name="..."`，可带 `filename="..."`
  （支持 quoted-pair 转义）；其余头原样收集（头名转小写）。
- 分隔：`CRLF "--" boundary CRLF`（下一 part）/ `CRLF "--" boundary "--" [CRLF]`（结束）。
- 关闭边界后的 CRLF 可选（RFC 2046）；**空 part** 完全合法（`PartBegin` 后直接 `PartEnd`）。
- 任意字节的二进制正文，原样透传、不做任何转码。

**明确不支持（遇到即返回明确错误，而不是“尽量解析”）：**

- RFC 2046 preamble（起始边界之前的内容）→ `missing_start_boundary`
- 关闭边界后的 epilogue / transport padding → `malformed_stream`
- obsolete line folding（头续行）、重复的 name/filename 参数、非 `form-data` 的 disposition
- HTTP 层的 chunked 请求体（`Transfer-Encoding`）、keep-alive（每连接只处理一个请求）

## 错误类型

| 错误标签 | 触发条件 | 服务端 HTTP |
|---|---|---|
| `invalid_boundary` | boundary 为空/>70/含非法字符/全 `-`，或 Content-Type 不是 multipart/form-data | 400 |
| `missing_start_boundary` | 流没有以 `--boundary` 开头 | 400 |
| `truncated` | `finish()` 时还在等待必需字节（缺结束边界、头没结束等） | 400 |
| `malformed_stream` | 分隔行后缀非法、关闭边界后有杂字节 | 400 |
| `missing_disposition` | part 无头或没有 `form-data; name=...` | 400 |
| `malformed_headers` | 头语法错误（无冒号、坏字段名、折行、坏引号等） | 400 |
| `header_too_large` | 单 part 头块 > `max_headers_size`（默认 16 KiB） | **413** |
| `too_many_parts` | part 数 > `max_parts`（默认 32） | **413** |
| `part_too_large` | 单 part 正文 > `max_part_size`（默认 8 MiB） | **413** |
| `total_too_large` | 整流 > `max_total_size`（默认 64 MiB） | **413** |

## 核心解析器怎么保证“流式且不误判”

`MultipartReader` 是一个字节驱动的状态机（`Start → FirstTerm → Headers → Body → AfterClose`）。

- 每次 `feed(chunk)` 只**确认**“不可能再参与边界匹配”的字节为正文；
  缓冲区尾部最多保留一个“可能是边界前缀”的尾巴。
- 正文状态搜索的是完整的 `CRLF "--" boundary`，且其后两个字节必须是 `CRLF` 或 `--`。
  缺前导 CRLF（如正文中间裸出现 `--B--`）一律是正文；
  有 CRLF、边界也对但后缀是 `-x` / `\rx` / 单个 `-`，也一律按正文处理并继续扫描。
- 当数据停在一个“看起来像边界但证据不足”的位置（例如只到 `\r\n--B-`）时，
  解析器**保留字节等待更多输入**，绝不提前下结论。
- 任意时刻内部缓冲 ≤ `分隔符长度 + 1`（boundary 70 时也只有 75 字节量级），
  与已传输的正文总量无关。测试 `parser_does_not_buffer_the_entire_body` 与属性测试
  都对该上界做了断言（409,600 字节流过，峰值缓存 0）。

## 快速开始

```bash
cargo build --release

# 启动本地服务（默认 127.0.0.1:8080；read_size=1 强制每个字节都走一遍状态机）
MULTIPART_READ_SIZE=1 ./target/release/multipart-server 8080

# 正常请求（curl 生成 multipart）
curl -s http://127.0.0.1:8080/upload \
  -F "field1=hello world" -F "empty=" \
  -F "file=@examples/binary-payload.bin;type=application/octet-stream"
```

成功响应示例（已实测）：

```json
{"ok":true,"parts":[
  {"name":"field1","filename":null,"size":11,"sha256":"b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"},
  {"name":"empty","filename":null,"size":0,"sha256":"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"},
  {"name":"file","filename":"binary-payload.bin","size":96,"sha256":"36dd86814cece97e1ee9d081188fd8f1076282dc0edd74a407e586986f600099"}
],"part_count":3,"total_bytes":107}
```

缺结束边界时：

```json
{"ok":false,"error":"truncated"}   // HTTP 400
```

### 环境变量配置

| 变量 | 默认 | 含义 |
|---|---|---|
| `MULTIPART_READ_SIZE` | 512 | 每次 TCP 读取字节数（调小强制跨块） |
| `MULTIPART_MAX_PARTS` | 32 | 最大 part 数 |
| `MULTIPART_MAX_HEADERS` | 16384 | 单 part 头块字节上限 |
| `MULTIPART_MAX_PART` | 8 MiB | 单 part 正文字节上限 |
| `MULTIPART_MAX_TOTAL` | 64 MiB | 整流字节上限 |

## 作为库使用

```rust
use minimultipart::{Limits, MultipartReader};
use minimultipart::event::Event;

let mut reader = MultipartReader::new(b"X", Limits::default())?;
let mut out = Vec::new();
for chunk in tcp_chunks {                 // 任意大小、任意切分
    for ev in reader.feed(&chunk)? {
        match ev {
            Event::PartBegin(meta) => { /* name/filename/headers */ }
            Event::Body(bytes)   => out.extend_from_slice(&bytes),
            Event::PartEnd       => { /* 一个 part 结束 */ }
            Event::End           => { /* 整个 multipart 结束 */ }
        }
    }
}
reader.finish()?;                         // 必须调用：检查缺结束边界/截断
```

## 测试

```bash
cargo test              # 单元 + 集成 + 属性 + TCP 端到端（共 28 个）
cargo clippy --all-targets
python3 scripts/acceptance.py         # 真实编译、起服务、原始 socket 验收（15 项）
```

重点测试对应验收要求：

- `binary_content_looking_like_boundary_is_not_misjudged` —— 5 种近似边界 ×
  **每一种分包大小（含 1 字节）**，正文都必须逐字节还原。
- `multiple_empty_parts` / `basic_two_parts_with_empty_one` —— 空部件。
- `missing_close_boundary_is_truncated` —— 多种截断位置报 `Truncated`，
  并反向验证“恰好停在完整关闭边界”算成功（CRLF 可选）。
- `limit_violations_have_distinct_errors` —— 四类超限各自报各自的错误。
- `parser_does_not_buffer_the_entire_body` —— 无完整缓存：40 万字节正文流过时
  内部缓存始终 ≤ 6 字节。
- `randomized_valid_streams_random_chunking` / `randomized_garbage_never_panics_and_stays_bounded`
  —— 300 个随机合法流（随机分包）与 50 路随机噪声。

## 安全说明

测试服务仅用于**本地**验收：无鉴权、无 TLS、顺序单连接处理、无 keep-alive，
请勿直接暴露到不可信网络。

## 许可证

MIT（见 [LICENSE](LICENSE)）。
