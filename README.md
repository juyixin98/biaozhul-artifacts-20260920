# mvcc-kv — 单进程 MVCC 键值服务（Rust + Axum）

纯后端、内存态的多版本并发控制（MVCC）键值服务。事务按**起始时间戳快照读**，提交时做**写写冲突检测（首提者胜）**；删除写**墓碑（tombstone）**而不抹掉历史；只读快照以时间戳钉住版本链，**快照关闭后**其依赖版本才可被垃圾回收。

> 快照实现方式：快照只是一个时间戳整数 + 共享版本链上的读视图，**不复制任何数据**（见 `src/db.rs` 中 `snapshot_open` / `read_version`）。

## 依赖与启动

- Rust（开发与实测版本 `rustc 1.98.1` / `cargo 1.98.1`，edition 2021）
- 仅 4 个直接依赖：`axum 0.8`、`tokio 1`（rt-multi-thread / net / macros）、`serde 1`（derive）、`serde_json 1`
- 开发依赖：`tower 0.5`（util）、`http-body-util 0.1`（仅集成测试用）
- 版本已锁定在提交的 `Cargo.lock`。

```bash
# 构建（依赖齐全时可离线：cargo build --offline）
cargo build

# 启动（默认监听 127.0.0.1:3000，可用环境变量改地址）
cargo run
MVCC_ADDR=0.0.0.0:8080 cargo run

# 自动化测试（11 个：7 个走 axum 路由的 in-process HTTP 集成测试 + 4 个引擎单元测试）
cargo test

# 对已启动的服务跑端到端 curl 验收脚本
./examples/demo.sh
# BASE=http://host:port ./examples/demo.sh
```

## 数据模型与并发控制

| 概念 | 规则 |
|---|---|
| 提交时间戳 | 稠密整数 1,2,3…，仅在发生写提交时分配；`last_commit_ts` 为当前最新版本 |
| 事务 | `begin` 时记录 `start_ts = last_commit_ts`；读只认 `commit_ts ≤ start_ts` 的最新版本（快照隔离），并支持**读己写**（缓冲写入对自身可见） |
| 提交 | 写集合中任一键存在 `commit_ts > start_ts` 的版本 → 返回 **409 write_conflict**，事务结束（首提者胜）；否则分配新时间戳，把缓冲写/墓碑追加进该键版本链 |
| 删除 | 追加一个 `value = null, deleted = true` 的墓碑版本，历史原样保留，老读者仍读到旧值 |
| 快照 | `snapshot/open` 只存一个 `ts`（默认取最新，也可指定历史时间戳）；与事务共享同一版本链，零数据复制 |
| 水位线 | 所有活跃事务的 `start_ts` 与打开快照的 `ts` 的最小值；没有任何读者时等于 `last_commit_ts` |
| GC（`POST /gc`） | 每个键保留所有 `ts > 水位线` 的版本，外加 `ts ≤ 水位线` 的**最新一个**（再老的版本没有任何读者能触及）。仅当**完全没有存活读者**时，才会把“只剩墓碑”的键整条删除（保证打开的快照在关闭前不丢失任何依赖版本） |

正确性要点：长读事务/打开的快照把水位线压在自己的起点，因此 GC 永远不会删掉它们依赖的版本；它们结束/关闭后，水位线前进，下一次 GC 才能回收。

## HTTP 接口

所有请求/响应均为 JSON；值本身是任意 JSON（字符串、数字、对象等）。错误统一为 `{"error": ..., "code": ...}`。

### 事务

| 方法与路径 | 说明 |
|---|---|
| `POST /txn/begin` | 开启事务 → `{txn_id, start_ts}` |
| `POST /txn/:id/commit` | 提交；冲突时 **409** `write_conflict`；只读提交返回其 `start_ts`，不推进时间戳 |
| `POST /txn/:id/abort` | 中止，丢弃缓冲写 |
| `GET  /txn/:id/status` | 事务信息（start_ts、已缓冲写的键） |
| `GET    /txn/:id/kv/:key` | 事务内快照读（读己写优先）→ `{key, value, found}` |
| `PUT    /txn/:id/kv/:key` | 缓冲写，body 为 JSON 值（空 body 当作 null） |
| `DELETE /txn/:id/kv/:key` | 缓冲删除（提交时形成墓碑） |

### 只读快照

