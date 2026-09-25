# RUNLOG —— 实际运行记录（如实）

日期：2026-09-23
平台：Linux 6.8.0-90-generic（x86_64），Rust 1.98.1（cargo 1.98.1）
项目目录：`/home/admin/Downloads/biaozhul/opp40/a`
网络：全程 `--offline`，零三方依赖（`Cargo.toml` 的 `[dependencies]` 为空）。

本文件记录实际执行过的命令与结果。**开发过程中出现过编译错误与
测试失败，均为代码/测试本身的问题，已修复并在下面如实列出**；
最终状态全部通过。

---

## 1. 工具链

```text
$ rustc --version
rustc 1.98.1 (48a229cea 2026-09-01)
$ cargo --version
cargo 1.98.1 (797e8a9bc 2026-08-05)
```

## 2. 构建

```text
$ cargo clean
$ cargo build --offline
   Compiling ipfrag v0.1.0
    Finished `dev` profile [unoptimized + debuginfo] target(s) in 0.96s

$ cargo build --release --offline
    Finished `release` profile [optimized] target(s) in 1.06s
```

`cargo clippy --offline --all-targets`：**0 个 warning / 0 个 error**。

## 3. 自动化测试（最终结果：43 项全过）

```text
$ cargo test --offline
running 32 tests                      # src 内单元测试（含真实 TCP 端到端 3 项）
test result: ok. 32 passed; 0 failed; 0 ignored; 0 measured

running 0 tests                       # bin
test result: ok. 0 passed; 0 failed

running 9 tests                       # tests/acceptance.rs 验收集成测试
test result: ok. 9 passed; 0 failed; 0 ignored; 0 measured

running 2 tests                       # 文档测试（doc-test）
test result: ok. 2 passed; 0 failed
```

`cargo test --release --offline` 结果相同（32 / 0 / 9 / 2）。

### 验收六项 ↔ 测试

| 验收项 | 集成测试 (`tests/acceptance.rs`) | 结果 |
|---|---|---|
| 乱序 | `acceptance_1_out_of_order_matches_original`（10000 字节、确定性乱序，重组后逐字节等于原始载荷） | PASS |
| 重复片 | `acceptance_2_duplicates_are_idempotent`（首片送 3 次，片数/内存不增长，刷新寿命） | PASS |
| 重叠 | `acceptance_3_overlap_is_rejected_and_drops_group`（冲突与“交叠区相同”两种都拒绝且整组丢弃） | PASS |
| 缺首片 | `acceptance_4_missing_first_fragment_blocks_completion`（尾片齐了也不完成；首片迟到后完成并对照） | PASS |
| ID 重用 | `acceptance_5_id_reuse_resets_incomplete_group`（旧组丢弃，新组重组） | PASS |
| 超时清理 | `acceptance_6_timeout_cleanup_explicit_and_lazy`（显式 purge + add 时惰性清理） | PASS |
| 长度上限/内存预算 | `budget_and_oversize_limits_are_reported_distinctly` | PASS |
| 原始 IPv4 / 未分片 / 坏报文 | `unfragmented_datagram_completes_immediately_via_raw_path`、`malformed_raw_packet_is_classified_error` | PASS |

单元层另有 `reassembles_in_order`、`reassembles_out_of_order`、
`identical_partial_overlap_is_still_rejected`、
`id_reuse_due_to_length_change`、`ttl_refresh_keeps_busy_group_alive`、
`total_memory_budget_is_enforced`、`oversized_datagram_is_rejected`、
`completed_payload_matches_full_original_for_various_sizes`
（对长度 1/7/1500/4001 重组后对照原始载荷）以及
`server::tests::tcp_end_to_end_*` / `tcp_purge_with_virtual_clock`
（真实 loopback TCP 连接）等。

## 4. 示例程序（实际输出）

```text
$ cargo run --offline --example byte_parser_demo
[subset]      tag=7, payload=[104, 101, 108, 108, 111], crc=0xabcd
[limit]       subset blocked read: requested=1, remaining=0
[limit]       limited() blocked a physically-present byte
[incomplete]  physical end: needed=Some(1), available=1
[incremental] len=5 tag=7 body=[104, 101, 108, 108, 111], stream left=4
OK: subset / limit / classified errors / incremental feed all demonstrated

$ cargo run --offline --example reassembly_demo
step 0: fragment 2 accepted -> pending(fragments=1, contiguous=0, total=None)
step 1: fragment 2 accepted -> pending(fragments=1, contiguous=0, total=None)
step 2: fragment 3 accepted -> pending(fragments=2, contiguous=0, total=Some(3001))
step 3: fragment 1 COMPLETED datagram: 3001 bytes (matches original: true)
overlap correctly rejected: overlapping fragments for 192.168.0.10 -> 203.0.113.7 proto=17 id=0xdead: incoming [8,24) overlaps stored [0,16) with 8 bytes (content CONFLICTING)
OK: offline reassembly demo finished
```

