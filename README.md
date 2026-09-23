# mvcc-kv — 单进程 MVCC 键值服务（Rust + Axum）

纯后端、无界面的 HTTP 键值服务，实现多版本并发控制（MVCC）：

- **快照读**：事务在开始时取起始时间戳 `start_ts`，整个事务期间只读到
  `commit_ts <= start_ts` 的已提交数据；之后的提交不可见，旧读不被阻塞也不被改变。
- **写-写冲突检测**：提交时采用 first-committer-wins。若本事务写集中的任意键
  在 `start_ts` 之后被其他事务提交过，则本事务以 **409 Conflict** 中止。
- **墓碑删除**：`delete` 不立即抹掉数据，而是追加一个墓碑版本，旧快照仍可读到删除前的值。
- **版本回收（GC）**：每个键是一条按提交时间戳排序的版本链，GC 按
  “存活快照最小时间戳”（水位线）**就地裁剪**旧版本——不复制整个数据库。
  快照（含未结束的长事务、独立只读快照）关闭前，其依赖的版本不会被回收。

## 依赖

- Rust（开发与实测版本 **1.98.1 stable**，edition 2021；较新的 stable 均可）
- 运行期 crate：`axum 0.8`、`tokio 1`（多线程运行时）、`serde 1`、`serde_json 1`、`base64 0.22`
- 开发期：`tower 0.5`（集成测试用 oneshot 驱动路由）、`base64`
- 无外部数据库、无系统级依赖；数据仅存内存（单进程）。

依赖版本锁定在 `Cargo.lock`。

## 启动

```bash
cargo run --release
# 或指定监听地址：
MVCC_LISTEN_ADDR=127.0.0.1:9000 cargo run --release
```

看到 `mvcc-kv listening on http://127.0.0.1:8080` 即就绪。
健康检查：`curl http://127.0.0.1:8080/health`。

## 测试

```bash
cargo test                 # 全部：9 个引擎单测 + 3 个 HTTP 端到端测试
cargo test --lib           # 仅引擎单元测试
cargo test --test api_acceptance   # 仅 HTTP 验收场景
```

端到端演示（需先启动服务，另开一个终端）：

```bash
bash examples/demo.sh                 # 默认 http://127.0.0.1:8080
bash examples/demo.sh http://127.0.0.1:9000
```

## HTTP 接口

键与值均为字节串，URL/JSON 中以 **URL-safe base64**（字母表 `-`、`_`，
可直接放入 URL 路径段）传输，文本也一样先编码。
下文示例用 `K=$(printf k1 | base64 | tr '+/' '-_')` 等方式计算；
`examples/demo.sh` 已自动处理编码。

### 事务

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/txn` | 开启读写事务，返回 `txn_id`、`start_ts` |
| GET | `/txn/{id}/get/{key_b64}` | 事务内读（读己之写优先，否则读快照） |
| POST | `/txn/{id}/put/{key_b64}` | 缓冲写，body：`{"value":"<base64>"}` |
| POST | `/txn/{id}/delete/{key_b64}` | 缓冲删除（提交时落墓碑） |
| POST | `/txn/{id}/commit` | 提交；冲突返回 **409**，非法/已结束事务返回 **404** |
| POST | `/txn/{id}/rollback` | 回滚，丢弃写集并释放快照 |

### 独立只读快照（不绑定事务）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/snapshot` | 在当前已提交点建快照，返回 `snapshot_id`、`snap_ts` |
| GET | `/snapshot/{id}/get/{key_b64}` | 在该快照点读 |
| POST | `/snapshot/{id}` | 关闭快照（关闭后其依赖版本才可回收） |

### 管理 / 诊断

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/admin/gc` | 执行一次版本回收，返回统计与当前水位线 |
| GET | `/admin/watermark` | 当前水位线（无存活快照为 `null`） |
| GET | `/admin/scan/{ts}` | 列出某时间戳点全部可见（未删除）键值 |
| GET | `/admin/versions/{key_b64}` | 查看键的原始版本链（`commit_ts`、是否墓碑） |

## 请求样例

设 `K=$(printf k1 | base64 -w0)`、`v1` 编码为 `djE=`。

```bash
# 1) 初始提交 k1=v1
T=$(curl -s -XPOST localhost:8080/txn | jq -r .txn_id)
curl -s -XPOST "localhost:8080/txn/$T/put/$K" \
  -H 'content-type: application/json' -d '{"value":"djE="}'
curl -s -XPOST "localhost:8080/txn/$T/commit"      # {"committed":true,"commit_ts":1}

# 2) 开长读事务
R=$(curl -s -XPOST localhost:8080/txn | jq -r .txn_id)
curl -s "localhost:8080/txn/$R/get/$K"             # {"value":"djE="} -> v1

# 3) 三次覆盖（各一个事务）
for V in djI= djM= djQ=; do
  W=$(curl -s -XPOST localhost:8080/txn | jq -r .txn_id)
  curl -s -XPOST "localhost:8080/txn/$W/put/$K" -H 'content-type: application/json' \
    -d "{\"value\":\"$V\"}" >/dev/null
  curl -s -XPOST "localhost:8080/txn/$W/commit"
