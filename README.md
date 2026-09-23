# UTXO 回滚验证器 (UTXO Rollback Validator)

离线链实验用的 **UTXO 状态服务**：输入简化区块/交易，做真实的密码学校验、
UTXO 记账、逐块保存撤销信息（undo），支持断开区块并接入替代块（reorg），
撤销与状态根更新在同一个 SQLite 事务内原子完成。纯后端（Rust + Axum + SQLite），
无前端。

金额一律为非负整数（线上类型为 `u64`，负数在 JSON 解析阶段即被拒绝）。

---

## 1. 构建与本地启动

需要 Rust（已在 1.98.1 验证）与 C 编译器（rusqlite 用 `bundled` 从源码编译 SQLite，
无需系统预装 sqlite）。

```bash
cargo build --release

# 启动 HTTP 服务（默认 127.0.0.1:3000，默认数据库 utxo.db）
./target/release/utxo-server --db chain.db --addr 127.0.0.1:3000
```

附带命令行工具：

```bash
./target/release/utxo-cli keygen                         # 生成真实 Ed25519 密钥
./target/release/utxo-cli gen-examples examples/generated # 离线生成已签名示例区块
./target/release/utxo-cli demo --base-url http://127.0.0.1:3000  # 端到端演示
```

### 一键验收

```bash
bash accept.sh                 # 构建 + 全部测试 + 临时库端到端演示 + 重启校验
# 或手动：
cargo test
```

`accept.sh` 使用临时目录数据库，结束自动清理，可重复运行。

---

## 2. 真实执行的计算 / 协议 / 密码操作

没有任何桩（mock）：

* **交易 ID**：对交易的规范化“未签名”字节做 **双重 SHA-256**（Bitcoin 风格 hash256）。
* **区块哈希**：对规范化区块头
  `version(u32) || height(u64) || prev_hash(32) || timestamp(i64) || merkle_root(32)`
  做双重 SHA-256。
* **Merkle 根**：真实的成对 hash256 树，奇数节点复制最后一个叶子；
  区块提交的 `merkle_root` 必须与重算值一致，否则整块拒绝。
* **花费授权**：真实 **Ed25519** 签名验证（`ed25519-dalek`）。每笔普通交易对其
  txid 签名，验证时用 **被花费输出的 owner 公钥** 验签——公钥不属于该输出、
  签名被篡改、签名者不是 owner 都会被拒绝。
* **状态根 (state root)**：对当前全部 UTXO，按 `(txid, vout)` 排序后，对每条
  `tag||outpoint||value||address||created_in` 叶子做 hash256，再构成 Merkle 树
  （空集对域标签 `0x03` 求 hash256）。状态根在应用区块后从**物理表**重算。
* **coinbase 奖励**：固定规则 `50 >> (height/100)`（初始 50，每 100 块减半，
  整数移位，最终为 0）**加本块手续费**；coinbase 输出总额必须精确等于该值，
  多一个都拒绝。coinbase 输入携带 `coinbase_tag = height 的 8 字节小端`
  （对应 Bitcoin 把高度写入 coinbase 的规则），保证不同高度的 coinbase 交易
  txid 天然不同。

### 逐笔/逐块校验规则（任一不满足，整块拒绝且状态不变）

* 第一笔必须是唯一 coinbase（恰好一个 `prev:null` 输入，height tag 正确），
  其余交易不得是 coinbase；每笔交易必须有输出；金额非负。
* 块内交易按顺序校验：
  * 每个输入必须引用**存在且未花费**的输出（查库 + 本块已增删视图）；
  * **块内双花**：同一交易内重复引用，或本块前序交易已花费，均拒绝；
  * coinbase 输出在同一块内不可花费（coinbase maturity）；
  * **owner + Ed25519 签名**必须通过；
  * `Σ输入 ≥ Σ输出`（否则“输入不足”），差额即手续费；
  * 交易 ID 在块内唯一。
* coinbase 金额 == 补贴 + 块内手续费。
* 父链规则：空库只接受 `height=0, prev_hash=全零` 的创世块；延伸块
  `prev_hash` 必须等于当前 tip 且高度 +1；同高度、父块已知的块按
  **替代块**处理（先原子断开旧 tip 再校验、接入新块）。

### 原子性

连接区块在单个 SQLite 事务内完成：可选的旧 tip 断开 → 花费标记 →
新增输出 → 重算状态根 → 写 `blocks` / `block_undo` → 移动 tip。
任何一步失败整体回滚（替代块非法时，旧 tip 会被完整恢复——有测试专门断言）。
断开区块在同一事务内删除本块输出、恢复本块花费的输出、删除 undo 与块行、
回退 tip，并把状态根恢复为父块记录值。

---

## 3. HTTP API

| 方法 | 路径 | 说明 |
|----|----|----|
| GET  | `/health` | 健康检查 |
| GET  | `/chain/tip` | 当前链 tip（空链返回 `{"empty":true}`） |
| GET  | `/blocks/tip` | tip 区块元数据 |
| GET  | `/blocks/{height}` | 活跃链指定高度区块；加 `?by_hash=<hex>` 按哈希查 |
| POST | `/blocks` | 提交区块；成功 201，协议拒绝 **409**，坏 JSON/负数 **400** |
| POST | `/blocks/disconnect` | 断开当前 tip（回滚一格） |
| GET  | `/utxos` | **当前** UTXO 集（可 `?address=`），响应显式带 `"view":"current"` |
| GET  | `/utxos/at/{height}` | **明确指定高度**的历史 UTXO 集，`"view":"historical"`，并校验历史状态根 |
| GET  | `/debug/replay` | 朴素 HashMap 重放，对每个高度比对状态根 |

