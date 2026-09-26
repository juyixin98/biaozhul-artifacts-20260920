# lzsw — LZ 滑动窗口编码（LZ77 子集）纯后端项目

流式二进制编解码库 + JSON 控制入口。零外部依赖：核心算法、JSON、
Base64 均为自行实现（仅使用 Rust 标准库）。

## 特性

- **LZ77 子集**：窗口与匹配长度受限（window ≤ 65535，匹配 3..=130，
  字面量段 ≤ 128），格式完整文档化（见 [docs/FORMAT.md](docs/FORMAT.md)）。
- **流式编解码**：`feed`/`finish` 接口，任意分块喂入；编码结果与分块
  方式无关（有测试保证）。
- **内存有界**：编码器 = 窗口 + 定长哈希表（约 384 KiB）；解码器 =
  窗口大小历史 + 单个 token，均与输入总长无关。
- **安全解码**：
  - 回距校验：`1 <= dist <= min(window, 已输出字节)`，引用不得越过
    已有输出；
  - 支持重叠复制（`len > dist` 逐字节拷贝）；
  - 输出预算 `max_output`（默认 16 MiB），超限即报错，防压缩炸弹；
  - 截断 token、非法头部一律拒绝。

## 构建与测试

```bash
cargo build
cargo test          # 单元测试 + 集成测试（往返/分块/非法输入/CLI）
cargo run -- examples/requests/compress.json
```

## 库用法

```rust
use lzsw::{Encoder, EncoderConfig, Decoder};

// 压缩（可分块喂入）
let mut enc = Encoder::new(EncoderConfig::default())?;
let mut compressed = Vec::new();
enc.feed(b"hello ", &mut compressed)?;
enc.feed(b"hello hello", &mut compressed)?;
enc.finish(&mut compressed)?;

// 解压（输出预算防炸弹）
let mut dec = Decoder::new(1 << 20);
let mut plain = Vec::new();
dec.feed(&compressed, &mut plain)?;
dec.finish()?;
```

也有一次性便捷函数 `lzsw::compress(data, window)` /
`lzsw::decompress(data, max_output)`。

## JSON 控制入口

二进制 `lzsw` 从文件（参数）或 stdin 读取一个 JSON 请求，向 stdout
输出一个 JSON 响应。退出码：成功 0，失败 1。

### 请求

| 字段           | 适用 op          | 必填 | 说明                                |
|----------------|------------------|------|-------------------------------------|
| `op`           | 两者             | 是   | `"compress"` / `"decompress"`       |
| `input_base64` | 两者             | 是   | 输入数据（Base64）                  |
| `window`       | compress         | 否   | 窗口大小，默认 4096，最大 65535     |
| `max_output`   | decompress       | 否   | 输出预算，默认 16777216（16 MiB）   |
| `chunk`        | 两者             | 否   | 按此块大小分块喂入（演练流式路径）  |

### 响应

```json
{"ok":true,"op":"compress","input_bytes":33,"output_bytes":45,"tokens":4,"output_base64":"..."}
{"ok":false,"op":"decompress","error_kind":"distance_before_output","error":"match distance 5 reaches before output start (only 3 bytes emitted)"}
```

`error_kind` 取值：`invalid_magic` / `unsupported_version` /
`invalid_header` / `distance_zero` / `distance_beyond_window` /
`distance_before_output` / `output_limit_exceeded` / `truncated` /
`json_error` / `base64_error` / `missing_field` / `invalid_field` /
`unknown_op` / `io_error`。

### 示例

```bash
# 压缩（请求样例见 examples/requests/）
cargo run -- examples/requests/compress.json
echo '{"op":"compress","input_base64":"aGVsbG8gaGVsbG8="}' | cargo run

# 解压（把上一条响应的 output_base64 填入请求）
cargo run -- examples/requests/decompress.json
```

## 项目结构

```text
src/format.rs    格式常量（魔数/版本/窗口与匹配长度上限）
src/encoder.rs   流式编码器（哈希链贪心匹配，内存有界）
src/decoder.rs   流式解码器（回距校验、重叠复制、输出预算）
src/json.rs      极简 JSON 解析/序列化（自实现）
src/base64.rs    Base64 编解码（自实现）
src/main.rs      JSON 控制入口（CLI）
docs/FORMAT.md   二进制格式规范
examples/requests/  请求样例
tests/           往返、分块边界、非法输入、CLI 端到端测试
```

## 验证记录（实际运行）

> 以下为本项目真实执行的命令与结果。测试最终全部通过；开发过程中
> 发现并已修复的缺陷如实列在末节。

### 环境

- rustc/cargo 1.98.1（stable，经 rustup 安装；官方源下载多次中断，
  改用 USTC 镜像 `mirrors.ustc.edu.cn/rust-static` 完成安装）
- 平台：Linux x86_64

### 命令与结果

```text
$ cargo build
    Finished `dev` profile [unoptimized + debuginfo] target(s)

$ cargo test
    test result: ok. 5 passed   (单元测试：json/base64)
    test result: ok. 3 passed   (tests/cli.rs       JSON 控制入口端到端)
    test result: ok. 17 passed  (tests/invalid.rs   非法回距/截断/预算/重叠复制)
    test result: ok. 6 passed   (tests/roundtrip.rs 重复字符/周期串/随机数据等)
    test result: ok. 3 passed   (tests/streaming.rs 分块边界)
    共 34 个测试，0 失败

$ cargo clippy --all-targets
    0 warnings（已逐一修复）

$ cargo fmt && git diff --exit-code   # 格式化后无残留差异
```

### 请求样例实测

```text
$ ./target/debug/lzsw examples/requests/compress.json
{"ok":true,"op":"compress","input_bytes":34,"output_bytes":33,"tokens":3,"output_base64":"TFpTVwEAABCCAAAABWhlbGxvII8GAAlsYXp5IHdvcmxk"}

$ ./target/debug/lzsw examples/requests/decompress.json
→ 解码回 "hello hello hello hello lazy world"（与原文一致）

$ ./target/debug/lzsw examples/requests/decompress_budget.json   # 1MB 'a'，预算 1024
exit code 1
{"ok":false,"op":"decompress","error_kind":"output_limit_exceeded","error":"decoded output exceeds budget of 1024 bytes"}
```

压缩率参考：1,000,000 字节重复字符 → 23,093 字节（约 2.3%）。

### 开发中发现并修复的缺陷（均有回归测试）

1. **头部长度不一致**：编码器写了 4 字节 reserved（共 14 字节），而
   规范与解码器为 12 字节，导致解码器把头部残余解析成 token、输出
   多出一个前导 NUL。修复为 2 字节 reserved，回归测试
   `encoder_header_matches_spec`。
2. **小窗口下 trim 越界**：`buf` 前端裁剪只考虑匹配历史窗口，未考虑
   尚未冲刷的字面量段，window=16 时 `lit_start - base` 下溢 panic。
   修复为裁剪点取 `min(pos - window, lit_start)`，回归测试
   `various_windows`（含 window=16）。
3. **编码结果依赖分块方式**：匹配覆盖区间的位置入哈希表时受
   “缓冲区末尾不足 3 字节” 保护影响，1 字节分块与整块喂入产生不同
   token 流。修复为统一在编码位置 p 前把 `<p` 的可哈希位置全部入表
   （`inserted_up_to` 不变量），回归测试
   `encoder_chunk_size_does_not_change_output`。

### 当前未通过项

无。
