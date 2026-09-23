# cas-gc — 内容寻址对象库与引用计数回收（Rust + Axum）

纯后端服务。实现**内容寻址存储（CAS）**：清单（manifest）可引用其他清单和数据块（blob）；
命名的**根（root）**与**未完成上传（staged upload，带独立保留期）**是 GC 的两类存活起点。
垃圾回收为单次临界区内完成的 **mark-and-sweep**，因此“并发发布根 / 回收 / 重启”不会误删
新根可达对象；孤立块在删除根后最终可被回收。

## 依赖与启动

- Rust + cargo（已用 **rustc 1.75.0** 实际编译测试通过；代码为 edition 2021，高版本 stable 亦可）
- 仅一个直接外部依赖集：`axum` / `tokio` / `serde` / `serde_json` / `sha2` / `hex`
  （开发依赖 `tower`、`tempfile`）
- 无外部数据库 / 无外部服务；数据落盘到本地目录（默认 `./cas-data`）
- 网络：首次构建需要从 crates.io 拉取依赖（`Cargo.lock` 已锁定版本，lockfile v3）
  - 注：为兼容 rustc 1.75，`getrandom` 在锁文件中钉到 `0.3.3`（新版 0.4.x 需要 edition2024）。用更新的 stable 工具链时可放开。

```bash
cargo build --release
CAS_ADDR=127.0.0.1:8080 CAS_DATA_DIR=./cas-data CAS_UPLOAD_RETENTION_SECS=3600 \
  ./target/release/cas-gc
# 或直接：cargo run
```

环境变量（均有默认值）：

| 变量 | 默认 | 含义 |
|---|---|---|
| `CAS_ADDR` | `127.0.0.1:8080` | 监听地址 |
| `CAS_DATA_DIR` | `./cas-data` | 数据目录（`objects/` + `state.json`，tmp+rename 原子写入） |
| `CAS_UPLOAD_RETENTION_SECS` | `3600` | 未完成上传默认保留期（秒），可在每次上传时单独覆盖 |

## 数据模型

- **Blob**：任意字节，哈希 `sha256` 即对象 ID。
- **Manifest**：JSON `{"manifests": ["<hash>", ...], "blobs": ["<hash>", ...]}`，
  可传递引用其他清单与块；写入时校验**整个传递引用闭包**已存在，否则 `422`。
- **Root**：`name -> manifest hash`。根的引用闭包始终受保护。
- **Upload（未完成上传）**：创建时即固化一个 manifest 并获得**独立保留期**；
  保留期内即使没有任何根引用它，其闭包也受 GC 保护。
  - `complete` 可在**同一原子操作**里把 manifest 发布为命名根；
  - `abort` 立即解除保护；保留期到期后下一次 GC 先使上传过期再回收其闭包。

## 并发安全是怎么保证的

所有变更（上传 blob/manifest、增删根、暂存/完成/中止上传）以及 GC 的
“标记 → 清除”全过程都在**同一个 `std::sync::Mutex`** 的临界区内完成
（见 `src/gc.rs` 顶部注释）。因此 GC 看到的是根集合的一致快照：

- GC 开始时已存在的根 → 其闭包一定被标记，绝不可能被本轮清除；
- GC 开始后才发布的根 → 由后续轮次负责，其对象在暂存期内由 upload 保护；
- 不存在“先标记、后发布、误清除”的交错窗口。

## HTTP 接口

```
POST   /blobs                       # raw body
GET    /objects/{hash}              # raw bytes（manifest 返回 application/json）
POST   /manifests                   # {"manifests":[...], "blobs":[...]}
GET    /manifests/{hash}
GET    /roots
GET    /roots/{name}
PUT    /roots/{name}                # {"manifest":"<hash>"}
DELETE /roots/{name}
POST   /uploads                     # {"manifest":{...}, "retention_secs":N?}
GET    /uploads
POST   /uploads/{id}/complete       # {"root":"<name>"}?   body 可空
POST   /uploads/{id}/abort
POST   /gc                          # 立即跑一轮 mark-and-sweep，返回报告
GET    /reachable                   # 当前根 + 未过期上传的全部可达哈希
GET    /status
```

`POST /gc` 返回：

```json
{
  "generation": 3,
  "marked": 5,
  "swept": 2,
  "swept_hashes": ["…", "…"],
  "expired_uploads": ["up-…"],
  "remaining": 9
}
```

## 请求样例

完整可运行的演练脚本见 [`examples/demo.sh`](examples/demo.sh)（要求 `curl`、`jq`）：

```bash
./examples/demo.sh            # 默认 http://127.0.0.1:8080
# CAS_BASE=http://host:port ./examples/demo.sh
```

手动执行：

