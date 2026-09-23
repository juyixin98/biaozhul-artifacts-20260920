# 运行记录（如实记录）

记录时间：2026-09-23（UTC）。环境：

```text
rustc 1.98.1 (48a229cea 2026-09-01)
cargo 1.98.1 (797e8a9bc 2026-08-05)
Linux 6.8.0-90-generic x86_64 (Ubuntu)
```

本项目**零第三方 crate**，所有运行均离线完成。

## 1. 构建

```bash
cargo build --release
# Finished `release` profile [optimized]
```

```bash
cargo clippy --all-targets
# 0 warnings / 0 errors
```

## 2. 自动化测试

```bash
$ cargo test
test result: ok. 12 passed; 0 failed   # tests/engine_tests.rs
test result: ok. 6 passed;  0 failed   # tests/fault_tests.rs
test result: ok. 3 passed;  0 failed   # tests/http_scenario.rs
# 合计 21 个测试全部通过，0 未通过
```

覆盖：

| 模块 | 覆盖点 |
|---|---|
| engine_tests (12) | 单调版本、事务内读己之写与固定快照、写写冲突拒绝+重试、不交叠键不冲突、删除墓碑、abort 丢弃、显式快照 pin、GC 空操作、无读者全量回收、**长读者钉住旧版本时 GC 保留所需段**、释放后全量回收、提交后重启恢复、压缩后重启恢复并继续提交 |
| fault_tests (6) | fsync 失败后版本不发布且可重试、rename 失败无文件残留、故障一次性自解除、unlink 失败标记 stale 并在下一次 pass 重试成功、base 未 durable 前绝不删旧文件、write 故障留下 `.tmp` 后重开自动清理 |
| http_scenario (3) | **验收主场景**（两写者+长读者可控交错）、未知事务 404、缺字段 400 |

## 2.1 测试驱动开发中发现并修复的两个真实缺陷

1. **GC 空操作路径不清理 stale 文件**：第一次 unlink 注入故障后，下一次 GC 因
   `horizon <= base_version` 提前返回，stale 文件永远不被重试。
   修复：早退前调用 `retry_stale_removals`（`src/engine.rs`）。对应测试
   `gc_remove_failure_leaves_stale_file_retried_next_pass` 先失败、修复后通过。
2. **验收脚本断言写错**（不是引擎问题）：钉住 v1 时 GC 会把 seg-1 折入 base-1，
   保留的是 seg-2/seg-3（2 个段），脚本误断言为 3，已修正为 `"segments":2`。

## 3. 真实 TCP 端到端验收

启动（端口 18123，独立数据目录）：

```bash
./target/release/mvcc-gc --addr 127.0.0.1:18123 --data /tmp/final-data
# mvcc-gc: data dir /tmp/final-data, current version 0
# mvcc-gc listening on http://127.0.0.1:18123

bash scripts/acceptance.sh 127.0.0.1:18123
# fails: 0  ok: 18
```

18 项内联断言全部通过，核心输出（节选）：

```json
// 长读者钉在 v1
{"ok":true,"snapshot":1,"version":1}

// A 先提交 -> v2；B 后提交 -> 409
{"error":"write-write conflict on key \"k\"","ok":false}     // HTTP 409

// B abort 后重新 begin、重放 -> v3
// 被钉住的快照始终读到旧值，最新读读到新值
{"found":true,"key":"k","value":"base"}                      // snapshot@1
{"found":true,"key":"k","value":"B"}                         // latest@3

// 快照未释放时 GC：horizon=1，仅 seg-1 折入 base，seg-2/3 保留
{"base_written":true,"files_removed":1,"horizon":1,...}
{"files":{"bases":1,"segments":2,...},"snapshots":[{"version":1}]}

// 释放后再 GC：horizon=3，旧文件全部删除，字节回收
{"files_removed":3,"horizon":3,"bytes_reclaimed":201,...}
{"files":{"bases":1,"segments":0,"live_bytes":52,...}}

// 注入 fsync 故障：提交被拒、版本不推进，重试成功
{"error":"injected fault: sync failed (injected)","ok":false}
{"ok":true,"version":4}
```

