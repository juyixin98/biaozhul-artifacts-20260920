# Merkle 状态证明服务 (Merkle State Proof Service)

纯后端的**版本化键值 Merkle 树服务**：Rust + Axum + RocksDB。

每个写批次原子地生成一个**不可变版本根**；任何只持有该根哈希的一方，
可以独立验证某个键的**存在证明**（含值）或**不存在证明**（绑定相邻键与边界）。
不存在**绝不**以空值表示——空值是合法的值，删除才是不存在。

---

## 1. 密码学协议（真实 SHA-256 计算）

所有哈希均为真实的 SHA-256（`sha2` crate），编码做了**域分离 (domain separation)**：

| 对象 | 编码 |
|---|---|
| 叶子 | `H_leaf(k,v) = SHA256( 0x00 ‖ u32be(|k|) ‖ k ‖ u32be(|v|) ‖ v )` |
| 内部节点 | `H_branch(L,R) = SHA256( 0x01 ‖ L(32字节) ‖ R(32字节) )` |
| 空树根 | `ROOT_EMPTY = SHA256( 0x02 )` |

- 标签字节 `0x00/0x01/0x02` 保证叶子、内部节点、空树根三者的原像空间互不相交。
- 对变长字段用 **u32 大端长度前缀**，消除拼接歧义
  （例如 `("ab","c")` 与 `("a","bc")` 的叶子哈希不同）。
- **键排序**：叶子按键的原始字节序（RocksDB / `BTreeMap` 字节序）严格升序排列。
- **奇数层提升**：某层节点数为奇数时，最后一个节点与自身配对
  `H(N,N)`（Bitcoin 风格），证明里该兄弟哈希就等于当前节点哈希。
- **路径方向**：完全由叶子下标 `index` 与各层宽度推导，不依赖任何存储的节点元数据。
  证明中每步带 `side`：`left` 表示兄弟在左（路径节点是右孩子，该层下标为奇），
  `right` 表示兄弟在右（路径节点是左孩子，下标为偶；越界时即自身复制）。
  验证端从 `side` 序列独立重建 `index`，与证明声明值不一致即拒绝。
- **单叶树**：根就是该叶子哈希（路径长度 0）。**空树**：根为 `ROOT_EMPTY`，
  固定常量 `dbc1b4c900ffe48d575b5da5c638040125f65db0fe3e24494b76ea986457d986`。

### 不存在证明（绑定相邻键 + 边界）

对查询键 `q`，证明携带 1–2 个**相邻现存叶子的存在证明**：

- **内部缺口**（前后都有键）：两个 bound —— 左邻居 `L < q`（下标 i），
  右邻居 `R > q`（下标 i+1），并强制 `i+1 = 右邻居下标`（**相邻叶子**），
  从而二者之间不存在其他键。
- **左边界**（`q` 小于最小键）：单个 right bound，且该叶子 `index == 0`（最小键）。
- **右边界**（`q` 大于最大键）：单个 left bound，且该叶子 `index == leaf_count-1`（最大键）。
- **空树**：`empty_tree=true`，无 bound，根必须等于 `ROOT_EMPTY`。

验证端只接受严格不等式与边界位置；任何"把查询键改成已存在键""删掉一个 bound"
"非相邻邻居""谎称空树"的篡改都会被拒绝。

---

## 2. 存储模型与发布原子性

RocksDB 三个列族：

- `data`：当前存活状态。键存在即"存在"；值带 `0x01` 存在标签，
  **空值与删除物理可分**（删除后键直接不存在）。
- `snapshots`：每个版本的**不可变**完整快照 `u64be(version)‖key → 时间戳‖标签‖值`，
  外加版本清单 `v‖u64be(v) → root‖leaf_count‖timestamp`。历史根与历史证明永久可查。
- `meta`：当前版本号计数器。

**发布**在**单个 `rocksdb::WriteBatch`** 内完成：先在内存中基于当前状态折叠整批、
构建好新树（失败则一个字节都不写），再一次性原子写入
①存活键增量 ②完整快照 ③版本清单 ④版本号推进。RocksDB 保证整批可见或整批不可见，
因此**发布失败/中途崩溃绝不会暴露半成品根**。