| 方法与路径 | 说明 |
|---|---|
| `POST /snapshot/open` | body `{}` 或 `{"ts": N}`（历史时间戳；超过最新提交时间戳 → 400）→ `{snapshot_id, ts}` |
| `POST /snapshot/:id/close` | 关闭快照，被钉住的版本此后可回收 |
| `GET  /snapshot/:id/status` | 快照信息 |
| `GET  /snapshot/:id/kv/:key` | 按快照时间戳读 → `{key, value, found}` |

### GC / 调试 / 便捷接口

| 方法与路径 | 说明 |
|---|---|
| `POST /gc` | 执行一轮回收 → 回收报告（水位线、活跃读者数、删除版本/墓碑/键数） |
| `GET  /stats` | 最新时间戳、活跃事务/快照数、键数、版本总数、墓碑数、水位线 |
| `GET  /debug/versions/:key` | 查看某键完整版本链 |
| `GET    /kv/:key` | 自动提交读（最新值；`?as_of=N` 为一次性历史读） |
| `PUT    /kv/:key` | 自动提交写（内部即 begin/put/commit） |
| `DELETE /kv/:key` | 自动提交删除 |

不存在的事务/快照返回 **404**；不存在的键返回 `200 {"found": false, "value": null}`。

## 请求样例（curl）

```bash
# 自动提交写入：k 得到 ts=1
curl -s -X PUT -H 'content-type: application/json' -d '"v0"' localhost:3000/kv/k

# 开启一个长读事务（start_ts=1；自动提交种子写已占用一个内部 id，故 txn_id 从 2 开始）
curl -s -X POST localhost:3000/txn/begin            # -> {"txn_id":2,"start_ts":1}

# 再开一个 ts=1 的只读快照
curl -s -X POST -H 'content-type: application/json' -d '{"ts":1}' \
  localhost:3000/snapshot/open                       # -> {"snapshot_id":1,"ts":1}

# 三次覆盖 + 一次删除（ts=2,3,4,5）
for v in v1 v2 v3; do curl -s -X PUT -d "\"$v\"" localhost:3000/kv/k; done
curl -s -X DELETE localhost:3000/kv/k

# 老读者视图不变，仍然是 v0
curl -s localhost:3000/txn/2/kv/k                   # {"value":"v0","found":true,...}
curl -s localhost:3000/snapshot/1/kv/k              # 同上
curl -s localhost:3000/kv/k                         # {"value":null,"found":false,...}

# 快照未关闭：GC 什么也回收不掉（watermark=1）
curl -s -X POST localhost:3000/gc
# {"versions_removed":0,...,"watermark":1,"active_txns":1,"active_snapshots":1}

# 并发写写：两个事务都从同一快照出发写同一个键（id 以实际返回为准）
TA=$(curl -s -X POST localhost:3000/txn/begin | sed 's/.*"txn_id"://;s/,.*//')
TB=$(curl -s -X POST localhost:3000/txn/begin | sed 's/.*"txn_id"://;s/,.*//')
curl -s -X PUT -d '"A"' localhost:3000/txn/$TA/kv/c
curl -s -X PUT -d '"B"' localhost:3000/txn/$TB/kv/c
curl -s -X POST localhost:3000/txn/$TA/commit       # 200 committed
curl -s -X POST localhost:3000/txn/$TB/commit       # 409 write_conflict

# 老事务结束后再 GC：旧版本被回收，tombstone 仍被打开的快照钉住
curl -s -X POST localhost:3000/txn/2/abort
curl -s -X POST localhost:3000/gc                   # versions_removed=4, watermark=5
curl -s localhost:3000/debug/versions/k             # 只剩 tombstone@5

# 快照关闭后下一轮 GC 才能删除墓碑与空键
curl -s -X POST localhost:3000/snapshot/1/close
curl -s -X POST localhost:3000/gc                   # keys_removed=1
```

完整分步脚本见 [`examples/demo.sh`](examples/demo.sh)；仓库内保存了一份干净服务上的实跑输出 [`examples/demo-output.txt`](examples/demo-output.txt)。

## 验收场景与实测结果（2026-09-23，本机实际运行）

环境：Linux 6.8（x86_64），Rust 1.98.1，cargo 1.98.1；依赖全部来自本机缓存（`--offline` 可构建）。

### 自动化测试：`cargo test` — 11 passed / 0 failed

