# rbitmap — 游程/混合容器 32 位无符号整数集合

纯 Rust、**零外部依赖**的整数集合库与命令行工具：

- 混合**稀疏有序数组**与**位图容器**的 Roaring 风格整数集合（32 位无符号）；
- 容器在基数跨越阈值时自动切换，**切换保持集合语义**；
- 并集、交集、差集、成员判定、增删；
- 自行设计并文档化的 **RBM1 流式二进制格式**（见 [FORMAT.md](FORMAT.md)）；
- 所有长度字段"先校验、后缓冲"，**内存占用与解码输出长度严格有界**，
  恶意长度、截断、乱序等输入一律返回错误而非 panic；
- 无状态 **JSON 控制入口**（库函数 + CLI），集合以 base64 承载；
- 核心算法（容器、集合运算、varint、base64、JSON）全部自行实现。

无前端，纯后端。

## 目录结构

```text
Cargo.toml
FORMAT.md                  RBM1 二进制格式规范（必读）
README.md
src/
  lib.rs                   crate 入口
  error.rs                 统一错误类型
  codec.rs                 varint + 受限 Reader/Writer（缓冲与流式两套）
  container.rs             数组/位图容器、切换、交并差
  bitmap.rs                顶层集合、集合代数、RBM1 编解码、Limits
  base64.rs                自实现 RFC 4648 base64
  json.rs                  自实现严格 JSON 解析/序列化
  api.rs                   JSON 请求/响应控制入口
  bin/rbitmap.rs           CLI
examples/                  请求样例（JSON）
tests/                     验收与单元测试
```

## 构建与测试

```bash
cargo build --release
cargo test                 # 全部测试（含随机 oracle 对照、恶意输入）
cargo clippy --all-targets # 若本机装有 clippy
```

## 快速开始

```bash
# 1) 用整数列表构建一个集合，返回 base64 编码
echo '{"op":"build","values":[1,2,3,70000,4294967295]}' | ./target/release/rbitmap

# 2) 把样例文件作为请求
./target/release/rbitmap examples/build.json

# 3) 批量：每行一个请求，每行一个响应
./target/release/rbitmap --batch examples/ops.jsonl
```

输出统一为 `{"ok":true,"result":{...}}`，失败为
`{"ok":false,"error":"..."}`。协议级错误是数据、不是崩溃，CLI 仍以 0 退出。

## JSON 协议

请求：

```json
{
  "op": "build | union | intersect | difference | insert | remove | contains | decode | stats",
  "values": [1, 2, 3],
  "set": "<base64 RBM1>",
  "other": "<base64 RBM1>",
  "value": 42,
  "limits": { "max_containers": 65536, "max_values": 268435456, "max_output": 1048576 }
}
```

| op | 输入 | result 关键字段 |
|----|------|-----------------|
| `build` | `values:u32[]` | `set`(base64), `stats` |
| `union` / `intersect` / `difference` | `set`, `other` | `set`, `stats` |
| `insert` / `remove` | `set`, `value` | `changed`, `set`, `stats` |
| `contains` | `set`, `value` | `present` |
| `decode` | `set` | `values:u32[]`（受 `max_output` 限制）, `stats` |
| `stats` | `set` | 基数、容器数量与种类、编码字节数 |

`limits` 三个字段都可选；详见 [FORMAT.md](FORMAT.md) 第 4 节。

## 设计要点

- **容器方案。** 高 16 位为键，低 16 位存入容器。数组容器是去重有序
  `u16` 列表；位图容器是 1024 个 u64（8 KiB）覆盖全部 65536 个低位值。
  阈值 `ARRAY_LIMIT = 4096`：插入到第 4097 个值升级为位图，删除回
  4096 及以下降级为数组。升/降级都精确保留值集合。
- **集合运算。** 数组×数组用有序归并；位图×位图逐字位运算；混合情形把
  稀疏侧投射进稠密侧。输出统一规范化，空容器立即消失。
- **有界解码。** 键数量先校验再读；数组声明长度先与
  `min(65536, 剩余值预算)` 比较再分配；位图固定 8 KiB，读完按 popcount
  重算基数并扣减预算；varint 拒绝非最短编码与溢出。
- **流式。** `encode_stream`/`decode_stream` 直接对接任意
  `std::io::Write/Read`，与缓冲路径字节一致（测试锁定）。

## 验收测试对应关系

| 验收要求 | 测试 |
|----------|------|
| 随机集合与标准集合（`BTreeSet`）比较 | `tests/bitmap_test.rs::random_sets_match_btree_oracle`（200 轮、4 种密度体制） |
| 容器边界（4096 双向切换、键边界 0/65535/65536） | `container_switching_preserves_semantics`、`boundary_values` |
| 极密（整容器填满）与极稀数据 | `extremely_dense_sets`、`extremely_sparse_sets` |
| 恶意容器长度 / 截断 / 乱序 / 重复 / 未知 tag | `decode_rejects_*` 系列 |
| 内存与输出长度限制 | `decode_output_budget_limits_cardinality`、`decode_*_limits_enforced`、`bounded_varint_enforces_limit_before_read` |
| 流式与缓冲编码一致 | `encode_stream_matches_encode`、`decode_stream_matches_decode` |
| JSON 入口 | `tests/api_test.rs` |
| varint/编解码原语 | `tests/codec_test.rs` |

## 许可

MIT