```bash
# 1) 上传数据块
curl -sS -X POST --data-binary 'shared block' http://127.0.0.1:8080/blobs
# {"hash":"4369...","size":12}

# 2) 构造清单（把 <SHARED> 换成上一步返回的 hash）
curl -sS -X POST http://127.0.0.1:8080/manifests \
  -H 'content-type: application/json' \
  -d '{"blobs":["<SHARED>"]}'

# 3) 发布根
curl -sS -X PUT http://127.0.0.1:8080/roots/rootA \
  -H 'content-type: application/json' -d '{"manifest":"<MANIFEST>"}'

# 4) 未完成上传（独立保留期 600 秒）
curl -sS -X POST http://127.0.0.1:8080/uploads \
  -H 'content-type: application/json' \
  -d '{"manifest":{"blobs":["<SHARED>"]},"retention_secs":600}'

# 5) 触发 GC / 查看可达集合 / 状态
curl -sS -X POST http://127.0.0.1:8080/gc
curl -sS http://127.0.0.1:8080/reachable
curl -sS http://127.0.0.1:8080/status

# 6) 删除根后再 GC：仅不可达块被清除，共享块由其他根保留
curl -sS -X DELETE http://127.0.0.1:8080/roots/rootA
curl -sS -X POST http://127.0.0.1:8080/gc
```

## 验收场景与测试

```bash
cargo test
```

`tests/acceptance.rs` 覆盖验收要求：

1. **共享子图**：两个根共享同一子清单/块；删除根 A 后 GC，只回收 A 独有块，
   共享块仍由根 B 保留；再删根 B，全部最终可回收（含磁盘文件已删除的断言）。
2. **并发发布另一根 vs 回收**：50 个闭包先暂存为未完成上传，
   多任务并发“完成上传（=发布新根）”与 4 路 GC 各 100 轮；
   每轮清除结果都与实时可达集合交叉校验，终局 GC 清除数为 0、50 个根与全部对象可读。
   另有 `republish_while_gc_running` 在 GC 密集运行中反复发布 200 个根并即时校验。
3. **回收重启**：关闭后以同一目录 `Store::open` 重开，根/对象保留，
   已回收对象保持缺失，重启后发布新根仍受保护。
4. **保留期**：保留期内 GC 不回收；`abort` 立即释放；`retention_secs=0` 到期即回收。
5. 内容寻址去重、缺失/传递缺失引用 422 拒绝。

## 布局与持久化

```
cas-data/
  objects/<sha256>      # blob 与 manifest 原文（manifest 即其 JSON 字节）
  state.json            # 代际、roots、全对象索引、manifests 集合、uploads（tmp+rename 原子写）
```

重启时扫描 `objects/` 与索引取并集再与磁盘文件对账：已删除文件不会产生幽灵对象，
崩溃残留（已写对象但未刷状态）会被采纳进索引并自然成为 GC 候选；指向缺失 manifest 的
根/上传在重开时被剔除。

## 实测结果（本环境真实运行记录）

环境：Linux x86_64，rustc/cargo 1.75.0，工具链以系统 deb 包解包到用户目录使用
（该机器上 rustup 官方源下载反复超时，故改用发行版 rustc 1.75；与代码兼容性无关）。

- `cargo build` / `cargo build --release`：成功，无编译警告。release 产物约 10 MB。
- `cargo test`：**8/8 通过**（约 19–27s，含多任务并发与真实磁盘重启用例）：
  - `shared_subgraph_survives_and_orphan_is_collected` ✅
  - `staged_upload_retained_until_aborted` ✅
  - `expired_upload_is_swept` ✅
  - `complete_upload_publishes_root` ✅
  - `content_addressing_dedup_and_missing_refs` ✅
  - `concurrent_publish_vs_gc_never_collects_live_roots`（50 上传 × 4 路 GC×100 轮）✅
  - `republish_while_gc_running`（GC 密集运行中发布 200 根并即时校验）✅
  - `state_survives_restart_and_unreachable_collected_after_reopen` ✅
- 真实 HTTP 端到端（`examples/demo.sh`，实际输出）：
  - 两根共享子图，另有一块仅被未完成上传引用。GC#1 `swept=0`（孤立块正被暂存保留期保护，`marked=8`）；
    abort 上传后 GC#2 清除孤立块与该上传 manifest 共 2 个，共享块仍可读；
    删除 rootA 后 GC#3 仅清除 A 独有的 manifest+blob 2 个，共享子图仍由 rootB 保留
    （`/reachable` 恰为 4 个哈希）；删除 rootB 后 GC#4 清除剩余 4 个，`remaining=0`。
- 真实 HTTP 并发压力：50 个未完成上传，8 路并发“完成=发布新根”与连续 GC 同时进行，
  50 次发布全部 HTTP 200；并发结束后终局 GC `swept=0`，`objects=100, roots=50`，无对象被误回收。
- 回收重启：停服后数据目录为 `objects/`(100 文件) + `state.json`(gc_gen=9, roots=50)；
  以同目录重启，`/status` 恢复 100 对象/50 根，重启后 GC `swept=0`，根指向的 manifest 可正常读取。

以上输出为实际运行所得；测试与 demo 可随时用上述命令复现。

## 未完成项 / 已知边界

- 状态用单个互斥锁串行化：实现简单、正确性可证，代价是 GC 与写入不能并行
  （对本验收规模无影响；更大规模可改为代际标记 + 写屏障）。
- 持久化每次变更全量写 `state.json`/`objects.json`：适合中小规模；未做分片/WAL。
- GC 为手动触发（`POST /gc`），未内置定时回收线程（可按需加后台任务）。
- 无鉴权/TLS/压缩；定位为本地或内网服务。
- 保留期精度为秒。