| 测试 | 覆盖的验收点 |
|---|---|
| `acceptance_long_reader_overwrites_delete_gc`（HTTP） | 长读事务跨越 3 次覆盖（ts2/3/4）与删除（ts5），全程读到旧值 v0；快照/事务存活时 GC 零回收（watermark=1）；关闭后才回收 v0–v3（回收 4 个版本）；快照视图在回收前后一致（deleted）；快照关闭后下一轮 GC 删除墓碑与空键，读仍为 not found |
| `acceptance_concurrent_writers_only_one_commits`（HTTP） | 两个并发事务同键写入：先提交者 200（ts=2），后提交者 **409 write_conflict**；版本链只新增 1 个版本；新事务读到胜者值并可继续提交 |
| `delete_is_tombstone_visible_to_older_readers_only`（HTTP） | 删除后版本链新增 `deleted=true` 墓碑；ts1 快照仍读到旧值，新读者读到 not found |
| `gc_pinning_by_open_snapshots`（HTTP） | ts2/ts3 两个快照分层钉住历史：watermark 随快照关闭逐档前进（2→3→4），每次只回收刚好失效的 1 个版本 |
| `read_your_writes_and_abort`（HTTP） | 读己写可见；abort 后缓冲写不落库 |
| `snapshot_future_timestamp_rejected`（HTTP） | 打开未来时间戳快照返回 400 |
| `read_only_commit_advances_nothing`（HTTP） | 只读事务提交不分配新时间戳 |
| `engine_snapshot_pins_gc_watermark_directly`（单元） | 直接驱动引擎：ts0 读者在多轮提交后仍读到空；ts0 事务 + ts1 快照把水位线钉在 0；逐档关闭后回收行为符合规则；无读者时墓碑随空键清除 |
| `engine_conflict_on_overwrite_and_delete_alike`（单元） | 覆盖与删除都参与写写冲突检测；失败事务句柄已销毁 |
| `engine_disjoint_write_sets_both_commit`（单元） | 写集合不相交的两个并发事务都可提交 |
| `engine_writes_buffered_until_commit_are_invisible_to_others`（单元） | 未提交缓冲写对其他事务不可见；abort 后不可见 |

### 实跑 `examples/demo.sh`（真实 HTTP，curl）— exit 0，关键结果摘录

- 种子写入 `k="v0"` @ts1；长读事务与 ts1 快照在此打开；随后三次覆盖 @ts2/3/4、删除 @ts5。
- 旧读不变：`GET /txn/2/kv/k` 与 `GET /snapshot/1/kv/k` 均返回 `"value":"v0","found":true`；新读者 `"found":false`；`?as_of=4` 读到 `"v3"`。
- 回收受限：读者存活时 `POST /gc` → `versions_removed=0, watermark=1`；关闭快照后长事务仍在 → 依旧 `0`。
- 并发写：txn/8 提交成功 `commit_ts=6`；txn/9 提交返回 **HTTP 409** `{"code":"write_conflict","error":"write-write conflict on key \"c\" ..."}`；之后 `GET /kv/c` 为 `"A-wins"`。
- 关闭快照前在 ts5 记录删除后视图；中止长事务后 GC → `versions_removed=4, tombstones_removed=0, keys_removed=0, watermark=5`，版本链只剩 `tombstone@5`；快照读、最新读、`as_of=5` 读结果与回收前完全一致（均 not found）。
- 关闭快照后再 GC → `versions_removed=1, tombstones_removed=1, keys_removed=1`，键彻底消失，`GET /kv/k` 仍为 not found。

完整输出保存在本机运行时记录（`/tmp/demo-output.log` 形态）；脚本可随时重放。

## 设计取舍与限制（如实说明）

- **单进程内存态**：无持久化，重启数据清空；所有操作在单个 `Mutex` 保护的哈希表上完成，临界区只做 O(版本链) 的只读扫描/一次追加，没有跨锁 await。多线程 HTTP 下功能正确，高并发吞吐受单锁限制（满足“单进程”要求；分片锁是显而易见的扩展方向，未做）。
- **GC 是手动触发的**（`POST /gc`），没有后台周期回收，便于观察“回收前/后”；可在外面 cron 调用。
- GC 后**早于水位线的历史时间戳不再可读**（版本已物理删除）——这是 GC 的定义；未关闭的快照不会失去任何版本。
- 事务句柄没有空闲超时，需要显式 commit/abort；遗留事务会一直钉住水位线（可用 `GET /stats` 发现）。
- 值为任意 JSON；键走 URL 路径段（简单字符最方便，未做键转义工具）。

## 目录结构

```
Cargo.toml          依赖清单（4 个直接依赖 + 2 个 dev 依赖）
Cargo.lock          锁定依赖（已提交）
src/db.rs           MVCC 引擎：版本链、事务、快照、水位线、GC
src/main.rs         Axum 路由/处理函数 + 7 个 in-process HTTP 集成测试
examples/demo.sh    curl 端到端验收脚本
```
