# bitpack — 位打包整数块流式编解码库

纯后端 Rust 项目：把 `i64` 序列切分为定长块，每块存储 **base 基准值 + 位宽为 w 的 zigzag 差值位打包**，装不下位宽的极端值通过**逃逸异常表**直存；流尾带**索引区**，可不扫描前序块直接定位并解码任意单块。全程**小端字节序**，格式完整规范见 [FORMAT.md](FORMAT.md)。

## 特性

- 位宽 0..=64（0 表示块内差值全为 0，64 为满宽），LSB-first 位打包
- 有符号差值：zigzag 编码，128 位中间运算，`i64::MIN/MAX` 不溢出
- 逃逸异常值：超出位宽的值进入 `(下标, 原值)` 异常表，极端点不抬高整体位宽
- 流式容器：32 字节定长头 + 块区 + 索引区，支持单块随机访问
- 安全解码：位宽越界、计数异常、长度不符全部报错；所有限制在分配前检查，坏头不会导致移位溢出或越界分配
- JSON 控制入口 `bitpack-ctl`：encode / decode / decode_block / inspect

## 构建与测试

```bash
cargo build
cargo test          # 单元测试 + 集成测试（往返 / 坏头鲁棒性 / CLI 端到端）
cargo build --release
```

## JSON 控制入口

`bitpack-ctl` 从文件参数或标准输入读取一个 JSON 请求，向标准输出写一个 JSON 响应。成功时退出码 0 且响应含 `"ok": true`；失败时退出码 1 且响应为 `{"ok": false, "error": "..."}`。

### encode

```bash
echo '{"op":"encode","values":[5,3,-7,100,3,5],"block_size":128}' | ./target/release/bitpack-ctl
```

```json
{"data":"QlBCMQEAAIAAAA...","ok":true,"stats":{"block_count":1,"block_size":128,"byte_len":79,"total_values":6}}
```

`values` 为 `i64` 数组；`block_size` 可选（默认 128，范围 1..=2^20）。`data` 为二进制流的 base64。

### decode

```bash
echo '{"op":"decode","data":"<base64>"}' | ./target/release/bitpack-ctl
# => {"ok":true,"values":[5,3,-7,100,3,5]}
```

可选 `"limits"` 覆盖默认资源限制（解码前强制检查）：

```json
{"op":"decode","data":"...","limits":{"max_input_bytes":1048576,"max_total_values":100000,"max_block_size":4096,"max_output_values":100000}}
```

默认值：`max_input_bytes` 64 MiB，`max_total_values` / `max_output_values` 2^22，`max_block_size` 2^16。

### decode_block（索引定位单块）

```bash
echo '{"op":"decode_block","data":"<base64>","block":3}' | ./target/release/bitpack-ctl
# => {"block":3,"ok":true,"values":[...]}
```

### inspect（只解析头与索引）

```bash
echo '{"op":"inspect","data":"<base64>"}' | ./target/release/bitpack-ctl
# => {"header":{"block_count":1,"block_size":128,"index_offset":79,"total_values":6},"index":[{"block_offset":32,"first_value_index":0}],"ok":true}
```

更多可直接使用的请求文件见 [examples/requests/](examples/requests/)。

## 库用法

```rust
use bitpack::{encode_stream, decode_stream, decode_block_at, inspect, Limits};

let data = encode_stream(&[5, 3, -7, 100, 3, 5], 128)?;
let values = decode_stream(&data, &Limits::default())?;
let block0 = decode_block_at(&data, 0, &Limits::default())?;
let info = inspect(&data, &Limits::default())?;
```

## 项目结构

```
src/
  lib.rs            库入口与公开 API
  error.rs          统一错误类型
  limits.rs         解码资源限制（分配前检查）
  bitio.rs          LSB-first 位读写器（核心算法，无第三方依赖）
  zigzag.rs         zigzag 有符号映射（128 位域）
  block.rs          块编解码：base + 位宽 + 逃逸异常表
  stream.rs         流头 / 索引 / 整流与单块解码
  bin/bitpack-ctl.rs JSON 控制入口
tests/
  roundtrip.rs      往返测试：位宽 0..=64、负值、极端异常点、单块定位
  corrupt.rs        坏头鲁棒性：截断、篡改、垃圾输入、限制强制
  cli.rs            JSON 入口端到端
FORMAT.md           字节级格式规范
TESTING.md          实测命令与结果记录
examples/requests/  请求样例（decode/inspect/decode_block 含真实可重放数据）
```

## 依赖说明

核心编解码算法（位打包、zigzag、块/流结构）全部自行实现，仅 JSON 序列化（`serde`/`serde_json`）与 base64（`base64` crate）使用成熟第三方库。