写批次语义：

- 同一批次内相同键的多个操作，**按输入顺序最后一个生效**（last write wins）。
- `put` 空值 `""` = 存一个真实的空值；`delete` = 不存在，二者严格区分。
- 批次内先删后写 = 复活；删除不存在的键 = 空操作（版本号仍递增）。
- 写操作经服务层异步互斥锁串行化，避免并发批次基于同一旧状态折叠。

---

## 3. HTTP API

二进制数据（键、值、哈希）在 JSON 中一律用**小写十六进制**（空字节串为 `""`，
也接受 `0x` 前缀）。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | 健康检查 + 当前版本 |
| GET | `/v1/info` | 当前版本、根、叶数、编码说明 |
| GET | `/v1/versions` | 全部已发布版本清单 |
| GET | `/v1/roots/{version}` | 指定版本的根（含版本 0 空树根） |
| POST | `/v1/batches` | 提交写批次，返回新版本根 |
| GET | `/v1/keys/{key_hex}` | 当前值（`exists` 区分存在；空值返回 `value:""`） |
| GET | `/v1/proofs/key/{key_hex}?version=N` | 存在/不存在证明（默认当前版本） |
| POST | `/v1/verify` | **仅依赖 root + proof** 的无状态验证 |

写批次请求：

```json
{ "ops": [
  {"op": "put",    "key": "626f62", "value": "323030"},
  {"op": "put",    "key": "64656c7461", "value": ""},
  {"op": "delete", "key": "6361726f6c"}
]}
```

验证请求（可直接把 `/v1/proofs/...` 的响应贴回）：

```json
{ "root": "<32字节根的hex>", "response": { ...完整证明响应... } }
```

也支持 `{ "root": "...", "proof": <InclusionProof> }` 或
`{ "root": "...", "proof": <NonExistenceProof> }`。
响应固定为 `{"valid": true/false, "reason": null/"原因"}`。

错误信封：`{"error": "bad_request|unknown_version|storage_error", "message": "..."}`，
对应 HTTP 400 / 404 / 500。

---

## 4. 本地启动

前置：Rust（cargo ≥ 1.80；本仓库以 1.98 开发）、C++ 编译器（编译 `librocksdb-sys`，
首次较慢）。

```bash
# 1) 构建（依赖版本已锁定在 Cargo.lock）
cargo build --release            # 或开发模式 cargo build

# 2) 启动（默认 db/merkle，监听 127.0.0.1:8080）
./target/release/merkle-proof-service --db db/merkle --addr 127.0.0.1:8080
# 可选：RUST_LOG=debug ./target/release/merkle-proof-service
```

---

## 5. 验收命令

### 5.1 自动化测试（推荐的一键验收）

```bash
cargo test
```

覆盖（详见第 6 节）：篡改路径哈希、错误键/错误值、方向位翻转、伪造 index/leaf_count、
不存在证明的各类攻击、空树、单叶、1..=20 种树形状全叶子证明、批次 last-write-wins、
空值 vs 删除、**批次/进程重启后历史根可查**，以及**独立参考验证器交叉检查**与
真实 HTTP 端到端测试。

### 5.2 端到端演示脚本（真实起服务 + curl + jq）

```bash
cargo build
bash examples/demo.sh
```

### 5.3 手动 curl 验收

```bash
./target/debug/merkle-proof-service --db /tmp/mk --addr 127.0.0.1:8080 &

# 空树根
curl -s localhost:8080/v1/roots/0 | jq .

# 写一个批次（alpha=100, bob=200；dup 最后一个生效为 last）
curl -s -XPOST localhost:8080/v1/batches -H 'content-type: application/json' -d '{
  "ops":[{"op":"put","key":"616c706861","value":"313030"},
         {"op":"put","key":"626f62","value":"323030"},
         {"op":"put","key":"647570","value":"6669727374"},
         {"op":"put","key":"647570","value":"6c617374"}]}' | jq .

# bob 的存在证明
curl -s 'localhost:8080/v1/proofs/key/626f62' | jq .

# bruce 的不存在证明（bob 与某更大键之间 / 边界，含相邻键绑定）
curl -s 'localhost:8080/v1/proofs/key/6272756365' | jq .

# 无状态验证（把上一步响应作为 response 字段，root 用上一步批次返回的根）
curl -s -XPOST localhost:8080/v1/verify -H 'content-type: application/json' \
  -d '{"root":"<ROOT_HEX>","response":<PROOF_JSON>}' | jq .
```

