# mvcc-reclaim

单机、文件支持的 MVCC 键值存储，纯后端：快照事务、单调版本发布、写写冲突拒绝、
以及**不删除活跃快照仍需要的版本**的空间回收。带一个零依赖的本地 HTTP 验证入口和
可注入的 I/O 故障层。无前端。

## 它做什么

- **固定快照读事务（Snapshot Isolation 的读侧）**：`begin_read()` 返回一个钉在
  当前最新已提交版本上的句柄；无论之后多少写事务提交，该句柄读到的键空间不变。
- **单调版本发布**：每次成功提交得到 `latest + 1`；发布点是“写入一个 `CMMT` 帧
  并立即 `fsync`”这一单个动作。未走到该点的写，对任何读者都不可见。
- **写写冲突（First Committer Wins）**：两个从同一快照出发的写事务触碰同一键，
  先提交者赢，后提交者在写盘**之前**收到 `Conflict { keys }`，可重试。
- **版本回收（GC / compaction）**：水位线 = 所有活跃快照中最旧的版本号。
  压缩时保留：
  1. 每个键在水位线**之下**的最新一条记录（作为基线；若它是墓碑则该键继续缺席）；
  2. 版本号 `>= 水位线` 的每一条已提交记录。
  
  因此任何活跃快照能读到的版本都不会被删；没有活跃快照时水位线取 `latest + 1`，
  每个键只保留最新一条。

## 目录结构

```
src/
  crc.rs    CRC-32（帧校验）
  io.rs     可注入 I/O：StdIo / FaultIo（注入错误）/ SimCrashIo（模拟掉电）
  store.rs  磁盘格式、帧编码、追加日志、崩溃恢复、压缩重写
  mvcc.rs   MVCC 引擎：快照、冲突判定、水位线、GC 计划
  json.rs   零依赖最小 JSON
  http.rs   本地 HTTP 验证服务（线程/连接）
  main.rs   mvcc-server 二进制
tests/
  acceptance.rs       验收：读一致性 / 冲突 / GC 保护与回收（含确定性线程交错）
  fault_injection.rs  验收：torn tail、模拟掉电、fsync 失败、GC rename 失败恢复
  http_api.rs         端到端：两个写者 + 长读者 + GC 的 HTTP 全流程
examples/demo.sh      可直接执行的 HTTP 请求样例脚本
```

## 构建与运行

```bash
cargo build --release
./target/release/mvcc-server --dir ./mvcc-data --addr 127.0.0.1:8080
```

无第三方 crate，仅用标准库。

## HTTP API（本地验证入口）

所有值以 UTF-8 字符串承载（验证用；键值在引擎内部是 `Vec<u8>`）。

| 方法与路径 | 请求体 | 说明 |
|---|---|---|
| `POST /tx/read` | `{}` | 开启快照读事务，返回 `read_txn_id` 与 `version` |
| `GET  /tx/read/{id}/get?key=` | – | 在固定快照上读 |
| `POST /tx/read/{id}/release` | `{}` | 显式释放快照（句柄 drop 也会释放） |
| `POST /tx/write` | `{}` | 开启写事务，返回 `write_txn_id`、`snapshot_version` |
| `PUT  /tx/write/{id}` | `{"ops":[{"put":{"k","v"}},{"del":{"k"}}]}` | 缓冲写 |
| `POST /tx/write/{id}/commit` | `{}` | 提交；冲突时 HTTP 409 |
| `POST /tx/write/{id}/abort` | `{}` | 放弃 |
| `POST /gc` | `{}` | 在当前水位线压缩；`ran:false` 表示无可回收 |
| `GET  /stats` | – | 版本、活跃快照、水位线、磁盘/可回收字节 |
| `GET  /get?key=` | – | 对最新版本的一次性读 |

### 请求样例（节选自 `examples/demo.sh`）

```bash
BASE=http://127.0.0.1:8080

# 初始提交 v1：x=v1, y=y1
curl -s -X POST $BASE/tx/write -d '{}'
# {"write_txn_id":1,"snapshot_version":0}
curl -s -X PUT  $BASE/tx/write/1 \
  -d '{"ops":[{"put":{"k":"x","v":"v1"}},{"put":{"k":"y","v":"y1"}}]}'
curl -s -X POST $BASE/tx/write/1/commit -d '{}'
# {"committed":true,"version":1}

# 长读者钉在 v1
curl -s -X POST $BASE/tx/read -d '{}'
# {"read_txn_id":1,"version":1}
curl -s "$BASE/tx/read/1/get?key=x"
# {"found":true,"key":"x","value":"v1"}

# 两个写者都从 v1 出发，都改 x
curl -s -X POST $BASE/tx/write -d '{}'          # -> id 2
curl -s -X POST $BASE/tx/write -d '{}'          # -> id 3
curl -s -X PUT  $BASE/tx/write/2 -d '{"ops":[{"put":{"k":"x","v":"A"}}]}'
curl -s -X PUT  $BASE/tx/write/3 -d '{"ops":[{"put":{"k":"x","v":"B"}}]}'
curl -s -X POST $BASE/tx/write/2/commit -d '{}'
# {"committed":true,"version":2}
curl -s -X POST $BASE/tx/write/3/commit -d '{}'
# HTTP 409 {"committed":false,"error":"write-write conflict","keys":["x"]}

# 最新读到 A；长读者仍读 v1
curl -s "$BASE/get?key=x"                       # ... "value":"A"
curl -s "$BASE/tx/read/1/get?key=x"             # ... "value":"v1"

# 释放长读者后，水位线上移，GC 真正回收旧版本字节
curl -s -X POST $BASE/tx/read/1/release -d '{}'
curl -s "$BASE/stats"
curl -s -X POST $BASE/gc -d '{}'
# 例：{"ran":true,"watermark":8,"bytes_before":422,"bytes_after":97,"reclaimed":325}
```

