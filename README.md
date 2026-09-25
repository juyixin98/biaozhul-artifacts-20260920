# ipfrag — 离线 IPv4 分片重组（纯后端）

用 Rust 从零实现的 **增量字节解析库** 与 **本地 TCP 测试服务**，完成离线
IPv4 分片重组。**不使用原始套接字、不需要任何系统权限**；重组器只消费
已经解析好的报文，因此可以在任意离线/沙箱环境运行。

刻意约束（对应任务要求）：

- **纯 Rust 标准库，零第三方依赖**（`Cargo.toml` 的 `[dependencies]` 为空）；
- IPv4 头解析、字节流解析、JSON、SHA-256 全部手写，
  **不使用 nom / pnet / etherparse / serde / sha2 等现成协议或解析库**代做核心；
- 不做前端，只提供 TCP JSONL 服务、命令行客户端与生成器。

---

## 1. 构建与测试

```bash
cargo build                      # 构建库与 3 个二进制
cargo test                       # 全部自动化测试（40 个：单元 + 协议场景 + 真实 TCP + doctest）
cargo clippy --all-targets       # 静态检查（0 warning）
cargo fmt                         # 格式化
./scripts/verify_e2e.sh           # 端到端：真实 TCP 重组 + Python 独立复算逐字节比对
```

工具链：Rust 1.98.1（stable），Linux x86_64。无网络构建（无依赖需要下载）。

## 2. 仓库结构

```
src/
  parser.rs       增量字节解析：ByteStream（feed/子集/长度上限/consume/compact）
                  + Cursor（大端整数、定界子集、bounded 读取）+ 明确 ParseError
  ipv4.rs         手写 IPv4 最小解析（version/IHL/total_length/id/flags-offset/
                  proto/src/dst/checksum/payload）与测试报文构造器 + RFC1071 校验和
  reassembly.rs   重组引擎：FlowKey 分组、重叠策略、TTL、内存预算（核心）
  sha256.rs       零依赖 SHA-256（载荷指纹，FIPS 180-4 向量自测）
  json.rs         零依赖最小 JSON（object/array/string/int/bool/null）
  server.rs       JSONL 协议处理器 + TCP 服务（127.0.0.1，每连接一线程）
  bin/
    ipfrag-server.rs   服务进程（CLI 配置 TTL/预算/重叠策略）
    ipfrag-client.rs   逐行发送 JSONL 请求并打印响应
    fraggen.rs         分片请求样例生成器（确定性载荷/乱序/packet_hex）
tests/
  protocol_scenarios.rs  协议层 10 个验收场景（确定性时钟，不开 socket）
  tcp_server.rs          真实 TCP 端到端 3 个测试（spawn 服务二进制）
examples/                可直接回放的 JSONL 请求样例
scripts/verify_e2e.sh    端到端逐字节校验脚本
RUNLOG.md                实际运行命令与结果的如实记录
```

## 3. 增量字节解析库（`parser`）

- `ByteStream::new(limit)`：长度上限（字节），`0` 表示不限；
- `feed(&[u8])`：增量追加，**超限时整段原子拒绝**（`LengthExceeded`，不部分写入）；
- `slice(start,len)` / `take_range`：取**子集**（借用视图 / 拷贝）；
- `bounded_subset` / `Cursor::take_bounded`：声明长度超过外层容器时返回 `BadLength`；
- `consume_n` + `compact`：消费前缀并回收物理空间，供“跨多次 feed 继续解析”；
- 明确、有限的错误类型：
  `Incomplete { need, have }` / `LengthExceeded { limit, attempted }` /
  `InvalidValue` / `BadLength` / `UnexpectedTag`。

IPv4 解析在 `parse_ipv4_slice` 上同时支持一次性 `parse_ipv4(&[u8])`
与增量 `parse_ipv4_inc(&ByteStream)`；报文未到齐返回 `Incomplete`，
可在追加数据后用同一缓冲重试。

## 4. 重组语义（`reassembly`）

### 分组键（RFC 791）

`FlowKey = (源地址, 目的地址, 协议号, Identification)`。

