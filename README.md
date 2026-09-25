# HTTP 分帧一致性（HTTP/1.1 Framing Consistency）

纯后端 Rust 项目：一个**逐字节增量解析**的 HTTP/1.1 请求分帧库，外加一个
**本地 TCP 测试服务**。核心解析器全部手写，**不依赖任何现成 HTTP 协议解析器**
（整个 crate 零第三方依赖、零 `unsafe`）。本项目**不做代理转发**：服务在本地
终结连接，自己完成分帧，把每个请求的分帧结果以 JSON 回显，遇到歧义即返回
400/413 并关闭连接。

分帧结论必须与 TCP 分段无关：同一段字节流，无论每次 `recv`/发送切在哪个字节
位置，得到的帧边界、错误类型与错误字节偏移都完全一致。这是本项目的核心验收
性质，并有穷举式测试保证（见下文“字节切分测试”）。

---

## 1. 支持的子集（明确边界）

**请求行**（严格三令牌，只接受 SP 分隔，禁止 HTAB/多余空白）

```
METHOD SP request-target SP "HTTP/1.1" CRLF
```

- `METHOD`：RFC 7230 `tchar`；`request-target`：可见 ASCII（`VCHAR`）；
  版本**只接受字面量 `HTTP/1.1`**（HTTP/1.0 等直接拒绝）。
- 行结束符**只接受 CRLF**；裸 LF、裸 CR 均拒绝；请求前不允许空行。

**头部字段**（RFC 7230 语法）

```
field-name ":" OWS field-value OWS CRLF
```

- `field-name` 为 token（`tchar`），冒号前出现 SP/HTAB 直接按非法名拒绝
  （即 `"X : y"` 这种走私歧义绝不做兼容）。
- 物理行首字符为 SP/HTAB 一律按 **obs-fold（非法折行）**拒绝。
- 字段值允许 `SP/HTAB/VCHAR/obs-text`（按字节处理，不要求 UTF-8）；
  CR/LF/其它控制字符非法。
- 头部区以一个空 CRLF 行结束。

**报文主体（三者恰居其一）**

1. 无 `Content-Length` 且无 `Transfer-Encoding` → 空主体；
2. 恰好一个合法 `Content-Length: n` → 固定长度主体（n 字节后即下一帧起点）；
3. 恰好一个 `Transfer-Encoding`，其值（去 OWS 后，大小写不敏感）**精确等于
   `chunked`** → chunked 主体，支持多块、chunk-ext（严格子集）与 trailer。

**HTTP/1.1 流水线**：一帧结束后剩余字节立即作为下一帧解析；`Connection: close`
被记录，回该帧响应后关闭连接。

**chunked 细节**：块大小为 1 个以上十六进制数字（解析进 `u64`，溢出拒绝）；
块数据后必须严格 CRLF；扩展允许 `name` / `name=token` /
`name="quoted-string"` 这一严格子集；trailer 语法同头部，但禁止出现
`Content-Length`/`Transfer-Encoding`/`Trailer` 字段。

`Content-Length` 特殊兼容：接受**单个字段内**逗号分隔且值完全一致的形式
（`Content-Length: 5, 5`，RFC 9112 允许）；值不一致即冲突拒绝。

### 明确拒绝（绝不静默“兼容/修正”）

| 情形 | 错误类型 (`ErrorKind`) | HTTP |
|---|---|---|
| 裸 LF（`\n` 前无 `\r`） | `bare_line_feed` | 400 |
| 裸 CR（`\r` 后非 `\n`） | `bare_carriage_return` | 400 |
| 请求行非严格三令牌 / 含 HTAB / 首尾空白 | `malformed_request_line` | 400 |
| 方法含非法字节 | `invalid_method` | 400 |
| target 含空白/控制字符 | `invalid_request_target` | 400 |
| 版本非 HTTP/1.1 | `unsupported_version` | 400 |
| 头部无冒号 | `malformed_header` | 400 |
| 头部名非法（含冒号前空白） | `invalid_header_name` | 400 |
| 字段值含非法控制字节 | `invalid_header_value` | 400 |
| obs-fold 折行（行首 SP/HTAB） | `obs_fold` | 400 |
| 两个 `Content-Length` 字段（即使相等） | `duplicate_content_length` | 400 |
| CL 非十进制/空/数值溢出 | `invalid_content_length` | 400 |
| 同一 CL 字段内值冲突（`5, 6`） | `conflicting_content_length` | 400 |
| TE 与 CL 同时存在（两种字段顺序都拒） | `te_and_cl` | 400 |
| TE 非单一 `chunked`（多编码/`identity`/重复 TE 字段等） | `invalid_transfer_encoding` | 400 |
| 块大小非十六进制 / 块后缺 CRLF | `malformed_chunk_size` | 400 |
| 块大小超过 `u64` | `chunk_size_overflow` | 413 |
| 单块/累计主体超上限 | `chunk_size_too_large` | 413 |
| chunk-ext 非法字节/空名 | `invalid_chunk_extension` | 400 |
| trailer 中出现 CL/TE/Trailer | `forbidden_trailer_field` | 400 |
| 请求行超限 | `request_line_too_long` | 400 |
| 头部区超限 | `header_section_too_large` | 400 |
| 头部字段数超限 | `too_many_headers` | 400 |
| 声明/实际主体超限 | `body_too_large` | 413 |
| trailer 区超限 | `trailer_section_too_large` | 400 |
| trailer 字段数超限 | `too_many_trailers` | 400 |

### 长度上限（`Limits`，可在库与服务端配置）

| 配置 | 默认值 |
|---|---|
| 请求行字节数（不含 CRLF） | 8192 |
| 头部区总字节 | 65536 |
| 头部字段个数 | 100 |
| 主体总字节 | 1 048 576 |
| 单条块大小行字节 | 1024 |
| trailer 区总字节 | 16384 |
| trailer 字段个数 | 16 |

