# ecstripe — 纠删码条带恢复（纯后端）

用 Rust 实现的流式纠删码编解码库 + JSON 控制入口。把输入字节流切成条带（stripe），
每个条带用**系统式 Reed-Solomon 码**（GF(2⁸)，k 数据片 + m 校验片）编码；
当每个条带**已知位置**的丢失片数不超过 m 时，可完整恢复原始流。

* 有限域运算基于成熟库 [`gf256`](https://crates.io/crates/gf256)（多项式 0x11d，生成元 0x2，查表实现）。
* **核心矩阵构造与恢复算法为本项目自行实现**（`src/gfmat.rs`、`src/stripe.rs`）：
  完整 Vandermonde 矩阵右乘其顶部逆矩阵得到系统式编码矩阵 `E = V·V_top⁻¹`
  （保证任意 k 行可逆）、GF(2⁸) 上的 Gauss-Jordan 求逆、按行重建丢失数据片。
* JSON 解析用 `serde_json`，外部完整性校验用 `sha2`（SHA-256）。无其他依赖。

## 能力承诺（重要）

* **只承诺恢复“已知丢失”（erasure）**：调用方必须通过 `missing_shards` 指明哪些片丢失。
* **只承诺丢失片数 ≤ m（校验片数）**：超过即返回 `too many missing shards` 错误。
* **不检测静默损坏**：格式不含任何校验和。某片被静默改写后，解码会“成功”但输出错误字节。
  必须由外部校验兜底——JSON 解码请求支持 `expected_sha256` 字段，不匹配则报错
  （若已知哪片损坏，把它标记为 `missing_shards` 即可正确恢复）。

## 二进制容器格式 `ECSR`（版本 1）

全部整数为小端。格式同时文档化在 `src/format.rs` 的模块注释中。

```
头部（20 字节）:
  magic         [u8;4]   "ECSR"
  version       u8       当前为 1
  data_shards   u8       k，数据片数，1..=255
  parity_shards u8       m，校验片数，1..=255，且 k+m <= 255
  flags         u8       保留，必须为 0
  stripe_size   u32      每片每条带字节数，1..=16777216 (16 MiB)
  original_len  u64      原始输入字节数

主体: 共 num_stripes = ceil(original_len / (k * stripe_size)) 个条带
      （original_len == 0 时为 0 个）。每个条带按片序号 0..k+m-1 依次存放：
  block_len     u32      必须等于 stripe_size，否则报 LengthMismatch
  block         [u8; block_len]
```

最后一个条带的数据区在编码前补零到 `k * stripe_size`；解码器按 `original_len` 截断输出，
补零不会泄漏到解码结果。片 0..k-1 为数据片（系统式，原始字节直通），片 k..k+m-1 为校验片。

## 资源限制

* **内存**：流式处理，任一时刻只缓冲一个条带，即 `(k+m) * stripe_size` 字节；
  另由 `max_stripe_memory` 封顶（默认 64 MiB）。编码与解码路径都强制检查——
  容器是不可信输入，伪造头部的超大 `stripe_size` 会被拒绝。
* **解码输出长度**：由 `max_output_bytes` 封顶（默认 1 GiB，硬上限 16 GiB），
  头部声明的 `original_len` 超限即在写出任何数据前报错。
* **参数上限**：`k`、`m` 各 ≤ 255，`k+m` ≤ 255，`stripe_size` ≤ 16 MiB。
* 长度一致性：每个块的 `block_len` 前缀必须与头部 `stripe_size` 一致；
  主体截断、尾部多余字节、头部 `original_len` 被篡改都会报错。

## 构建与测试

```sh
cargo build
cargo test          # 单元测试 + 集成测试（含丢片组合枚举）
cargo run -- < examples/requests/encode.json
```

## JSON 控制入口

二进制 `ecstripe` 从 stdin 读一个 JSON 请求（或 `--request PATH` 从文件读），
向 stdout 输出一个 JSON 响应；`"ok": true` 时退出码为 0，否则为 1。

### encode

```json
{
  "op": "encode",
  "data_shards": 4,
  "parity_shards": 2,
  "stripe_size": 4096,
  "input_path": "examples/data/input.bin",
  "output_path": "examples/data/output.ecsr",
  "max_stripe_memory": 67108864
}
```

响应：`{"ok":true,"op":"encode","input_bytes":N,"output_bytes":M,"stripes":S,...}`

### decode

```json
{
  "op": "decode",
  "input_path": "examples/data/output.ecsr",
  "output_path": "examples/data/recovered.bin",
  "missing_shards": [1, 4],
  "max_output_bytes": 1073741824,
  "expected_sha256": "<原始输入的 SHA-256，十六进制，可选>"
}
```

响应：`{"ok":true,"op":"decode","output_bytes":N,"stripes":S,"reconstructed_shards":R,"sha256":"...","verified":true}`

* `missing_shards`（可选，默认 `[]`）：已知丢失的片序号，个数 ≤ m。
* `expected_sha256`（可选）：提供时解码输出的 SHA-256 必须匹配，否则 `ok:false`。
  不提供时响应中 `verified` 为 `null`，`sha256` 字段始终给出实际输出的哈希。
* 错误响应统一为 `{"ok":false,"op":...,"error":"..."}`。

请求样例见 `examples/requests/`。

## 测试覆盖（验收对照）

| 验收项 | 测试 |
|---|---|
| 枚举小参数丢片组合全部可恢复 | `tests/codec_tests.rs::enumerate_all_erasure_combinations_recover`（(k,m) ∈ {(2,1),(4,2),(3,3)}，枚举 C(n,0..m) 全部组合）；`src/stripe.rs`、`src/gfmat.rs` 单元测试枚举更多参数 |
| 缺片过多报错 | `too_many_missing_shards_fail`、`json_too_many_missing_and_output_cap` |
| 长度不一致报错 | `corrupt_block_length_prefix_fails`、`truncated_container_fails`、`trailing_bytes_fail`、`tampered_original_len_fails` |
| 静默损坏需外部校验 | `silent_corruption_is_not_detected_without_external_check`、`json_silent_corruption_fails_external_checksum` |
| 内存/输出长度限制 | `parameter_limits_enforced_on_encode`、`hostile_header_limits_enforced_on_decode`、`decode_output_limit_enforced` |
| JSON 入口健壮性 | `json_bad_requests_are_reported_not_panics` 等 `tests/control_tests.rs` |

## 实测记录

> 以下为 2026-09-25 在本机实际运行的命令与结果（如实记录）。

### 环境

* OS: Linux 6.8.0-90-generic (x86_64)
* Rust: rustc 1.98.1 (48a229cea 2026-09-01) / cargo 1.98.1（使用本机 `/home/admin/rust-local` 工具链；
  共享 `~/.rustup` 当时被多个并发进程争抢导致安装失败，故改用已有的本地工具链）
* 依赖: gf256 0.3.1, serde 1.0.229, serde_json 1.0.151, sha2 0.10.9

### 命令与结果

```
$ cargo build            # 成功（dev，22.55s），初版有 1 个 unused variable 警告，已修复
$ cargo test             # 23 passed; 0 failed
  - src/lib.rs 单元测试        5 passed（矩阵求逆枚举、单位阵校验、条带丢片枚举、缺片过多等）
  - tests/codec_tests.rs      13 passed（含 enumerate_all_erasure_combinations_recover：
                                  (k,m)∈{(2,1),(4,2),(3,3)} 枚举全部 C(n,0..m) 丢片组合均恢复）
  - tests/control_tests.rs     5 passed（JSON 入口端到端、静默损坏外部校验、错误请求）

$ cargo build --release  # 成功
$ head -c 100000 /dev/urandom > examples/data/input.bin
$ ./target/release/ecstripe --request examples/requests/encode.json
  {"data_shards":4,"input_bytes":100000,"ok":true,"op":"encode","output_bytes":172220,
   "parity_shards":2,"stripe_size":4096,"stripes":7}                          # exit=0
$ ./target/release/ecstripe --request examples/requests/decode.json          # missing_shards=[1,4]
  {"ok":true,"op":"decode","output_bytes":100000,"reconstructed_shards":7,
   "sha256":"f89c68e4…2c8693","stripes":7,"verified":null}                    # exit=0
$ cmp examples/data/input.bin examples/data/recovered.bin                    # ROUNDTRIP_OK
$ ./target/release/ecstripe --request <decode_verified.json 填入原始 sha256>
  {"ok":true,…,"verified":true}                                              # exit=0

# 失败模式（stdin 方式调用）：
missing_shards=[0,1,2]（3 > m=2） → {"error":"too many missing shards: 3 missing but only 2 parity shards",…}  exit=1
expected_sha256 故意填错           → {"error":"sha256 mismatch: …",…}                                          exit=1
max_output_bytes=99999 < 100000    → {"error":"decoded output of 100000 bytes exceeds limit of 99999 bytes",…} exit=1
```

### 未通过项 / 已知限制

* 当前无未通过的测试。
* 开发过程中曾发现并修复一处算法缺陷（有测试为证）：初版直接把 `[I_k; V]` 堆叠作编码矩阵，
  在特征 2 的有限域上存在奇异的 k 行子矩阵（`gfmat` 枚举测试在 k=6,m=3 时捕获）。
  已改为完整 Vandermonde 矩阵右乘顶部逆矩阵的标准构造 `E = V·V_top⁻¹`，
  并由 `inverse_of_random_submatrix_roundtrips` 枚举全部行子集验证可逆。
* 已知限制（设计承诺，非缺陷）：格式不检测静默损坏，必须外部校验；
  只支持已知位置的丢失恢复，不做错误定位（无 syndrome/Berlekamp-Massey）；
  恢复以条带为单位，丢失片集合对全部条带生效。
