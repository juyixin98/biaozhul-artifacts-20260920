# cas-store — 内容寻址块仓库（纯后端）

一个用 Rust 实现的、文件系统支持的内容寻址（content-addressable）块存储：

- 按 **SHA-256** 存储**不可变块**，同内容自动**去重**；
- **具名根（named roots）** 指向块图，根更新通过临时文件 + rename **原子发布**；
- **标记-清扫 GC** 回收孤儿块，与并发读取/上传/根切换安全共存——**可达块永不误删**；
- 哈希只用于**寻址、去重与完整性校验**，不作为安全/信任边界；
- 所有磁盘 I/O 经过**可注入的 `Vfs` 层**，测试用 `FaultFs` 精确注入故障（如"根切换 rename 瞬间崩溃"）。

无 Web 框架、无异步运行时：HTTP 验证入口是手写的极简 HTTP/1.1 服务（每连接一线程，单请求/连接）。

## 构建与运行

```bash
cargo build
cargo test                      # 45 个测试（含并发与故障注入）
cargo run -- /tmp/cas-data      # 启动服务, 默认 127.0.0.1:8080
cargo run -- /tmp/cas-data --addr 0.0.0.0:9000
```

请求样例（完整走通上传→发布→切换→GC→校验）：

```bash
./examples/requests.sh                    # 正常流程
./examples/corrupt_and_verify.sh /tmp/cas-data   # 篡改磁盘块 → /verify 发现
cargo run --example gc_concurrent_demo    # 并发混沌演示（读+切根+GC 同时进行）
```

## 磁盘格式（`cas-store v1`）

```
<repo>/
  format                      # 一行 "cas-store v1\n"，不符则拒绝打开
  repo.lock                   # flock(LOCK_EX|LOCK_NB) 目标，进程级单写者
  blocks/
    ab/
      <64-hex>                # 块原始字节，发布后不可变
      <64-hex>.meta           # JSON: {"refs": ["<hash>", ...]}，声明出边
  roots/
    <root-name>               # JSON: {"root","hash","published_at"}，原子发布
  tmp/                        # 预留
```

- 块数据与 `.meta`、根清单都经 **唯一临时文件 + fsync + rename** 原子发布；
  中断只可能留下 `.tmp.*` 残文件，**绝不会**在内容地址上出现撕裂文件；
  残文件在下次 `open` 时清扫。
- 块文件创建后永不修改（rename 覆盖只发生在同内容去重竞争时，字节必然相同）。
- `.meta` 是可选的边信息：缺失视为无出边（叶子块合法）；孤儿 `.meta` 会被 GC 清理。

## 同步边界（明确声明）

| 边界 | 机制 |
|---|---|
| **进程间** | 单写者：`open` 时对 `repo.lock` 取非阻塞排他 `flock`；第二个进程得到 `Locked` 错误。读者进程不在设计内（需要时自行加只读模式）。 |
| **进程内线程** | 任意线程并发读/写。上传、发布、读取持 `gc_lock` 读守卫；GC 仅在**最终标记+清扫**阶段持写守卫（排他）。读操作在整个读取期间持有读守卫，清扫无法把正在服务的字节从读端抽走。 |
| **崩溃** | temp+fsync+rename：观察者只能看到旧文件或新文件。根切换在 rename 瞬间崩溃 → 旧根完好（有专门故障注入测试）。 |
| **哈希** | SHA-256 仅用于寻址/去重/完整性。上传不认证；`?verify=1` 与 `/verify` 提供完整性校验。 |

### GC 并发协议

1. 读守卫下做一次预标记（仅用于报告）；
2. 写守卫下**重新**快照根、重新标记（权威），然后删除不可达块。
   权威标记发生在排他窗口内，因此"标记时可达、清扫时被删"不可能发生。
3. **暂存（staging）**：`Repository::stage(hash)` 把"已上传、尚未挂根"的块
   固定为临时 GC 根（引用计数，RAII 守卫）。`put_root` 内部对目标自动
   暂存。没有它，"上传完成→根切换"间隙里的 GC 合法回收未来根是符合
   语义但违反直觉的行为——这是本仓库明确处理的一个真实竞态。

## HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/blocks` | 上传原始字节（`X-Refs: h1,h2` 声明引用）；或 JSON `{"data":"<hex>","refs":[...]}` |
| PUT | `/blocks/<sha256>` | 内容寻址上传，地址与内容不符 → 400 `hash_mismatch` |
| GET | `/blocks/<sha256>` | 下载；`?verify=1` 重哈希校验，损坏 → 422 |
| HEAD | `/blocks/<sha256>` | 存在性 + 大小（`X-Content-Sha256`） |
| PUT | `/roots/<name>` | 原子发布/切换根（`X-Block-Hash` 头或 JSON `{"hash":...}`）；目标缺失 → 409 |
| GET | `/roots` `/roots/<name>` | 列出 / 读取根 |
| DELETE | `/roots/<name>` | 删除根（其独占块成为 GC 候选） |
| POST | `/gc?dry_run=0\|1` | 标记-清扫（默认 `dry_run=1` 只报告） |
| POST | `/verify` | 全量完整性扫描（重哈希 + 悬空引用） |
| GET | `/healthz` `/` | 存活 / 帮助 |

错误统一为 JSON：`{"error": "<code>", "message": "..."}`，状态码映射：
400 请求非法/hash 不匹配，404 不存在，409 引用缺失/仓库被锁，422 内容损坏，500 内部错误。

## 可注入 I/O 层与故障演练

`src/vfs.rs` 定义 `Vfs` trait，三个实现：

- `RealFs` — 生产后端（O_EXCL、fsync、目录 fsync）；
- `MemFs` — 纯内存后端，并发测试零临时目录；
- `FaultFs` — 装饰器，按规则注入故障：
  `Fault { path_contains, op: Rename|Write|Create, once: Some(n) }`。
  rename 故障**故意留下临时文件**，精确模拟"数据已写、rename 未发"的崩溃点。

## 验收场景 → 测试对照

| 验收项 | 测试 |
|---|---|
| 并发上传同一块 | `gc.rs::concurrent_uploads_of_same_block_deduplicate`（16 线程）、`http_api.rs::concurrent_clients_uploading_same_block` |
| 孤儿块回收 | `gc.rs::dry_run_reports_but_keeps_everything` / `sweep_removes_orphan_only` |
| 缺失引用 | `gc.rs::missing_refs_are_reported_not_fatal_and_reachable_set_is_protected`（外部删块造洞，GC 报告不误删可达块） |
| 根切换中断 | `roots.rs::root_publish_crash_leaves_old_root_intact`（FaultFs 在第二次 rename 注入崩溃，旧根完好） |
| 可达块不误删（GC×读×切根并发） | `gc.rs::gc_never_removes_blocks_read_while_sweeping`（6 读线程 + 2 切根线程 + 30 轮 GC）、`gc_with_concurrent_root_switch_...` |
| 完整性 | `faults.rs::corrupted_block_on_disk_is_detected` 等 |
| 进程锁 | `gc.rs::opening_twice_is_rejected_by_process_lock` |

运行记录见 [RUNLOG.md](RUNLOG.md)。

## 已知限制

- HTTP 层是验证入口而非生产网关：单请求/连接、无 TLS、无认证、请求体上限 64 MiB。
- 单写者进程；跨进程并发写不在设计内（flock 直接拒绝）。
- 暂存（staging）是进程内状态：进程崩溃后未发布的上传成为普通孤儿，由下次 GC 回收（语义安全）。
- 块整体读入内存；未做流式/分块上传。
