# 账户状态快照裁剪（Account-State Snapshot Pruning）

用 **Go + Pebble + 纯 HTTP** 实现的本地账户历史状态库：每块保存**增量（delta）**，
定期构造**全量快照**；查询历史高度时从最近快照叠加增量得到状态摘要。构造快照期间
新块照常写入，快照绑定固定高度；只保留最近 3 份快照，且裁剪绝不删除活跃读者依赖的
增量；读者租约超时会**显式失败**；崩溃后能识别临时快照，缺文件的快照绝不会被列为可用。

纯后端，无前端。所有计算、协议编解码与密码操作（Ed25519 签名/验签、SHA-256 哈希、
二进制默克尔根）均真实执行，没有桩实现。

---

## 1. 它做了什么

| 需求 | 实现 |
|---|---|
| 每块保存增量 | 执行交易时记录每个账户的 **before/after 镜像**，随块原子写入 Pebble（一个 batch） |
| 定期全量快照 | 手动 `POST /v1/snapshots`，或 `-snapshot-every` 周期触发 |
| 指定高度查询 | 从「≤ 目标高度的最新快照」叠加 `(base, height]` 增量；无快照则从创世重放 |
| 构造期间继续写块 | 快照基于 **Pebble 一致性快照（point-in-time）**构建，全程只持读锁；追加块持写锁但不阻塞快照读取，新块不会进入本次快照 |
| 快照绑定固定高度 | 快照构建器在开始时拿到 Pebble 快照，只读该视图；提交前还用块头的 `state_root` 交叉校验 |
| 保留最近 3 份 | `POST /v1/prune` 保留最新 3 份，其余删除（受钉住保护的除外） |
| 不删活跃读者依赖 | 读者租约同时 **pin 快照文件**（引用计数）与 **delta 区间**（裁剪下界）；活跃租约会把 delta 删除上界压到 `base-1`，base=0 的创世租约完全禁止删除 |
| 读者超时显式失败 | 租约有 TTL；过期查询返回 **HTTP 410 `lease_expired`**，被裁剪的历史返回 **410 `history_pruned`** |
| 崩溃识别临时快照 | 临时目录名为 `.building-H<height>-<id>`，扫描时直接忽略；发布用「目录 rename + fsync」原子完成 |
| 缺文件快照不可用 | 重启扫描时校验：文件存在、checksum、内部校验和、**Ed25519 清单签名**、state_root/total_supply 重算；任何不符 → 隔离为 `.invalid-*`，绝不列出 |
| 并发/中断/竞争/重放一致性 | 见 §6 测试 |

---

## 2. 目录结构

```
.
├── go.mod / go.sum            # 锁定依赖（cockroachdb/pebble v1.1.5）
├── cmd/
│   ├── snapd/main.go          # HTTP 服务守护进程
│   └── snapdemo/main.go       # 确定性演示/验收客户端
├── internal/
│   ├── types/                 # 地址/哈希/交易/区块/增量的规范定长编码、默克尔根
│   ├── crypto/                # Ed25519 交易签名、节点密钥、快照清单签名（真实密码学）
│   ├── state/                 # 交易执行、费用燃烧、状态根、增量叠加
│   ├── store/                 # Pebble 块/delta/元数据存储 + point-in-time 读视图
│   ├── snapshot/              # 快照文件格式、原子提交、崩溃识别、签名校验、引用计数
│   ├── engine/                # 追加、快照叠加查询、租约、构建与裁剪协调
│   │   └── enginetest/        # 端到端测试（并发/中断/竞争/重放）
│   ├── replay/                # 独立重放对照（重放 vs 块头 vs 查询路径 vs 快照文件）
│   └── server/                # JSON HTTP API
├── examples/
│   ├── genesis.json           # 示例创世（alice/bob/carol 各 1,000,000）
│   └── proposals.jsonl        # 12 个已签名区块提案
└── scripts/acceptance.sh      # 一键构建+测试+端到端验收
```

---

## 3. 本地启动

前置：Go 1.22+（在 1.22.2 验证）、gcc（Pebble 使用 cgo/zstd）。