提交示例：

```bash
curl -s -X POST localhost:3000/blocks \
  -H 'content-type: application/json' \
  -d @examples/generated/block0-genesis.json
```

**历史查询必须给高度**：`/utxos` 永远是当前集；`/utxos/at/H` 依据不可变的
`created_in` / `spend_height` 列计算“在 H 时未花费”，绝不会拿当前 UTXO 冒充，
且会把重算出的历史根与该块存储的根比对，不一致直接报内部错误。

### 请求体结构

```jsonc
{
  "height": 1,
  "prev_hash": "<64 hex>",
  "timestamp": 100,
  "merkle_root": "<64 hex, 必须与块内 txid 树一致>",
  "txs": [
    // coinbase 必须在第一位
    { "inputs": [ { "prev": null, "coinbase_tag": "<8字节LE高度 hex>" } ],
      "outputs": [ { "value": 55, "address": "<32字节公钥 hex>" } ] },
    // 普通交易
    { "inputs": [ { "prev": { "txid": "<64hex>", "vout": 0 },
                    "pubkey": "<32hex>", "signature": "<64hex Ed25519 over txid>" } ],
      "outputs": [ { "value": 30, "address": "<32hex>" },
                   { "value": 15, "address": "<找零地址>" } ] }
  ]
}
```

---

## 4. 示例输入

`utxo-cli gen-examples`（已提交一份到 `examples/generated/`，真实签名）生成：

* `wallets.json` — miner/alice/bob 的地址与密钥（演示用，勿用于真实场景）
* `block0-genesis.json` — 创世，50 → miner
* `block1-transfer.json` — miner→alice 30，找零 15，费 5，coinbase 55
* `block2-transfer.json` — alice→bob 10，找零 19，费 1，coinbase 51
* `invalid-block1-double-spend.json` — 块内双花（期望 409，含 “double spend”）
* `invalid-block1-bad-signature.json` — 翻转签名字节（期望 409）
* `invalid-block1-greedy-coinbase.json` — coinbase 多拿 1（期望 409）
* `invalid-block1-insufficient.json` — 输入不足（期望 409）

---

## 5. 与朴素重放实现的结果对照

`src/naive.rs` 是一份**刻意独立**的参考实现：不与连接期校验共享逻辑，
从高度 0 起用普通 `HashMap` 重建 UTXO，自行重跑全部规则（存在性、双花、
owner/Ed25519 验签、金额、coinbase 规则、Merkle/区块哈希），并在每个高度
用相同的状态根定义重算，与数据库存储的状态根逐一比对。

* `GET /debug/replay` 返回每高度 `(stored, replayed, equal)` 与总量；
* 引擎测试 `naive_replay_matches_storage_along_the_whole_chain` 在正常链、
  回滚后都断言 `roots_match == true`；
* 端到端演示最后一步（`[12]`）会打印对照结果，例如
  `naive replay matches storage at every height: 3 blocks, 5 utxos, supply 150`。
  总量 150 = 3 个区块 × 补贴 50（手续费只是再分配，不增发），可独立核对。

---

## 6. 测试覆盖

`cargo test`（共 15 个测试，全部真实签名/哈希、临时 SQLite 文件）：

* 块内双花（同交易重复输入）、跨交易双花；拒绝后 tip/状态根不变，随后合法块仍可接入
* 输入不存在 / 已花费 / 输入不足 / 签名非法 / 非 owner 签名
* 负数金额在解析层被拒
* coinbase 精确等于补贴+手续费；补贴减半表
* **连续断开**多个区块并核对 UTXO 恢复、tip 高度与状态根
* 断开后重连**逐字节复现相同状态根**
* 同高度**替代块**接入（reorg）：旧链 UTXO 被移除、新链 UTXO 生效
* **非法替代块**：未知父块、替代块自身双花（失败 reorg 必须回滚到旧 tip）、
  高度不随父块、在更高 tip 上替换非 tip 块
* **重启持久化**：关闭进程后用同一数据库重开，tip/历史根/undo 均可用，
  重启后仍能连续回滚，且朴素重放一致
* HTTP 层：真实端口、201/400/404/409 状态码、当前与历史视图区分

---

## 7. 存储模型 (SQLite, WAL)

* `blocks(hash PK, height, prev_hash, timestamp, merkle_root, state_root, fee_total, tx_count, data)`
* `utxo(txid, vout, value, address, created_in, spend_height)`，主键 `(txid,vout)`；
  未花费时 `spend_height IS NULL`，花费后记录花费高度——历史查询据此实现。
* `block_undo(block_hash PK, height, spent_json, created_json)`：每块撤销信息。
* `metadata(key='tip')`：当前 tip 哈希。

回滚/重放的幂等性通过 upsert 处理：重新接入曾断开的块时，残留的“已花费”行
会被恢复为未花费，而不是触发主键冲突。

## 8. 范围与限制（如实说明）

* 只支持把**当前 tip** 替换为同高度替代块（单块回滚式 reorg）。更深的重组：
  先用 `POST /blocks/disconnect` 连续断开（或按序提交替代块），再接入替代链。
  这是离线实验场景下的明确取舍；尝试在更高 tip 上直接替换非 tip 块会得到 409。
* 无 P2P / 工作量证明 / 难度调整 / 时间戳共识；区块生产者自行构造并签名，
  `utxo-cli` 与 `builder.rs` 提供真实的离线构造能力。
