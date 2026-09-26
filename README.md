# canohuff — 规范霍夫曼编码（Canonical Huffman）纯后端编解码

Rust 实现的流式二进制编解码库 + JSON 控制入口 CLI。**零外部依赖**，核心算法
（建树、长度限制、规范码分配、位流读写）全部自行实现，无任何前端。

## 功能特性

- 规范（canonical）霍夫曼压缩：码表只传输**码长表**，解码端按
  `(码长, 符号)` 排序重建完全相同的码字
- **确定性建树**：堆排序键为 `(频率, 子树最小符号)`，构成全序，等频率输入
  永远产生同一棵树、同一份输出字节
- 码长上限 32 bit；自然霍夫曼树超限时自动对频率右移缩放重建（启发式限长，
  保证收敛，见下文）
- 流式处理：编解码内存占用与输入大小无关（64 KiB I/O 缓冲 + 256 项码表）
- 边界情形：空输入、单符号输入（分配 1 bit 码字）均可往返
- 解码端严格校验：过度订阅（Kraft 和 > 1）一律拒绝；不完整码表
  （Kraft 和 < 1）按策略处理；截断位流、伪造声明长度、尾随垃圾均报错
- 资源限制：可配置输入上限与解码输出上限（默认各 256 MiB）
- 输出压缩率（ratio = 输出字节 / 输入字节）；**不保证所有输入都变小**，
  不可压缩输入会膨胀（码表头开销 + 约 8 bit/字节的码字）

## 流格式 `CHF1`

所有整数小端；位流 MSB-first；最后一个字节不足 8 bit 时低位补零。

```
偏移    大小      字段
0       4         magic：ASCII "CHF1"
4       8         original_len: u64，解压后字节数
12      2         symbol_count: u16，码表条目数 K
14      2*K       码表条目：(symbol: u8, code_len: u8)，按 symbol 升序
14+2K   ...       规范码位流
```

- 头部最大 14 + 512 = 526 字节。
- 规范码重建规则：符号按 `(code_len, symbol)` 升序排序，从 0 开始按长度
  类别依次连续分配码字（与 DEFLATE 相同的 canonical 约定）。
- 空输入：`original_len = 0, K = 0`，无位流。
- 单符号：该符号 `code_len = 1`（码字 `0`），每符号 1 bit。

## 解码端校验策略

| 情况 | 行为 |
|------|------|
| 过度订阅（Kraft 和 > 1） | 一律拒绝：`Oversubscribed` |
| 不完整码表（Kraft 和 < 1） | 由 `incomplete_policy` 决定：`permit`（默认，遇到未定义位序列时报 `UndefinedCode`）或 `reject`（直接报 `IncompleteTable`） |
| 码长为 0 或 > 32 | 拒绝：`InvalidCodeLength` |
| 码表符号重复 | 拒绝：`DuplicateSymbol` |
| 声明长度 > 输出上限 | 解码前拒绝：`OutputTooLarge` |
| 位流/头部截断 | 拒绝：`Truncated` |
| 尾部非零填充位或多余字节 | 拒绝：`TrailingData` |
| 声明有数据但码表为空 | 拒绝：`EmptyTableWithData` |

## 构建与测试

```bash
cargo build --release   # 生成 target/release/chf
cargo test              # 单元/集成/CLI 端到端测试
```

## JSON 控制入口

```
chf <request.json>      # 从文件读取请求
chf -                   # 从标准输入读取请求
```

请求字段：

| 字段 | 类型 | 必填 | 默认 | 说明 |
|------|------|------|------|------|
| `op` | string | 是 | — | `"compress"` 或 `"decompress"` |
| `input` | string | 是 | — | 输入文件路径 |
| `output` | string | 是 | — | 输出文件路径 |
| `max_input_bytes` | number | 否 | 268435456 | 压缩输入上限 |
| `max_output_bytes` | number | 否 | 268435456 | 解压输出上限 |
| `incomplete_policy` | string | 否 | `"permit"` | `"permit"` / `"reject"` |

响应（stdout，单行 JSON；成功退出码 0，失败退出码 1）：

```json
{"ok":true,"op":"compress","input_bytes":1048576,"output_bytes":1049130,"ratio":1.0005,"elapsed_ms":42}
{"ok":false,"op":"decompress","error":"truncated stream: unexpected end of input"}
```

`ratio` = 输出字节 / 输入字节（< 1 表示变小）；空输入时为 `null`。

### 请求样例

压缩（另见 `examples/requests/compress.json`）：

```json
{"op":"compress","input":"data.bin","output":"data.chf"}
```

解压，限制输出 64 MiB 并拒绝不完整码表（`examples/requests/decompress.json`）：

```json
{"op":"decompress","input":"data.chf","output":"restored.bin","max_output_bytes":67108864,"incomplete_policy":"reject"}
```

试用：

```bash
head -c 1048576 /dev/urandom > /tmp/rand.bin
./target/release/chf examples/requests/compress.json   # 修改路径后
```

## 项目结构

```
src/
  lib.rs      库入口与格式说明
  error.rs    统一错误类型
  table.rs    核心算法：确定性建树、限长、规范码分配与校验
  bitio.rs    MSB-first 位流读写
  encode.rs   流式编码器 + 头部写入
  decode.rs   流式解码器 + 头部校验 + 策略
  json.rs     极简 JSON 解析/转义（零依赖）
  bin/chf.rs  JSON 控制入口 CLI
tests/
  roundtrip.rs   往返测试（空/单符号/等频率/随机多尺寸/确定性）
  corruption.rs  截断、伪造码长、伪造长度、尾随垃圾、输出上限
  cli.rs         CLI 端到端（含 ratio 字段与失败路径）
examples/requests/  请求样例
```

## 设计说明与诚实声明

- **不保证压缩**：随机/已压缩数据通常会变大（码表头 + 熵编码极限），
  ratio 如实上报，测试只断言膨胀有界且可往返。
- 限长启发式（频率右移重建）不保证长度受限下的最优码，但保证：
  确定性、收敛、满足 32 bit 上限、前缀码合法。
- 解码是严格的：任何截断、伪造、尾随数据都会报错而不是悄悄产出
  错误数据。
