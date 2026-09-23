# forkindexer — 分叉账本索引器（Fork Ledger Indexer）

纯后端服务：从本地 JSON 区块流建立**交易索引**与**余额索引**，正确处理
孤儿暂存、三层分叉、链重组（reorg）、重复投递与崩溃重启。
栈：**Go 1.22 + chi v5 + PostgreSQL（pgx v5，database/sql 接口）**。

> 范围声明：本项目只实现题目约定的简化模型（整数转账、累计权重、SHA-256 块哈希）。
> **不声称、也不兼容完整以太坊**（无 EVM、Gas、签名、叔块/ommmer、RLP、PoW 难度等）。

---

## 1. 模型契约（Model contract）

区块（block）：

```json
{
  "hash": "0x<64 hex>",
  "parent_hash": "0x<64 hex>",
  "height": 0,
  "transfers": [ {"from": "0x<40 hex>", "to": "0x<40 hex>", "amount": 1000} ]
}
```

- **哈希是真实密码学运算**：`hash = "0x" + hex( SHA-256( canonicalJSON( body) ) )`。
  body 不含 `hash` 字段本身；canonical JSON 为紧凑、字段序固定的：
  `{"parent_hash":...,"height":N,"transfers":[{"from":...,"to":...,"amount":N},...]}`。
  代码见 `internal/domain/domain.go`（`CanonicalBody` / `BlockHash` / `Normalize`）。
- 创世块：`height=0` 且 `parent_hash = 0x00…00`（64 个 0）。模型允许**唯一**创世。
- 其余块：`height == parent.height + 1`，父块必须存在。违反规则的块及其后代标记为 `rejected`。
- 金额为非负 **int64** 整数；账户即 40 位小写十六进制地址。
- **不做透支校验**：余额可以为负（建模约定，不是 bug）。自转（from==to）余额中性。
- **同哈希异内容拒绝**：哈希是内容的指纹；存储层对已存在哈希逐字节比较 `raw`，
  任何不一致返回 `same_hash_different_content`。

信封（envelope，带投递序列号的流）：

```json
{"sequence": 1, "block": { ...block... }}
```

`sequence` 从 1 起严格递增；`cursor` 记录已提交的最大序号。

### 分叉选择（真实执行，非模拟）

- 每个块权重 1，链**累计权重 = 链上块数**。
- 选择累计权重最大的链尖；**权重相同按链尖哈希字典序（小者胜）**。
- 缺父块先置 `staged`（孤儿暂存），父链补齐后自动级联连接。
- 换头时在**单个数据库事务**内：沿旧链回滚余额 → 沿新链重放余额 →
  重算 `in_chain` 标记 → 更新 `head` 与投递 `cursor`。因此重组期间查询
  不会读到半新半旧的混合状态（读事务用 `REPEATABLE READ` 快照）。

---

## 2. 目录结构

```
cmd/forkindexer/        CLI：serve / migrate / ingest / verify / gen-examples
internal/domain/       区块模型、规范化、SHA-256 哈希、int64 校验运算
internal/pgstore/      PostgreSQL 存储：暂存/连接/分叉选择/单事务重组/查询/重建校验
internal/httpserver/   chi 路由与 HTTP 处理器
internal/integration/   真实 PostgreSQL 集成测试（每测试独立 schema）
examples/              三层分叉示例流（哈希均为真实 SHA-256 计算）
scripts/smoke.py       HTTP 端到端验收脚本
```

---

## 3. 本地启动

需要：Go ≥ 1.22、PostgreSQL（本机已验证 16）。

```bash
# 1) 建库建角色（按你的环境调整；也可用已有超级用户）
sudo -u postgres psql -c "CREATE ROLE admin LOGIN PASSWORD 'forkindexer';"
sudo -u postgres createdb -O admin forkindexer
# 本机 peer socket 免密时默认 DSN 即可：
#   postgres:///forkindexer?host=/var/run/postgresql
# TCP/密码方式：
export DATABASE_URL='postgres://admin:forkindexer@localhost:5432/forkindexer?sslmode=disable'

# 2) 拉依赖（已锁定版本，见 go.mod / go.sum）
go mod download

# 3) 建表（serve 也会自动迁移）
go run ./cmd/forkindexer migrate

# 4) 启动
go run ./cmd/forkindexer serve -addr 127.0.0.1:8080
```

生成示例输入：

```bash
go run ./cmd/forkindexer gen-examples examples
# examples/stream1_three_forks.ndjson  三层分叉、孤儿乱序流
# examples/stream2_replay.ndjson        同样的流，用于重复投递验证
# examples/stream3_bare_seed.ndjson     裸块（无 sequence，不移动游标）
# examples/bad_*.json                     两种坏块样本
```

文件投递（NDJSON，每行一个信封或裸块；信封按 64 个一批进同一事务）：

```bash
go run ./cmd/forkindexer ingest examples/stream1_three_forks.ndjson
```

---

## 4. HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET  | `/healthz` | 数据库连通性 |
| GET  | `/v1/chain/head` | 当前链头、高度、累计权重、游标 |
| POST | `/v1/blocks` | 投递单个块（裸块）或 `{"sequence","block":{...}}` 信封 |
| POST | `/v1/envelopes` | 批量信封（JSON 数组，整批一个事务，原子） |
| GET  | `/v1/blocks/{hash}` | 区块详情，含 `status` 与 `in_chain` |
| GET  | `/v1/blocks/{hash}/transactions` | 该块内转账 |
| GET  | `/v1/chain/blocks?start_height=&limit=` | 规范链上的块 |
| GET  | `/v1/addresses/{addr}/balance` | 余额 + 所属链头 |
| GET  | `/v1/addresses/{addr}/transactions?limit=` | 该地址在规范链上的转账 |
| GET  | `/v1/verify` | 从创世重建并对账（见下） |

