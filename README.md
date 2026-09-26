# bpb — 位打包整数块流式编解码库

纯后端 Rust 项目：把 `i64` 序列编码为紧凑的二进制流，并提供 JSON 控制入口（CLI）。
核心算法（位打包、zigzag、base64）均为本仓库自行实现，外部依赖仅 `serde`/`serde_json`（JSON 解析）。

## 功能

- 流式二进制格式：文件头 + 整数块序列 + 末尾索引
- 每块：`base` 基准值 + 位宽（0..=64）的 zigzag 有符号差值位打包
- 逃逸表：装不下当前位宽的异常值单独存储，不拉宽整个块
- 末尾索引可定位任意单块，支持只解码一个块
- 全格式小端字节序，LSB-first 位序
- 解码端资源限制：块数、总值数、单块值数、输出值数均可限
- 坏输入安全：所有长度先校验后分配，不会移位溢出、越界分配或 panic

## 构建与测试

```sh
cargo build --release   # 生成 target/release/bpb
cargo test              # 42 个测试（单元 + 往返 + 腐坏输入 + JSON 端到端）
```

## JSON 控制入口

CLI 从文件参数或 stdin 读一个 JSON 请求，向 stdout 写一个 JSON 响应。
成功时退出码 0 且响应 `"ok": true`；失败时退出码 1 且响应 `"ok": false, "error": ...`。
二进制数据在 JSON 中以 base64 字符串承载。

### 请求

```json
{"op": "encode",  "values": [1, 2, 3], "block_size": 128}
{"op": "decode",  "data": "<base64>", "max_output": 1000}
{"op": "decode",  "data": "<base64>", "block_index": 2}
{"op": "inspect", "data": "<base64>"}
```

- `encode`：`values` 为 i64 数组；`block_size` 可选，默认 128，范围 1..=256。
  响应含 `data`（base64 流）与 `stats`（值数、块数、字节数）。
- `decode`：`max_output` 可选，限制输出值数（硬上限 2^26）；`block_index` 可选，
  给出时只通过索引解码该块。
- `inspect`：返回文件头、索引和每块元数据（base、位宽、逃逸数、块长），不解码值。

### 示例

```sh
./target/release/bpb examples/encode_request.json
./target/release/bpb examples/decode_request.json
./target/release/bpb examples/decode_block_request.json
./target/release/bpb examples/inspect_request.json
echo '{"op":"encode","values":[100,102,101]}' | ./target/release/bpb
```

`examples/` 下的 decode/inspect 样例由 encode 样例的实际输出生成。

## 二进制格式（版本 1）

所有多字节整数为**小端**。位流为 **LSB-first**：第一个写入的位落在字节 0 的 bit 0，
第九位落在字节 1 的 bit 0；一个 n 位值先存其最低位。

### 流布局

```
header | block 0 | block 1 | ... | block N-1 | index
```

### 文件头（28 字节）

| 偏移 | 大小 | 字段          | 说明                     |
|------|------|---------------|--------------------------|
| 0    | 4    | magic         | 固定 `"BPB1"`            |
| 4    | 1    | version       | 固定 1                   |
| 5    | 1    | flags         | 保留，必须为 0           |
| 6    | 2    | reserved      | 保留，必须为 0           |
| 8    | 4    | block_count   | 块数                     |
| 12   | 8    | total_values  | 全部块的值总数           |
| 20   | 8    | index_offset  | 索引区的绝对偏移         |

### 块

| 大小                | 字段          | 说明                                       |
|---------------------|---------------|--------------------------------------------|
| 4                   | count         | 值个数，1..=256                            |
| 8                   | base          | 基准值（i64），取块内第一个值              |
| 1                   | bit_width     | 位宽 w，0..=64                             |
| 4                   | escape_count  | 逃逸表项数，0..=count                      |
| ceil(count*w/8)     | packed        | 每个槽位 w 位的 zigzag 差值，LSB-first     |
| 12 * escape_count   | escapes       | 表项 (index u32, value i64)，按 index 递增 |

解码规则：`value[i] = base + zigzag_decode(slot[i])`；若 `i` 在逃逸表中，则取逃逸表
的原始值，packed 中对应槽位为占位内容（编码器写 0），解码时读取并丢弃。
**每个槽位（含逃逸槽位）都占 w 位**，保证槽位 i 的位偏移恒为 `i * w`。

