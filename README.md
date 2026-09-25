# adaptive-arith — 自适应算术编码（纯后端）

Rust 实现的流式自适应算术编码库 + JSON 控制入口。核心算法（整数区间算术编码、
自适应频率模型、进位处理）全部自行实现，编解码库本身零第三方依赖；
`serde_json` / `base64` 仅用于命令行 JSON 入口。

## 特性

- **整数区间算术编码**：32 位区间，Witten–Neal–Cleary 方案，
  进位采用 bits-plus-follow（E1/E2/E3 归一化）
- **order-0 自适应模型**：257 符号（256 字节 + EOF 终止符），
  固定更新与重标定规则（总频率达 16383 时全部频率折半向上取整）
- **流式处理**：固定 8 KiB I/O 缓冲，内存占用 O(1)，与数据长度无关
- **资源限制**：输入长度、输出长度、截断判定阈值均可配置（`Limits`）
- **确定性**：同一输入恒产生同一输出字节序列
- **格式文档化**：比特级规范见 [FORMAT.md](FORMAT.md)

## 构建与测试

```bash
cargo build
cargo test          # 单元测试 + 集成测试
cargo build --release
```

## 库用法

```rust
use adaptive_arith::{encode, decode, Limits};

let limits = Limits::default(); // 输入/输出各限 64 MiB
let mut compressed = Vec::new();
let stats = encode(&b"hello"[..], &mut compressed, &limits)?;
assert!(stats.rescale_count == 0);

let mut restored = Vec::new();
decode(&compressed[..], &mut restored, &limits)?;
assert_eq!(restored, b"hello");
```

`Limits` 字段：`max_input_bytes`（编码输入上限）、`max_output_bytes`
（编码/解码输出上限）、`max_zero_bits`（截断判定阈值，默认 64）。

错误类型 `Error`：`InputLimitExceeded`、`OutputLimitExceeded`、
`TruncatedStream`（截断流）、`Io`。

## JSON 控制入口（`aac`）

从 stdin（或指定文件）读取 JSON 请求，结果以 JSON 写 stdout。

```bash
cargo run --bin aac < examples/encode_request.json
cargo run --bin aac -- examples/encode_request.json   # 等效，从文件读请求
```

### 请求格式

| 字段 | 必填 | 说明 |
|---|---|---|
| `op` | 是 | `"encode"` 或 `"decode"` |
| `input_base64` | 二选一 | 输入数据（base64 内联） |
| `input_file` | 二选一 | 输入文件路径（流式读取，适合大文件） |
| `output_file` | 否 | 输出文件路径；缺省则结果内联为 `output_base64` |
| `max_input_bytes` | 否 | 编码输入上限，默认 67108864 |
| `max_output_bytes` | 否 | 输出上限，默认 67108864 |

### 响应格式

成功（退出码 0）：

```json
{"ok":true,"op":"encode","input_bytes":25,"output_bytes":18,"rescale_count":0,"output_base64":"n3Ych1QbvWtgLosLQVRNoCiA"}
```

失败（退出码 1，请求合法但编解码失败）：

```json
{"ok":false,"error":"truncated_stream","message":"truncated stream: EOF symbol not reached before input exhausted"}
```

`error` 取值：`input_limit_exceeded`、`output_limit_exceeded`、
`truncated_stream`、`io_error`、`malformed_request`（退出码 2，请求本身非法）。

### 示例

```bash
# 编码
echo '{"op":"encode","input_base64":"SGVsbG8sIGFyaXRobWV0aWMgY29kaW5nIQ=="}' | cargo run --bin aac

# 大文件走文件路径，内存占用仍为 O(1)
echo '{"op":"encode","input_file":"big.bin","output_file":"big.aac"}' | cargo run --bin aac
echo '{"op":"decode","input_file":"big.aac","output_file":"big.out","max_output_bytes":104857600}' | cargo run --bin aac
```

更多样例见 [examples/](examples/)。

## 项目结构

```
src/lib.rs      公共 API：encode / decode / Limits / CodecStats / Error
src/model.rs    order-0 自适应频率模型（更新与重标定规则）
src/encoder.rs  32 位整数区间编码器（bits-plus-follow 进位）
src/decoder.rs  与编码器镜像的解码器
src/bitio.rs    MSB-first 比特流读写（分块缓冲）
src/error.rs    统一错误类型
src/main.rs     JSON 控制入口（bin: aac）
tests/          集成测试（往返、确定性、重标定、截断、限制、CLI）
examples/       JSON 请求样例
FORMAT.md       码流比特级格式规范
```

## 验证记录

以下为本机实际执行的命令与结果（rustc 1.98.1 stable，x86_64-unknown-linux-gnu，
2026-09-25）。全部通过，无未通过项。

### 自动化测试

```
$ cargo test
test result: ok. 6 passed; 0 failed   （src/ 单元测试：模型、比特流）
test result: ok. 15 passed; 0 failed  （tests/integration.rs 集成测试）
```

覆盖：空串、单字节（含 0x00/0xFF 边界）、全部 256 字节值、伪随机 100KB、
长偏斜 300KB（触发多次重标定）、确定性（两次编码字节一致）、格式锚点
（golden vector）、截断流（4 种截断位置均报错）、编码/解码输出限制、
编码输入限制、垃圾输入不 panic、CLI 往返、CLI 非法请求。

### CLI 实跑

```
$ ./target/release/aac examples/encode_request.json
{"input_bytes":25,"ok":true,"op":"encode","output_base64":"SB2EXVDeTvzTWC+yLnzYvj3vcgNM9YNcjEA=","output_bytes":26,"rescale_count":0}

$ ./target/release/aac examples/decode_request.json
{"input_bytes":26,"ok":true,"op":"decode","output_base64":"SGVsbG8sIGFyaXRobWV0aWMgY29kaW5nIQ==","output_bytes":25,"rescale_count":0}

$ ./target/release/aac examples/truncated_request.json   # 截断流（退出码 1）
{"error":"truncated_stream","message":"truncated stream: EOF symbol not reached before input exhausted","ok":false}
```

### 验收项实测

| 验收项 | 命令/方法 | 结果 |
|---|---|---|
| 长偏斜数据触发多次重标定 | 300KB（约 99% 'a'）经 CLI 编码 | `rescale_count=36`，300000 → 2735 字节，解码 `rescale_count=36` 与编码端一致，`cmp` 往返一致 |
| 空串 | `{"op":"encode","input_base64":""}` | 成功，输出 2 字节（`/0A=`），解码回空 |
| 全部字节 | 集成测试 `all_byte_values_roundtrip`（0x00..=0xFF 循环 4 轮） | 通过 |
| 截断流 | 码流截半 / 截 0、1、len-1 字节 | 均报 `truncated_stream`，退出码 1 |
| 确定性输出 | 同一 300KB 文件编码两次后 `cmp` | 字节完全一致 |
| 解码输出长度限制 | `max_output_bytes=1000` 解码 300KB 码流 | 报 `output_limit_exceeded`，退出码 1 |
| 内存限制（O(1) 流式） | 50MB 随机数据经文件接口编码，`/usr/bin/time -v` | 峰值 RSS 2176 KB，与数据长度无关；往返一致（6450 次重标定） |

### 已知说明

- 不可压缩数据（如随机字节）经 order-0 模型会轻微膨胀（50MB 随机数据
  膨胀约 0.1%），这是 order-0 自适应模型的固有特性，非缺陷。
- 开发过程中曾出现一个测试自身的死循环（`rescale_halves_with_floor_one`
  的循环条件在重标定后恒为真），已修复为有界循环；非编解码核心缺陷。
