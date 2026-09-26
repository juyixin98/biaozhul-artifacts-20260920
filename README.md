# incutf8 — 增量 UTF-8 验证与解码（纯后端）

一个零依赖 Rust 项目：流式（增量）UTF-8 验证/解码库 + JSON 控制入口（CLI）。
核心算法（UTF-8 状态机、UTF-8 编码器、Base64、JSON 子集）全部自行实现，
不依赖任何第三方 crate，也不使用 `std::str::from_utf8` 之类的现成验证器。

## 功能

- **跨块增量验证与解码**：`Decoder` 是 O(1) 内存状态机，多字节序列可以在
  任意字节处被切分到不同的块中，状态在 `feed()` 调用之间保持。
- **严格校验**（RFC 3629 / Unicode 推荐实践）：
  - 拒绝过长编码（overlong）：`C0 80`、`E0 80..9F ..`、`F0 80..8F ..` 等；
  - 拒绝 UTF-16 代理项（surrogate）U+D800..=U+DFFF：`ED A0..BF ..`；
  - 拒绝超出 U+10FFFF 的值：`F4 90..BF ..`、`F5..F7 ..`；
  - 拒绝孤立续字节（0x80..=0xBF）与非法字节（0xF8..=0xFF）。
- **结束时未完成序列报错**：`finish()` 时若序列未完结，报告
  `incomplete_sequence`，并给出**原始字节偏移**（序列首字节在整个流中的
  绝对偏移）以及已消费的字节数。
- **错误恢复不静默替换**：损坏内容永远不会被替换成 U+FFFD 之类的占位符。
  `abort` 策略在首个错误处停止；`collect` 策略记录错误、丢弃坏字节、
  重新同步后继续解码，输出中只包含流里合法的部分，所有损坏都在
  `errors` 数组中显式报告。
- **内存与输出长度限制**：
  - 解码器状态为 O(1)（最多挂起 3 字节）；
  - `max_output_codepoints` 限制解码输出的码点数量（默认 1,000,000），
    超限报 `output_limit_exceeded` 并置 `truncated: true`；
  - `validate` 模式不缓冲输出，内存与输入大小无关；
  - CLI 限制请求体大小（默认 32 MiB，`--max-request-bytes` 可调）。

## 构建与测试

```sh
cargo build            # 构建库与 CLI（target/debug/incutf8）
cargo test             # 运行全部自动化测试
cargo build --release  # 发布构建
```

## 库 API（Rust）

```rust
use incutf8::{Decoder, ErrorPolicy, decode_all, encode_codepoint, encode_str};

// 增量解码：任意切分输入
let mut d = Decoder::new(ErrorPolicy::Abort);
d.feed(b"hello \xF0\x9F");   // 🦀 的前两个字节
d.feed(&[0xA6, 0x80]);       // 🦀 的后两个字节
let report = d.finish();     // 结束时检查未完成序列
assert!(report.ok());

// 一次性解码
let report = decode_all(b"abc", ErrorPolicy::Abort, 1_000_000);

// 编码
let mut bytes = Vec::new();
encode_codepoint(0x1F980, &mut bytes).unwrap();
let bytes = encode_str("héllo 🦀");
```

`DecodeReport` 字段：`output`（解码输出，必为合法 UTF-8）、`errors`
（`DecodeError { kind, offset, sequence_start, byte, sequence_len, detail }`）、
`consumed_bytes`、`output_codepoints`、`truncated`、`pending_sequence_bytes`。

错误类别（`ErrorKind`）：`invalid_lead_byte`、`overlong_encoding`、
`invalid_continuation_byte`、`surrogate_code_point`、`code_point_out_of_range`、
`incomplete_sequence`、`output_limit_exceeded`。

## JSON 控制协议

CLI 从 stdin（或 `--request FILE`）读取**一个 JSON 请求对象**，向 stdout
输出**一个 JSON 响应对象**。

退出码：`0` = 成功；`1` = 请求合法但解码/编码有错误；`2` = 请求本身非法
（JSON 语法错误、未知 op、Base64 非法、请求超限等）。

