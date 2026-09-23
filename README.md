# mvcc-gc — MVCC 版本回收（纯后端）

单机、文件支持的键值存储：**快照隔离（SI）事务 + 单调提交版本 + 感知活跃快照的版本回收（压缩）**。
纯 Rust、零第三方依赖；附带本地 HTTP 验证入口、可注入故障的 I/O 层与自动化测试。

- 读：事务/快照固定在开始时的版本，读一致性不受后续提交影响。
- 写：先提交者赢（first-writer-wins），写写冲突的提交被拒绝（HTTP 409）。
- 回收：GC 只能压缩到“最老活跃读者”版本以下；长读者释放后空间才会真正回收。
- 崩溃边界：每次提交/压缩都是 `临时文件 → fsync → 原子 rename → fsync 目录`，恢复时只见到整文件。

无前端。

---

## 1. 构建与运行

```bash
cargo build --release
cargo run --release -- --addr 127.0.0.1:8080 --data ./data
```

要求：Rust 1.75+（开发使用 1.98）。无外部 crate、无系统依赖。

停止：`Ctrl-C`。崩溃（含 `SIGKILL`）后可直接重开同一数据目录；恢复会清理未完成发布的 `.tmp` 文件，无需手工干预。

## 2. 测试与验收

```bash
cargo test                 # 22 个单元/集成测试（引擎、故障注入、HTTP 场景）

# 真实 TCP 端到端走查（先启动服务）：
./scripts/acceptance.sh    # 默认 127.0.0.1:8080
```

- `tests/engine_tests.rs` — 单调版本、固定快照读、写写冲突、GC 保留/回收、重启恢复、目录锁。
- `tests/fault_tests.rs` — fsync/rename/write/remove 注入：失败原子性、重试、崩溃残骸清理。
- `tests/http_scenario.rs` — 可控调度交错两个写者 + 长读者的验收主场景（经 HTTP 路由）。
- `scripts/acceptance.sh` — 对真实运行中的服务做同样的交错验收（含磁盘字节统计）。

请求/响应样例：[`docs/requests.md`](docs/requests.md)。
实际运行记录：[`docs/RUN.md`](docs/RUN.md)（含命令、结果与未通过项的如实记录）。

## 3. HTTP 接口（JSON，`Connection: close`）

| 方法 路径 | 请求体 | 说明 |
|---|---|---|
| `GET /health` | — | 存活检查 |
| `GET /stats` | — | 版本、活跃事务/快照、文件与字节统计 |
| `POST /tx/begin` | — | 开事务，返回 `tx` 与 `read_version` |
| `POST /tx/{id}/put` | `{"key","value"}` | 缓冲写 |
| `POST /tx/{id}/delete` | `{"key"}` | 缓冲删（墓碑） |
| `POST /tx/{id}/get` | `{"key"}` | 事务内读（含读己之写） |
| `POST /tx/{id}/commit` | — | 提交；冲突返回 **409**；I/O 故障 500 |
| `POST /tx/{id}/abort` | — | 中止，丢弃缓冲写 |
| `POST /snapshot` | — | 钉住一个显式只读快照 |
| `POST /snapshot/{id}/get` | `{"key"}` | 在钉住的版本上读 |
| `POST /snapshot/{id}/release` | — | 释放快照 |
| `POST /gc` | — | 执行一次回收/压缩 |
| `GET/POST /admin/faults` | `{"op","leave_written"}` | 查看/装填一次性 I/O 故障 |
| `POST /admin/faults/reset` | — | 解除故障 |

键和值在 HTTP 层按 UTF-8 字符串传输；引擎内部是 `Vec<u8>`。
错误状态码：400 请求体错误、404 事务/快照不存在或键不存在语义、409 冲突、500 I/O。

## 4. 并发与正确性模型

- 进程内单个 `Mutex` 串行化状态转移；**提交是临界区**，版本严格按提交顺序分配（`v, v+1, …`，无空洞）。
- 快照隔离：事务 `T` 的读版本 = begin 时的最新提交版本；它只能读到 `version ≤ read_version` 的单元格。
- 冲突检测（提交时）：`T` 写入的每个键，若其最新提交版本 `> T.read_version`，拒绝整个提交（首写者赢）。
  冲突不自动结束事务——客户端可 `abort` 后重新 begin、重放写（标准 SI 重试模式）。
- 长读者有两种形态：未结束的事务（其读版本同样钉住下界）与显式 `/snapshot`。
- GC 下界（horizon）`H = min(所有活跃事务读版本, 所有显式快照版本, 当前版本)`。