done

# 4) 删除（墓碑）
D=$(curl -s -XPOST localhost:8080/txn | jq -r .txn_id)
curl -s -XPOST "localhost:8080/txn/$D/delete/$K"
curl -s -XPOST "localhost:8080/txn/$D/commit"

# 5) 旧读不变：R 仍读 v1；新事务读到 null
curl -s "localhost:8080/txn/$R/get/$K"             # {"value":"djE="}

# 6) 并发写，只有一个提交成功
A=$(curl -s -XPOST localhost:8080/txn | jq -r .txn_id)
B=$(curl -s -XPOST localhost:8080/txn | jq -r .txn_id)
curl -s -XPOST "localhost:8080/txn/$A/put/$K" -d '{"value":"dzE="}' -H content-type:application/json
curl -s -XPOST "localhost:8080/txn/$B/put/$K" -d '{"value":"dzI="}' -H content-type:application/json
curl -s -XPOST "localhost:8080/txn/$A/commit"      # 200 committed
curl -s -i -XPOST "localhost:8080/txn/$B/commit"   # HTTP 409 write-write conflict

# 7) 快照开着时 GC 不回收 v1；关闭后回收，当前视图不变
curl -s -XPOST localhost:8080/admin/gc             # versions_reclaimed=0
curl -s -XPOST "localhost:8080/txn/$R/rollback"
curl -s -XPOST localhost:8080/admin/gc             # 回收旧版本与墓碑
```

## 并发与正确性说明

- 所有状态在一把 `std::sync::Mutex` 后保护：`begin / put / delete / get /
  commit / gc` 都是在锁内完成的短临界区，因此提交时间戳严格单调、冲突判定确定，
  不存在丢版本或读到撕裂状态。Axum handler 为 async，但引擎调用本身非异步且不跨
  `.await` 持锁，不会阻塞运行时其他任务的 IO 调度（临界区仅为内存计算）。
- 冲突检测区间：`(start_ts, +∞)` 中键上出现任何已提交版本即冲突
  （等价于 Snapshot Isolation 的提交检测；本服务无唯一索引等额外谓词冲突）。
- GC 安全性：设水位线 `w = min(存活快照 ts)`。某键版本链中“`w` 可见的最新版本”
  及其后所有版本保留，更旧版本回收；没有存活快照时仅保留最新版本。
  因此任何进行中的读，其可见版本都不会被回收。裁剪后若只剩墓碑，键对现存与未来
  所有事务都不可见，整条链删除。
- 快照不复制数据：快照只是一个时间戳数字；历史值由每键版本链就地保留，
  GC 通过 `Vec::drain` 就地截断。

## 实测结果记录

- 环境：Linux x86_64，Rust 1.98.1 stable，`cargo test` 全绿：
  - 引擎单元测试 **9/9 通过**
  - HTTP 端到端测试 **3/3 通过**（含主验收场景
    `acceptance_long_reader_overwrites_delete_conflict_and_gc`）
  - 编译无警告；`Cargo.lock` 已生成并随源码交付。
- 实跑 `examples/demo.sh`（全新进程、空库起步）关键观测：
  - 长读事务 `txn_id=2, start_ts=1` 跨越 k1 的 v2/v3/v4 三次覆盖（commit_ts 2/3/4）
    与墓碑删除（commit_ts 5），全程读到 `v1`；新事务读到 `null`。
  - 版本链诊断显示删除后恰为 5 个版本
    `(1,value)…(4,value),(5,tombstone)`。
  - 并发两事务写同一键：c1 提交成功（commit_ts 8），c2 返回
    `HTTP 409 write-write conflict`，重复提交返回 404；最终落库值为 c1 的 `w1`。
  - 长读事务存活时 `POST /admin/gc` 返回 `watermark=1,
    versions_reclaimed=0`（不回收其依赖版本）；回滚长读事务后 `watermark=null`，
    再 GC：`versions_reclaimed=7, keys_removed=1`（仅余墓碑的 k2 整条移除），
    k1 版本链裁剪为只剩 commit_ts 8 一个版本。
  - 回收前/后用新事务读 k1 均为 `w1`，脚本断言
    `OK: 回收前后可见结果一致`。

## 已知边界 / 未完成项

- 纯内存存储，进程退出数据丢失；无预写日志 / 持久化恢复。
- 无客户端身份认证与多租户隔离；`/admin/*` 与事务接口未做权限控制。
- 事务长期不提交/不回滚会一直钉住旧版本（典型 MVCC “长事务阻碍 GC”问题），
  本版本未实现事务空闲超时自动中止。
- 未实现区间扫描的事务内接口（仅有按时间戳的 `/admin/scan/{ts}` 诊断接口）。
- 单节点、单进程；未做复制、分片与分布式时间戳。