乱序顺序为：片2 → 片2(重复) → 尾片3 → 首片1；最后一步完成，
程序断言 `payload == 原始 3001 字节载荷`（输出 `matches original: true`）。

## 5. TCP 服务端到端（实际运行，无需特权）

启动（绑定 loopback，非 root）：

```text
$ ./target/debug/reasm-server --port 9131 --ttl-ms 30000
[reasm-server] config: ttl=30000ms budget=1048576B max_payload=65535B
[reasm-server] listening on http://127.0.0.1:9131 (raw TCP, NDJSON)
```

用标准库 Python 客户端喂 `examples/requests.ndjson`：

```text
$ python3 examples/client.py --port 9131
... {"op":"status",...,"pending_datagrams":1,...}
... {"result":"completed",...,"total_length":300,"payload_hex":"00254a…237"}
  -> payload vs original: MATCH
... {"error":"overlap_conflict", ... "content CONFLICTING","status":"error"}
  -> server reported error: overlap_conflict
done, payload mismatches: 0
```

- 重组完成的 300 字节数据报 `payload_hex` 与
  `examples/original_payload.hex`（完整原始载荷）**逐字节一致**，
  客户端 `payload mismatches: 0`；
- 纯 NDJSON 响应留档在 `examples/sample_responses.ndjson`（10 行：
  9 个 `status:"ok"` + 1 个 `overlap_conflict`）。

超时清理的真实 TCP 演示（虚拟时钟 `now_ms`，TTL=500ms）：

```text
$ printf '{"op":"fragment",...,"now_ms":0}\n' | nc -N 127.0.0.1 9124
{"...result":"pending"...}
$ printf '{"op":"purge","now_ms":499}\n'  | nc -N 127.0.0.1 9124
{"op":"purge",...,"purged":[],"pending_datagrams":1,...}
$ printf '{"op":"purge","now_ms":500}\n'  | nc -N 127.0.0.1 9124
{"op":"purge",...,"purged":[{"freed_bytes":4,"key":"... id=0x0001"}],
 "pending_datagrams":0,...}
```

499ms 不清理，500ms（`now - updated >= ttl`）清理并释放 4 字节。

## 6. 过程中出现过的失败（已修复，如实记录）

最终全绿，但开发中确实遇到并修掉了下列问题：

1. **编译错误：借用冲突**
   `Reassembler::add_fragment` 中 `get_mut` 后又调用
   `&self` 方法 `status_of`，E0502。修复：把进度快照构造改成
   模块级自由函数 `status_of(&Key, &Pending)`。
2. **编译错误：跨线程生命周期**
   server 最初用栈上 `Mutex<Reassembler>` 给 `thread::spawn` 借引用，
   E0597。修复：改为 `Arc<Mutex<_>>`，每连接 clone 一个 Arc。
3. **编译错误：`Ipv4Addr: From<(u8,u8,u8,u8)>` 不存在**
   测试里误用元组 `From`。修复：用 `Ipv4Addr::from([a,b,c,d])`。
4. **文档测试参数写错**
   早期 lib 文档示例把 4 个地址八位组平铺成 8 个参数，且
   `try_parse(Reader::read_u32_be)` 触发高阶函数生命周期 HRTL 错误。
   修复：地址写成元组、闭包写成 `|r| r.read_u32_be()`。
5. **3 个测试构造出“非法分片几何”**（`duplicate_fragments_are_idempotent`、
   `overlapping_fragments_drop_whole_datagram`、
   `identical_partial_overlap_is_still_rejected`）：
   非尾片长度/偏移不是 8 的倍数，被 IPv4 解析器按 RFC 正确拒绝
   （`fragment offset must be a multiple of 8` /
   `non-final fragment payload length … not a multiple of 8`）。
   这是**测试数据错误、产品代码行为正确**；把片长/偏移改为 8 对齐后通过。
6. **1 个测试 TTL 边界算错**（`ttl_refresh_keeps_busy_group_alive`）：
   最后一片在 t=999，TTL=1000，到期点应为 1999；原测试在 1999 断言存活。
   修正断言为 1998 存活、1999 清理。
7. **运行脚本问题（非 Rust）**：`nc` 不带 `-N` 时等待服务端关连接，
   导致 `examples/client.sh` 挂住；已改为 `nc -N`（发完半关闭），
   并在 README 注明服务是长连接。Python 客户端不受影响。

## 7. 已知边界 / 未覆盖项（如实）

- 只实现 IPv4；未实现 IPv6 分片扩展头（任务也只要求 IPv4）。
- 重叠策略仅 `Reject`；枚举已预留 RFC 5722“覆盖”策略但**未实现**。
- IPv4 头校验和错误当前**不硬拒绝**（在 `checksum_valid` 报告），
  这是有意的设计选择；若上层要求可在引擎配置中再收紧。
- TCP 服务为测试用途：每连接一线程、无认证、只绑 `127.0.0.1`。
- 不抓包、不使用原始套接字；无任何前端代码。
