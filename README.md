# snapprune — 账户状态快照裁剪服务

纯后端服务：本地账户历史状态库（Pebble），每块保存增量（delta），定期构造全量快照；
查询历史高度时从最近快照叠加增量，返回状态摘要；保留最近 3 份快照并裁剪被覆盖的增量。

## 设计

**存储布局（Pebble key 空间）**

| Key | 内容 |
|---|---|
| `meta/head` | 当前已提交块高（8 字节大端） |
| `delta/<height>` | 每块增量 `{"height":N,"changes":{"acct":delta}}` |
| `snap/manifest` | 已提交快照清单（仅含完整快照） |
| `snap/seal/<height>` | 快照封印（账户数/总余额/SHA-256），与 manifest 同一 batch 原子提交 |
| `snap/data/<height>/<acct>` | 快照全量数据，每账户一个 key |

**核心机制**

- **增量写入**：`AppendBlock` 严格按 head+1 顺序提交，单 batch 落盘（WAL sync）。
- **快照构造**：绑定固定高度 H（默认当前 head）。构造期间新块正常写入 —— 增量不可变，
  构造只读 `delta ≤ H`。数据 key 先写入 `snap/data/H/`，最后用一个 batch 同时写
  manifest 条目和 seal —— 这是唯一的原子提交点。
- **崩溃恢复**：`Open` 时校验 manifest 每个条目：seal 缺失、账户数/总余额/哈希与磁盘
  数据不符 → 条目被丢弃并删除数据；`snap/data`、`snap/seal` 中不在 manifest 里的高度
  （构造中断留下的临时快照）→ 直接清除。缺文件/损坏的快照永远不会被列为可用。
- **历史查询**：取 ≤ h 的最新快照作为基线，叠加 `(base, h]` 的增量，返回
  `{accounts, total_balance, state_hash(SHA-256)}`。查询前注册**读者租约**（带截止时间），
  租约过期仍未读完 → 显式失败 `ErrReaderTimeout`（HTTP 408）。
- **裁剪**：仅保留最近 `keep`（默认 3）份快照；被快照覆盖的增量（`height ≤ floor`）删除。
  活跃读者 pin 住的快照及其所需增量**绝不删除**；读者租约过期后不再受保护。
  查询已裁剪高度 → 显式失败 `ErrHeightPruned`（HTTP 410）。

## 构建与启动

```bash
go build ./...                 # 依赖已锁定（go.mod / go.sum，pebble v1.1.5）
go build -o snapd ./cmd/snapd
./snapd -addr 127.0.0.1:8080 -db ./data -keep 3 -reader-ttl 10s -snapshot-interval 0
# -snapshot-interval N: 每 N 块自动构造快照；0 = 仅手动触发
```

## 验收命令

```bash
go vet ./...
go test ./...            # 全部一致性测试
go test -race ./...      # 竞态检测（并发查询/构造/裁剪）
```

自动化测试覆盖：

- `TestReplayConsistency` — 快照+增量重建与创世重放逐高度对照（哈希/总额/账户数）
- `TestConcurrentQueriesDuringSnapshot` — 8 路并发历史查询 × 快照构造 × 新块写入
- `TestSnapshotInterruption` — 构造中途崩溃，重启后临时快照被识别清除、不被列出
- `TestMissingSealNotListed` / `TestCorruptSnapshotDataNotListed` — 缺 seal / 数据损坏的快照不被列为可用
- `TestPruneKeepsThree` — 恰好保留 3 份快照，裁剪高度显式 410
- `TestPruneRespectsActiveReader` — 活跃读者 pin 住的快照与增量不被裁剪
- `TestReaderTimeout` — 读者租约超时显式失败
- `TestHTTPFlow` — HTTP 端到端（写入→快照→查询→裁剪→410/408）

## 示例运行

```bash
./snapd -db ./data &                    # 启动服务
./examples/demo.sh                      # 加载 examples/blocks.json 并演示全流程
```

## HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/v1/blocks` | 提交一块增量 `{"changes":{"alice":100}}` → `{"height":N}` |
| GET | `/v1/head` | 当前块高 |
| GET | `/v1/state/{height}` | 历史状态摘要；`?timeout_ms=` 覆盖读者租约（`0` = 立即超时） |
| GET | `/v1/state/{height}/accounts/{acct}` | 单账户历史余额 |
| POST | `/v1/snapshots/build` | 构造快照 `{"height":H}`（0 = 当前 head），幂等 |
| GET | `/v1/snapshots` | 列出可用（已提交且校验通过）快照 |
| POST | `/v1/prune` | 立即裁剪 |
| GET | `/healthz` | 健康检查 |

错误码：`400` 未来高度/非法输入，`408` 读者超时，`410` 高度已裁剪，`500` 内部错误。