差值与 zigzag：`delta = value - base` 按 i128 计算；落在 i64 范围内的做 zigzag
变换（`zigzag(x) = (x << 1) ^ (x >> 63)`，把有符号映射为无符号：0→0, -1→1, 1→2,
i64::MIN→2^64-1）。zigzag 值 >= 2^w 的槽位进入逃逸表；delta 超出 i64 范围的
（如 base=i64::MIN、value=i64::MAX）强制逃逸，原始值按 i64 存于逃逸表。

位宽 0：所有非逃逸槽位的差值都是 0，packed 区长度为 0。
位宽 64：zigzag 值占满 u64，无需截断。

编码器的位宽选择：枚举 w ∈ 0..=64，最小化代价 `count*w + escapes*96`（一个逃逸
表项 12 字节 = 96 位），代价相同取较小 w。

### 索引（位于 index_offset，block_count 项，每项 20 字节）

| 大小 | 字段         | 说明                       |
|------|--------------|----------------------------|
| 8    | offset       | 块的绝对偏移               |
| 8    | first_value  | 块内第一个值的全局序号     |
| 4    | count        | 块内值数（与块头一致）     |

索引项按块顺序排列，`first_value` 为前缀和，总和必须等于 `total_values`。
索引区之后允许有尾部字节（解码器忽略）。

## 资源限制与坏输入防护

解码端 `Limits`（默认值）：

| 限制                | 默认值      |
|---------------------|-------------|
| max_blocks          | 2^20        |
| max_total_values    | 2^24        |
| max_block_values    | 256（硬上限）|
| max_output_values   | 2^24        |

防护措施：

- 所有声明长度（块数、值数、索引偏移、逃逸数）先与限制和输入实际长度校验，
  通过后才分配内存；分配上限为 `max_output_values`。
- 位宽 > 64 在读任何位之前被拒绝；位读写器使用 128 位累加器，
  读写满 64 位也不会出现 ≥64 的移位。
- 逃逸表索引必须 < count 且严格递增；`base + delta` 溢出 i64 报错。
- 索引偏移、块偏移做 checked 算术，越界即 `Truncated`/`InvalidData`。
- JSON 入口请求体上限 512 MiB；`max_output` 有 2^26 硬上限。

## 项目结构

```
src/
  lib.rs      crate 入口、zigzag 变换
  format.rs   格式常量与布局文档
  bitio.rs    LSB-first 位读写器（128 位累加器）
  encode.rs   编码器（位宽代价模型、逃逸选择）
  decode.rs   解码器、校验、inspect、单块解码
  base64.rs   base64 编解码（自实现）
  json_io.rs  JSON 请求分发
  main.rs     CLI 入口
tests/
  roundtrip.rs  往返测试（位宽 0/64、负值、极端逃逸、多块索引）
  corrupt.rs    腐坏输入测试（坏头、截断、越界、逐字节翻转）
  json_api.rs   CLI 端到端测试
examples/       请求样例
```

## 验证记录（实际运行）

环境：Linux x86_64，rustc/cargo 1.98.1（官方 tarball 安装于 `~/rust-local`，
因本机 rustup 1.29.1 安装工具链反复失败而改用 tarball 直装）。

| 命令 | 结果 |
|------|------|
| `cargo build` / `cargo build --release` | 通过 |
| `cargo test` | **42 passed, 0 failed**（lib 7 + corrupt 15 + json_api 8 + roundtrip 12）|
| `cargo clippy --all-targets` | 0 警告（修复 6 处风格提示后）|
| `./target/release/bpb examples/encode_request.json` | ok，10 值 → 3 块 169 字节 |
| `./target/release/bpb examples/decode_request.json` | ok，值完全还原（含 i64::MIN/MAX 逃逸）|
| `./target/release/bpb examples/decode_block_request.json` | ok，经索引只解码块 1 |
| `./target/release/bpb examples/inspect_request.json` | ok，头/索引/块元数据正确 |

开发中发现并修复的问题（均有对应测试覆盖）：

1. `HEADER_LEN` 常量误写为 24（实际布局 28 字节），导致编码器块偏移整体错 4 字节，
   合法流无法自解码。修复后往返测试通过。
2. 解码器最初不为逃逸槽位读取占位位，导致逃逸后的第一个非逃逸值错位
   （`i64::MIN+1` 被解成 `i64::MIN`）。修复：每个槽位（含逃逸槽位）都读取 w 位。
3. 测试自身的生成器 `2 * bound` 在 63 位档溢出 i64，改用 u64 计算。

未通过项：无（最终全部通过）。已知限制：本机 rustup 损坏导致 `cargo fmt`/`clippy`
需用独立 `CARGO_HOME` 运行（`CARGO_HOME=/tmp/cargo-home cargo clippy`），与代码无关。
