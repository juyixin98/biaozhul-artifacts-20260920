# 运行记录（RUNLOG）

本文件如实记录实际执行过的命令与结果。环境：

- Linux 6.8.0-90-generic（x86_64）
- `rustc 1.98.1 (48a229cea 2026-09-01)`、`cargo 1.98.1`
- 零第三方依赖（仅标准库）；`#![forbid(unsafe_code)]`

## 1. 构建

```bash
$ cargo build --release
# 无 warning、无 error，产物 target/release/dual-sb（约 872 KB）
```

`cargo clippy --all-targets`：**0 warning / 0 error**（中途出现过的 3 个
风格项——可省略生命周期、延迟初始化、复杂类型——均已消除）。

## 2. 自动化测试

```bash
$ cargo test
test result: ok. 7 passed   (src 单元测试：crc32 / format / io)
test result: ok. 0 passed   (main 无单测)
test result: ok. 13 passed  (tests/crash_recovery.rs)
test result: ok. 4 passed   (tests/persistence.rs)
test result: ok. 0 passed   (doc-tests)
```

合计 **24 个测试全部通过**。覆盖：

- CRC-32 已知向量（`"123456789"` → `0xCBF43926`）；
- 超级块/记录编解码、半页撕裂页与单 bit 翻转被 CRC 拒绝；
- 模拟器页缓存/掉电/撕裂前缀语义；
- **完整崩溃矩阵**：提交第 3 代时 8 个故障点 × 3 种撕裂（24 用例）；
- 第 2 代首次回退矩阵（8 点、半页）；
- 半页超级块写 → 回退旧槽；两个根均损坏（半清零 & 双 bit 翻转）→ 拒绝；
- 更高代次但指针越界 → 拒绝并回退；引用记录头 CRC 损坏 → 回退；
- 撕裂数据记录被截断；未同步脏写掉电后全丢；槽位交替、代次单调；
- **真实文件**后端：重开持久化、删除、磁盘文件形状、孤儿尾部截断、
  越界根（带合法 CRC）拒绝、空目录以第 0 代打开。

## 3. CLI 崩溃演示（实际输出）

```bash
$ ./target/release/dual-sb demo matrix
DataWriteBegin torn=None  -> gen 2 (want 2) value="v2" orphan=0  PASS
DataWriteBegin torn=Half  -> gen 2 (want 2) value="v2" orphan=26 PASS
DataWriteBegin torn=Short -> gen 2 (want 2) value="v2" orphan=51 PASS
DataWriteEnd   ...（同上三态）... PASS
DataSyncBegin  ...（None orphan=0；Half/Short 为撕裂头前缀 26/51）... PASS
DataSyncEnd    ...（全 52：数据已同步但根未发布）orphan=52 PASS
RootWriteBegin / RootWriteEnd / RootSyncBegin ... orphan=52，回退 gen 2 PASS
RootSyncEnd    torn=None/Half/Short -> gen 3 (want 3) value="v3" orphan=0 PASS
24 / 24 cases passed

$ ./target/release/dual-sb demo matrix2
8 / 8 cases passed

$ ./target/release/dual-sb demo scenarios
half-page-superblock-write       -> recovered gen 2 (expected 2) ... passed=true
both-roots-corrupt               -> open refused: both superblocks invalid (as required)
out-of-bounds-newer-root-rollback-> recovered gen 2 ... gen-99 slot rejected: OutOfBounds
```

观察到的截断字节数与故障模型一致：

- 数据写阶段半页撕裂 → 26（52 字节记录的一半）/ 51（短前缀）字节孤儿；
- 数据已 `fsync` 但根未发布 → 整 52 字节成为孤儿，恢复时截断；
- 最终根屏障之后 → 0 截断，新一代完整可见。

## 4. HTTP 端到端（真实文件后端）

```bash
$ ./target/release/dual-sb serve --dir /tmp/dsbhttp --addr 127.0.0.1:18xxx
```

依次 `put name=alice`(g1)、`put city=paris`(g2)、`put name=bob`(g3)、
`get`、`list`、`delete city`(g4)，重启进程后：

```
gen 4 [{'key': 'name', 'value': 'bob'}]      # city 删除已持久
启动恢复日志：
  selected slot Some(1) gen Some(4), data 225 bytes (0 truncated)
  slot 0: valid gen 3, 172 bytes, 53 orphan tail
  slot 1: valid gen 4, 225 bytes, 0 orphan tail
```

`/demo/matrix` 返回 `cases 24 passed 24 all True`；`/demo/scenarios`
三个场景结论均正确。请求样例见 `examples/requests.sh`、
`examples/requests.http`（已 `chmod +x`，可用 curl 复现）。

## 5. 开发过程中真实踩到、曾导致测试失败并已修复的缺陷（如实记录）

这些问题都曾让 `cargo test` 或真机 CLI 失败，定位并修复后全部转绿：

1. **记录偏移 4 对齐的错误假设。** 早期 `parse_at` 要求偏移 4 对齐；
   记录按变长载荷紧凑排列（首条 53 字节），第二条落在偏移 53（非 4 对齐），
   导致真实文件两次提交后恢复误报 `OutOfBounds`。删除该对齐检查，仅保留
   边界/CRC/自描述偏移检查。
2. **「无前驱」哨兵与偏移 0 冲突。** 最初用 `prev_off == 0` 表示链首，
   但偏移 0 本身是第一条记录的合法地址，第二条记录合法地 `prev_off=0`，
   造成链遍历提前终止。改为显式哨兵 `NIL_PREV = u32::MAX`，并规定
   回走到偏移 0 即链首、链首必须带 NIL。
3. **错误地要求整条链代次相同。** 每条记录本就保留各自写入代次，只有
   链头代次必须等于超级块代次。修正为仅校验头记录代次。
4. **崩溃策略在种子提交阶段被误触发。** 策略最初全程武装，第 1 个种子
   提交就触发了 `SimulatedCrash`。改为：先以「绝不崩溃」策略完成种子
   提交，**打开最终仓库之后**再武装一次性策略（恢复自身的
   truncate/fsync 因此不会误触发注入崩溃）。
5. **模拟器读路径未叠加脏页层。** `read` 只返回持久字节，活动进程在
   `fsync` 前看不到自己刚写的记录。改为返回「持久层 ⊕ 脏层」的页缓存
   视图；重启后脏层为空，自然只返回掉电存活字节。
6. **RootSyncEnd 的语义。** 该点在最终根屏障返回后触发：返回
   `SimulatedCrash` 错误，但提交已持久，恢复必须选到新一代。测试据此
   期望 `want = rounds`（而非回退）。

## 6. 未通过项 / 限制（如实说明）

- 最终状态：**全部验收用例通过，无未通过项**；无 `unsafe`，无外部依赖。
- 已知范围限制（非失败，属设计取舍）：
  - 记录后向偏移 `prev_off` 为 u32，单数据文件逻辑上限约 4 GiB；
  - 超级块固定 4096 字节、整块覆写，依赖「单块写可能撕裂但可被整页
    CRC 检出」这一故障模型（未模拟盘片静默损坏后 CRC 恰好仍合法的情形，
    那需要更强的校验/副本，超出本题范围）；
  - HTTP 服务为本地验证用的简单每连接线程模型，非生产级服务器。
