# RUNLOG — 实际运行记录

本文件如实记录交付环境中的实际命令与结果。

- 日期：2026-09-23T15:54:58+08:00
- 系统：Linux-6.8.0-90-generic-x86_64-with-glibc2.39 (x86_64)，内核 6.8.0-90-generic
- rustc 1.98.1 (48a229cea 2026-09-01)；cargo 1.98.1 (797e8a9bc 2026-08-05)
- 工作目录：`/home/admin/Downloads/biaozhul/opp40/b`
- 所有命令均在**无 root、无 CAP_NET_RAW** 条件下运行；服务只绑定 127.0.0.1，
  不使用原始套接字。

---

## 1. 自动化测试 `cargo test`

```
running 26 tests
test ipv4::tests::incremental_parse_waits_for_whole_datagram ... ok
test ipv4::tests::rejects_bad_version_and_checksum ... ok
test ipv4::tests::parses_basic_fragment_fields ... ok
test ipv4::tests::rejects_reserved_flag_and_short_total_length ... ok
test json::tests::escapes_and_unicode ... ok
test json::tests::rejects_bad_inputs ... ok
test json::tests::roundtrip_basic ... ok
test parser::tests::consume_and_compact_reclaims_space ... ok
test parser::tests::incremental_feeds_and_reads ... ok
test parser::tests::length_limit_is_enforced_atomically ... ok
test parser::tests::subsets_and_bounded_reads ... ok
test reassembly::tests::conflicting_last_fragments_are_rejected ... ok
test parser::tests::unlimited_stream_when_limit_zero ... ok
test reassembly::tests::drop_assembly_policy_removes_state_on_overlap ... ok
test reassembly::tests::duplicate_fragments_are_ignored ... ok
test reassembly::tests::full_path_through_ipv4_parse_and_reassembly ... ok
test reassembly::tests::id_reuse_resets_old_assembly ... ok
test reassembly::tests::max_datagram_limit_is_enforced ... ok
test reassembly::tests::memory_budget_is_enforced ... ok
test reassembly::tests::non_fragment_packet_passes_through ... ok
test reassembly::tests::missing_first_fragment_buffers_then_completes ... ok
test reassembly::tests::out_of_order_reassembly_matches_original ... ok
test reassembly::tests::overlapping_fragments_are_rejected_by_default ... ok
test sha256::tests::known_vectors ... ok
test reassembly::tests::timeout_ttl_reclaims_assemblies ... ok
test sha256::tests::streaming_matches_one_shot ... ok
test result: ok. 26 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s
running 0 tests
test result: ok. 0 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s
running 0 tests
test result: ok. 0 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s
running 0 tests
test result: ok. 0 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s
running 10 tests
test scenario_bad_packet_and_bad_hex_are_reported_with_clear_errors ... ok
test scenario_duplicate_fragment_is_idempotent ... ok
test scenario_id_reuse_resets_old_assembly ... ok
test scenario_memory_budget_and_datagram_limits_surface_named_errors ... ok
test scenario_non_final_fragment_payload_must_be_8_aligned ... ok
test scenario_missing_first_fragment_stays_pending_then_completes ... ok
test scenario_overlapping_fragment_is_rejected_with_error_type ... ok
test scenario_timeout_purge_reclaims_buffers ... ok
test scenario_packet_hex_goes_through_handwritten_ipv4_parser ... ok
test scenario_out_of_order_completes_and_matches_original_payload ... ok
test result: ok. 10 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s
running 3 tests
test tcp_ping_config_stats_roundtrip ... ok
test tcp_overlap_is_rejected_and_bad_json_handled ... ok
test tcp_full_reassembly_and_duplicate_over_real_socket ... ok
test result: ok. 3 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.44s
running 1 test
test src/parser.rs - parser::ByteStream (line 344) ... ok
test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.11s
```

**40 个测试全部通过，0 失败**：26 个库单元测试 + 10 个协议层场景测试 +
3 个真实 TCP 端到端测试 + 1 个 parser 文档 doctest。

## 2. 静态检查

- `cargo clippy --all-targets`：无 warning / 无 error（最后一行：`Finished `dev` profile [unoptimized + debuginfo] target(s) in 0.05s`）
- `cargo fmt`：已应用，代码已按 rustfmt 默认风格排版。
- `cargo build`：debug 与 release 均编译通过，零第三方依赖（`[dependencies]` 为空，
  构建过程无需联网下载 crate）。

## 3. 端到端逐字节校验 `scripts/verify_e2e.sh`

启动真实 TCP 服务，用 `fraggen` 以两种模式（洗牌乱序 + 原始 IPv4 `packet_hex`；
逆序 + 显式字段）回放，再由独立 Python 脚本按相同 PRNG 复算“完整原始载荷”，
对 `total_bytes`、`sha256`、回显的 `payload_hex` 做逐字节比对：

```
[1/4] cargo build
[2/4] start server on 127.0.0.1:9140
[3/4] case: shuffle + packet_hex (seed=1234, length=200)
[4/4] independent byte-level verification
  total_bytes  server=200 expected=200
  sha256       server=77f6aab22a1e65aa98019b6fd1b2e2e7d452d4c009310cb41c046acc5640a872
               expect=77f6aab22a1e65aa98019b6fd1b2e2e7d452d4c009310cb41c046acc5640a872
  MATCH: reassembled datagram is byte-identical to original payload
[3/4] case: reverse + fields (seed=99, length=137)
[4/4] independent byte-level verification
  total_bytes  server=137 expected=137
  sha256       server=7d2f2a510e0e0dec5524cf98ce6e550081ee2bf4fe4cc530a68f45daf3b6a63e
               expect=7d2f2a510e0e0dec5524cf98ce6e550081ee2bf4fe4cc530a68f45daf3b6a63e
  MATCH: reassembled datagram is byte-identical to original payload

E2E OK: reassembled datagrams are byte-identical to original payloads
```

