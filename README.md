# bschema — 流式二进制 Schema 演进编解码库

一个**零依赖**的 Rust 纯后端库与 CLI，实现名为 **BSE1** 的带字段编号的二进制消息
格式，覆盖 schema 演进的核心场景：可选字段、未知字段保留与转发、重复字段
（packed/unpacked）策略，以及对**不兼容类型演进的显式拒绝**。

- 核心算法全部自行实现：LEB128 varint、zig-zag、IEEE 754 定长编解码、FNV-1a
  指纹、JSON 解析/序列化、Base64。
- 流式：解码基于任意 `Read`，长度前缀先记账后读取；输入/输出/嵌套深度/重复
  数量均有显式上限。
- JSON 是控制入口：既能用子命令，也能用单个 JSON **请求信封**驱动全部操作。

## 文档

- [`FORMAT.md`](./FORMAT.md) — BSE1 线格式规范（字节布局、标签、类型、演进规则）。
- [`examples/`](./examples) — schema、消息与 8 个请求样例。

## 构建

```bash
cargo build --release
cargo test
```

生成的二进制：`target/release/bschema`。

## Schema 长什么样

```json
{
  "root": "Person",
  "messages": [
    {
      "name": "Person",
      "fields": [
        {"number": 1, "name": "id",    "type": "int32",  "required": true},
        {"number": 2, "name": "name",  "type": "string", "required": true},
        {"number": 3, "name": "email", "type": "string"},
        {"number": 4, "name": "scores","type": "sint32", "repeated": "packed"},
        {"number": 5, "name": "address","type": "Address"}
      ]
    },
    {
      "name": "Address",
      "fields": [
        {"number": 1, "name": "street", "type": "string", "required": true},
        {"number": 2, "name": "zip",    "type": "int32"}
      ]
    }
  ]
}
```

字段规则：

| 键 | 取值 |
|----|------|
| `number` | 1..=2^29-1，消息内唯一；字段的**身份**就是编号，改名安全 |
| `type` | `int32` `int64` `sint32` `sint64` `fixed32` `fixed64` `bool` `double` `string` `bytes`，或已声明的消息名 |
| `required` | `true` 时编/解码都强制存在；缺省为可选 |
| `repeated` | `true`/`"unpacked"`（每值一个标签）或 `"packed"`（一个紧凑区域） |

> 注意：`int32/int64` 的 VARINT 载荷只表示非负值；需要负数请用 `sint32/sint64`
> （zig-zag）。这让宽度提升与类型冲突都能被精确判定。

## 快速上手（子命令）

```bash
# 编码（JSON -> 二进制）
bschema encode --schema examples/schemas/person_v1.json \
  --message examples/messages/person_full.json --out msg.bse1

# 解码（二进制 -> JSON），--meta 附带 schema 指纹与是否匹配
bschema decode --schema examples/schemas/person_v1.json --input msg.bse1 --meta

# 透明转发：用中间版本的 schema 解码再编码，未知字段原样保留
bschema forward --schema person_v1.json --input newer.bse1 --out proxy.bse1

# 文本通道下用 base64
bschema encode --schema s.json --message m.json --base64
bschema decode --schema s.json --input b64.txt --base64

# schema 指纹（FNV-1a64，字段改名不影响指纹）
bschema fingerprint --schema s.json
```

## JSON 控制入口（请求信封）

所有操作都可以用单个 JSON 文档完成，适合作为服务/控制面调用：

```bash
bschema request < examples/requests/01_encode_v2.json
```

`op` 为 `encode | decode | forward | fingerprint`；二进制经
`input_base64`/`output_base64` 内联，或经 `input_file`/`output_file` 落盘；
schema 可内联 `schema` 或引用 `schema_file`；可带 `limits`。完整链路样例：