字节账（同一键 k 连续 3 个版本的场景）：

```text
GC 前（3 段）            : 222 字节
钉住 v1 时 GC 后         : 201 字节（base-1=~63 + seg-2 + seg-3）
释放快照、再 GC 后       :  52 字节（仅 base-3）
```

空间回收**只在快照释放后发生**，量化验证了“不得删除仍被活跃快照需要的版本”。

## 4. 磁盘格式抽查（真实文件系统）

```text
$ ls -la /tmp/final-data
base-0000000000000003        52 B
seg-0000000000000004         73 B
```

`xxd` 抽查帧头：

```text
4343 564d  = 小端 u32 0x4D564343 = "MVCC" magic
base-3: ... 03 ...              Base 帧, horizon=3
        ... 04 ... 01 00 00 00 6b ... Put(k) cell_version=3, key="k"
        ... 42 ...              value "B"
seg-4 : 01 ... 04 ...           Begin v4
        04 ... 66 ... 31 ...    Put(f)="1"
        02 ... 04 ...           Commit v4
```

## 5. 重启与崩溃恢复（真实进程）

**优雅停止后重开**：

```text
$ kill <pid>
$ ./target/release/mvcc-gc --addr 127.0.0.1:18099 --data ./run-data
mvcc-gc: data dir ./run-data, current version 4     <- 从磁盘恢复版本号
/stats -> {"base_version":3,"current_version":4,...,"segments":1}
get k  -> "B"                                         <- base+段重放正确
commit -> version 5                                  <- 在恢复状态上继续单调分配
```

**`kill -9` 强杀后重开**（无任何清理机会）：

```text
$ kill -9 <pid>
$ ls run-data/            base-...03  seg-...04   （无 LOCK、无 .tmp，磁盘保持一致）
$ ./target/release/mvcc-gc ...
mvcc-gc: data dir ./run-data, current version 4
get k -> "B"
```

无需手工干预即可重开（见第 7 节关于 LOCK 的设计变更）。

## 6. 故障注入在真实服务上的表现

```bash
curl -X POST .../admin/faults -d '{"op":"write","leave_written":true}'
# 提交中途失败，磁盘上留下半个 .seg-...06.tmp（前缀已落盘）
ls -a data/ | grep tmp   # .seg-0000000000000006.tmp
```

重开时该残骸被删除，`current_version` 不包含失败事务（内存
`FaultVfs+MemVfs` 场景由 `reopen_cleans_temp_debris_after_crash` 单测固化）。

## 7. 开发过程中的一次设计修正（如实记录）

最初实现了一个基于 `LOCK` 文件 + `Drop` 删除的目录占用锁。联调时暴露出两个问题：

1. 验证过程中我从外部对正在运行的数据目录执行了 `rm -rf`，把 LOCK 从外部删掉，
   暴露了“锁文件可被外部绕过”；
2. 更根本地：`SIGKILL` 后 LOCK 残留会阻止合法重开，而做正确的 fcntl `flock`
   在零依赖约束下需要平台特定代码，且内存文件系统无法套用。

鉴于目标明确是**单机单进程**（多进程共享目录本就是非目标），最终**移除 LOCK 机制**，
改为文档中的单写者进程约定（`README.md` §9）。这是设计范围收窄，不是缺陷掩盖：
移除后优雅重启和 `kill -9` 重启均实测正常。对应删除了
`lock_prevents_double_open` 一个测试（其断言的保证不再存在）。

## 8. 未通过项 / 已知限制

- **未通过项：无。** 最终状态 21/21 测试通过、18/18 端到端断言通过、clippy 0 警告。
- HTTP 层为最小实现（每连接一线程、`Connection: close`、JSON 字符串承载键值），
  不是生产级服务器；无鉴权、无范围扫描、无自动 GC 调度（手动 `/gc` 以便观察）。
- 全量键索引驻留内存；段文件用于持久化与恢复，读路径不走磁盘索引。
- 不保证两个进程同时打开同一目录的安全（明确的非目标）。