### 重叠片：明确的拒绝策略（不静默覆盖）

`OverlapPolicy` 二选一：

- `RejectNewFragment`（默认）：新片与已有数据相交，且不是“范围+内容完全相同的
  重复片”时，**拒绝新片并保留原组装**，返回 `ReassembleError::Overlap`；
- `DropAssembly`：一旦重叠，**丢弃整个组装**并立即把其字节归还内存预算。

另有：同范围不同内容 → `ConflictingData`；
两个末片声明的结束位置不一致 → `ConflictingLastFragment`。

**Identification 重用 vs 首片重传**（偏移 0 的片再次到达一个已有首片的未完成组装）：

- 范围与内容完全一致 → 判为重传，返回 `Duplicate`，组装续命；
- 否则 → 判为 ID 重用，丢弃旧组装（字节归还预算），以新首片重新开始，
  返回 `IdReuseReset { discarded_bytes }`。

### 寿命（TTL）

每条组装记录 `last_update_ms`；`now - last_update >= assembly_ttl_ms` 即过期。

- 显式：`purge_expired(now_ms)`（协议 `op=purge`）；
- 机会式：插入片时按 `purge_interval_ms` 自动全量清理；
- 时间戳由调用方注入，测试用确定性时钟；服务默认用系统 UNIX 毫秒时钟，
  请求也可用 `now_ms` 覆盖（自动化测试 TTL 时不依赖真实 sleep）。

### 内存预算（全部硬上限，超限即拒绝、绝无无界缓冲）

| 配置 | 默认 | 含义 |
|---|---|---|
| `mem_budget_bytes` | 4 MiB | 所有未完成组装负载字节总和 |
| `max_datagram_bytes` | 65535 | 单个重组数据报最大字节 |
| `max_assemblies` | 1024 | 同时存在的组装数量 |
| `assembly_ttl_ms` | 30000 | 组装寿命 |
| `purge_interval_ms` | 1000 | 插入时自动清理间隔 |
| `overlap_policy` | reject_new_fragment | 重叠策略 |

完成后组装立即移除并归还字节；未分片报文（offset=0, MF=0）直接以 `Completed` 透传。

## 5. TCP 测试服务（JSONL）

只监听 **127.0.0.1**（无特权、无 raw socket）。每条 TCP 连接上按
“一行一个 JSON 请求 → 一行一个 JSON 响应”通信；非法 JSON 返回
`{"ok":false,"error":"bad_json",...}` 且**不会断开连接**。

### 启动

```bash
./target/debug/ipfrag-server --port 9000 \
    --ttl-ms 30000 --mem-budget 4194304 --max-datagram 65535 \
    --max-assemblies 1024 --overlap reject_new_fragment
# --port 0 时由系统分配端口，实际端口打印在 stderr
```

### 请求

| op | 说明 | 关键字段 |
|---|---|---|
| `ping` | 连通性 | — |
| `frag` | 提交一个片 | 见下 |
| `status` | 未完成组装（可带四元组查单条） | `now_ms` 可选 |
| `purge` | 立即清理过期组装 | `now_ms` |
| `stats` | 运行计数器 | — |
| `config` | 当前生效配置 | — |

`frag` 支持两种输入：

```jsonc
// A) 显式字段
{"op":"frag","src":"10.0.0.1","dst":"10.0.0.2","protocol":17,"id":1,
 "offset":0,"mf":true,"payload_hex":"0102...","now_ms":0,"include_payload":false}

// B) 完整 IPv4 线格式报文（走手写 IPv4 解析器，校验头部校验和）
{"op":"frag","packet_hex":"4500001c....","include_payload":true}
```

`offset` 为字节单位，必须是 8 的倍数；`mf=true` 表示还有后续片，
其 `payload` 长度也必须是 8 的倍数（末片 `mf=false` 长度任意）。

### 响应事件与错误码

成功事件：`accepted` / `duplicate` / `id_reuse_reset` / `completed`。
`completed` 携带 `total_bytes`、`fragment_count`、`sha256`（完整数据报指纹），
`include_payload:true` 时额外回显 `payload_hex`。

错误（`ok:false`）：

