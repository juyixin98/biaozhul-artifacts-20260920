# 节点区间同步器（Node Range Syncer）

纯后端的区块链区间同步服务：从多个可故障的本地测试节点**并行拉取**分段区块，
逐块做**真实密码学校验**（SHA-256 哈希链 + Ed25519 签名），只有
**连续校验通过的前缀**才能写入 SQLite 并推进检查点。支持检查点续传、
坏段换源重试、乱序有界缓存、取消竞争安全与重启恢复。

- 语言/框架：Go 1.22、gRPC（protobuf）、SQLite（`modernc.org/sqlite`，纯 Go，无需 cgo）
- 依赖已在 `go.mod`/`go.sum` 锁定，并通过 `vendor/` 提供完全离线可复现构建

---

## 1. 信任模型与安全性质

同步器**不信任任何远端节点声明的数据本身**，只信任两样东西：

1. 带外提供的**可信样例**（`examples/fixture.json` 中的 `trusted_sample`）：
   若干检查点高度对应的区块哈希，以及样例链尖。
2. 编译进程序的 **Ed25519 签发者公钥**（测试签发者 seed 固定且公开，仅用于本地测试）。

区块结构与校验（全部真实执行，见 `internal/chain/chain.go`）：

```
payload_hash = SHA256(payload)
block_hash   = SHA256( height(8B big-endian) || parent_hash(32B) || payload_hash(32B) )
signature    = Ed25519.Sign(issuer_privkey, block_hash)

校验：
  ① Ed25519.Verify(issuer_pubkey, block_hash, signature)   —— 防 payload 篡改 / 伪造签名
  ② block.parent_hash == SHA256(前驱块承诺)                 —— 防错误父哈希 / 断链
  ③ 检查点 block_hash == trusted_sample[height]             —— 防整条替代链
```

据此保证的性质：

| # | 性质 | 实现位置 |
|---|------|----------|
| 1 | **只有连续校验通过的前缀可发布**；缺口之后的区块即使拉回且验签通过，也只能停在内存缓存，绝不写库、绝不推进检查点 | `syncer.drainCache` + `storage.AppendVerifiedSegment`（库内二次连续性检查） |
| 2 | **远端宣称高度 ≠ 已验证进度**。`ChainTip` 仅用于展示/告警；同步目标以可信样例链尖为准，虚高高度还会被真实探测取证 | `queryAdvertisedTips` + 虚高探测 |
| 3 | **并行拉取最多 4 段**（信号量严格限流） | `engine.sem`，容量 `MaxParallel` |
| 4 | **乱序结果有界缓存**（默认 8 段），调度窗口随检查点滑动，绝不无界增长 | `spawnWindow`，窗口 `[next, next+WindowSegs*segSize)` |
| 5 | **坏段换源重试，不能跳过缺口**。超时 / gRPC 错误 / 签名失败 / 父哈希错误 / 短段均计入尝试次数；所有源耗尽则失败，检查点停在缺口前 | `pickPeer` 轮换 + `ErrExhausted` |
| 6 | **取消后旧请求结果不得推进检查点**。所有 DB 写入只发生在主 goroutine；取消后主循环不再接收结果，worker 结果写入带缓冲 channel 后被丢弃 | `engine.loop` 的单一写者模型 |
| 7 | **重启续传**：启动时从 SQLite 读链尖，并对所有已到达检查点复核样例哈希 | `recoverFromStore` |

持久化：SQLite（WAL，`synchronous=FULL`，单写连接），区块、节点宣称状态、
完整**来源证据审计日志**（哪一段被谁服务/被谁以什么原因拒绝/检查点何时推进）全部落盘。

---

## 2. 目录结构

```
.
├── proto/syncpb/sync.proto        # gRPC 协议：ChainTip（宣称）/ FetchSegments（区间拉取）
├── internal/
│   ├── chain/                     # SHA-256 + Ed25519、链式校验、确定性测试链、夹具与样例
│   ├── storage/                   # SQLite：已验证区块 / 检查点 / 证据日志 / 节点宣称状态
│   ├── harness/                   # 可注入故障的桩节点（真实 gRPC server）与客户端
│   └── syncer/                    # 同步引擎（并行窗口、有界乱序缓存、换源、取消、报告）
├── cmd/
│   ├── genfixture/                # 生成确定性诚实链 + 可信样例 JSON
│   ├── stubnode/                  # 启动单个可故障注入的桩节点
│   └── syncer/                    # 同步执行器（输出文本摘要 + JSON 报告）
├── examples/fixture.json          # 示例输入：64 个区块（高度 0..63）+ 检查点 0/16/32/48/63
├── script/                        # 三个端到端验收演示
├── vendor/                        # 锁定依赖（离线可复现）
└── Makefile
```