### 回收不变式

> 只要存在读版本为 `R` 的活跃读者，GC 就不得删除任何回答“键 K 在版本 R 的值”所需要的数据。

实现上：GC 先把每个键在 `H` 可见的最新值写成一个新的 **base 文件**并 fsync/rename 落盘，
**然后**才删除所有 `version ≤ H` 的旧文件。因此：

- base 发布前发生故障：旧文件一个都没删，状态不变；
- base 发布后、删除中途发生故障：base 已足够恢复，残留旧文件在下次 GC 或重启时清理；
- 长读者钉在旧版本时 `H` 不下降到其之下，需要的段文件被保留（验收脚本与单测均断言）。

## 5. 磁盘格式（明确约定）

数据目录：

```text
<root>/
  seg-0000000000000001       版本 1 的已提交事务段
  seg-0000000000000002       …
  base-0000000000000003      压缩到版本 3 的基线快照
  .seg-*.tmp / .base-*.tmp   发布过程中的临时文件（重启清理）
```

文件为帧序列，所有整数小端：

```text
u32 magic   = 0x4D56_4343 ("MVCC")
u8  record  = 1 Begin | 2 Commit | 3 Base | 4 Put | 5 Delete
u32 length  = payload 长度
[payload]
u32 crc32   = 对 magic..payload 的 CRC32（IEEE 多项式，同 zlib）
```

- 段：`Begin(version)` → 0..N 个 `Put/Delete`（按键排序）→ `Commit(version)`。
- base：`Base(horizon)` → 压缩点每个存活键一个 `Put`（墓碑在 base 中直接省略）。
- Put 负载：`u32 klen | key | u64 cell_version | u32 vlen | value`；Delete 无 value 部分。
- 段/base 均经 `write_atomic` 发布；恢复逐文件校验 magic、CRC、Begin/Commit 版本一致。

崩溃恢复规则：只重放文件名形如 `seg-/base-` 且校验通过的整文件；删除 `.tmp`；
若发现 `base-H`，则跳过/清理 `version ≤ H` 的旧文件，从 base 重放其后的段。

## 6. 同步边界（durability）

| 动作 | 持久化点 | 故障语义 |
|---|---|---|
| 提交 | 段字节写临时文件 → `fsync(file)` → rename → `fsync(dir)` | fsync 前崩溃：无该版本；rename 后：重启可见该版本 |
| 压缩 | base 同样序列，**成功后**才 unlink 旧文件 | base 未 durable 前绝不删旧数据 |
| 删除 | 普通 unlink；失败则标记 `stale` 重试 | 不影响正确性，仅延迟空间释放 |

注意：本项目在真实文件系统上依赖 `fsync` 与同目录 rename 的 POSIX 语义；
`MemVfs` 上的“崩溃”由 `FaultVfs` 在同一组边界点精确模拟。

## 7. 可注入 I/O 层

- `vfs::Vfs` trait：`create_dir_all/list_dir/open_read/create_new/rename/remove_file/sync_parent/write_atomic`。
- `RealVfs`：真实文件系统（`File::sync_all`、目录 fsync）。
- `MemVfs`：内存文件系统，用于全部单测的“崩溃后重开”。
- `FaultVfs`：包装层，可令下一次 `write/sync/rename/sync_parent/remove` 失败；
  `leave_written=true` 时先施加效果再返回错误，模拟“已落盘但调用方未确认”。

## 8. 目录结构

```text
Cargo.toml
src/
  main.rs      二进制入口（--addr/--data）
  lib.rs
  error.rs     错误类型
  json.rs      零依赖最小 JSON
  vfs.rs       Vfs / RealVfs / MemVfs / FaultVfs
  format.rs    磁盘帧、CRC32、编解码、恢复解析
  engine.rs    MVCC 索引、事务、快照、单调版本、GC、重放
  server.rs    HTTP/1.1 路由与线程模型
tests/         22 个自动化测试
scripts/       端到端验收脚本
docs/          请求样例、运行记录
```

## 9. 明确的范围限制（非目标）

- 单机单进程；无复制、无分布式事务。不支持两个进程同时打开同一数据目录（非目标，不做跨进程互斥）；进程内多线程安全。
- 键值在内存索引中保存；段文件是提交记录与恢复来源，但读路径不做磁盘索引（教学/验证规模）。
- 非持续的长事务不限制数量；它们会持续钉住 GC 下界，这是有意的语义而非泄漏。
- 不做范围扫描、TTL、鉴权与压缩调度自动化（`/gc` 手动触发，便于观察）。