### op: `ping`

```json
{"op":"ping"}
→ {"ok":true,"op":"ping","version":"0.1.0"}
```

### op: `decode`

请求字段：

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `op` | string | 必填 | `"decode"` |
| `chunks` | string[] | — | Base64 编码的输入块（按序喂给解码器；与 `data` 二选一） |
| `data` | string | — | 单块 Base64 输入（与 `chunks` 二选一） |
| `final` | bool | `true` | 是否为流的最后一块；`false` 时挂起序列不算错误 |
| `error_policy` | string | `"abort"` | `"abort"`（首个错误即停）或 `"collect"`（记录并恢复） |
| `max_output_codepoints` | uint | 1000000 | 解码输出码点上限 |
| `emit_output` | bool | `true` | 是否返回解码文本 |

响应字段：

| 字段 | 说明 |
|---|---|
| `ok` | 无任何错误时为 `true` |
| `consumed_bytes` | 实际处理的输入字节数（提前停止时小于输入长度） |
| `output_codepoints` | 输出的码点数 |
| `output_text` | 解码文本（`emit_output:true` 时存在） |
| `truncated` | 因输出上限而停止时为 `true` |
| `pending_sequence_bytes` | 结束时仍挂起的序列字节数 |
| `errors` | 错误数组：`kind`、`offset`（绝对字节偏移）、`sequence_start`、`sequence_len`、`byte_hex`、`detail` |

### op: `validate`

与 `decode` 相同，但不缓冲、不返回输出文本（恒定内存），只报告合法性。

### op: `encode`

请求（二选一）：`{"op":"encode","text":"..."}` 或
`{"op":"encode","codepoints":[104, 0x4E2D]}`（十进制整数）。

响应：`data_base64`（编码结果）、`bytes`（字节数）、`errors`
（非法码点，含 `index`、`codepoint_hex`、`kind`）。

### 请求样例

见 `examples/requests/`：

```sh
# 正常解码（"hello, 世界"）
incutf8 --request examples/requests/decode_ok.json
# 跨块多字节序列（🦀 被切成 F0 9F | A6 80 两块）
incutf8 --request examples/requests/decode_split_multibyte.json
# 过长编码 C0 80 → overlong_encoding
incutf8 --request examples/requests/decode_invalid_overlong.json
# 结束时序列未完成（E2 82）→ incomplete_sequence，offset 为首字节偏移
incutf8 --request examples/requests/decode_incomplete_final.json
# collect 策略恢复（"A" 0xFF "B" → 输出 "AB"，错误显式报告）
incutf8 --request examples/requests/decode_collect_recovery.json
# 编码
incutf8 --request examples/requests/encode_text.json
# 校验代理项 ED A0 80 → surrogate_code_point
incutf8 --request examples/requests/validate_surrogate.json
```

也可以直接通过管道：

```sh
echo '{"op":"decode","data":"aGVsbG8="}' | incutf8
```

## 自动化测试

| 测试文件 | 覆盖 |
|---|---|
| `tests/split_points.rs` | 语料上**所有**两块切分点、所有三块切分点对、逐字节喂入、伪随机切分；跨块挂起状态计数 |
| `tests/invalid_bytes.rs` | 全部 256 个单字节、**全部 65536 个双字节组合**（与独立参考实现对照）；过长编码/代理项/超范围/孤立续字节/截断序列的具体用例；abort 与 collect 恢复策略；输出上限 |
| `tests/roundtrip.rs` | **全部 1,114,112 个合法码点**的编码→解码往返；编码器拒绝代理项与超范围值；与语言内建 UTF-8 表示一致性 |
| `tests/cli.rs` | 端到端：真实进程跑 JSON 请求，校验响应字段与退出码 |

## 实际运行记录

> 以下为本仓库真实执行的结果（Linux x86_64，rustc 1.98.1 (48a229cea 2026-09-01)，
> 工具链装于 `/tmp/rust-local`，通过 `export PATH=/tmp/rust-local/bin:$PATH` 使用）。

### 构建