---

## 6. 测试矩阵

| 文件 | 覆盖点 |
|---|---|
| `src/hash.rs` 单测 | 域分离标签互不碰撞、长度前缀消歧、空值叶子钉死向量 |
| `src/tree.rs` 单测 | 空树/单叶/偶数/奇数（自配对）根的手工构造、拒绝未排序输入 |
| `src/proof.rs` 单测 | 0..=12 叶全叶子包含证明；不存在证明（边界/内部/空树/单叶） |
| `tests/proofs.rs` | **篡改攻击**：翻转路径哈希、改键、改值、跨根验证、方向位翻转、伪造 index/leaf_count、不存在证明改查询键/删 bound/谎报空树/非相邻邻居；**独立验证器交叉检查**全部证明 |
| `tests/store.rs` | 空树 v0、不可变历史根、历史证明、last-write-wins、先删后写复活、空值≠删除、空批次、**重启恢复**、未知版本错误 |
| `tests/api.rs` | 真实 Axum 服务端到端（原始 HTTP/1.1 客户端，无测试框架）：批次、证明、验证、篡改拒绝、历史版本、错误码 400/404、**优雅停机后冷重启** |

### 独立验证器（为什么是真正的"交叉检查"）

`tests/common/independent.rs` **不调用**本服务任何验证/哈希封装函数，
只使用 SHA-256 原语，按 README 第 1 节的协议规范从零独立实现了
包含证明与不存在证明验证。测试把服务返回的 JSON（与外部依赖方拿到的字节完全一致）
反序列化进这套独立实现并要求通过；同时对同一批证明跑服务端验证器，
两套独立代码必须结论一致。所有被篡改的证明必须被两套实现同时拒绝。

此外 `examples/verify.py` 是**第三种独立实现**（纯 Python 标准库 `hashlib`，
与 Rust 完全无关），可直接对线上服务返回的 JSON 做无状态校验，例如
`curl -s http://host/v1/proofs/key/<hex> | python3 examples/verify.py <ROOT_HEX>`。
验收时已对 1..=30 叶的树做过 584 项 Python 交叉验证（含 30 项篡改拒绝）。

---

## 7. 目录结构

```
Cargo.toml / Cargo.lock      依赖声明与锁定版本
src/
  hash.rs      域分离 SHA-256：叶子/内部/空树根
  encoding.rs  hex JSON 类型 + 持久化二进制编解码
  proof.rs     证明协议类型 + 纯（存储无关）验证器
  tree.rs      排序键 Merkle 树构建与证明生成
  store.rs     RocksDB：原子批次发布、不可变快照、历史版本
  service.rs   异步服务门面（写串行化、阻塞操作卸载）
  api.rs       Axum 路由与处理器
  main.rs      启动入口（--db / --addr）
tests/         证明攻击、存储重启、HTTP 端到端 + 独立参考验证器(Rust)
examples/      batches.json（示例请求）、demo.sh（端到端脚本）、verify.py（Python 独立验证器）
```

## 8. 设计说明与限制

- 每次发布为该版本写入完整快照（写放大换取历史永久可查与实现直接、可靠）。
  存活状态本身增量更新；适合状态规模适中、重视审计与历史证明的场景。
- 每次证明从快照列族顺序扫描并重建内存树（简单、无节点存储的一致性风险）。
  对百万级以上键或极高 QPS，可增量持久化内部节点；协议与证明格式无需改变。
- 未引入证明聚合/批处理证明；每键一个标准 Merkle 路径，语义最简单、验证最易审计。
