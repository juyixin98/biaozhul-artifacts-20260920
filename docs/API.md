# JSON 控制接口（v1）

`cdc` 可执行文件（或库函数 `cdc::control::handle`）接收一个 JSON 请求，
返回一个 JSON 响应。二进制 `.cdc` 数据统一用 **base64** 字符串承载。

- 成功：`{"ok": true, ...}`
- 失败：`{"ok": false, "error": {"code": "...", "message": "..."}}`
  （任何错误都不会导致进程 panic。）

调用方式：

```bash
# 从 stdin
echo '<request-json>' | cdc --pretty

# 从文件
cdc --pretty --request-file examples/encode.json
```

## op 一览

| op | 作用 | 关键入参 | 关键出参 |
|----|------|----------|----------|
| `encode` | 行数组 → `.cdc` | `rows` 或 `segments` | `data_b64`, `write_stats` |
| `decode` | `.cdc` → 行数组 | `data_b64` | `rows`, `segments`, `read_stats` |
| `merge` | 多文件多段 → 单段全局字典 | `inputs_b64[]` | `data_b64`, `merge_stats` |
| `inspect` | 只看结构不解值 | `data_b64` | `segments`, `read_stats` |

通用：`limits`（对象，各字段可选，缺省取库默认值）、`canonical`（布尔，仅 merge）。

---

## 1. encode

请求：

```json
{
  "op": "encode",
  "rows": ["alpha", null, "", "beta", "alpha"],
  "rows_per_segment": 1000000,
  "limits": {
    "max_segment_bytes": 67108864,
    "max_value_len": 16777216,
    "max_dict_cardinality": 10000000
  }
}
```

- `rows`：字符串或 `null` 数组。`null` = NULL，`""` = 空字符串，二者严格区分。
- 也可用 `segments`：`[["b","a"], [null,"c"]]`，每个子数组强制成为一个段
  （与 `rows` 二选一，不可同时给）。
- `rows_per_segment`：达到该行数自动切段（也可放进 limits）。

响应：

```json
{
  "ok": true,
  "data_b64": "Q0RDMQAB...",
  "magic": "CDC1",
  "write_stats": {
    "segments": 1, "rows": 5, "nulls": 1,
    "distinct_sum": 3, "bytes_written": 28
  }
}
```

## 2. decode

```json
{
  "op": "decode",
  "data_b64": "Q0RDMQAB...",
  "limits": {
    "max_segment_bytes": 268435456,
    "max_rows": 100000000,
    "max_value_bytes": 1073741824,
    "max_dict_bytes": 134217728,
    "max_cardinality": 20000000,
    "max_segments": 100000
  }
}
```

响应中的 `rows` 为字符串或 `null`；`segments` 给出每段
`segment_no / rows / nulls / cardinality`；`read_stats` 汇总段数、行数、
NULL 数、输出字节数。

## 3. merge

```json
{
  "op": "merge",
  "canonical": false,
  "inputs_b64": ["Q0RDMQ...file1", "Q0RDMQ...file2"],
  "limits": {
    "max_distinct": 20000000,
    "max_dict_bytes": 268435456,
    "max_rows": 100000000
  }
}
```

- 输入按数组顺序拼接；相同字节跨段映射到同一全局 ID；NULL 换算到全局行坐标。
- `canonical`：`false` 按跨段首次出现顺序编号；`true` 按 UTF-8 字节序编号。
  两种顺序解码出的逐行值完全一致。

响应：

```json
{
  "ok": true,
  "data_b64": "Q0RDMQAB...",
  "merge_stats": {
    "input_segments": 4,
    "rows": 12,
    "nulls": 2,
    "global_distinct": 5,
    "local_distinct_sum": 9,
    "deduped": 4
  }
}
```

## 4. inspect

```json
{ "op": "inspect", "data_b64": "Q0RDMQAB..." }
```

只返回文件头与每段结构信息，不还原行值，适合在不知道内容时先探结构。

## 错误码

| code | 含义 |
|------|------|
| `bad_json` / `bad_request` / `bad_base64` | 输入不合法 |
| `corrupt_or_unsupported_format` | 魔数/版本/标志/结构非法 |
| `truncated_input` | 字节被截断 |
| `crc_mismatch` | 段 CRC-32 校验失败 |
| `dictionary_error` | 字典 ID 越界 / 基数非法 |
| `limit_exceeded` | 触发任一内存、行数或输出长度限制 |
| `io_error` | 底层 I/O 错误 |

## 库 API（非 JSON）

需要处理超大批量数据时，直接使用流式库 API，避免 JSON/base64 与一次性物化：

```rust
use cdc::writer::{FileWriter, EncodeLimits};
use cdc::reader::{FileReader, DecodeLimits};
use cdc::merge::{merge_sources, MergeOptions};

// 逐行编码（自动/显式切段，内存只保留一个段）
let mut w = FileWriter::new(Vec::new(), EncodeLimits::default())?;
w.write_row(Some("a"))?; // Some(...) 普通值 / 空串
w.write_row(None)?;      // NULL
let bytes = w.finish()?;

// 逐行解码（受 DecodeLimits 约束）
let mut r = FileReader::new(BufReader::new(Cursor::new(&bytes)), DecodeLimits::default())?;
while let Some(row) = r.next_row()? { /* row.value: Option<String> */ }
```
