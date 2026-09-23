# 节点区间同步器 (Node Interval Synchronizer)

纯后端：Go + gRPC + SQLite。为本地测试节点做**区间（分段）同步**，按检查点续传，
只有**连续且密码学校验通过的前缀**会被发布；远端节点宣称的高度绝不等于已验证进度。

哈希运算用真实 **SHA-256**（`crypto/sha256`），块哈希基于长度前缀的确定性编码重算；
协议是真实的 **gRPC over HTTP/2**（`protoc` 生成），持久化是真实的 **SQLite**（CGO-free
驱动 `modernc.org/sqlite`）。没有任何桩计算或假校验——校验失败会如实报告并退出非零。

---

## 1. 它保证什么

| 需求 | 实现位置与做法 |
| --- | --- |
| 只发布连续校验通过的前缀 | `internal/syncer` 仅在 `frontier`（下一个未发布高度）处刷盘；`store.AppendVerified` 在同一事务里再次做 `chain.VerifyAppend` 并推进检查点。**缺口不可跳过。** |
| 检查点续传 | 检查点高度+哈希存于 SQLite `metadata`，与区块同事务提交。重启后从检查点继续，乱序数据只在内存，重启后重新拉取。 |
| 远端宣称高度不代表进度 | 目标高度与创世哈希来自**离线可信样例**；`Status.advertised_height` 只记录为证据，从不移动 `frontier`。三个节点分别虚报 `+7 / 0 / -2`。 |
| 并行拉取最多 4 段 | 固定大小 worker 池（`--parallel`，默认 4）；`TestParallelCap` 用计数器断言在飞数不超限。 |
| 乱序结果有界缓存 | 只调度紧贴 `frontier` 的窗口段（`frontier, frontier+seg, ...`），缓存上界 `O(parallel × segment)`，与链长无关。 |
| 坏段换源重试、不跳缺口 | 段失败按尝试轮换到不同节点（`failover` 计数）；某段在所有尝试都失败时整体停止于该缺口，缺口之后绝不发布。 |
| 错误父哈希（分叉） | 自洽分叉能通过“段内校验”，但在到达可信前沿做**锚点边界校验** `VerifyAppend(cpHash, frontier, seg)` 时被拒，记录 `parent_mismatch` 后换源。 |
| 取消后旧结果不得推进检查点 | 协调器单线程改状态；取数 goroutine 的 RPC context 是运行 context 的子级，取消后结果被丢弃（`stale_ignored`），绝不下发。`TestCancelDoesNotAdvanceCheckpoint` 在取消后 sleep 再断言检查点不移动，并能从该点续传。 |
| 记录来源证据 | 每次尝试（成功/超时/哈希不符/父哈希不符/形状错误/虚标/发布/取消后丢弃）都写入 SQLite `evidence` 表，并导出 `data/evidence.json`。 |
| 最终链摘要与可信样例一致 | `internal/verify` 重算整条已发布链并与样例的 tip/数量/digest 比对；摘要写 `data/summary.json`，不一致则退出码 1。 |

---

## 2. 目录结构

```
proto/nodesync/sync.proto   gRPC 协议（Status / GetSegment）
internal/
  chain/      SHA-256 哈希链、确定性编码、连续性/父哈希/自哈希校验
  gen/        确定性生成规范链（可信样例与桩节点共用同一份数据）
  sample/     离线可信样例（创世哈希、目标 tip、链 digest），加载时自校验
  pb/         protoc 生成代码（已提交，无需安装 protoc 即可构建）
  pbconv/     proto <-> domain 转换
  stub/       三个桩节点：分段区块 / 超时 / 中段损坏 / 错误父哈希分叉 / 虚报高度
  grpcnode/   真实 gRPC 客户端，适配 syncer.Client
  store/      SQLite：区块、检查点、证据；只接受连续已校验追加
  syncer/     同步引擎（并行窗口、有界乱序缓存、换源重试、取消安全）
  verify/     最终链摘要 + 与可信样例比对
  integration/ 真实 gRPC + 文件 SQLite 的端到端测试
cmd/
  genchain/   离线生成 testdata/trusted_sample.json
  node/       启动一个桩节点
  syncer/     运行同步，打印摘要、导出证据
testdata/     可信样例 + 三个节点行为配置（示例输入）
scripts/      proto_gen.sh / test.sh / run_nodes.sh / e2e.sh
vendor/       锁定依赖（go mod vendor），可离线构建
```

---

## 3. 协议

