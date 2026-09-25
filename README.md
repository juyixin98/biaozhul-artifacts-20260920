# ipfrag —— 离线 IPv4 分片重组（纯后端，Rust，零三方依赖）

一个从零实现的 IPv4 分片重组项目，包含：

1. **手写增量字节解析库**（`byte_reader`）——明确支持**子集**、**长度上限**
   与**分类错误类型**，支持字节流随到随喂（incomplete 不消费、可重试）；
2. **手写 IPv4 头部解析/构造**（`ipv4`）——基于上面的解析库，逐字段
   解析 RFC 791 头部与选项，**不使用任何现成协议解析器**；
3. **离线分片重组引擎**（`reassembly`）——按
   `(源地址, 目的地址, 协议号, 标识 ID)` 四元组分组，显式的**重叠片
   拒绝策略**、**寿命（TTL）**与**内存预算**；
4. **本地 TCP 测试服务**（`server` + 二进制 `reasm-server`）——
   基于 `std::net`，换行分隔 JSON（NDJSON），只监听 `127.0.0.1`，
   **不使用原始套接字、不需要任何系统权限**。JSON 解析/序列化、
   hex 编解码均为手写。

> 纯后端，无任何前端代码。全部代码仅使用 Rust 标准库，
> 可完全离线构建（`--offline`）。

## 目录结构

```
Cargo.toml
src/
  lib.rs                 # 库入口与模块文档
  byte_reader.rs         # 手写增量字节解析库（子集/上限/错误类型）
  ipv4.rs                # 手写 IPv4 头部解析、构造、RFC1071 校验和
  reassembly.rs          # 重组引擎：分组/重叠拒绝/TTL/内存预算/ID 重用
  server.rs              # 本地 TCP 服务 + 手写 JSON + hex
  bin/reasm_server.rs    # 服务可执行入口
examples/
  byte_parser_demo.rs    # 解析库能力演示（子集/上限/错误/增量喂入）
  reassembly_demo.rs     # 离线重组演示（乱序/重复/缺首片/重叠）
  make_samples.rs        # 生成 NDJSON 请求样例
  requests.ndjson        # 已生成的请求样例（10 行）
  original_payload.hex   # 样例对应的 300 字节完整原始载荷（对照用）
  sample_responses.ndjson# 一次真实服务运行的响应留档
  client.py / client.sh  # 零依赖测试客户端
tests/
  acceptance.rs          # 六个验收场景的集成测试
README.md
RUNLOG.md                # 实际运行命令与结果（含过程中失败项，如实记录）
```

## 构建与测试

```bash
cargo build --offline            # 零三方依赖，可离线构建
cargo test  --offline            # 32 单元/文档测试 + 9 集成验收测试
cargo clippy --all-targets       # 0 告警
cargo build --release --offline
```

无需 `cargo fmt` 之外的任何工具链组件，无需 root/特权，无需网络。

## 快速上手

### 1. 库方式（离线重组）

```rust
use ipfrag::reassembly::{Reassembler, Config, AddResult};

let mut engine = Reassembler::new(Config::default());
let tail = ipfrag::ipv4::build_fragment(
    (192,168,0,1), (10,0,0,200), 17, 0x1234,
    1200, false, vec![0xBB; 83]);          // 尾片先到（乱序）
let head = ipfrag::ipv4::build_fragment(
    (192,168,0,1), (10,0,0,200), 17, 0x1234,
    0, true, vec![0xAA; 1200]);

assert!(engine.add_packet(&tail, 0).unwrap().is_pending());
let AddResult::Completed(d) = engine.add_packet(&head, 10).unwrap() else {
    panic!("still pending");
};
assert_eq!(d.payload.len(), 1283);          // 与完整原始载荷一致
```

两个可运行示例：

```bash
cargo run --example byte_parser_demo       # 解析库：子集/长度上限/错误/增量
cargo run --example reassembly_demo        # 重组：乱序/重复/缺首片/重叠拒绝
```

### 2. TCP 测试服务

```bash
cargo run -- --port 9000 --ttl-ms 30000 \
            --memory-budget 1048576 --max-payload 65535
# 或
./target/debug/reasm-server --help
```

协议为 **NDJSON**（每行一个请求 JSON，每行一个响应 JSON）。
请求字段：

| op | 字段 | 说明 |
|---|---|---|
| `fragment` | `src,dst,protocol,id,offset,mf,payload_hex[,df,ttl,now_ms]` | 逻辑分片 |
| `raw_packet` | `packet_hex[,now_ms]` | 完整 IPv4 报文（走手写 IPv4 解析器） |
| `purge` | `now_ms` | 显式清理过期组 |
| `status` | — | 引擎统计 |
| `reset` | — | 清空引擎 |

`offset` 以**字节**计（必须是 8 的倍数）；`payload_hex` 为无分隔十六进制。
`now_ms` 可省略（用系统时钟），给定则使用**虚拟时钟**——这让超时清理
可以被确定性地测试。

快速联通（另开终端）：

```bash
python3 examples/client.py --port 9000     # 自动对照重组结果与原始载荷
# 或
./examples/client.sh 9000
```

