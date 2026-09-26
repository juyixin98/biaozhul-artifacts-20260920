# canonical-huffman (`chfc`)

零依赖、纯后端的**规范霍夫曼编码**（Canonical Huffman）流式二进制编解码库，
附带一个命令行工具和一个 JSON 控制入口。核心建树、规范码生成、位流 I/O、
JSON/Base64/Hex 全部自行实现，无任何第三方 crate。

- 语言：Rust 2021 edition，stable 工具链
- 依赖：**无**（`[dependencies]` 为空）
- 前端：无（纯库 + CLI + JSON stdin/stdout）

## 功能

- 确定性 Huffman 建树：平局按 `(权值, 次级键)` 决定，等频率输入输出逐字节可复现
- 只编码**码长表**，码字由 canonical 算法重算（位向量实现，支持最坏 255 位码长）
- 分块（block）流式容器 `.chfc`：压缩端内存只与块大小相关，解压端逐位取码、
  每次只从输入流预取一个字节
- 明确处理边界情形：
  - **空输入**：一个空块，无码长表、无码流
  - **单符号**：约定码长 1、码字 `0`，载荷为 N 个 0 位
- 解码安全检查：
  - 码长表严格校验（范围、symbol 严格升序、结构自洽）
  - **过度订阅**（Kraft 和 > 1）：任何策略下一律拒绝
  - **不完整码表**（Kraft 和 < 1）：默认拒绝；`allow` 策略放行，
    位流走到未定义位串时返回 `undefined_codeword`
  - 截断头部、截断位流、码字中途结束、非零填充位、BFINAL 后尾随数据
  - 所有长度声明先校验再解码：单块输出上限与总输出上限，防止伪造长度导致巨量分配
- 统计中如实给出压缩率；**不保证所有输入变小**（高熵/小输入通常变大）

## 构建

```bash
cargo build --release
# 二进制：target/release/chfc
cargo test
```

## CLI 用法

```bash
# 文件压缩 / 解压
chfc compress   -i input.bin -o input.chfc [--block-size 1048576]
chfc decompress -i input.chfc -o output.bin \
                [--max-output 1073741824] [--max-block 16777216] \
                [--incomplete-policy reject|allow]

# JSON 控制入口：从文件或 stdin 读一条 JSON 请求，响应写到 stdout
chfc json examples/compress_request.json
echo '{"op":"compress","data":"aGVsbG8="}' | chfc json
```

压缩/解压统计打印到 **stderr**，stdout 只输出 JSON 响应。

## JSON 控制入口

请求：

```json
{
  "op": "compress",
  "data": "<base64 或 hex 字符串>",
  "encoding": "base64",
  "block_size": 1048576
}
```

解压请求额外支持：

```json
{
  "op": "decompress",
  "encoding": "base64",
  "data": "...",
  "max_output_bytes": 1073741824,
  "max_block_bytes": 16777216,
  "incomplete_policy": "reject"
}
```

- `encoding`：`base64`（默认，标准字母表带填充）或 `hex`
- `block_size`：压缩块大小，1..=16,777,216，默认 1,048,576（1 MiB）
- `max_block_bytes`：单块声明原始长度上限，默认 16 MiB
- `max_output_bytes`：解码输出总长度上限，默认 1 GiB
- `incomplete_policy`：`reject`（默认）或 `allow`

成功响应：

```json
{
  "ok": true,
  "op": "compress",
  "result": {
    "encoding": "base64",
    "data": "Q0hGAQ...",
    "stats": {
      "input_bytes": 91,
      "output_bytes": 60,
      "blocks": 1,
      "distinct_symbols_total": 28,
      "ratio": 0.659341
    }
  }
}
```

失败响应（HTTP 无关的纯信封）：

```json
{"ok": false, "error": {"kind": "oversubscribed", "message": "oversubscribed code: Kraft sum exceeds 1"}}
```

