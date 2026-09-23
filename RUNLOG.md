# RUNLOG — 实际运行记录

环境：Linux 6.8.0-90-generic (Ubuntu 24.04)，rustc 1.98.1 / cargo 1.98.1。
所有命令在仓库根目录执行。日期：2026-09-23。

## 1. 构建

```
$ cargo build            → Finished dev profile（0 警告）
$ cargo build --release  → Finished release profile [optimized] in 11.23s
```

## 2. 自动化测试（最终结果）

```
$ cargo test
  unittests src/lib.rs   6 passed
  unittests src/main.rs  0 passed
  tests/blocks.rs        9 passed
  tests/faults.rs        6 passed
  tests/gc.rs           11 passed
  tests/http_api.rs      7 passed
  doc-tests              0 passed, 1 ignored（文档中的 ignore 示例）
  合计 45 passed, 0 failed
```

稳定性复跑：`tests/gc.rs::gc_never_removes_blocks_read_while_sweeping`
（6 读线程 + 2 切根线程 + 30 轮 GC）连续 10 次全部通过；
完整套件连续 3 次全部通过。

### 开发过程中出现并已修复的失败（如实记录）

1. **死锁**：`merge_meta` 持有 `meta_lock` 又调用再次加锁的 `write_meta`
   （std Mutex 不可重入），`identical_content_deduplicates` 等 2 个测试挂起。
   gdb 抓栈定位后改为调用 `_locked` 变体，删除重复加锁函数。
2. **测试自身错误**：`refs_are_stored_and_traversed` 误用真实存在的哈希当
   "缺失引用"；`reopen_persists...` 与 `real_fs_gc_and_reopen_roundtrip`
   在旧仓库未 drop（flock 未释放）时重开 → `Locked`。均已修正测试。
3. **真实并发缺陷（重要）**：并发"根切换 + GC"场景中，当两个根瞬时指向
   同一棵树时，另一棵树的根块**合法地**不可达并被 GC 回收，随后切回失败
   （`MissingReference`）。这不是标记/清扫的互斥问题（读守卫验证 40 个
   叶子从未被误删），而是"已上传、未挂根"目标缺少保护的语义缺口。
   修复：引入**暂存（staging）**机制——`Repository::stage()` RAII 守卫 +
   `put_root` 内部自动暂存，GC 权威标记把 staged 集合并入根集合。
   同时修正测试场景（backup 根原本只固定叶子、未固定树根 A/B）。
4. **HTTP HEAD**：`Content-Length` 被空 body 覆盖为 0 → `write_response`
   改为尊重显式头。
5. **故障注入语义**：写故障后 `MemWriter` 的 Drop 仍把残数据发布到内容
   地址 → 块数据改为 temp+rename 原子发布，写失败时清理临时文件。

## 3. 端到端演示（真实进程 + curl）

```
$ cargo run -- /tmp/cas-demo --addr 127.0.0.1:18099   # 后台启动
$ ./examples/requests.sh 127.0.0.1:18099
```

关键结果（完整输出见会话记录）：

- 上传/去重：`deduplicated: false → true`，哈希一致；
- `PUT /blocks/<hash>` 地址不符 → `400 {"error":"hash_mismatch"}`；
- 发布根 `main`→v1，切换到 v2，`GET /roots` 反映新值；
- 孤儿块（未挂根）在 `dry_run=1` 中列出但不删（`orphan still present: 200`）；
- `dry_run=0` 后：孤儿 404，共享叶 L1 200，v2 叶 L3 200，v1 独有叶 L2 404
  （切换后不可达，**符合预期**）；`reachable: 3` 与 v2 树一致；
- 删除根后再 GC：3 个剩余块全部回收，`blocks_total: 0`。

### 故障演练

```
$ ./examples/corrupt_and_verify.sh /tmp/cas-demo 127.0.0.1:18099
  篡改磁盘块首字节 → 普通 GET 返回损坏字节（存储层不主动重哈希）
  → GET ?verify=1 → 422 {"error":"corrupt"}
  → POST /verify → corrupt: ["f6b0c08f..."]  ✔
```

### 磁盘格式 / 进程锁 / 崩溃恢复

```
$ find /tmp/cas-demo -type f
  blocks/f6/f6b0c08f...  blocks/f6/f6b0c08f....meta  format  repo.lock
$ cat /tmp/cas-demo/format → cas-store v1
$ ./target/debug/cas-store /tmp/cas-demo   # 第二个进程
  → failed to open repository: repository is locked by another process ✔
$ touch blocks/f6/.tmp.999.crashed && 重启服务
  → .tmp 残留被 open 清扫；仓库正常服务；此前标记的 corrupt 块仍被 /verify 报告 ✔
```

### 并发混沌演示

```
$ cargo run --example gc_concurrent_demo
  6 读线程(verified read) + 1 切根线程(stage+put_root) + 30 轮 GC(每轮先造 5 个孤儿)
  → 无任何可达块被删；孤儿全部被回收；输出 "done"，退出码 0 ✔
```

## 4. 未通过项 / 已知限制（最终状态）

- 测试：**无未通过项**（45/45 通过，1 个文档示例按约定 ignore）。
- 功能限制（设计内，见 README「已知限制」）：HTTP 层单请求/连接、无 TLS/
  认证；单写者进程；staging 为进程内状态（崩溃后未发布块成为孤儿，语义
  安全）；块整体入内存，无流式上传。