`fragment` 成功返回 `result:"pending"`；最后一片使数据报完整时返回
`result:"completed"` 并给出 `payload_hex` 与 `total_length`。
错误返回 `status:"error"` 与机器可读的 `error` 代码：
`parse_failed` / `invalid_fragment` / `overlap_conflict` /
`oversized_datagram` / `budget_exceeded` / `invalid_json` /
`invalid_hex` / `invalid_request` 等。

## 设计与语义（明确的策略，不做“隐式猜测”）

### 分组键

`(src, dst, protocol, identification)`，与 RFC 791 的分片归属粒度一致。
不同上层协议、不同地址对、不同 ID 的数据报互不干扰。

### 重叠片策略：`Reject`（当前唯一策略）

- **完全重复片**（偏移、长度、逐字节内容均相同）→ 幂等接受：
  不增加片数、不重复计内存，仅把该组寿命刷新到当前时间；
- **任何其它字节范围交叠**（即使交叠区内容恰好相同）→
  返回 `ReassemblyError::OverlapConflict`（服务端 `overlap_conflict`），
  并**整体丢弃该数据报**，释放其已缓存字节。

这是刻意保守的策略，避免重叠注入导致的歧义；
`OverlapPolicy` 为 `#[non_exhaustive]` 枚举，保留未来扩展
（例如 RFC 5722 的后到覆盖策略）的位置。

### 重组完成判定

片按偏移升序、互不相交地保存；必须**严丝合缝**覆盖
`[0, total)`（`total` 由 MF=0 的尾片确定）才完成。缺首片、中间有洞
都会一直 `pending`，并在状态中给出 `contiguous_from_zero` 与
`total_length`。

### 寿命（TTL）

每片到达都把该组过期时间刷新为 `now + fragment_ttl_ms`（默认 30s）。
清理是**惰性 + 显式**：每次 `add_*` 前自动清理过期组，
也可调用 `purge_expired(now)`（服务端 `purge`）。时间由调用方注入，
便于确定性测试。

### 内存预算

- `total_memory_budget`（默认 1 MiB）：全引擎分片缓存字节**硬上限**。
  超限的新片返回 `BudgetExceeded`，**不会**为腾地方淘汰其它数据报；
- `max_datagram_payload`（默认 65535）：单组长度上限，超限返回
  `OversizedDatagram`。被拒片不写入、不计费。

### ID 重用

四元组+ID 在旧数据报未完成时被新数据报重用，通过两种信号识别：
(1) 又来了一个偏移 0 的首片且内容/长度与已缓存首片不同；
(2) 新片越过了本组已知总长度。命中即把旧组整体丢弃、从本片重启。

### IPv4 解析严格性

物理截断 → `Ipv4Error::Truncated`（可喂更多字节后重试的语义）；
版本 ≠ 4、IHL < 5、Total Length < 头长、保留标志位置位、
DF 与非零偏移共存、非尾片载荷非 8 倍数等 → `Ipv4Error::Malformed`。
头部校验和错误只在 `checksum_valid` 中报告，不做硬拒绝
（重组层可以自行决定策略）。

## 验收场景与测试对应

| 验收项 | 库内测试 | 集成测试 |
|---|---|---|
| 乱序 | `reassembles_out_of_order` 等 | `acceptance_1_out_of_order_matches_original` |
| 重复片 | `duplicate_fragments_are_idempotent` | `acceptance_2_duplicates_are_idempotent` |
| 重叠 | `overlapping_fragments_drop_whole_datagram`、`identical_partial_overlap_is_still_rejected` | `acceptance_3_overlap_is_rejected_and_drops_group` |
| 缺首片 | `missing_first_fragment_never_completes` | `acceptance_4_missing_first_fragment_blocks_completion` |
| ID 重用 | `id_reuse_replaces_old_datagram`、`id_reuse_due_to_length_change` | `acceptance_5_id_reuse_resets_incomplete_group` |
| 超时清理 | `expired_groups_are_purged_lazily_and_explicitly`、`ttl_refresh_keeps_busy_group_alive` | `acceptance_6_timeout_cleanup_explicit_and_lazy` |
| 对照完整原始载荷 | `completed_payload_matches_full_original_for_various_sizes` | 各 `acceptance_*` 完成分支 + `examples/client.py` |
| 内存/长度上限 | `total_memory_budget_is_enforced`、`oversized_datagram_is_rejected` | `budget_and_oversize_limits_are_reported_distinctly` |
| TCP 端到端 | `server::tests::tcp_*`（真实 loopback 连接） | — |

实际运行命令、输出与过程中出现过的失败项（均已修复）见
**[RUNLOG.md](RUNLOG.md)**。

## 范围与非目标

- 仅 IPv4；不做 IPv6 分片头（Fragment Extension Header）。
- 离线重组：输入是报文字节或逻辑分片字段；**不抓包、不用原始套接字**。
- TCP 服务仅用于测试：单连接一线程、无认证、只绑 loopback。
- 手写 JSON 只支持协议所需的类型（整数/布尔/字符串/数组/对象/null），
  显式拒绝浮点。