| 文件 | 演示 |
|------|------|
| `01_encode_v2.json` | 用新 schema 编码富消息 |
| `02_decode_with_v2.json` | 同版本解码，`fingerprint_match=true` |
| `03_decode_new_data_with_old_schema.json` | 旧 schema 读新数据，新字段进入 `__unknown_fields__` |
| `04_forward_through_v1.json` | 旧 schema 解码+再编码（代理转发） |
| `05_decode_forwarded_with_v2.json` | 新 schema 从转发结果中完整恢复新字段 |
| `06_decode_incompatible_type.json` | string→int64 不兼容，**拒绝** |
| `07_encode_explicit_zeros.json` | 显式零值 vs 缺省 |
| `08_fingerprint_v1.json` | 指纹 |

## 演进语义（验收点）

1. **新 schema 读旧数据**：新增字段缺省，不产生 null；旧数据正常解码。
2. **旧 schema 读新数据**：不认识的编号作为 unknown 保留原始字节，JSON 中出现在
   `__unknown_fields__`，再编码时逐字节转发。
3. **转发**：`forward` 后，新 schema 能无损取回所有字段（集成测试以"空 schema"
   视角逐字节比对转发前后载荷）。
4. **不兼容类型变化**：标签里有独立 4 位类型标识，`string→int64`、
   `fixed64→double`、`string→bytes`、`varint→zigzag` 等直接在标签层面拒绝；
   `int32→int64` 是兼容加宽，反向只在出现越界值时拒绝。
5. **重复字段策略**：packed 与 unpacked 互读，甚至可在线上交错出现。
6. **缺失 vs 显式零**：缺失字段线上无标签、JSON 无键；显式 `0`/`""`/`false`
   有标签、有键，二者在值模型里用 `Absent` 与 `Present(0)` 区分。
7. **截断**：对每条测试消息做"每个切点"截断，均返回结构化错误而非 panic。

## 资源限制

| 限制 | 默认 | 作用点 |
|------|------|--------|
| `max_message_bytes` | 64 MiB | 解码读到的每个字节，含被跳过的未知字段 |
| `max_output_bytes`  | 64 MiB | 编码写出的每个字节 |
| `max_nesting` | 64 | 嵌套消息深度，编/解码双侧 |
| `max_repeated` | 1,000,000 | 单个重复字段的元素数 |

长度在读取前先与剩余预算比较，`length=2^63-1` 之类的恶意输入不会导致巨量分配。

## 作为库使用

```rust
use bschema::{Limits, Schema};
use bschema::json::parse;

let schema = Schema::from_json(&parse(schema_text)?)?;
let msg    = bschema::json_to_message(&schema, &parse(message_text)?)?;
let bytes  = bschema::encode(&schema, &msg, &Limits::default())?;
let out    = bschema::decode(&schema, &bytes, &Limits::default())?;
let json   = bschema::message_to_json(&schema, &out.message);
```

## 测试

```bash
cargo test                 # 单元 + 集成（约 30 个用例）
cargo clippy --all-targets # lint
```

测试覆盖：全标量类型精度（含 2^53 以上整数、NaN/−0）、双向新旧读取、未知字段
转发（含嵌套消息内部）、packed/unpacked 互读、类型冲突拒绝、宽度越界、逐切点
截断、坏魔数/坏版本/非规范 bool/未知标签 id、四类资源限制、流式解码一致性、
请求信封。

## 模块布局

```
src/
  error.rs       错误类型（截断/类型冲突/缺必填/越界 分立）
  varint.rs      LEB128 与 zig-zag
  wire.rs        4 位类型标签
  ieee.rs        f64/f32 位转换与整数宽度校验
  base64.rs      Base64
  json.rs        零依赖 JSON（保留整数精度）
  schema.rs      schema 模型与校验
  fingerprint.rs FNV-1a64 线契约指纹
  limits.rs      资源限制
  value.rs       值模型（Absent/Present/Repeated）+ JSON 桥接
  writer.rs      流式编码器
  reader.rs      流式解码器
  cli.rs         CLI 与 JSON 请求信封
```