```bash
# 1) 编译
go build -o /tmp/snapd ./cmd/snapd
go build -o /tmp/snapdemo ./cmd/snapdemo

# 2) 生成确定性示例创世与 12 个已签名提案
/tmp/snapdemo demoinit examples/genesis.json
/tmp/snapdemo genblocks examples/proposals.jsonl 12

# 3) 启动（首次需 -genesis；-snapshot-every 可开周期快照）
/tmp/snapd -addr 127.0.0.1:8080 -data ./data \
  -genesis examples/genesis.json -lease-ttl 5s -snapshot-every 30s

# 4) 灌入区块（另一终端）
/tmp/snapdemo replay http://127.0.0.1:8080 examples/proposals.jsonl
```

启动参数：

| 参数 | 默认 | 说明 |
|---|---|---|
| `-addr` | `127.0.0.1:8080` | 监听地址 |
| `-data` | `./data` | 数据目录（Pebble、`snapshots/`、`node_key.json`） |
| `-genesis` | 空 | 首次启动的创世文件 |
| `-chain-id` | `demo-1` | 链 ID（须与创世一致） |
| `-lease-ttl` | `5s` | 读者租约 TTL |
| `-snapshot-every` | `0` | 周期性快照+裁剪间隔，0 表示仅手动 |

---

## 4. HTTP API

历史状态查询**必须**先创建显式读者租约。

| 方法与路径 | 作用 |
|---|---|
| `GET  /healthz` | 健康检查、当前 tip |
| `GET  /v1/info` | 链 ID、tip、节点公钥、创世哈希、快照列表 |
| `POST /v1/blocks` | 提交下一块（验签、链连接、执行、原子落盘） |
| `GET  /v1/blocks/{h}` | 取区块（含块哈希、状态根、交易根） |
| `POST /v1/leases` | 创建读者租约，body `{"height":N}` |
| `GET  /v1/leases` | 活跃租约列表 |
| `GET  /v1/leases/{id}` | 查租约 |
| `DELETE /v1/leases/{id}` | 提前释放租约（解除 pin） |
| `GET  /v1/state?lease={id}&account={0x..}` | 租约下查询状态摘要（可聚焦单账户） |
| `GET  /v1/snapshots` | 可用快照、保留数、崩溃临时目录 |
| `POST /v1/snapshots` | 在 `{"height":N}` 构造全量快照 |
| `DELETE /v1/snapshots/{h}` | 删除快照（有活跃读者时 409） |
| `POST /v1/prune` | 保留 3 份 + delta 裁剪（返回被谁阻塞） |
| `POST /v1/recover` | 清理崩溃残留 `.building-*` |
| `POST /v1/replay` | 独立重放对照（签名/交易根/状态根/查询路径/快照文件） |

典型调用：

```bash
B=http://127.0.0.1:8080

# 在高度 8 建快照，在高度 10 查询
curl -s -X POST $B/v1/snapshots -d '{"height":8}'
L=$(curl -s -X POST $B/v1/leases -d '{"height":10}')
ID=$(echo "$L" | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
curl -s "$B/v1/state?lease=$ID"

# 裁剪并查看报告
curl -s -X POST $B/v1/prune

# 独立重放对照（matches=true 才一致）
curl -s -X POST $B/v1/replay
```

错误码（部分）：`lease_expired`(410)、`history_pruned`(410)、`snapshot_busy`(409)、
`snapshot_exists`(409)、`snapshot_pinned`(409)、`bad_height`(400)、`invalid`(400, 含验签失败)。

---

## 5. 关键设计与一致性论证

**为什么查询能拿到一致状态。** 查询从一个已落盘并通过签名/校验和验证的全量快照
（或创世）出发，按高度顺序叠加该区间内每个 delta。delta 只记录账户的 after 镜像，
按序应用即重现执行结果。若区间内任一 delta 缺失，立即返回 `history_pruned`，不会
返回一个"部分正确"的状态。

**为什么构造快照不影响写块、且不会串高度。** `BuildSnapshot` 首先取一个 Pebble
一致性快照（`db.NewSnapshot()`），之后全部读取都走这个只读视图；它不需要长时间持锁。
与此同时 `AppendProposal` 向 live DB 提交的块对该视图不可见。提交前，构建器重算出的
`state_root` 必须等于绑定高度块头里已提交的 `state_root`，否则拒绝发布。

**原子发布与崩溃恢复。** `state.dat` 与 `manifest.json` 先写入临时目录
`.building-*`（各自 temp-file + rename + fsync），再对目录做一次 `rename` 并 fsync
父目录。因此读者只会看到「两文件齐备」或「完全不存在」。重启扫描忽略所有
`.building-*`；`POST /v1/recover` 清理它们。对已发布快照，启动时重新校验，缺文件、
checksum 不符、签名不符、重算 state_root 不符的目录被改名隔离为 `.invalid-*`，
不会出现在可用列表中。

