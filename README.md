# http-framing — HTTP/1.1 请求分帧一致性（纯后端）

一个用 Rust 从零实现的**增量字节解析库**及配套的**本地 TCP 测试服务**，
目标是：无论字节以何种分片方式到达（整包、每次一字节、任意位置切分），
对同一字节流必须做出**完全一致**的分帧决定。用于验证 HTTP/1.1
请求分帧子集并暴露请求走私（request smuggling）类歧义。

- **纯 Rust、仅依赖 std**；核心解析没有使用任何现成 HTTP 协议解析库。
- **不做代理转发**：服务端只分帧并回报它看到的结构，不把请求发往任何上游。
- **无前端**：只有库、TCP 服务、命令行和自动化测试。

## 目录结构

```
src/
  error.rs     错误类型目录（22 种稳定 ErrorKind，含 HTTP 状态映射）
  parser.rs    纯函数语法单元：请求行、头字段、Content-Length、chunk-size
  framer.rs    增量状态机（Head / Fixed / Chunked，跨 feed 保持游标）
  server.rs    本地 TCP 参考服务（线程/连接；同构的 stdio 处理函数）
  lib.rs       库入口
  main.rs      http-framing-server 可执行程序
tests/
  common/mod.rs 公共测试语料（合法、歧义走私、超限、截断）
  split.rs          验收核心：每个字节位置切分
  framer_behaviour.rs 解码结果 / 流水线 / 流式语义
  server_tcp.rs     真实 TCP 端到端（随机端口、服务端 1 字节 read）
samples/       12 个精确 CRLF 的原始请求样例 + 说明
scripts/send_samples.sh 批量/逐字节发送样例
```

## 支持子集（明确边界）

- 请求行：`METHOD SP request-target SP HTTP/1.1 CRLF`
  - 仅 `HTTP/1.1`（`HTTP/1.0` 等返回 `unsupported-version`）；
  - target 仅 origin-form（`/…`）与 `OPTIONS *`；
  - 分隔符只能是恰好一个 SP。
- 头区块：`field-name ":" [SP] field-value CRLF`，空行结束。
  - 冒号后**至多一个** SP 作为 OWS；
  - 字段值允许可打印 ASCII 与**单个**内部 SP（使 `Content-Length: 5, 5`
    这类输入能到达 CL 语法并被正确拒绝），拒绝制表符、控制字符、
    连续空格、首尾空白。
- 主体分帧：
  - 无 CL 且无 TE → 无主体；
  - **恰好一个**严格十进制 `Content-Length`（无符号、无前导零、无空白、
    无逗号列表、无 u64 溢出）；
  - **恰好一个** `Transfer-Encoding: chunked`（大小写不敏感）；
  - chunked 按 RFC 9112 §4 解码：十六进制 chunk-size、每块后必须 CRLF、
    `0` 块后解析 trailer（trailer 中禁止 CL/TE）；
    **不支持** chunk 扩展（`;…` → `chunk-size-invalid`）。
- 流水线：同一连接上的多个请求独立分帧，按顺序逐一回报。

## 明确拒绝的歧义（走私防御）

| 输入特征 | ErrorKind |
|---|---|
| `Transfer-Encoding` 与 `Content-Length` 同时出现（任意顺序） | `te-with-content-length` |
| 两个 `Content-Length`（相同或冲突） | `duplicate-content-length` |
| CL 值含逗号/空白/前导零/符号/溢出 | `invalid-content-length` |
| TE 不是精确的 `chunked`（identity、`gzip, chunked`、重复 TE） | `invalid-transfer-encoding` |
| 非法折行 obs-fold（CRLF 后接 SP/HT） | `obsolete-line-folding` |
| 冒号前空白、字段名含空格、多前导 SP、尾随空白、请求行多 SP | `ambiguous-whitespace` / `invalid-header-name` |
| 裸 LF、行内杂散 CR | `bad-line-ending` |
| chunk 扩展、非十六进制 chunk-size | `chunk-size-invalid` |
| chunk 数据后不是 CRLF | `chunk-terminator` |
| trailer 中出现 CL/TE | `trailer-forbidden-field` |