---

## 3. 快速开始（本地启动）

要求：Go 1.22+（无需 cgo、无需系统 SQLite）。

```bash
# 1) 生成示例输入（已随仓库提供 examples/fixture.json，可重新生成）
make fixture

# 2) 启动三个故障桩节点（三个终端，或用演示脚本自动管理）
go run ./cmd/stubnode -id node-a -addr 127.0.0.1:50051 \
    -timeout 0:8 -timeout-ms 3000 -corrupt-payload 24:32 -errors 48:56 -inflate-tip 10
go run ./cmd/stubnode -id node-b -addr 127.0.0.1:50052 \
    -corrupt-parent 8:16 -short 32:40 -timeout 56:64 -timeout-ms 3000
go run ./cmd/stubnode -id node-c -addr 127.0.0.1:50053 \
    -timeout 40:48 -timeout-ms 3000

# 3) 运行同步器
rm -f run/sync.db*
go run ./cmd/syncer -fixture examples/fixture.json -db run/sync.db -report run/report.json \
    -peer node-a=127.0.0.1:50051 \
    -peer node-b=127.0.0.1:50052 \
    -peer node-c=127.0.0.1:50053
```

桩节点故障开关（区间均为半开 `start:end`，可逗号分隔多个）：

| 参数 | 含义 |
|------|------|
| `-corrupt-payload 24:32` | 段内中间区块 payload 被篡改（哈希承诺 + Ed25519 签名失败） |
| `-corrupt-parent 8:16` | 段内中间区块父哈希置错并用真实私钥重签（签名有效但链断裂） |
| `-timeout 0:8 -timeout-ms 3000` | 区间请求服务端挂起 3s（触发客户端超时） |
| `-errors 48:56` | 区间请求直接返回 gRPC Internal 错误 |
| `-short 32:40` | 区间只返回 1 个区块（短段故障） |
| `-inflate-tip 10` | `ChainTip` 宣称高度虚高 10（实际不持有这些高度） |
| `-invalid-tip-hash` | 宣称链尖哈希填全 0 |

---

## 4. 验收命令

```bash
# 自动化测试（13 个：6 个密码学单元测试 + 7 个端到端场景）
make test

# 竞态检测（取消竞争相关 goroutine 路径）
make test-race

# 三个端到端演示（自动启停节点，断言退出码与链摘要）
make demo1   # 全故障综合：中段损坏/错误父哈希/短段/超时/gRPC错误/虚高高度 → 换源后与样例一致
make demo2   # 取消竞争：取消后等旧请求返回确认检查点不动，再用同一 DB 重启续传完成
make demo3   # 不可填缺口：三源全坏 → 失败被如实报告，0..23 连续前缀保留、缺口后不入库
```

### 演示 1 的预期关键输出

```
已验证检查点高度 : 63
链尖与样例一致   : true
自高度0连续无缺口: true
节点宣称最大高度 : 73 （仅参考，非已验证进度）
成功拉取段数     : 8  已提交段数: 8  被拒段数: 7
-- 区块来源分布（来源证据）--
  node-a: 8 个区块 / node-b: 24 个区块 / node-c: 32 个区块
```

7 次拒绝覆盖了所有注入故障（可在 JSON 报告 `evidence` 中逐条查看，
包含 `签名校验失败`、`父哈希错误`、`短段`、`DeadlineExceeded`、`Internal`）。

### 退出码约定

- `0`：到达目标且链尖与可信样例一致
- `2`：到达目标前结束（取消 / 部分前缀已提交 / 缺口不可填），连续前缀已持久化，可重跑续传
- `1`：硬错误（无任何进展、IO 错误等）

---

## 5. 自动化测试覆盖的场景