```
$ cargo build
   Compiling incutf8 v0.1.0 (/home/admin/Downloads/biaozhul/opp169/b)
    Finished `dev` profile [unoptimized + debuginfo] target(s) in 1.39s
```

无编译警告。

### 自动化测试（`cargo test`）

全部通过，**未通过项：0**。

| 测试目标 | 结果 |
|---|---|
| `tests/cli.rs` | 15 passed; 0 failed |
| `tests/invalid_bytes.rs` | 12 passed; 0 failed（含全部 256 单字节 + 全部 65536 双字节组合对照） |
| `tests/roundtrip.rs` | 5 passed; 0 failed（含全部 1,114,112 个合法码点往返） |
| `tests/split_points.rs` | 5 passed; 0 failed（含所有两块/三块切分点、逐字节、伪随机切分） |
| 合计 | **37 passed; 0 failed** |

### 样例请求（`incutf8 --request examples/requests/<file>`）

```
== decode_ok.json                      exit=0
{"ok":true,"op":"decode","final":true,"consumed_bytes":13,"output_codepoints":9,"truncated":false,"pending_sequence_bytes":0,"output_text":"hello, 世界","errors":[]}

== decode_split_multibyte.json        exit=0   （🦀 = F0 9F | A6 80 跨两块）
{"ok":true,"op":"decode","final":true,"consumed_bytes":4,"output_codepoints":1,"truncated":false,"pending_sequence_bytes":0,"output_text":"🦀","errors":[]}

== decode_invalid_overlong.json       exit=1   （C0 80）
{"ok":false,...,"errors":[{"kind":"overlong_encoding","offset":0,"sequence_start":0,"sequence_len":1,"detail":"lead byte is only ever used for overlong encodings","byte_hex":"0xC0"}]}

== decode_incomplete_final.json       exit=1   （E2 82 后流结束）
{"ok":false,...,"pending_sequence_bytes":2,"errors":[{"kind":"incomplete_sequence","offset":0,"sequence_start":0,"sequence_len":2,"detail":"input ended with 2 of 3 bytes of a multi-byte sequence"}]}

== decode_collect_recovery.json       exit=1   （"A" 0xFF "B"，collect 策略）
{"ok":false,...,"output_text":"AB","errors":[{"kind":"invalid_lead_byte","offset":1,...,"byte_hex":"0xFF"}]}
（输出为 "AB"：坏字节被丢弃并显式报告，未静默替换为 U+FFFD）

== encode_text.json                   exit=0
{"ok":true,"op":"encode","data_base64":"aMOpbGxvIPCfpoAg5Lit","bytes":15,"errors":[]}

== validate_surrogate.json            exit=1   （ED A0 80 = U+D800）
{"ok":false,"op":"validate",...,"errors":[{"kind":"surrogate_code_point","offset":1,"sequence_start":0,"sequence_len":1,"detail":"expected continuation byte in 0x80..=0x9F, got 0xA0","byte_hex":"0xA0"}]}
```

### 管道与限制

```
$ echo '{"op":"decode","data":"aGVsbG8="}' | incutf8 ; echo exit=$?
{"ok":true,...,"output_text":"hello","errors":[]}
exit=0

$ echo '{"op":"decode","data":"aGVsbG8="}' | incutf8 --max-request-bytes 10 ; echo exit=$?
{"ok":false,"error":"request exceeds the limit of 10 bytes (use --max-request-bytes to raise it)"}
exit=2

$ echo 'not json' | incutf8 ; echo exit=$?
{"ok":false,"error":"invalid JSON: JSON error at character 2: invalid literal, expected 'null'"}
exit=2
```

### 环境备注（如实记录）

- 系统预装的 rustup stable 工具链已损坏（`rustc` 报 missing manifest），
  且 rustup 在线重装反复失败（下载校验和错误 / premature eof）。
  改用官方独立安装包 `rust-1.98.1-x86_64-unknown-linux-gnu.tar.xz`
  安装到 `/tmp/rust-local` 后完成构建与测试。
- 本 crate 零依赖，构建过程不需要访问 crates.io。
