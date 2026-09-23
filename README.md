# UTXO 回滚验证器（UTXO Rollback Validator）

为离线链实验构建的纯后端 UTXO 状态服务。输入简化区块/交易（金额为整数原子单位），
按顺序执行、逐块保存**撤销信息（undo）**，可连续断开区块并接入替代分支；
状态根与撤销在**同一个 SQLite 事务**内原子更新。所有哈希（txid、Merkle 根、
区块哈希、状态根）都由真实的 **SHA-256 / 双重 SHA-256** 计算，没有桩实现。

技术栈：Rust + [Axum](https://github.com/tokio-rs/axum) 0.7 + SQLite
（[`rusqlite`](https://github.com/rusqlite/rusqlite) bundled，无需系统 libsqlite3）。

---

## 1. 快速开始

```bash
# 编译并启动（首次会编译 bundled SQLite，约 1–2 分钟）
cargo run

# 自定义
UTXO_DB=./chain.db UTXO_ADDR=127.0.0.1:8080 cargo run --release
```

启动后：

```bash
curl -s http://127.0.0.1:8080/health
# {"status":"ok"}
curl -s http://127.0.0.1:8080/tip
# {"height":0,"hash":"00…00","state_root":"d9d5…f695"}
```

## 2. 一键验收

```bash
# 端到端验收（真实 HTTP + 真实 SQLite 文件 + 重启），自动选空闲端口
./accept.sh

# Rust 自动化测试（10 个，含重启持久化、双花、连续断块、非法替代块等）
cargo test
```

`accept.sh` 共 47 项断言：合法链、块内双花、负金额、输入不足、未知输入、
错误 coinbase、错误前向哈希、整块拒绝、连续断两块、非法/合法替代块、
历史查询强制高度、朴素重放逐块对照、重启持久化。

---

## 3. 协议（真实执行的规则）

### 3.1 交易

```json
{
  "version": 1,
  "txid": "<可选，32 字节双 SHA-256 的 hex，服务端会校验，不一致则拒绝>",
  "vin": [
    {"txid": "<被花费输出的 txid，32B hex>", "vout": 0, "scriptSig": "<hex>"}
  ],
  "vout": [
    {"address": "addr_alice", "value": 100}
  ]
}
```

- `vin` 为空 ⇒ **coinbase**，且只能是块内第一笔交易。
- `txid = sha256d( canonical_bytes(tx) )`，canonical 编码带
  `"utxo-tx:"` 前缀、大端定宽整数、长度前缀的字符串；`scriptSig` 为
  hex 解码后参与哈希。`scriptSig` 本身不验公钥签名（实验模型里没有密钥体系），
  但其字节被 txid 覆盖、被如实存储。
- 金额为 `i64`；**负数输出直接拒绝**（`NEGATIVE_AMOUNT`）。

### 3.2 区块

```json
{
  "version": 1,
  "height": 1,
  "prevHash": "00…00",
  "timestamp": 1700000001,
  "txs": [ ... ]
}
```

- `merkleRoot`：对各 txid 按 Bitcoin 风格（奇数层复制最后一个节点）两两
  `sha256d` 归并。
- `blockHash = sha256d("utxo-block:" || version || height || prevHash || merkleRoot || timestamp)`。
- 高度必须严格等于 `当前 tip + 1`，`prevHash` 必须等于当前 tip 哈希，否则整块拒绝。

### 3.3 块内按顺序执行（任何一步失败 ⇒ 整块回滚）

1. 结构检查：版本、非空 txs、首笔为 coinbase 且只有一笔 coinbase、输出非空、
   地址非空、金额非负、重复 txid / 块内重复交易拒绝、可选 txid 必须等于实算值。
2. 按交易顺序、按输入顺序解析每个输入：
   - 输出必须存在且在**父高度的 UTXO 集**中未花费，或在本块**更早**的交易中创建；
   - 同一输入在块内出现两次 ⇒ `DOUBLE_SPEND`；
   - 引用了块内更靠后才创建的输出 ⇒ `INPUT_IN_FUTURE`；
   - 引用不存在/已花费输出 ⇒ `UNKNOWN_INPUT`。
3. 每笔非 coinbase：`Σ输入值 >= Σ输出值`，否则 `INSUFFICIENT_INPUTS`；
   差额即手续费（用 `i128` 求和，溢出拒绝）。
4. **coinbase 奖励 = 固定补贴 `5000` + 本块所有手续费**；金额不符
   （`BAD_COINBASE_AMOUNT`）整块拒绝。
5. 全部通过后，在一个 SQLite 事务里：写新 UTXO、标记被花费 UTXO、
   写 `blocks`、写本块全部 **undo 记录**、推进 `meta(tip, state_root)`，
   然后 `COMMIT`。校验中途失败则事务 `DROP`，数据库回到上一块后的状态。

### 3.4 撤销与重接（reorg）

- 每写一个 UTXO 变化都记录 undo：
  - `spend`（断开时把输出的 `spent_height` 恢复）
  - `create`（断开时删除该输出）
- `POST /blocks/disconnect {"targetHeight": N}`：从 tip 向下逐块、
  **按 undo 记录逆序**回滚，可一次断开多块；删除对应 `blocks`/`undo` 行，
  tip 落到目标高度并重算该高度状态根——全程单事务原子。
- 断开后即可提交不同的替代块（`prevHash` 指向目标 tip）。引用已废弃分支
  输出的替代块会得到 `UNKNOWN_INPUT`（即“非法替代块”）。

### 3.5 状态根（历史必须带高度）

```
state_root(h) = sha256d( "utxo-state:" ‖ Σ encode(entry) )
encode(entry) = txid(32B) ‖ vout(u32BE) ‖ value(i64BE) ‖ len(addr)(u16BE) ‖ addr
```

仅统计 `created_height <= h 且 (spent_height IS NULL 或 spent_height > h)`
的输出，按 `(txid, vout)` 排序。状态根是历史快照：高度 2 的根不会因为
之后又接了区块而改变。**所有历史查询接口强制要求 `?height=`**，不存在
“用当前 UTXO 冒充历史”的入口；不传高度直接 400，未知高度 404。

### 3.6 朴素重放对照

`GET /replay/verify?height=N`：完全独立的第二套实现——从创世开始，用内存
`HashMap` 重放已存储区块（重新做结构检查、链接检查、输入/金额/coinbase 校验
和集合增删），逐高度用**同一套 canonical 编码**算出状态根，与 SQLite 增量
实现里存的根逐块比对，返回每个高度的 `roots_match` 和总的 `all_match`。
两套实现共享的只有“编码格式”（协议本身），执行逻辑各写一遍。

---

## 4. HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 存活检查 |
| GET | `/tip` | 当前 tip：高度、哈希、状态根 |
| POST | `/blocks` | 提交区块（body 为区块 JSON）；成功 `201`，任何校验失败 `400` 整块拒绝 |
| GET | `/blocks/:height` | 取该高度已提交区块及哈希/merkle；未知高度 `404` |
| POST | `/blocks/disconnect` | body `{"targetHeight": N}`，原子断块到该高度 |
| GET | `/state-root?height=N` | **必须带 height**；返回该高度的 UTXO 状态根 |
| GET | `/utxos/:txid/:vout?height=N` | **必须带 height**；查某输出在该高度是否未花费 |
| GET | `/replay/verify?height=N` | 朴素重放与增量实现逐块对照（缺省 height=tip） |

错误响应统一形如：

```json
{"error":"DOUBLE_SPEND","message":"input … is spent twice inside the block"}
```

错误码：`HEIGHT_MISMATCH`、`PREV_HASH_MISMATCH`、`EMPTY_BLOCK`、
`COINBASE_FIRST`、`MULTIPLE_COINBASE`、`BAD_BLOCK_VERSION`、`BAD_TX_VERSION`、
`EMPTY_OUTPUTS`、`NEGATIVE_AMOUNT`、`EMPTY_ADDRESS`、`UNKNOWN_INPUT`、
`DOUBLE_SPEND`、`INPUT_IN_FUTURE`、`INSUFFICIENT_INPUTS`、
`BAD_COINBASE_AMOUNT`、`DUPLICATE_TX`、`TX_ALREADY_EXISTS`、`TXID_MISMATCH`、
`UNKNOWN_HEIGHT`(404)、`NO_OUTPOINT`(404)、`CANNOT_DISCONNECT_GENESIS`、
`OVERFLOW`、`MALFORMED`。

---

## 5. 手工演示

```bash
cargo run &
B=http://127.0.0.1:8080

curl -s -X POST $B/blocks -H 'content-type: application/json' \
  --data @examples/block1.json
curl -s -X POST $B/blocks -H 'content-type: application/json' \
  --data @examples/block2.json

# 块内双花 → 整块拒绝，tip 不动
curl -s -X POST $B/blocks -H 'content-type: application/json' \
  --data @examples/block3-doublespend.json

# 历史查询必须带高度
TX2=$(python3 -c "import json;print(json.load(open('examples/expected.json'))['block2.spendTxid'])")
curl -s "$B/utxos/$TX2/1?height=2"     # 200（此时未花费）
curl -s "$B/state-root?height=2"       # 高度 2 的历史根
curl -s "$B/state-root"                # 400，拒绝隐式“当前”

# 连续断开两块，再换替代分支
curl -s -X POST $B/blocks/disconnect -H 'content-type: application/json' \
  -d '{"targetHeight":1}'
curl -s -X POST $B/blocks -H 'content-type: application/json' \
  --data @examples/alt-block3-stale-input.json   # 400 UNKNOWN_INPUT（非法替代块）
curl -s -X POST $B/blocks -H 'content-type: application/json' \
  --data @examples/alt-block2.json               # 201
curl -s "$B/replay/verify"                       # all_match=true
```

## 6. 示例输入（`examples/`）

由 `cargo run --bin gen-fixtures` **实际执行一条内存链**后生成
（所有哈希都是实算的，不是手写的）：

- `block1.json` / `block2.json` / `block3.json`：合法主链（block3 含 1 手续费）
- `block3-doublespend.json`：块内两笔交易花同一输出
- `block3-negative.json`：输出金额为负
- `block3-overspend.json`：输入总和 < 输出总和
- `block3-unknown-input.json`：引用不存在的输出
- `block3-bad-reward.json`：coinbase 金额违反固定补贴+手续费规则
- `block3-bad-prev.json`：`prevHash` 被篡改
- `alt-block2.json` / `alt-block3.json`：断开后的合法替代分支
- `alt-block3-stale-input.json`：非法替代块（花已废弃分支的输出）
- `expected.json`：生成器实算出的 txid / 区块哈希 / 状态根，供脚本比对

重新生成：`cargo run --bin gen-fixtures`（默认写 `./examples/`）。

---

## 7. 存储与崩溃恢复

SQLite（WAL，`synchronous=FULL`），四张表：

- `meta`：单行，当前 `tip_height / tip_hash / state_root`；
- `blocks(height PK, hash UNIQUE, prev_hash, merkle_root, timestamp, block_json)`：
  含高度 0 的创世空块（全零哈希）；
- `utxos(txid, vout, value, address, created_height, spent_height)`：
  历史输出不删除，靠两个高度列做时间旅行查询；
- `undo_records(height, undo_type, txid, vout, …)`：每块的撤销序列。

连接/断开/状态根推进都在单事务内，进程崩溃后 WAL 重放保证不会出现
“块已写入但 UTXO 只改了一半”。`Db::open` 幂等：重启直接复用已有库，
测试 `restart_persists_state` 用真实文件验证关闭→重开后 tip、状态根、
重放结果完全一致。

## 8. 项目结构

```
src/
  error.rs       错误类型 / HTTP 状态码 / 错误码字符串
  hash.rs        双 SHA-256、Hash32、Merkle 根、区块哈希
  model.rs       区块/交易模型、txid canonical 编码、静态结构校验
  db.rs          SQLite schema、历史 UTXO/状态根查询（强制高度谓词）
  chain.rs       connect / disconnect（undo）/ naive_replay 独立对照
  api.rs         Axum 路由（阻塞调用放 spawn_blocking）
  main.rs        启动入口
  bin/gen_fixtures.rs  生成 examples/ 与 expected.json
tests/api_tests.rs    10 个端到端 HTTP 测试
accept.sh             47 项一键验收
```

## 9. 明确的边界（如实说明）

- `scriptSig` 只做 hex 解码、参与 txid 并存储，**不做 ECDSA 公钥/签名验证**
  （本协议没有地址密钥模型）；哈希、Merkle、状态根等密码学运算是真实执行的。
- 没有难度/PoW、时间戳共识、P2P 网络；它是单节点的 UTXO 执行与回滚层。
- 金额为 `i64` 整数原子单位，无小数。