| 测试 | 验证内容 |
|------|----------|
| `TestFullSyncWithAllFaults` | 六类故障同时存在：换源重试、最终逐哈希一致、来源证据、虚高告警、拒绝原因齐全 |
| `TestParallelismCap` | 任一节点观测到的并发在途请求 ≤ 4 |
| `TestGapCannotBeSkipped` | 三源对同一区间全坏 → `ErrExhausted`；检查点停 23；24..63 一律未入库 |
| `TestCancelSafety` | 取消后等待在途慢请求（3s）全部返回，检查点不被推进；再运行从检查点续传完成 |
| `TestRestartAcrossStoreReopen` | 关闭并重开 SQLite（模拟进程重启），先到 31 再续传到 63 |
| `TestTamperedTrustedSampleRejected` | 可信样例检查点哈希被污染时立即失败，绝不用远端数据"自证" |
| `TestBoundedOutOfOrderCache` | 前段卡住时乱序后段进入缓存，缓存段数永远不超过窗口（3 段） |
| `TestTamperedPayloadRejected` 等 6 个 | SHA-256 承诺、Ed25519 验签、攻击者密钥、错误父哈希可区分性 |

---

## 6. 同步算法简述

1. **恢复**：读 SQLite 已提交链尖 `tip`，对 `≤ tip` 的样例检查点逐一复核哈希；从 `tip+1` 开始。
2. **询问宣称高度**：记录每个节点的 `ChainTip`（证据 + 展示），对超过样例链尖的宣称做一次真实拉取探测（预期失败）。
3. **有界并行调度**：段按固定大小对齐。窗口 `[next, next+W*size)` 内、未缓存、未在途、
   未耗尽重试的段，经容量 4 的信号量启动 goroutine 拉取；首轮源按段索引轮换，
   第 k 次失败轮换到第 k 个下家源。
4. **段内校验（并行，在 worker 中）**：非空、段首对齐、高度连续、逐块 Ed25519 验签、
   段内父哈希链、长度（非尾段短段即故障）。通过才把结果投递给主循环。
5. **乱序缓存与连续提交（仅主 goroutine 写库）**：结果按段起点入缓存；
   从 `next` 开始逐段做"相对已提交链"的最终链接校验，命中缺口立即停止；
   提交后立即复核样例检查点。缓存只装窗口内的段，天然有界。
6. **完成/失败**：连续前缀到达样例链尖 → 复核链尖哈希并完成；
   某段所有源都失败且无在途请求 → 返回 `ErrExhausted`，前缀保留；
   context 取消 → 停止消费结果、等待 worker 退出，返回已提交前缀。

---

## 7. 输出与证据

- 终端：人可读最终链摘要（检查点比对、来源分布、宣称状态、警告）。
- JSON 报告（`-report`）：
  - `verified_tip_hash` vs `trusted_sample_tip_hash`：最终链尖逐字节比对
  - `checkpoints[]`：每个检查点的样例哈希、实测哈希、是否一致、**来源节点**
  - `blocks_served_by_node`：每个节点贡献了多少区块
  - `evidence[]`：完整时间线（`peer_tip` / `advertised_height_probe` /
    `segment_received` / `segment_rejected`（含真实失败原因）/
    `checkpoint_advance` / `sync_complete` 等）
- SQLite：`blocks`（含 `node_id` 来源列）、`evidence`、`peer_status` 表。

故障如实报告原则：任何校验失败、超时、缺口不可填、样例不一致都会以明确错误信息
返回并记录证据，程序不会用远端数据静默"修复"分歧。

---

## 8. 复现说明

- 测试链是**确定性**生成的（固定 Ed25519 seed、payload 为 `block-<h>-payload`），
  任意机器上 `genfixture` 输出的哈希完全一致，例如高度 63 的链尖哈希恒为
  `e86962686cada05e35524d7b6e1a0c201800d33e6a213eeff9d930f1341108dd`。
- 离线构建：仓库含 `vendor/`，`go build -mod=vendor ./...` 与
  `go test -mod=vendor ./...` 无需网络。
- 重新生成 gRPC 桩代码（一般不需要）：`make proto`（需 protoc 3.21+、
  protoc-gen-go v1.33、protoc-gen-go-grpc v1.3）。
