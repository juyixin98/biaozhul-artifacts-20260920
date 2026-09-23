# 实际运行记录

环境：Linux 6.8.0-90-generic x86_64；cargo 1.98.1 (797e8a9bc 2026-08-05)；rustc 1.98.1。
日期：2026-09-23。以下命令均在项目根目录实际执行。

## 1. 自动化测试

命令：`cargo test`

结果（共 36 项，全部通过，0 失败）：

| 测试目标 | 数量 | 结果 |
|---|---|---|
| 库单元测试（`frame` 成帧、`name` 基础） | 7 | ok |
| `tests/decode_tests.rs`（解码/编码验收） | 25 | ok |
| `tests/server_tests.rs`（应答规则 + 真实 TCP 端到端） | 4 | ok |
| bin/examples 的 doc/harness 目标 | 0 | ok |

关键覆盖（验收点一一对应）：

- 前向指针：`forward_pointer_is_followed`（QNAME 指针指向后续 RDATA）。
- 越界标签：`out_of_bounds_label`；指针越界：`pointer_out_of_bounds`；
  截断指针：`truncated_pointer`。
- 指针环：`pointer_self_loop`（自环）、`pointer_pair_loop`（互环）。
- 截断资源记录：`truncated_resource_record`（RDLENGTH=8 只剩 4 字节）；
  另有 `cname_name_crossing_rdlength_rejected`（CNAME 名字越过 RDLENGTH /
  RDATA 带多余尾巴两种形态）。
- 跳转次数上限：`pointer_chain_limit_is_enforced_and_configurable`
  （71 跳链在默认上限 64 下被拒，上限调到 128 后解码成功）。
- 已知报文语义往返：`uncompressed_query_is_byte_identical_after_roundtrip`
  （无压缩输入逐字节一致）、`semantic_roundtrip_of_compressed_response`
  （压缩输入→非压缩输出，再次解析语义相等）、`forward_pointer_is_followed`。
- 未知类型保留字节：`unknown_rdata_preserved_as_bytes`（压缩/非压缩两种形态）。
- 其他：255 字节上限按展开后长度计（`compressed_name_length_measures_expanded_form`，
  含展开恰好 255 字节的用例）、标签 63/名字 255/区段记录数上限、
  保留前缀 01 与扩展前缀 10、Z 位回显、AAAA、大小写不敏感比较等。

静态检查：`cargo clippy --all-targets` 零警告零错误；`cargo fmt --check` 通过。

## 2. 样例生成

命令：`cargo run --release --example gen_samples`

输出：`已生成 14 个样例到 samples/`、`合法样例自检通过`。

## 3. 本地 TCP 服务端到端

命令：`./scripts/demo.sh`（构建 release → 起服务 127.0.0.1:10053 →
对 14 个样例逐帧发送，退出码 0）。

实际应答汇总：

| 样例 | 服务应答 |
|---|---|
| query_a.bin | NOERROR，`www.example.com. 60 IN A 192.0.2.1` |
| query_aaaa.bin | NOERROR，`… IN AAAA 2001:db8::1` |
| query_cname.bin | NOERROR，`… IN CNAME alias.example.com.` |
| query_root.bin | NOERROR，`. 60 IN A 192.0.2.1` |
| response_alias_compressed.bin（QR=1，回指压缩） | 回显：解码后非压缩重编码，TTL 300 的 CNAME 保持 |
| response_forward_pointer.bin（QR=1，前向指针） | 回显：QNAME=`www.example.com.`，CNAME=`example.com.` |
| response_unknown_type.bin（TYPE99） | 回显：RDATA `v=spf1 -all` 11 字节逐字节保留 |
| bad_loop_self / bad_loop_pair | FORMERR（PointerLoop） |
| bad_label_overrun | FORMERR（UnexpectedEof） |
| bad_truncated_rr | FORMERR（RdataLengthMismatch） |
| bad_reserved_label | FORMERR（ReservedLabelKind） |
| bad_extended_label | FORMERR（UnsupportedExtendedLabel） |
| stress_pointer_chain（71 跳） | FORMERR（PointerChainTooLong） |

另由 `tests/server_tests.rs` 验证：同一连接连续三帧流水线处理、
声明 5000B 帧（>4096 上限）连接被关闭、声明 16B 只给 2B（截断帧）连接被关闭。

## 4. 开发过程中发现并修正的问题（如实记录）

1. 初版指针链/前向指针**样例构造错误**（RR 与指针槽位顺序放反、A 记录
   RDATA 里塞名字），被测试捕获后按协议修正——库的拒绝行为本身是正确的。
2. 初版把指针自身 2 字节计入名字 255 上限，口径过严（可能误杀多指针
   拼接的合法名字），已改为按**展开后**长度计量，并补了恰好 255 字节的用例。
3. CNAME 的 RDATA 名字原本只受报文尾约束，可能越过 RDLENGTH 吞掉下一条
   记录的字节；已增加“物理消耗必须恰好等于 RDLENGTH”校验，补两种形态测试。
4. 测试服务最初用阻塞式 `incoming()` 无限循环，导致测试进程无法退出；
   改为非阻塞轮询 + 停止标志，连接加读超时。

## 5. 未通过项

无。截至最后一次运行，`cargo test` 36/36 通过，clippy/fmt 干净，
demo.sh 退出码 0，14 个样例应答全部符合预期。