| error code | 触发 |
|---|---|
| `overlap` | 片与已有数据相交 |
| `conflicting_data` | 同范围不同内容 |
| `conflicting_last_fragment` | 末片长度互相矛盾 |
| `datagram_too_large` | 超过单数据报上限 |
| `memory_budget_exceeded` | 超过全局内存预算 |
| `too_many_assemblies` | 组装数超限 |
| `bad_hex` / `bad_packet` / `bad_flow` / `bad_request` / `bad_json` / `unknown_op` | 输入问题 |

## 6. 快速上手（实际命令）

```bash
cargo build

# 终端 1
./target/debug/ipfrag-server --port 9055

# 终端 2：生成乱序分片并回放
./target/debug/fraggen --id 42 --mtu-payload 24 --length 100 \
    --seed 7 --order shuffle > /tmp/req.jsonl
./target/debug/ipfrag-client --port 9055 /tmp/req.jsonl | grep completed
# 生成器文件头里的 "# expected: ... sha256=..." 应与 completed 响应一致
```

预置样例（`examples/`）：

| 文件 | 场景 |
|---|---|
| `reassembly_forward.jsonl` / `_reverse` / `_shuffle` | 三种投递顺序，同一载荷相同指纹 |
| `reassembly_packet_hex.jsonl` | 原始 IPv4 线格式（经手写解析器） |
| `missing_first.jsonl` | 缺首片：保持 pending，不完成 |
| `overlap_rejected.jsonl` | 重叠被拒、原组装保留、相邻末片仍可完成 |
| `duplicate_and_id_reuse.jsonl` | 完全重复片 + ID 重用重置 |
| `timeout_purge.jsonl` | 注入时钟下的 TTL 清理（需短 TTL 启动） |
| `errors.jsonl` | 各类结构化错误，连接不断开 |

```bash
for f in examples/reassembly_*.jsonl; do ./target/debug/ipfrag-client --port 9055 "$f"; done
# TTL 演示（确定性时钟）
./target/debug/ipfrag-server --port 9056 --ttl-ms 500 --purge-interval-ms 3600000
./target/debug/ipfrag-client --port 9056 examples/timeout_purge.jsonl
```

## 7. 验收覆盖对照

| 验收点 | 单元测试 (`src/`) | 集成测试 (`tests/`) |
|---|---|---|
| 乱序 | `reassembly::tests::out_of_order_reassembly_matches_original` | `scenario_out_of_order_*`、`tcp_full_reassembly_*` |
| 重复片 | `duplicate_fragments_are_ignored` | `scenario_duplicate_fragment_is_idempotent` |
| 重叠（拒绝策略） | `overlapping_fragments_are_rejected_by_default`、`drop_assembly_policy_removes_state_on_overlap` | `scenario_overlapping_*`、`tcp_overlap_is_rejected_*` |
| 缺首片 | `missing_first_fragment_buffers_then_completes` | `scenario_missing_first_*` |
| ID 重用 | `id_reuse_resets_old_assembly` | `scenario_id_reuse_resets_old_assembly` |
| 超时清理 | `timeout_ttl_reclaims_assemblies` | `scenario_timeout_purge_reclaims_buffers` |
| 内存/长度上限 | `memory_budget_is_enforced`、`max_datagram_limit_is_enforced` | `scenario_memory_budget_and_datagram_limits_*` |
| 对照完整原始载荷 | `full_path_through_ipv4_parse_and_reassembly` | 端到端 sha256 + 逐字节（`verify_e2e.sh`） |

## 8. 明确的非目标与限制

- 不重组 IPv6（仅 IPv4）；不解析二层封装（输入是 IP 层数据报）；
- 不支持 IPv4 选项的语义解析（IHL>5 时选项计入头部、参与校验和，但不解释选项）；
- 不模拟 BSD“旧片优先”/RFC 5722“新片优先”的静默覆盖——重叠一律按配置显式拒绝；
- 测试服务为每连接一个 OS 线程，面向功能测试而非高性能压测；
- 时钟单位为毫秒单调语义；请求注入的 `now_ms` 倒退会被忽略（不清理）。
