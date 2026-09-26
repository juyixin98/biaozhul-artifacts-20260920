# cdict — 列式字典编码（Columnar Dictionary Coding）

纯后端 Rust 库 + CLI：对**字符串列**做流式字典编码（dictionary encoding），
支持**分段（segmented）字典**、跨段 **ID 重映射合并**、NULL 独立表示，
二进制格式有完整文档，内存与解码输出长度均受显式限制。

* **零第三方依赖**（std-only）：varint、字典编码、分段合并、JSON 控制面
  全部在 crate 内自行实现；断网环境也可构建。
* 格式规范：[`docs/FORMAT.md`](docs/FORMAT.md)。
* 无前端、无网络服务，仅库、命令行与自动化测试。

## 1. 它做什么

字典编码把一列字符串中的去重值收集为字典，每行只存字典 id：

```text
行:   "apple"  NULL  "banana"  "apple"  ""
字典: ["apple", "banana", ""]            # 首次出现顺序
id:      1      0       2        1      3
```

要点：

* **NULL 独立表示**：id `0` 永远是 NULL，空串 `""` 是普通字典值。
  全 NULL 段的字典为空。
* **分段**：按行数切段，每段独立字典，内存峰值只与单段大小成正比。
* **分段合并**：多个段/流合并时构建全局字典，逐段建立
  `本地 id → 全局 id` 重映射表翻译每行，合并前后逐行值完全一致。
* **字典顺序不影响解码**：id 在同段字典命名空间内解释，重排字典并同步
  改写 id 不改变任何解码值。
* **大基数退化**：当每行都是新值时，编码退化为"N 字典条目 + N id"，
  仍然正确且受限制保护。

## 2. 构建

需要 Rust 稳定版工具链（开发于 1.96/1.98）：

```bash
cargo build --release
# 二进制: target/release/cdict
```

## 3. 快速上手（JSON 控制入口）

CLI 接收**一个 JSON 请求对象**（来自文件或 stdin），数据文件路径在请求
内部给出，返回一个 JSON 响应对象。

```bash
mkdir -p out
cargo run --release -- examples/encode.request.json
# {"ok":true,"op":"encode","rows":8,"segments":3}

cargo run --release -- examples/decode.request.json
cat out/rows.decoded.json
# ["apple","banana",null,"apple","","樱桃",null,"banana"]

cargo run --release -- examples/encode2.request.json
cargo run --release -- examples/merge.request.json
# {"ok":true,"op":"merge","rows":12,"input_streams":2,"input_segments":5,
#  "output_segments":3,"global_dict_entries":4}

cargo run --release -- examples/inspect.request.json
# {"ok":true,"op":"inspect","rows":12,"segments":3,"null_rows":3,
#  "string_bytes":33,"distinct_values":4}
```

数据走 stdin/stdout（用 `"-"`）时，JSON 响应自动转到 stderr，避免与数据
互相污染。**注意 stdin 只有一个**：CLI 参数为 `-` 时表示**请求本身**来自
stdin，因此若数据也要用 `input:"-"` 走 stdin，请求就必须来自文件参数
（两者不能同时占用 stdin）。

```bash
# 请求来自文件参数，行数据经 stdin 传入，二进制写到 stdout（响应转 stderr）
printf '{"op":"encode","input":"-","output":"-"}' > /tmp/enc.json
echo '["x", null, "y"]' | cdict /tmp/enc.json > out/s.cdc 2>out/enc.json.resp
cat out/enc.json.resp        # {"ok":true,...}

# 二进制文件 -> JSON 行到 stdout（请求也可直接走 stdin，因为输入是文件）
cdict - <<<'{"op":"decode","input":"out/s.cdc","output":"-"}'
# ["x",null,"y"]
```

### 请求字段

| 字段 | 适用 op | 说明 |
|---|---|---|
| `op` | 全部 | `encode` / `decode` / `merge` / `inspect` |
| `input` | encode/decode/inspect | 行 JSON 文件或 `.cdc` 文件；`"-"` = stdin |
| `inputs` | merge | 输入 `.cdc` 路径数组 |
| `output` | encode/decode/merge | 输出路径；`"-"` = stdout |
| `segment_rows` | encode/merge | 每段行数；`0`/缺省 = 单段 |
| `limits` | 全部 | 可选限制覆盖，字段见下 |

* encode 的输入 / decode 的输出是 JSON 数组，元素为 `string` 或 `null`。
* 成功响应：`{"ok": true, "op": ..., ...统计字段}`。
* 失败响应：`{"ok": false, "error": "..."}`，进程退出码 1。

`limits` 可覆盖字段（均为非负整数）：
`max_dict_bytes`、`max_dict_entries`、`max_rows`、`max_string_bytes`、
`max_decoded_string_bytes`、`max_segments`。缺省值见
[`docs/FORMAT.md`](docs/FORMAT.md) §5。

更多请求样例见 [`examples/`](examples/)。

## 4. 作为库使用

```rust
use cdict::{encode_to_vec, decode_to_vec, merge_to_vec, Limits};

let rows = vec![Some("a"), None, Some("b"), Some("a")];
let limits = Limits::default();

// 每 2 行一段
let bytes = cdict::encode_to_vec(&rows, &limits, 2)?;
let (decoded, stats) = decode_to_vec(&bytes[..], &limits)?;
assert_eq!(decoded.len(), 4);

// 合并多个独立编码的流（自动跨段重映射 id）
let other = encode_to_vec(&[Some("c"), Some("a")], &limits, 1)?;
let (merged, mstats) = merge_to_vec(&[&bytes[..], &other[..]], &limits, 0)?;
```

需要真正逐行流式（不物化整列）时使用 `Encoder` / `Decoder` /
`decode_for_each` / `Merger`：

```rust
use cdict::{Encoder, decode_for_each, Limits};
use std::io::sink;

let mut enc = Encoder::new(sink(), Limits::default(), 100_000);
enc.push_row(Some("streamed"))?;
enc.push_row(None)?;
enc.finish()?;
```

## 5. 测试

```bash
cargo test                # 全部单元 + 集成测试
cargo test --release      # 含大基数退化等较大用例，release 更快
```

验收相关用例（`tests/end_to_end.rs`）：

* 空列、空串、全 NULL、空串与 NULL 混合；
* Unicode（2/3/4 字节 UTF-8、emoji、代理对经由 JSON 层）；
* 大基数（5000 个全异值）多段编码退化后逐行一致；
* 多种段大小（含 1 行/段）下合并前后逐行值完全一致；
* 字典插入顺序相反的两流解码值各自正确（顺序不影响解码）；
* 合并后相同值获得相同全局 id；
* 截断、魔数错误、悬空 id、各类资源限制被拒绝。

## 6. 目录结构

```text
Cargo.toml
src/
  lib.rs            公共 API
  error.rs          Error 与 Limits
  varint.rs         LEB128（含流式读写）
  segment.rs        CDCT 分帧/段头/字典/id 读写与校验
  encoder.rs        流式分段字典编码器
  decoder.rs        流式解码器（逐条/回调/物化）
  merge.rs          全局字典构建与跨段 id 重映射合并
  json.rs           自研 JSON 解析/生成
  control.rs        JSON 控制面（encode/decode/merge/inspect）
  bin/cdict.rs      CLI 入口
tests/              端到端与控制面集成测试
docs/FORMAT.md      二进制格式权威规范
examples/           请求与行数据样例
```

## 7. 许可

MIT
