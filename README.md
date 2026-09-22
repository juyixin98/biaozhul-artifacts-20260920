# 分叉账本索引器 (Fork Ledger Indexer)

面向链数据团队的纯后端服务：从**本地 JSON 区块流**建立交易与余额索引，正确处理
**缺父暂存、链分叉、按累计权重选链（平局按哈希）、一次性事务重组、崩溃重启与
重复投递**。

> **范围声明**：这是一个**约定模型**的教学/工程实现——区块只有
> `hash / parentHash / height / transactions`，交易是整数转账。它**不兼容完整
> 以太坊**（无签名、EVM、Gas、Merkle Patricia Trie、RLP 等）。所有哈希都是对
> 规范 JSON 原像真实执行的 **SHA-256**，所有金额都是**任意精度整数**（`big.Int`
> 以十进制字符串存储）。

## 技术栈

- **Go 1.22**
- **chi v5**（HTTP 路由）
- **PostgreSQL ≥ 13**（本仓库在 16 上开发）+ **pgx/v5**
- 无前端、无消息队列、无 ORM

## 它保证什么

| 需求 | 实现 |
|---|---|
| 缺父块暂存 | 父块未知先存为 `staged`；父链补全后连通性定点传播为 `connected` |
| 分叉按累计权重选择 | 权重 = 链长（每块权重 1）；最重链胜出 |
| 平局按哈希 | 等权重时，链头哈希字典序**更小**者胜出（确定性、无随机） |
| 一次事务撤销旧分支再应用新分支 | 单个 `SERIALIZABLE` 事务：撤销旧独占路径→应用新独占路径→改写 `canonical_*` 与 `balances`→更新游标与链头，**全部原子提交** |
| 游标与余额同步提交 | `chain_state(head_hash, ingest_seq, stream_offset)` 与块、余额在同一事务提交 |
| 重复块不重复记账 | 内容寻址（哈希主键）；同哈希同原像 = 幂等跳过，余额与 `received_seq` 不变 |
| 同哈希异内容拒绝 | 存储规范原像字节，重复投递逐字节比对，不同即拒绝（HTTP 409/400） |
| 查询返回所属链头 | 所有读操作在单个 `REPEATABLE READ` 快照事务内完成 |
| 重组期间不能混读 | MVCC 快照：读者要么看到重组前全貌、要么重组后全貌，绝不可能半新半旧；并以单事务探针校验 `head ↔ canonical_blocks ↔ 递归链深` 一致 |
| 增量结果 == 从创世块重建 | `verify/rebuild` 与 `indexer verify`：忽略维护态表，在内存中独立重放全部块并逐行比对 |
| 余额不透支 | 候选链重放时余额不足→该块及全部后代标记 `invalid`，永不成为规范链 |

写者之间通过事务级咨询锁串行化；锁以 `pg_try_advisory_xact_lock` 获取（从不在
过期快照上排队等待），写冲突（SQLSTATE 40001）与读快照失效都带退避重试。

## 目录结构

```
cmd/
  server/           HTTP API（forkindexerd）
  indexer/          流式摄入 / reset / verify / state（indexer）
  genfixtures/      生成 testdata/ 示例（真实 SHA-256）
  genlongstream/    生成 61 块流（崩溃/重启验收用）
internal/
  model/            区块模型、规范序列化、真实 SHA-256 哈希与校验
  store/            连接池与嵌入式幂等迁移（schema.sql）
  indexer/          暂存/连通性、选链、原子重组、独立重建、只读查询
  api/              chi 路由
scripts/
  acceptance.sh     端到端验收（暂存→平局→重组→重复→拒绝→崩溃重启→重建比对）
testdata/           NDJSON 示例输入、篡改样本、expected.json
```

## HTTP API

| 方法与路径 | 说明 |
|---|---|
| `GET  /healthz` | 健康检查 |
| `POST /v1/blocks` | 摄入：单个 JSON 对象、JSON 数组，或 NDJSON（`application/x-ndjson`）；可带 `?offset=N` 同步推进字节游标 |
| `GET  /v1/head` | 当前规范链头 |
| `GET  /v1/state` | 链头 + 摄入序号 + 流字节偏移 |
| `GET  /v1/blocks/{hash}` | 按哈希查任意块（含 `status`、`canonical`） |
| `GET  /v1/height/{n}` | 高度 n 处的规范块 |
| `GET  /v1/chain?from={hash}` | 链头（或指定规范块）回溯到创世的整条链 |
| `GET  /v1/accounts/{addr}/balance` | 规范链上的可用余额（未知账户为 `"0"`） |
| `GET  /v1/accounts/{addr}/transactions?limit=` | 该地址在规范链上的转账 |
| `POST /v1/verify/rebuild` | 独立从创世重建并比对；一致返回 200，不一致返回 417 |