**快照完整性。** `manifest.json` 含 `state.dat` 的 SHA-256，`state.dat` 自身尾部带
SHA-256 校验和，整个清单由节点 Ed25519 密钥签名（`node_key.json`，0600 权限，首次随机
生成）。换节点密钥或链 ID 的快照会被拒绝。

**裁剪与读者保护。** 租约在一把 RWMutex 的读锁内完成「选 base → 校验 delta 连续 →
pin 快照 → 注册租约」，与持写锁的 `Prune` 互斥，因此校验通过的数据不可能在租约
落库前被删。裁剪时：
- 快照层删除超过最新 3 份、且引用计数为 0 的快照；有 pin 的进入 `protected_by_readers`；
- delta 删除上界 = min(最老保留快照 − 1, 各活跃租约 `base − 1`)；base=0 的创世
  租约令上界为 0（完全不删）。

**租约超时。** 每次查询比对当前时间与 `expires_at`，过期立即返回 410 并释放其 pin；
`Prune` 也会清扫过期租约。默认 TTL 5 秒，可用 `-lease-ttl` 调整。

**重放对照（三重一致性）。** `replay` 从创世独立重新执行每个区块：
1. 重算 `state_root` 对照每个块头；
2. 重算交易默克尔根对照块头，并逐笔重新 Ed25519 验签；
3. 在快照高度，对照「快照+delta 查询路径」结果与「直接加载快照文件」结果。
任何分歧都会在 `mismatches` 中列出高度与类型，HTTP 返回 409。

---

## 6. 自动化测试

```bash
# 全部单元/集成测试（含 -race）
go test -race -count=1 ./...

# 一键端到端验收（构建 + 竞态测试 + 起服务 + 所有需求场景）
./scripts/acceptance.sh
```

测试覆盖（`internal/engine/enginetest/engine_test.go` 等）：

- **并发历史查询**：8 个读者在不同高度反复建租约/查询，同时构建快照与裁剪，
  每次结果都与独立重放的根一致（`-race` 下运行）。
- **快照中断**：注入「提交前崩溃」，断言只留下 `.building-*`、不被列出；
  `Recover` 后可重建。
- **裁剪竞争**：一个 goroutine 持续追加块，另两个分别狂建快照/狂裁剪，
  最终裁剪收敛到 ≤3，无死锁/panic。
- **活跃读者保护**：旧快照上的租约钉住快照与 delta 区间，裁剪无法删除；
  释放租约后才被回收。手动删除被钉住的快照返回错误。
- **创世租约**：base=0 的租约完全禁止 delta 裁剪。
- **租约超时**：推进假时钟后查询返回 `ErrLeaseExpired`；过期后裁剪清扫 pin。
- **缺文件 / 篡改快照**：删掉 `state.dat` 或翻转字节，重启后隔离，绝不列出。
- **真实密码学**：签名者地址不符、翻转签名位均被拒绝；快照清单签名被独立校验。
- **HTTP 语义**：无租约 400、过期 410、裁剪历史 410、重复快照 409、坏签名 400。
- **重启持久化**：关闭再打开后快照仍可用、重放仍一致。

---

## 7. 数据与文件格式

Pebble 键布局（高度大端，天然按时间有序）：

```
"b" || height(8) -> JSON 区块（头 + 交易）
"d" || height(8) -> JSON 增量（before/after 镜像）
"m" || name      -> 元数据（tip、genesis、chain_id）
```

区块、增量、tip 在一个 `Batch` 内 `Sync` 提交，三者原子可见。

快照 `state.dat`：

```
"SPS1"(4) || count(8) || [ address(20) || nonce(8) || balance(8) ] * count || sha256(前述全部)(32)
```

`manifest.json`：高度、ID、chain_id、账户数、state_root、total_supply、state.dat 的
SHA-256、节点公钥与对其余字段规范 JSON 的 Ed25519 签名。

---

## 8. 演示密钥说明

`snapdemo` 的 `demoinit/genblocks/transfer` 使用由种子字符串
（`alice`/`bob`/`carol`）经 SHA-256 派生的 Ed25519 密钥，**仅为让示例可复现，不提供
任何熵，绝不可用于真实场景**。生产请用随机账户密钥；节点密钥 `node_key.json` 始终
使用 `crypto/rand` 随机生成。
