# 运行记录（RUNLOG）

日期：2026-09-25。环境：Linux 6.8.0-90-generic，x86_64。
工具链：机器上原本没有 Rust；rustup 官方源下载超时，且共享
`~/.rustup` 被本机其他并发会话争抢导致安装损坏，最终改用项目本地
隔离安装（`RUSTUP_HOME=$PWD/.rustup`、`CARGO_HOME=$PWD/.cargo`，
阿里云镜像源），cargo/rustc 1.96.0。以下命令均需先：

```sh
export RUSTUP_HOME=$PWD/.rustup CARGO_HOME=$PWD/.cargo PATH="$PWD/.cargo/bin:$PATH"
```

## 构建

- `cargo build` —— 首次**未通过**：`src/decoder.rs` 字面量分支存在
  借用冲突（E0502，`self.inbuf` 的不可变借用与 `push_hist(&mut self)`
  冲突）。修复为字段级分离借用后通过。
- `cargo build --release` —— 通过。
- `cargo clippy --all-targets` —— 首次 4 个警告（`is_multiple_of`、
  `contains`、`RangeInclusive::contains`、文档缩进），修复后 0 警告。
- `cargo fmt --check` —— 首次 3 处格式差异，`cargo fmt` 后通过。

## 测试（`cargo test`，debug）

共 32 个测试，最终全部通过（6 个测试目标全部 `test result: ok`）：

| 测试目标 | 数量 | 结果 |
|----------|------|------|
| lib 单元测试（json/base64） | 7 | 通过 |
| tests/roundtrip.rs | 7 | 通过 |
| tests/corrupt.rs | 13 | 通过 |
| tests/chunks.rs | 5 | 通过 |

### 未通过项及修复（如实记录）

1. `corrupt::output_budget_stops_bomb` —— 断言 `compressed.len() < 1024`
   过严：100 KB 的 `'a'` 实际压缩为约 2.4 KB（1 字面量 + 770 个 3 字节
   匹配令牌，格式上限 MAX_MATCH=130）。断言放宽为 `< 4096`。
2. `corrupt::output_budget_stops_bomb` —— `attempted` 期望值写错：
   先产出 1 个字面量，首个匹配令牌申请 130，应为 131 而非 130。
3. `roundtrip::repeated_char_compresses_and_roundtrips` —— 压缩率断言
   `< len/50`（2000 字节）同样低于实际 2320 字节，放宽为 `< len/20`。

以上均为测试断言/期望值的错误，非编解码逻辑错误；修正后未再失败。

## JSON 控制入口实测（release 二进制）

- `lzsw < examples/requests/encode.json`
  → `{"ok":true,"op":"encode","input_len":17,"output_len":18,"data":"TFpTVwEADwAFaGVsbG8giAAG"}`（exit 0）
- `lzsw < examples/requests/encode_chunked.json`（chunk_size=4）
  → 输出与非分块完全一致（exit 0）
- `lzsw < examples/requests/decode.json`
  → `{"ok":true,...,"data":"aGVsbG8gaGVsbG8gaGVsbG8="}`（exit 0）
- `lzsw < examples/requests/decode_chunked.json`（chunk_size=1）
  → 结果一致（exit 0）
- `lzsw < examples/requests/decode_budget.json`（max_output=5）
  → `{"ok":false,"error":{"kind":"OutputBudgetExceeded","message":"output budget 5 exceeded (attempted 6)"}}`（exit 1）
- 手工构造非法回距（distance=5 > 已产出 2）
  → `DistanceTooLarge`（exit 1）
- 手工构造截断字面量令牌 → `TruncatedToken`（exit 1）
- 非法 base64 / 未知 op → `BadRequest`（exit 1）

## 端到端大规模往返

5,314,000 字节周期数据（`"the quick brown fox! "*1000 + 0..=255` × 250），
经 JSON CLI 以 chunk_size=65536 编码、chunk_size=4096 解码：

- 编码输出 124,029 字节（约 43:1）
- 解码结果与原始数据 sha256 一致（`86cd4ddf84b0d132…`），往返无损

## 已知限制

- JSON 控制入口将整个请求/响应读入内存（base64 字符串），内存界限
  保证针对编解码库本身。
- 编码器为贪心单候选匹配（3 字节键哈希表），压缩率低于 deflate/zstd，
  属于格式子集的有意取舍。