错误：坏块/坏哈希 → `400`；同哈希异内容 → `409`；未知 → `404`。

## 本地启动

需要本机 PostgreSQL（Ubuntu 上 `service postgresql start`）。下面用 Unix socket
peer 认证（与本仓库开发环境一致）；TCP 连接改用标准 `postgres://user:pass@host:5432/db?sslmode=disable`。

```bash
# 1) 建库（任选其一）
sudo -u postgres psql -c "CREATE DATABASE forkindexer OWNER admin;"
sudo -u postgres psql -c "CREATE DATABASE forkindexer_test OWNER admin;"   # 跑测试用

# 2) 依赖已锁定（go.mod / go.sum）；拉取并构建
make build           # 产出 bin/forkindexerd、bin/indexer、bin/genfixtures

# 3) 生成示例输入（真实哈希）
make fixtures        # 写 testdata/stream.ndjson 等

# 4) 启动 HTTP API（首次启动自动迁移建表）
DATABASE_URL="host=/var/run/postgresql user=admin dbname=forkindexer" \
HTTP_ADDR="127.0.0.1:8080" make run
```

环境变量：`DATABASE_URL`（默认 `postgres://localhost:5432/forkindexer?sslmode=disable`）、
`HTTP_ADDR`（默认 `:8080`）。

## 快速验收（一条命令）

```bash
make test-setup                                   # 幂等创建 forkindexer_test
DATABASE_URL="host=/var/run/postgresql user=admin dbname=forkindexer" make acceptance
```

`scripts/acceptance.sh` 会真实验证：

1. 先到的 A1 缺父块 → `staged`，无链头；
2. 补 G/B1/A2 → 高度 1 平局按哈希选链，A2 出现后**重组**为 G→A1→A2；
3. 余额 Alice=940、Bob=60、Carol=0（她的收款只存在于被废弃分叉）；
4. 整条链、账户转账查询正确；
5. 整流重复投递 → 0 new / 4 duplicate，余额不变；
6. 同哈希异内容 → HTTP 400 且报错给出真实 SHA-256；
7. `/v1/verify/rebuild` 增量结果与重建一致；
8. **崩溃/重启**：对 61 块流用真实子进程在第 2、5 批提交前 `os.Exit` 模拟两次崩溃，
   重启从流开头重放（已提交块按哈希去重），最终 `ingestSeq=61`、游标正确、重建一致。

看到 `✓ acceptance passed` 即为通过。

## 自动化测试

```bash
make test          # 全部单元 + 集成测试（需 forkindexer_test；不可达则 skip）
make test-race     # 竞态检测
```

覆盖：模型哈希/校验、孤儿暂存与连通、**三层分叉与逐层重组**、纯哈希平局、
重复幂等、同哈希异内容、透支失效与后代继承、错误高度链接、8 分支并发 + 并发
快照读不混读、2^200 大整数、铸币/销毁、不相关创世树、乱序投递（5 个随机种子）
顺序无关性、真实子进程崩溃/重启断点续传、连接池重建（进程重启等价）。

## CLI

```bash
bin/indexer ingest-file --file testdata/stream.ndjson --batch-size 256 \
    [--exit-after N] [--exit-before-batch K]
bin/indexer reset           # 清空索引与游标
bin/indexer state           # 链头 + 游标
bin/indexer verify          # 从创世重建并比对（一致退出码 0）
```

`--exit-before-batch K`：在本进程第 K 个批次**提交之前**退出（无该批任何内容落库），
用于确定性的崩溃演练；重启从头重扫流，已落库块按哈希去重，因此既不会漏账也不会重账。

## 数据模型（节选）

- `blocks(hash PK, parent_hash, height, transactions jsonb, preimage bytea,
  received_seq, status)` — 全部分叉的块都保留，内容寻址
- `canonical_blocks(height PK, hash)`、`canonical_transactions(...)` — 当前规范链
- `balances(address PK, amount)` — 规范链可用余额，归零账户删除（与重建语义一致）
- `chain_state(head_hash, ingest_seq, stream_offset)` — 单行，与每次摄入同事务提交

## 设计取舍

- **权重即链长**：约定模型无工作量证明，累计权重定义为链上块数。
- **哈希是内容哈希**：输入里 `hash` 可省略（服务端计算）；给出则必须与
  `SHA256(canonical JSON)` 相等，否则拒绝。这保证“同哈希异内容”在密码学意义上可检出。
- **不做交易级签名验证 / Nonce / Gas**：按约定模型，仅强制余额非负。
