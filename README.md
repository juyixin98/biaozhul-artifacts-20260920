# cdc — 列式字符串字典编码（Columnar Dictionary Coding）

纯后端 Rust 库 + 命令行。对**字符串列**做流式字典（dictionary）编解码：
段内相同字符串只存一份，行里存字典 ID；支持**分段字典合并**与**跨段 ID 重映射**；
**NULL 独立表示**（不进字典），空字符串 `""` 是正常字典值，二者严格区分。

- 纯 Rust，**零第三方依赖**（JSON、base64、CRC-32、LEB128、哈希字典全部自行实现）；
- **流式**：编码/解码任意时刻只保留至多一个段，内存有界；
- 二进制格式**完全文档化**（[`docs/FORMAT.md`](docs/FORMAT.md)），每段带 CRC-32；
- 明确的**内存与解码输出长度限制**，超限即报错而非继续分配；
- JSON 控制入口（[`docs/API.md`](docs/API.md)），二进制以 base64 承载。

## 何时有用 / 退化

- 低基数列（大量重复值）压缩明显；
- **高基数（近乎每行一个新值）时字典编码会“退化”**为近似直接存储——本实现保证
  这种情形依然正确、有界，并有测试覆盖（见下“大基数退化”）。

## 构建与测试

```bash
cargo build --release
cargo test
cargo clippy --all-targets -- -D warnings   # 若安装了 clippy
```

## 快速使用

```bash
# 编码（行数组；null = NULL，"" = 空串）
echo '{"op":"encode","rows":["alpha",null,"","alpha"]}' \
  | cargo run --release -- --pretty

# 显式分段编码
cargo run --release -- --pretty --request-file examples/encode_segments.json

# 端到端演示：编码 → 解码 → 分段合并 → inspect
bash examples/demo.sh
```

### 作为库

```toml
[dependencies]
cdc = { path = "." }
```

```rust
use std::io::{BufReader, Cursor};
use cdc::writer::{FileWriter, EncodeLimits};
use cdc::reader::{FileReader, DecodeLimits};
use cdc::merge::{merge_sources, MergeOptions};

// 逐行流式编码
let mut w = FileWriter::new(Vec::new(), EncodeLimits::default())?;
w.write_row(Some("a"))?; // 普通值 / 空串
w.write_row(None)?;      // NULL
let bytes = w.finish()?;

// 逐行流式解码（受 DecodeLimits 约束）
let mut r = FileReader::new(BufReader::new(Cursor::new(&bytes)),
                            DecodeLimits::default())?;
while let Some(row) = r.next_row()? {
    println!("{:?}", row.value); // Option<String>
}

// 多段合并 + 跨段 ID 重映射
// let segs: Vec<SegmentData> = ...;          // 来自一个或多个 .cdc 文件
// let (merged, stats) = merge_sources(&segs, &MergeOptions::default())?;
# Ok::<(), cdc::Error>(())
```

## 核心设计

### 段内编码

每个段自描述：

```
字典：distinct 字符串，ID 0..d-1（按首次插入顺序）
NULL：单独的、严格升序的行号列表，不占任何 ID
行：  非 NULL 行按行序（剔除 NULL）存字典 ID
```

因此 NULL 与空串天然分离；行 ID 用 LEB128 变长编码，小 ID 只需 1 字节。

### 分段与合并

- 写入时按行数 / 缓冲字节自动切段，也可显式制造段边界；
- **合并**把一个或多个文件中的若干段变成**单个段 + 一份全局去重字典**。
  映射锚点是**字节相等**：局部 ID → 字节 → 全局 ID。相同字节无论原局部 ID
  是什么，合并后都指向同一全局 ID；NULL 位置换算到全局行坐标；
- `canonical` 开关在“跨段首次出现顺序”与“UTF-8 字节序”之间选择全局字典编号。
  **字典顺序不影响解码结果**：逐行值在两种顺序下完全一致。

### 有界性

| | 限制 |
|--|--|
| 编码 | 单段缓冲字节、单段行数、单值长度、单段基数 |
| 解码 | 段帧大小（分配前判定）、累计行数、累计输出字节、段字典字节/基数、段数 |
| 合并 | 全局基数、全局字典字节、总行数 |

段长度字段在校验通过前不用于分配内存；畸形 varint、越界 ID、非升序 NULL、
非法 UTF-8、CRC 不匹配、尾部多余字节等都会被拒绝。

## 格式

见权威规范 [`docs/FORMAT.md`](docs/FORMAT.md)。要点：文件头 `CDC1` + 版本 +
标志；段帧 `kind | body_len(varint) | body | crc32-le`；段正文含段号、行数、
字典、NULL 位、行 ID。

## 项目结构

```
src/
  lib.rs        模块与常量
  varint.rs     LEB128
  crc32.rs      CRC-32（查表）
  table.rs      去重字典（FNV-1a + 线性探测，自己实现）
  writer.rs     流式分段编码器
  reader.rs     流式分段解码器 + 限制
  merge.rs      分段合并 / 跨段 ID 重映射
  json.rs       零依赖 JSON 解析/序列化
  base64.rs     base64
  control.rs    JSON 控制入口分发
  bin/cdc.rs    命令行（stdin 或 --request-file）
tests/          端到端 + JSON 接口集成测试
docs/           格式规范与 API 文档
examples/       请求样例与演示脚本
```

## 验收点对应

| 验收要求 | 覆盖位置 |
|----------|----------|
| 空串 | `tests/end_to_end.rs::empty_string_is_distinct_from_null` |
| 全 NULL | `all_null_column` |
| Unicode | `unicode_values_roundtrip` |
| 大基数退化 | `high_cardinality_degenerate_dict` |
| 分段合并、跨段重映射 | `merge_*` 系列 |
| 合并前后逐行值一致 | `merge_preserves_every_row_across_segments_and_files` |
| 字典顺序不影响结果 | 同一测试比较 first-seen 与 canonical 两种结果 |
| 限制内存/输出长度 | `*_limit_enforced`、`detects_crc_corruption` 等 |