**不做的事**：不实现 HTTP/1.0 及更早、不实现 `identity`/压缩等其它传输编码、
不解析 `Expect: 100-continue`（不主动发 `100`）、不转发、不做 TLS、不做前端。

---

## 2. 目录结构

```
Cargo.toml
src/
  lib.rs                        # 库入口 + parse_all 便捷封装；forbid(unsafe_code)
  error.rs                      # ErrorKind / ParseError 错误分类与状态码映射
  text.rs                       # RFC 7230 字节谓词、OWS 修剪
  framer.rs                     # 核心：增量逐字节状态机（请求行/头/定长/chunked/trailer）
  server.rs                     # 本地 TCP 服务（每连接一个 Framer，JSON 回显）
  bin/http-framing-server.rs    # 服务端可执行文件（CLI 参数）
  bin/http-framing-raw.rs       # 原始字节回放客户端（可逐字节发送）
tests/
  common/mod.rs                 # 字节切分差分器 + 原始 HTTP 响应读取工具
  framer_unit.rs                # 合法帧 / 拒绝类型 / 上限 / 偏移稳定性（54 项）
  byte_split.rs                 # 每个字节位置切分 + 随机分段 + 逐字节（24 项）
  samples_test.rs               # 以 samples/ 与清单驱动的全样例切分测试（2 项）
  server_e2e.rs                 # 启动真实二进制打真实 TCP 的端到端测试（11 项）
samples/                        # 31 个原始请求样例 + samples.json 清单
tools/gen_samples.py            # 样例生成器（字节精确，含 CRLF）
README.md
RUNLOG.md                       # 实际运行命令与结果的如实记录
```

---

## 3. 快速开始

需要 Rust 工具链（开发环境为 rustc/cargo 1.98.1）。

```bash
cargo build --release

# 启动本地测试服务（默认 127.0.0.1:8080）
./target/release/http-framing-server --bind 127.0.0.1:8080

# 用原始字节回放工具发送样例（字节不会被重新编码）
./target/release/http-framing-raw --bind 127.0.0.1:8080 samples/05-chunked-simple.http

# 逐字节发送（每字节间隔 1ms），强制走增量路径
./target/release/http-framing-raw --bind 127.0.0.1:8080 --byte-by-byte 1 \
    samples/09-pipeline-two.http
```

也可以直接用 shell：

```bash
# 合法 GET
printf 'GET /hello HTTP/1.1\r\nHost: e\r\n\r\n' | nc 127.0.0.1 8080

# 典型 CL.TE 走私歧义 → 400 te_and_cl 并关闭连接
printf 'POST / HTTP/1.1\r\nHost: e\r\nContent-Length: 6\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n' | nc 127.0.0.1 8080
```

服务端每个成功分帧的请求返回一条 JSON（流水线请求按序各返回一条）：

```json
{"type":"frame","seq":1,"method":"POST","target":"/c","framing":"chunked",
 "header_count":2,"headers":[["Host","e"],["Transfer-Encoding","chunked"]],
 "body_len":5,"body":"hello","trailer_count":1,"trailers":[["X-A","1"]],
 "consumed":60}
```

错误响应：

```json
{"type":"error","status":400,"error":"te_and_cl","offset":26}
```

服务端 CLI：`--bind ADDR`、`--idle-timeout SECS`、`--max-body BYTES`、
`--max-header-section BYTES`。

---

## 4. 库 API 速览

```rust
use http_framing::{Framer, Step, Limits};

let mut framer = Framer::new();           // 或 Framer::with_limits(Limits{..})
let (step, consumed) = framer.step(&buf); // buf 可以是任意长度的任意字节切片
match step {
    Step::Incomplete        => { /* 继续喂字节，已吸收 consumed 字节 */ }
    Step::Frame(frame)       => { /* 一帧完成；剩余字节从 &buf[consumed..] 继续 */ }
    Step::Error(parse_error) => { /* 连接作废；error.kind 与 error.offset 确定 */ }
}
```

`ParseError { kind: ErrorKind, offset: usize }` 中的 `offset` 是**相对于整条
连接字节流**的绝对偏移，并且与分段方式无关（有专门的稳定性测试）。

---

## 5. 自动化测试与验收对应

```bash
cargo test            # 全部 91 项
cargo clippy --all-targets   # 零告警
cargo fmt --check
```

- **字节位置切分**（验收硬性要求）：`tests/byte_split.rs` 对每个输入
  在**每一个字节位置**切成两段分别喂入，与“一次性喂入”的参考结果比对；
  另外逐字节喂入，并用 6 个种子的伪随机多段切分加压。
- **覆盖内容**：流水线请求（含 GET→定长 POST→chunked→GET 四连）、
  chunked 尾部（trailer）与扩展、4KiB 长定长主体、50 个小块、
  超限请求行/头部区/声明主体/块主体，以及 CL.TE、TE.CL、重复 CL、
  CL 冲突列表、obs-fold、冒号前空白、块后缺 CRLF 等典型走私歧义。
- **样例驱动**：`samples/` 31 个样例（14 合法 / 17 非法），每个样例同样在
  每个字节位置切分验证；清单 `samples.json` 声明接受/拒绝与确切错误码。
- **真实 TCP 端到端**：`tests/server_e2e.rs` 启动真实服务二进制（绑定随机
  端口），用裸 socket 发送字节，验证 200 JSON、流水线按序响应、逐字节送达、
  400/413、以及致命分帧错误后连接关闭且私藏的“第二个请求”不会被处理。

实际执行命令与结果（含中途发现并修复的问题）见 **[RUNLOG.md](RUNLOG.md)**。