## 4. 验收场景真实运行记录（JSONL over TCP）

### 4.1 乱序重组：三种投递顺序产出同一数据报指纹

每个样例在同一服务器实例上各回放一次；`MATCH` 表示 completed 响应的 sha256
与生成器文件头 `# expected: ... sha256=...` 完全一致：

```
--- 乱序三种顺序 + packet 模式：对照文件头 expected 与 completed 响应
[reassembly_forward] total=100 MATCH
[reassembly_reverse] total=100 MATCH
[reassembly_shuffle] total=150 MATCH
[reassembly_packet_hex] total=80 MATCH
```

### 4.2 缺首片：中间片/末片先到，组装保持 pending，不错误完成

全新服务器实例（仅运行本例）：

```
--- 缺首片（全新服务器实例，端口 9091）
event=accepted has_first=False has_last=False
event=accepted has_first=False has_last=True
stats: assembly_count=1 bytes_used=48 datagrams_completed=0

--- 错误处理（全新实例，端口 9092）：连接不中断，最后 ping 成功
REJECTED: bad_hex :: hex payload must have an even number of digits
REJECTED: bad_flow :: protocol must be in 0..=255
REJECTED: bad_request :: fragment offset must be a multiple of 8 bytes
REJECTED: bad_request :: missing boolean field `mf`
REJECTED: bad_packet :: incomplete input: need at least 9 bytes, have 8
REJECTED: unknown_op :: unknown op `bogus`; expected one of ping/frag/status/purge/stats/config
ok: True
```

`bytes_used=48` 说明缺片数据被正常缓冲；`assembly_count=1`、
`datagrams_completed=0` 说明首片缺失时不产生数据报。

### 4.3 重叠拒绝 / 完全重复片 / Identification 重用

```

--- 重叠拒绝 / 重复片 / ID 重用（全新服务器实例，端口 9137）
ipfrag-server listening on 127.0.0.1:9137
[overlap_rejected.jsonl]
event: accepted
REJECTED: overlap
event: completed

[duplicate_and_id_reuse.jsonl]
event: accepted 
event: duplicate 
event: id_reuse_reset 16
event: completed
```

- 重叠片返回 `overlap` 错误，原组装保留，随后合法的相邻末片仍可 `completed`；
- 范围与内容完全相同的重传返回 `duplicate`，不重复计费；
- 首片范围相同但内容不同 → `id_reuse_reset`，丢弃旧的 16 字节并以新首片完成。

### 4.4 超时清理

```

--- TTL 超时清理（短 TTL + 注入时钟，端口 9138）
event: accepted
event: accepted
status: count=2
status: count=2
purge: removed=2 reclaimed_bytes=16
status: count=0
stats: assemblies_expired=2 bytes_used=0

--- 系统时钟机会式清理（TTL=300ms，sleep 500ms 后插入触发）
event: accepted
before sleep: assemblies_expired=0 assembly_count=1
    (sleep 500ms > TTL 300ms)
event: accepted
after sleep:  assemblies_expired=1 assembly_count=1 (5001 已被自动回收)
```

上半段使用注入的确定性时钟（`now_ms`）：now=500 时两个 500ms TTL 的组装
被 `purge` 一并回收；下半段使用系统时钟：sleep 超过 300ms TTL 后，下一次
插入触发机会式清理，旧组装 id=5001 被自动回收（`assemblies_expired=1`）。

### 4.5 错误处理：结构化拒绝，连接不中断

```
--- 错误处理（全新实例，端口 9092）
REJECTED: bad_hex :: hex payload must have an even number of digits
REJECTED: bad_flow :: protocol must be in 0..=255
REJECTED: bad_request :: fragment offset must be a multiple of 8 bytes
REJECTED: bad_request :: missing boolean field `mf`
REJECTED: bad_packet :: incomplete input: need at least 9 bytes, have 8
REJECTED: unknown_op :: unknown op `bogus`; expected one of ping/frag/status/purge/stats/config
ok: True
```

6 个错误请求分别返回明确的 error code，最后一个 `ping` 仍然成功，
证明错误不会中断 TCP 连接。

## 5. 开发过程中如实记录的问题与处置

1. **SHA-256 56 字节测试向量最初臆造了期望值**：与实现输出不符。
   用 Python `hashlib` 独立计算后确认实现正确（空串、`abc`、56×`a` 三个向量
   最终全部通过），改为独立来源的期望值。
2. **独立校验脚本初次复现 PRNG 不一致**：原因是 Python 复现脚本对 xorshift64*
   的 u64 回绕/状态更新顺序建模错误（任意精度整数未在正确位置收窄、
   且误把乘法后值当作下一状态）。修正后与 Rust 逐字节一致；Rust 侧代码本身
   标准且正确，未改动。该独立复现逻辑固化在 `scripts/_compare_payload.py`。
3. **演示时端口 9090 被本机其他服务占用**（监听 `*:9090`，非本项目）：
   首次绑定报 `Address already in use`，改用空闲端口 9137 后全部正常。
   这是环境端口冲突，非程序缺陷；服务对绑定失败会以退出码 1 明确报错。
4. 早期实现中“首片再次到达即判定 ID 重用”过于激进，会把完全相同的首片重传
   也当成重用；已细化为“范围+内容完全一致 → duplicate，否则 → id_reuse_reset”，
   并有对应测试锁定。

## 6. 未通过项

无。验收要求的乱序、重复片、重叠、缺首片、ID 重用、超时清理全部通过，
重组结果与完整原始载荷逐字节一致（长度 + SHA-256 + payload 三重核对）。
