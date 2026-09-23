# 运行结果记录（如实记录）

记录时间：2026-09-23。以下全部在本环境真实执行，未做修饰。

- 平台：Ubuntu 24.04.4 LTS（x86_64，Linux 6.8）
- 工具链：`rustc 1.98.1 (48a229cea 2026-09-01)`、`cargo 1.98.1 (797e8a9bc 2026-08-05)`，edition 2021
- 依赖（见 `Cargo.lock`，已锁定）：axum 0.7.9、tokio 1.53.1、serde 1.0.229、serde_json 1.0.x；
  开发依赖 tempfile 3.27、tower 0.5.3。无外部数据库。
- 构建：`cargo build` / `cargo build --release` 均 **零警告**通过。

## 1. 自动化测试：`cargo test`

命令：`cargo test --no-fail-fast -- --nocapture`，退出码 **0**。

| 测试文件 | 用例数 | 结果 |
| --- | --- | --- |
| 单元测试（base64 编解码） | 2 | 全过 |
| `tests/store_test.rs`（读写/持久化/共享/隔离/复制计数） | 6 | 全过 |
| `tests/acceptance_test.rs`（**验收场景**：两级分支+交错覆盖+删分支+全量核对+全回收） | 1 | 全过 |
| `tests/crash_test.rs`（**逐点/逐页真实崩溃注入与恢复**） | 7 | 全过 |
| `tests/http_test.rs`（Axum 端到端） | 2 | 全过 |
| **合计** | **18** | **全部通过** |

### 验收场景的复制页数统计（测试实际打印）

```
COW copies = 29, naive whole-snapshot copies = 50
```

场景：main 先写 5 页 → 建 child → 建 grandchild（两级分支），随后 8 次跨分支交错覆盖。

- COW：建 3 个分支各复制 1 个根页（3）+ 5 次基础写×2（10）+ 8 次覆盖×2（16）= **29 个物理页复制**；
  其中建分支对叶子数据 **0 复制**。
- 朴素“整快照复制”：两次分支各复制 5 页（10）+ 8 次整快照写各复制 5 页（40）= **50**。
- 交错覆盖后存活物理页 = **3 个当前根 + 12 个互异叶子 = 13**（与独立根遍历交叉验证一致）。

### 逐点故障注入（真实子进程 `exit(37)`，全部 `crashed=true`）

写页（12 点）、建分支（8 点，含逐页共享计数点 `before_share/after_share`）、删分支（5 点）
**每个点都真实触发了崩溃**，重开恢复后：已提交快照逐字节不变、操作全有或全无、
磁盘页集合 == 可达页集合（无孤儿、无泄漏临时文件）、引用计数 == 独立根遍历计数；随后解除故障重放成功。

```
write @ before_page / after_page / before_setrc / after_setrc / before_root / after_root /
        before_rootrc / after_rootrc / before_manifest   -> post_commit=false（崩溃后操作不可见）
write @ after_manifest / before_free / after_free        -> post_commit=true （崩溃后操作已可见）
branch @ before_root / after_root / before_rootrc / after_rootrc /
         before_share / after_share / before_manifest / after_manifest
delete @ before_manifest_del（不可见）/ after_manifest_del / before_dec / before_free / after_free（已生效）
```

另含两个专项：

- `crash_overwriting_a_three_way_shared_leaf`：覆盖被三方共享的页崩溃后，其余两个分支的同页内容不变；
- `crash_on_each_shared_page_during_branch`：用 `before_share:n` 令建分支时在**第 n 个共享页（n=1..=4）**
  加引用的瞬间逐个崩溃，恢复后重试，最终每个共享叶计数精确为 2；
- `repeated_crashes_then_convergence`：5 个不同点连撞+重放，最终收敛且其他分支保持快照隔离；
- `orphan_from_pre_commit_crash_is_reclaimed_on_later_open`：提交前崩溃留下的新页在重开时被回收，再次重开状态不变。

## 2. 端到端示例：`./scripts/demo.sh`（退出码 0）

实际输出节选（两级分支 + 交错覆盖后的每页内容与物理页 id）：

```
contents after divergence:
  main[0] pid=22 main@0          child[0] pid=2  base-0        grandchild[0] pid=2  base-0
  main[1] pid=14 main@1          child[1] pid=20 child@1       grandchild[1] pid=4  base-1
  main[2] pid=6  base-2          child[2] pid=16 child@2       grandchild[2] pid=6  base-2
  main[3] pid=8  base-3          child[3] pid=8  base-3        grandchild[3] pid=18 gc@3
```

可观察到：`pid=8` 被 main/child 共享、`pid=6` 被 main/grandchild 共享、`pid=2` 被 child/grandchild
共享；分叉的槽位物理页 id 不同。`/stats` 实际返回：

```json
{ "live_page_count": 13, "next_id": 24, "pages_minted_total": 23, "slots": 64,
  "refcounts": {"2":2,"4":1,"6":2,"8":2,"10":3,"14":1,"16":1,"18":1,"19":1,"20":1,"21":1,"22":1,"23":1} }
```

删除 child 后，grandchild 与之共享的页仍可读：

```
grandchild[2] still readable ... -> pid=6 base-2
```

真实进程崩溃段：

```
armed before_manifest
process exit code at crash point: 37 (expected 37)
-- reopen runs recovery; child[1] 仍是旧的已提交值 base-1 --
4 YmFzZS0x
-- 解除故障重放 --
11 Y3Jhc2gtdGhlbi1yZXRyeQ==        # base64("crash-then-retry")
```

## 3. HTTP 冒烟（release 二进制 + curl）

按 README 的 curl 样例对 `target/release/cow-server` 实测：

- 建分支返回 `copied_pages:1`；写页返回 `copied_pages:2`；
- 建 child 后 main/child 的同一槽位返回相同 `page_id`（物理共享）；child 覆盖后两分支页 id 与内容不同（隔离）；
- 状态码：未知分支 `404`、重名分支 `409`、超过 4096 字节 `400`；
- 一次 main 两页 + child 分叉覆盖后 `/stats`：`minted 8 / live 5`，
  `refcounts {共享页:2, 其余:1}`，与预期一致。

## 4. 复现方式

```bash
cargo test                 # 全部自动化测试（含真实 exit(37) 崩溃注入）
./scripts/demo.sh          # 起 HTTP 服务的完整演示 + 崩溃/恢复
cargo build --release
COW_DATA_DIR=./cow-data COW_ADDR=127.0.0.1:8080 ./target/release/cow-server
```

## 5. 未完成项 / 已知限制（如实说明）

- 每个快照为**固定单层 64 槽位**（根页直接存放叶子指针，256 KiB）。未实现多级页树；
  协议可直接推广到多级 COW B 树（沿路径复制、根原子切换），但本作业未做这一层。
- 单进程内用 `Mutex` 串行化；**未支持多个进程同时挂载同一数据目录**（无文件锁防护）。
- 删除分支时逐页递减引用计数的循环若中途崩溃，靠**下次打开时恢复**兜底（已被逐点测试覆盖），
  未在当前存活进程内自动续做未完成的清理。
- HTTP 模式的故障注入以 `panic` 中止单个请求（连接返回 500），进程不退出；
  只有设置 `COW_CRASH_KILL=1`（CLI/演示路径）才是真实 `exit(37)` 进程死亡。
- 页数据不做压缩与内容校验和；MANIFEST 为 JSON 全量重写（快照数量极大时可改为追加日志以减少写放大）。
- 测试依赖真实文件系统的 `rename`/`fsync` 语义（Linux/POSIX）；未在 Windows 上验证。