错误以 `{"code":..., "error":...}` 返回，典型状态码：
`409 same_hash_different_content / sequence_gap / sequence_reuse`，
`422 hash_mismatch / rejected`，`404 not_found`，`400 invalid_parameter`。

---

## 5. 验收命令

### 一键：Make

```bash
make deps       # go mod download
make migrate     # 建表
make examples    # 生成示例
make test        # 全部单测 + 真实 PG 集成测试
make serve       # 启动 HTTP
make smoke       # 端到端 HTTP 场景（需要服务在跑）
make verify      # 从创世块重建对账
make ingest      # make ingest F=examples/stream1_three_forks.ndjson
```

### 命令行验收（三层分叉 + 重复投递 + 重建相等）

```bash
# 干净库
sudo -u postgres psql -c 'DROP DATABASE IF EXISTS forkindexer' && \
sudo -u postgres createdb -O admin forkindexer

go run ./cmd/forkindexer ingest examples/stream1_three_forks.ndjson
go run ./cmd/forkindexer verify            # 增量 == 从创世重建 → ok:true

# 重复投递整段流：不得重复记账，游标不变
go run ./cmd/forkindexer ingest examples/stream2_replay.ndjson
go run ./cmd/forkindexer verify

# 坏块必须被拒绝（退出码非 0）
go run ./cmd/forkindexer ingest examples/bad_hash_mismatch.json || echo "rejected as expected"
```

预期最终规范链 `G-B1-B2-B3`，余额：
`alice=-1195  bob=1160  dave=35  carol=0`。

### HTTP 端到端

```bash
go run ./cmd/forkindexer serve -addr 127.0.0.1:8080 &
python3 scripts/smoke.py http://127.0.0.1:8080 examples/stream1_three_forks.ndjson
```

脚本覆盖：孤儿暂存、三级分叉、累计权重与哈希平局、重组后余额、
整流重放幂等、序列空洞/复用拒绝、坏哈希拒绝、未知块 404、重建对账。

### 自动化测试（真实 PostgreSQL，非 mock）

```bash
# 测试库（默认 DSN 见 internal/integration/helpers_test.go）
sudo -u postgres createdb -O admin forkindexer_test
go test ./...                       # 普通
go test -race ./...                # 竞态
FORKINDEXER_TEST_DSN='postgres://user:pass@localhost:5432/forkindexer_test?sslmode=disable' go test ./...
```

测试包含的关键不变量：

- `TestIncrementalEqualsRebuildAcrossForks`：乱序流上**每投递一个块**都断言
  增量余额与从创世独立重放完全一致，最后再整流重放。
- `TestCrashRestartDurability`：提交一半→关闭连接池（模拟进程退出）→冷启动，
  head/cursor/余额原样存活，再从持久化游标继续。
- `TestThreeForkCumulativeWeightAndReorg`：三层分叉、权重平局按哈希、B3 以权重 4 胜出。
- `TestReorgBalanceUndoThenApply`：单事务先撤销旧分支再应用新分支。
- `TestDuplicateBlockIsNotDoubleCounted` / `TestSequenceConflicts`：重复块不重复记账、
  序列空洞与同序列异块拒绝。
- `TestSameHashDifferentContentAtStore`：存储层逐字节内容指纹防御。
- `TestConcurrentReadsDuringReorgs`：重组期间并发读不报错、不混读。

### 从创世重建（验收 oracle）

`GET /v1/verify` 或 `forkindexer verify` 在单个 `REPEATABLE READ` 快照中：
递归遍历规范链、独立重放所有转账得到期望余额，与 `balances` 表全量比对；
同时核对 `in_chain` 标记集合恰为链头祖先、投递序号 1..cursor 连续。
任何偏差返回 `ok:false` 并列出差额（命令行以非 0 退出码结束）。

---

## 6. 关键设计与正确性说明

- **单事务原子性**：每次投递在一个事务里取 `pg_advisory_xact_lock`
  串行化写者，完成「插入→级联连接→选头→重组→余额→游标」。
  崩溃时事务回滚，绝不会出现游标前进而余额未改的撕裂状态。
- **重组读一致性**：所有读接口各自开 `REPEATABLE READ` 只读事务，
  读到的 head 与余额来自同一快照；重组提交前后分别是两个一致世界，不混读。
- **余额 int64 溢出检查**：链上重放与增量更新均用带边界判定的加减法
  （`checkedAdd` / `domain.ApplyTransfer`），溢出直接拒绝该投递。
- **幂等**：信封 `sequence <= cursor` 且同哈希 → 直接返回 `already_known`，
  不插入、不记账、不动游标；同序列异块拒绝。裸块按哈希去重。
- **级联连接**：新块插入为 staged，随后循环提升所有「父已连接」的块，
  每次循环重新评估，非法根块（零父非零高、第二个创世、高度不连续）
  连同其暂存后代一并 reject，避免无限循环与漏处理。

## 7. 依赖（已锁定）

见 `go.mod` / `go.sum`：`github.com/go-chi/chi/v5 v5.1.0`、
`github.com/jackc/pgx/v5 v5.6.0`（均为兼容 Go 1.22 的锁定版本）。