一把梭：

```bash
./target/release/mvcc-server --dir /tmp/mvcc-demo --addr 127.0.0.1:18080 &
BASE=http://127.0.0.1:18080 examples/demo.sh
```

## 磁盘格式（明确的同步边界）

单个追加文件 `mvcc.log`。整数全部大端。

```
文件头:  b"MVCCRCL1" (8 字节 magic) + 1 字节格式版本(=1)        共 9 字节
之后是若干相互独立的帧，每帧：
  u16 type | u32 payload_len | payload | u32 crc32
  其中 crc32 覆盖 type || payload_len || payload
```

帧类型：

| type | 名称 | payload |
|---|---|---|
| 1 | `PUTV` | `u64 version | u32 klen | key | u32 vlen | value` |
| 2 | `DELV` | `u64 version | u32 klen | key`（墓碑） |
| 3 | `CMMT` | `u64 version | u64 first_record_offset`（提交标记） |
| 4 | `VSET` | `u32 count | count×u64 version`（压缩清单） |

**提交/同步边界**：一个事务先把 `PUTV/DELV` 数据帧追加进去（**不** fsync），
随后追加**单个** `CMMT` 帧并立即 `fsync`。版本是否已提交，当且仅当存在
通过 CRC 校验的 `CMMT`。打开文件时：

- 最后一个完整帧之后的字节视为 **torn tail**（写一半掉电），截断丢弃；
- 没有匹配 `CMMT` 的数据帧是死字节，永不被解读为已提交；
- `CMMT` 指向的数据偏移必须真实存在，否则判为损坏。

**压缩（compaction）**：把保留的记录复制到临时文件，追加一个 `VSET` 清单
（列出保留记录的全部版本），`fsync` 后做两段 rename：

```
fsync(tmp);  rename(log -> old);  fsync(dir);
             rename(tmp -> log);  fsync(dir);
             remove(old);
```

打开时覆盖该窗口的所有崩溃点：有 `log` 无 `old`（正常/完成）；无 `log` 有
`old`（第一次 rename 后崩溃 → 改回 `old -> log`）；两者都有（第二次 rename 已
完成 → 删 `old`）；残留 `*.tmp.*` 删除。压缩后若继续提交，新帧追加在 `VSET`
之后；解析器把“`VSET` 中列出的版本”与“其后 `CMMT` 的版本”取并集，并只对
`VSET` 之前的记录强制“必须在清单中”的校验。

## 可注入 I/O 与故障模拟

引擎只依赖 `io::Io` trait（`open_or_create / rename / remove / truncate_file /
sync_dir / read_file`），文件句柄的 `sync_all` 也走 trait。

- `FaultIo`：按操作类型与出现次数注入 `io::Error`（open/write/sync/rename，可按
  路径过滤）。
- `SimCrashIo`：记录每个文件上次成功 `sync_all` 时的长度；`crash()` 把文件截断
  回该长度，模拟掉电丢失未落盘页缓存。

`tests/fault_injection.rs` 用它们验证：torn tail 修复、已写未 sync 的提交掉电
后消失、发布时 fsync 失败提交不成立、GC 两次 rename 各自失败后的恢复。

## 并发与确定性交错

引擎内部用一把 `Mutex<Inner>` 串行化提交；冲突检查 → （可选 `CommitGate` 钩子）
→ 落盘 → 内存发布都在同一临界区。`CommitGate` 是仅供测试注入的同步点：它在
冲突检查之后、发布之前触发。`tests/acceptance.rs` 的
`threaded_interleaved_conflict_and_monotonic_versions` 用 channel 让写者 W1 停在
发布点、写者 W2 阻塞在引擎锁上，再放行 W1，确定性地产生跨线程冲突；
`many_threads_publish_distinct_monotonic_versions` 让 8 个线程各自重试直到提交
成功，断言版本号恰好是 1..=8。

## 测试

```bash
cargo test                 # 19 个测试（单元 + 3 个集成套件）
cargo clippy --all-targets # 零警告
```

- `tests/acceptance.rs`：固定快照读一致性、FCW 冲突、GC 先保护活跃快照（水位线=3
  时保留 v2 基线与 v3..v8、回收 v1），释放后把版本集合从 8 个压到 2 个基线版本，
  压缩后继续提交、重启持久化，删除基线墓碑不复活。
- `tests/fault_injection.rs`：8 个磁盘/故障用例（见上）。
- `tests/http_api.rs`：完整 HTTP 交错，含 500 错误后进程仍存活。

## 范围与非目标

- 单机、单进程、单文件；不做复制、分片、网络共识。
- HTTP 服务是本地验证入口（线程/连接、无 TLS、无鉴权），不是生产服务器。
- 事务内写集合缓存在内存；适合中小事务。GC 是整文件重写，按 `POST /gc` 触发，
  不做后台自动调度——这样回收边界在测试与演示中是确定可观察的。
