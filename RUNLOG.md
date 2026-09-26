# 运行记录（RUNLOG）

本文件如实记录实际执行过的命令与结果。环境：Linux x86_64，工具链
`rustc/cargo 1.98.1 (stable)`。

> 环境备注（如实记录）：机器上默认的 `~/.rustup` stable 工具链安装不完整
> （`rustc` 一度报 `missing manifest`），且有其它并行任务在重装默认工具链，
> 导致 `cargo` 无法使用。为不干扰其它任务，本项目使用**隔离工具链**
> `~/.rustup-opp166a` 完成全部构建与测试。复现命令里因此带了环境变量前缀；
> 在工具链正常的机器上直接 `cargo ...` 即可。

```bash
export RUSTUP_HOME=$HOME/.rustup-opp166a
export CARGO_HOME=$HOME/.cargo-opp166a
TB=$HOME/.rustup-opp166a/toolchains/stable-x86_64-unknown-linux-gnu/bin
export RUSTC=$TB/rustc PATH="$TB:$PATH"
```

## 1. 构建

| 命令 | 结果 |
|------|------|
| `cargo build` | 初次 1 个编译错误（`Range::contains` 需 `&u8`），修复后 `Finished dev` |
| `cargo build --release` | `Finished release`，产出 `target/release/cdc`（约 486 KiB） |

## 2. 自动化测试

命令：`cargo test`

最终结果（**42 个测试全部通过，0 失败**）：

```
running 15 tests ... src/lib.rs            (单元测试)
test result: ok. 15 passed; 0 failed
running 6 tests  ... tests/control_api.rs  (JSON 控制入口)
test result: ok. 6 passed; 0 failed
running 21 tests ... tests/end_to_end.rs   (端到端)
test result: ok. 21 passed; 0 failed
Doc-tests / bin: 0
```

单元测试覆盖：LEB128（含溢出/截断）、CRC-32 标准向量、FNV 字典去重与扩容、
base64 RFC 4648 向量与非法输入、JSON 解析/代理对/严格数字词法。

端到端覆盖验收点：

| 验收点 | 测试 |
|--------|------|
| 空串与 NULL 区分 | `empty_string_is_distinct_from_null` |
| 全 NULL（多段、字典为空） | `all_null_column` |
| Unicode（中/日/韩/组合字符/emoji） | `unicode_values_roundtrip` |
| 大基数字典退化（12 万行各不同） | `high_cardinality_degenerate_dict` |
| 分段合并 + 跨段 ID 重映射 | `merge_cross_segment_ids_are_remapped`、`merge_preserves_every_row_across_segments_and_files` |
| 合并前后逐行值一致 | 上一测试 + `randomized_segmentation_merge_is_value_preserving`（5000 行确定性随机切 4 文件） |
| 字典顺序不影响解码 | 同测试比较 first-seen 与 canonical 两种输出的逐行值 |
| 内存/输出长度限制 | `row/value_bytes/segment_size/value_len/tiny_segment_byte_limit_*` |
| 损坏检测 | `detects_bad_magic_and_truncation`、`detects_crc_corruption` |
| 合并幂等、全 NULL/空文件合并 | `merge_idempotent`、`merge_all_null_and_empty_inputs` |

### 过程中出现并已修复的问题（如实记录）

1. **编译错误**：`(b'1'..=b'9').contains(c)` 类型不匹配 → 改为 `contains(&c)`。
2. **测试编译错误**：
   - 库未导出 `Result` 类型别名 → 在 `lib.rs` `pub use error::Result;`；
   - 一个测试里临时值生命周期不足 → 拆出 `let` 绑定。
3. **一个测试断言初次失败**：`errors_are_structured_not_panics` 原本期望
   3 字节 base64 输入报"坏魔数"，实际正确行为是"截断"
   （`UnexpectedEof` → `truncated_input`）。**这是测试预期写错，不是代码 bug**，
   已改为：3 字节 → `truncated_input`，6 个零字节 → `corrupt_or_unsupported_format`。
4. **clippy 4 个风格警告**（`is_multiple_of`、常量块大小、`let_and_return`、
   `type_complexity`）→ 全部清理。

## 3. 静态检查与格式化

| 命令 | 结果 |
|------|------|
| `cargo fmt` | 通过（已应用） |
| `cargo clippy --all-targets -- -D warnings` | **0 警告 0 错误** |

## 4. CLI 实际运行结果

通过 `target/release/cdc`（stdin 或 `--request-file`）实测：

- **encode**（`examples/encode_segments.json`，3 个显式段）：
  `write_stats = {segments:3, rows:11, nulls:3, distinct_sum:7, bytes_written:87}`。
- **decode**：11 行逐行还原，空串显示为 `""`，NULL 显示为 `null`，三段结构与输入一致。
- **merge**（两段文件 A 三段 + 文件 B 一段，共 4 输入段）：
  ```
  merge_stats = {input_segments:4, rows:21, nulls:5,
                 global_distinct:8, local_distinct_sum:12, deduped:4}
  ```
  合并结果为**单段**，解码出的 21 行 == A 拼接 B 的原始行（逐行一致）。
- **inspect**：正确报告魔数 `CDC1`、版本 1、单段结构。
- **CRC 损坏**：翻转正文字节后返回
  `{"ok":false,"error":{"code":"crc_mismatch",...}}`，未输出任何数据行。
- **输出长度限制**：`limits.max_value_bytes=10` 解码超限时返回
  `{"ok":false,"error":{"code":"limit_exceeded",...}}`。
- **端到端脚本**：`CDC=./target/release/cdc bash examples/demo.sh` 退出码 0。

## 5. 未通过项 / 已知限制

- 无未通过的测试或检查（最终 `cargo test`、`clippy -D warnings`、release 构建均通过）。
- 合并（merge）按定义是**有界批量**操作（需见到全部段才能确定全局字典），
  不是流式；编码/解码侧是流式的（内存至多一个段）。这是设计取舍，已在
  `docs/FORMAT.md` 与 README 中说明。
- 未做、也按要求不做任何前端。