错误 `kind` 包括：`bad_magic`、`unsupported_version`、`truncated_header`、
`truncated_stream`、`reserved_bits_set`、`invalid_length_table`、
`oversubscribed`、`incomplete_code`、`unexpected_end_of_code`、
`undefined_codeword`、`non_zero_padding`、`trailing_data`、`length_mismatch`、
`limit_exceeded`、`output_too_long`、`bad_request`、`io_error` 等。

## `.chfc` 容器格式（版本 1）

所有整数字段大端；头部字段字节对齐，块内码流为 MSB-first 位流。

```text
文件前奏（6 字节）
  0  4  magic   = b"CHFC"
  4  1  version = 0x01
  5  1  global_flags = 0x00（保留，必须为 0）

重复若干块，最后一块 BFINAL=1：
  块头（19 字节，字节对齐）
    0   1  block_flags：bit0=BFINAL，bit1=SINGLE，其余保留为 0
    1   8  original_len：本块原始字节数 u64
    9   8  payload_bits：本块码流有效位数 u64
    17  2  num_symbols：码长表条目数 u16（0..=256）
  码长表：num_symbols 个 (symbol:u8, length:u8)，symbol 严格升序
  码流：ceil(payload_bits/8) 字节，末字节低位用 0 填充（填充位必须为 0）
```

特殊情形与不变量：

- **空输入**：恰好一块，BFINAL=1，`num_symbols=0`，三个长度字段全 0。
- **单符号块**：SINGLE=1，`num_symbols=1`，表中 length 必须为 1，
  `payload_bits == original_len`；码流为 original_len 个 `0` 位。
- **多符号块**：SINGLE=0，`num_symbols>=2`；编码器输出的码长表总是 Kraft
  完备（`Σ 2^-length == 1`）。
- 解码端额外约束 `payload_bits <= original_len * 255`（多符号块）。
- BFINAL 块之后不允许有任何字节。

### Canonical Huffman 细节

1. 统计块内 256 符号频率。
2. 二叉最小堆建树，键为 `(weight, tie)`：叶子 `tie=symbol`（0..255），
   内部节点 `tie=256+创建序号`。键全序唯一，结果确定。
3. 树仅用于提取每符号码长（0 = 不出现）。
4. 码字完全重算：按 `(length, symbol)` 排序，第一个码为长度 L 个 0；
   每用掉一个码，码值 +1；长度增加时码值左移补 0。
   - 生成中码值在某长度溢出却仍有符号 → 过度订阅
   - 生成结束码值未恰好达到 `2^max_len` → 不完整
5. 码流按码字高位在前发射。

## 库 API

```rust
use canonical_huffman::stream::{compress_stream, decompress_stream, Limits, IncompletePolicy};

// 流式：任意 Read/Write，内存与块大小而非输入总长相关
compress_stream(&mut reader, &mut writer, 1 << 20)?;
decompress_stream(&mut reader, &mut writer, &Limits::default(), IncompletePolicy::Reject)?;

// 内存便捷封装
let blob = canonical_huffman::compress_bytes(input, 1 << 20)?;
let raw  = canonical_huffman::decompress_bytes(&blob, &Limits::default(), IncompletePolicy::Reject)?;
```

## 关于压缩率

Huffman 只利用单字节符号频率：高熵数据（随机字节、已压缩数据）不会变小，
且每个块固定有 25 字节头（前奏 6 + 块头 19）加至多 512 字节码长表开销。
本项目如实报告 `ratio = output_bytes / input_bytes`（空输入为 0），
**不承诺输出一定短于输入**。文本等低熵数据通常能明显压缩。

## 项目结构

```text
src/
  error.rs     错误类型
  bits.rs      MSB-first 位流读写
  huffman.rs   确定性建树 / 码长 / 规范码 / 解码前缀树
  format.rs    .chfc 容器
  stream.rs    有界流式编解码
  jsonapi.rs   JSON 控制入口（自带 JSON/Base64/Hex）
  bin/chfc.rs  CLI
tests/         集成测试（随机往返、截断、伪造长度、等频率、限制、JSON）
examples/      JSON 请求样例
```

## 实测记录

见 [RUNLOG.md](RUNLOG.md)（包含实际执行的命令、结果与未通过项的如实记录）。