## 长度上限（流式生效，默认值可命令行覆盖）

| 限制 | 默认 | 超限错误 |
|---|---|---|
| 请求行字节数 | 8192 | `request-line-too-large` (431) |
| 头区块总字节数 | 65536 | `headers-too-large` (431) |
| 头字段数量 | 100 | `headers-too-large` (431) |
| 主体总字节数（CL 声明值与 chunked 解码累计） | 1 MiB | `body-too-large` (413) |
| 单个 chunk 大小 | 1 MiB | `chunk-too-large` (413) |

限制在**流式解析过程中**判定（例如第二个 chunk 使累计超限时立即拒绝），
不会先把超限数据全部缓冲。

## 错误类型

`ErrorKind` 共 22 个稳定标识，见 `src/error.rs`；服务端把标识放在
`X-Frame-Error` 响应头。完整清单：

```
bad-line-ending  malformed-request-line  invalid-method  invalid-target
unsupported-version  header-missing-colon  invalid-header-name
invalid-header-value  obsolete-line-folding  ambiguous-whitespace
duplicate-content-length  invalid-content-length  invalid-transfer-encoding
te-with-content-length  chunk-size-invalid  chunk-too-large
chunk-terminator  trailer-forbidden-field  request-line-too-large
headers-too-large  body-too-large  incomplete
```

## 构建与运行

```bash
cargo build --release

# TCP 服务（默认 127.0.0.1:8080）
./target/release/http-framing-server --addr 127.0.0.1:8080 --read-size 1

# 选项：覆盖各项上限
#   --max-request-line N --max-header-block N --max-header-count N
#   --max-body-bytes N --max-chunk-size N

# stdio 模式：从标准输入读原始字节，把响应写到标准输出
cat samples/03_chunked_trailer.http | ./target/release/http-framing-server --stdio
```

每个成功分帧的请求得到一个 `200`，响应体是手工拼的 JSON，例如：

```json
{"method":"POST","target":"/upload","headers":[["Host","example.com"],
["Transfer-Encoding","chunked"]],"framing":{"mode":"chunked"},
"body_hex":"57696b697065646961","body_len":9,
"trailers":[["ETag","\"deadbeef\""],["X-Checksum","ok"]]}
```

分帧失败时返回**一个** `4xx`（带 `X-Frame-Error`、`Connection: close`），
随后排空对端已发数据并关闭连接——被拒头部之后的字节绝不会被当作第二个请求。

## 自动化测试

```bash
cargo test
```

- `tests/split.rs::every_byte_position_split` 是验收核心：对语料中**每个**
  请求，在**每一个字节位置 k（0..=len）**把输入切成 `[0,k)` + `[k,n)`
  两次喂入，结果必须与整包喂入完全一致（合法流：请求数与解码字节一致；
  非法流：ErrorKind 一致）。
- 另有“每次 1 字节”与不规则多片切分两组调度；`all_error_kinds_exercised…`
  保证语料覆盖全部 22 种错误。
- `tests/server_tcp.rs` 在真实 TCP 上验证（服务端 `read()` 粒度设为 1 字节，
  客户端也逐字节发送）：流水线 3 请求 3 响应、chunked 解码、
  走私尝试得 400 且无 200、超限得 431、EOF 截断得 400。

## 设计要点：为什么切分等价性成立

- 所有跨 feed 状态都在状态机里（当前阶段、行起点、扫描游标、已读主体数），
  不存在“分片时一套规则、整包时另一套规则”。
- 行结束扫描对“缓冲末尾恰好是裸 CR”做了游标保留，使下一片的 LF 能与之配对；
  真正的行内杂散 CR 在该行被 CRLF 终止时立即判错。
- 固定长度主体按字节计数消费，主体中的 `CRLF` / `GET …` 只是不透明负载。
- chunked 的每个阶段（size-line → data → CRLF → … → trailer → 终止 CRLF）
  都是显式状态，不允许 chunk 扩展或隐式猜测。