```proto
service NodeSync {
  rpc Status(StatusRequest) returns (StatusResponse);        // advertised_height 不可信
  rpc GetSegment(SegmentRequest) returns (SegmentResponse);  // 按 [start, start+limit) 取段
}
```

块哈希：`SHA256( BE(height) || len32(parent_hash)||parent_hash || BE(timestamp) || len32(body)||body )`。
长度前缀使编码无歧义；校验时**不信任线上来的 hash 字段，全部重算**。

---

## 4. 本地启动

要求：Go 1.22+（已在 1.22.2 验证）。依赖已 `vendor`，**无需联网、无需 CGO、无需 protoc**。

```bash
# 一键验收：生成样例 -> 起三个故障节点 -> 全新同步 -> 重启续传 -> 打印证据与结论
./scripts/e2e.sh
```

手动分步：

```bash
# 1) 生成离线可信样例（32 个非创世块，tip=32）
go run -mod=vendor ./cmd/genchain --length 32 --out testdata/trusted_sample.json

# 2) 终端 A：起三个桩节点（也可分开三个终端各跑一个）
./scripts/run_nodes.sh
#    alpha :50061  虚报 +7；对 9..16 段超时
#    beta  :50062  对 4..7 段返回被篡改的块（自哈希不符）
#    gamma :50063  虚报 -2；17 起返回错误父哈希的自洽分叉

# 3) 终端 B：全新同步
go run -mod=vendor ./cmd/syncer \
  --node alpha=127.0.0.1:50061 --node beta=127.0.0.1:50062 --node gamma=127.0.0.1:50063 \
  --db data/sync.db --segment 8 --parallel 4 --timeout 600ms --attempts 6

# 4) 再次运行同一条命令 -> 立即完成（checkpoint 32 -> 32，零拉取），演示续传
```

`--timeout` 必须短于桩节点的挂起时长，超时才会触发换源。

---

## 5. 自动化测试（验收命令）

```bash
# 全部单元测试 + 真实 gRPC/文件 SQLite 集成测试，开 -race
./scripts/test.sh
# 等价于：
go test -mod=vendor -race -count=1 ./...
```

覆盖：
- `internal/chain`：篡改检测、分叉边界拒绝、缺块/高度跳跃、哈希确定性。
- `internal/syncer`：损坏换源、超时换源、错误父哈希边界拒绝、并行上限、
  **持久缺口不跳过**、**取消竞争后检查点不推进且可续传**、检查点驱动续传、虚报高度被忽略。
- `internal/stub`：真实 gRPC（bufconn）下三类故障与虚报状态的行为。
- `internal/verify`：完整链匹配、部分前缀不匹配、样例文件被篡改时加载即失败。
- `internal/integration`：真实 TCP gRPC 三故障节点 + 文件 SQLite，全量同步 + 关闭重开后续传。

---

## 6. 一次真实运行的预期证据（`data/evidence.json`）

```
alpha  h1-8   ... (虚标 39，记录 advertised_mismatch 后忽略)
beta   h1-8   attempt=1 hash_mismatch   block hash mismatch (data tampered) at height 4
gamma  h1-8   published  ... up to 8          <- 换源修复
alpha  h9-16  attempt=1 timeout (DeadlineExceeded)
beta   h9-16  published  ... up to 16          <- 换源修复
gamma  h17-24 attempt=1 parent_mismatch trusted-anchor boundary check failed ...
alpha  h17-24 published ... up to 24           <- 自洽分叉被边界拒绝后换源
beta   h25-32 published ... up to 32
```

最终摘要 `data/summary.json`：`matches_sample=true`，`height 0..32`，digest 等于可信样例。

---

## 7. 重新生成 gRPC 代码（可选）

仓库已提交生成产物，仅在改 `.proto` 时需要。脚本会使用 `tools/` 下锁定版本的
`protoc 25.1` / `protoc-gen-go v1.34.2` / `protoc-gen-go-grpc v1.4.0`：

```bash
./scripts/proto_gen.sh
```

---

## 8. 失败如何如实报告

- 同步未达目标（存在修不好的缺口）或最终摘要与样例不一致：`cmd/syncer` 退出码 **1**，
  标准输出打印 `RESULT: INCOMPLETE/MISMATCH` 与原因，证据照常落盘。
- 任一档位查询全部失败、参数非法、样例自校验失败等：直接报错退出。
- 密码学/协议/SQLite 全部真实执行；测试用 `go test` 判定，不看日志“看起来成功”。
