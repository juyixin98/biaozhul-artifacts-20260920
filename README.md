# lzsw — LZ 滑动窗口编码（LZ77 子集）

纯后端 Rust 项目：流式二进制编解码库 + JSON 控制入口。零外部依赖（JSON、
base64 与核心算法均自行实现）。

## 功能特性

- LZ77 子集：32 KiB 滑动窗口，匹配长度 3..=130，字面量游程 1..=128
- 流式 API：编码器/解码器均可分块喂入，内存占用有界（与输入大小无关）
- 解码器支持重叠复制（distance < length，RLE 语义）
- 安全约束：
  - 回距不得超过窗口大小（32768）
  - 回距不得越过已有输出起点（引用尚未产生的字节即报错）
  - 输出预算 `max_output`：解码产出超过预算立即报错，防止压缩炸弹
  - 截断的令牌/帧头在 `finish` 时报 `TruncatedToken`

## 构建与测试

```sh
cargo build
cargo test
cargo run --bin lzsw < examples/requests/encode.json
```

## 二进制格式（版本 1）

### 帧头（8 字节）

| 偏移 | 长度 | 内容 |
|------|------|------|
| 0    | 4    | magic：`"LZSW"` |
| 4    | 1    | 版本号 = 1 |
| 5    | 1    | flags，必须为 0 |
| 6    | 1    | window_log2 = 15（窗口 32768 字节） |
| 7    | 1    | 保留，必须为 0 |

### 令牌流（直至输入结束）

每个令牌以 1 字节 tag 开头：

- **tag < 0x80（字面量游程）**：后跟 `tag + 1` 个原始字节（1..=128）。
- **tag >= 0x80（匹配）**：长度 = `(tag & 0x7F) + 3`（3..=130），
  后跟 2 字节大端回距 distance（1..=32768）。语义：从已解码输出的
  倒数第 distance 个字节起复制 length 个字节；distance < length 时
  逐字节复制，产生重叠重复（RLE）。

流结束即输入结束；最后一个令牌必须完整，否则为 `TruncatedToken`。

### 解码器校验规则

| 规则 | 错误 |
|------|------|
| magic 不符 | `InvalidMagic` |
| 版本不支持 | `UnsupportedVersion` |
| flags/保留字节非零 | `InvalidHeader` |
| window_log2 ≠ 15 | `UnsupportedWindow` |
| distance > 32768 | `DistanceExceedsWindow` |
| distance > 已产出字节数 | `DistanceTooLarge` |
| 产出将超出 max_output | `OutputBudgetExceeded` |
| 输入在令牌/帧头中间结束 | `TruncatedToken` |

## 库 API

```rust
use lzsw::{Encoder, Decoder, compress, decompress};

// 一次性
let compressed = compress(b"hello hello hello");
let back = decompress(&compressed, 1 << 20).unwrap();

// 流式
let mut enc = Encoder::new();
let mut out = enc.update(b"chunk 1");
out.extend_from_slice(&enc.update(b"chunk 2"));
out.extend_from_slice(&enc.finish());

let mut dec = Decoder::new(64 * 1024 * 1024); // 输出预算
let mut plain = dec.update(&out)?;
dec.finish()?;
```

内存界限：编码器每次 `update` 返回后内部缓冲 ≤ 窗口 + 最大匹配长度
（约 32 KiB）加上本次喂入的分块；解码器只保留最近 32 KiB 历史与未
解析的输入尾部。注意：JSON 控制入口本身会把整个请求/响应（base64）
读入内存，内存界限保证针对的是编解码库。

## JSON 控制入口

`lzsw` 二进制从 stdin 读一个 JSON 请求，向 stdout 写一个 JSON 响应。

请求字段：

| 字段 | 必填 | 说明 |
|------|------|------|
| `op` | 是 | `"encode"` 或 `"decode"` |
| `data` | 是 | base64 编码的输入字节 |
| `max_output` | 否 | 解码输出预算（字节），默认 64 MiB |
| `chunk_size` | 否 | 按该大小分块喂入编解码器（演练流式路径） |

成功响应：

```json
{"ok":true,"op":"encode","input_len":17,"output_len":15,"data":"TFpTVwEADwA..."}
```

失败响应（退出码 1）：

```json
{"ok":false,"error":{"kind":"DistanceTooLarge","message":"match distance 5 exceeds bytes produced so far (2)"}}
```

### 示例

```sh
# 编码（"hello hello hello" 的 base64 为 aGVsbG8gaGVsbG8gaGVsbG8=）
$ lzsw < examples/requests/encode.json
{"ok":true,"op":"encode","input_len":17,"output_len":18,"data":"TFpTVwEADwAFaGVsbG8giAAG"}

# 解码回去
$ lzsw < examples/requests/decode.json
{"ok":true,"op":"decode","input_len":18,"output_len":17,"data":"aGVsbG8gaGVsbG8gaGVsbG8="}

# 预算不足的解码被拒绝
$ lzsw < examples/requests/decode_budget.json
{"ok":false,"error":{"kind":"OutputBudgetExceeded","message":"output budget 5 exceeded (attempted 6)"}}
```

更多样例见 `examples/requests/`。

## 项目结构

```
src/
  lib.rs       库入口与重导出
  error.rs     错误类型（kind 字符串与 JSON 接口一致）
  format.rs    帧头/令牌常量与写出
  encoder.rs   流式编码器（3 字节键哈希表，内存有界）
  decoder.rs   流式解码器（重叠复制、回距校验、输出预算）
  json.rs      最小 JSON 解析/序列化（自实现）
  base64.rs    base64 编解码（自实现）
  bin/lzsw.rs  JSON 控制入口
tests/
  roundtrip.rs 重复字符 / 周期串 / 随机数据往返
  corrupt.rs   非法回距、截断令牌、坏帧头、输出预算
  chunks.rs    分块边界（编码/解码各种 chunk 大小）
examples/requests/  JSON 请求样例
```
