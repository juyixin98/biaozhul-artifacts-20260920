# 运行记录（如实记录）

- 日期：2026-09-23
- 环境：Ubuntu (Linux 6.8.0-90-generic)，OpenJDK 21.0.12.1；无 Maven/Gradle，`javac` 直接构建；无网络依赖。
- 所有命令均在项目根目录实际执行。

## 1. 自动化测试：`./scripts/test.sh`

结果：**10 / 10 通过，0 失败，退出码 0**。

```
PASS  json/parse-and-write round trip
PASS  storage/atomic write never leaves a half file
PASS  source/offsets are line numbers and torn tails are trimmed
PASS  operator/keyed sum is deterministic and snapshot-restoreable
PASS  engine/no-fault continuous run reaches baseline offset and sums
PASS  fault-matrix/crash at every checkpoint stage then recover == continuous baseline
PASS  fault-matrix/repeated crashes across and within epochs still converge
PASS  boundary/external side effect before commit is duplicated on recovery
PASS  scheduler/injectable count policy and virtual-clock interval policy
PASS  http/json service: append, drain, checkpoint, status, fault+recover

tests: 10, passed: 10, failed: 0
```

测试覆盖说明：
- 故障矩阵测试在 epoch 2 的 5 个阶段（PENDING_WRITE / STATE_WRITE / COMMIT_RENAME /
  TABLE_APPLY / AFTER_COMMIT）逐一注入故障，断言恢复后的 `committedOffset` 与 `sums`
  与无故障基线逐字节相等，并二次恢复确认无重复提交。
- `RepeatedFaultsTest`：连续 3 个 epoch 崩在 STATE_WRITE、混合 TABLE_APPLY+STATE_WRITE 链路、
  以及同一 epoch 先 STATE_WRITE 后 COMMIT_RENAME 两次崩溃；全部收敛且 `processedCount` 精确。
- `UnsafeSideEffectTest`：证明外部副作用在恢复重试时触发 2 次（边界演示，非缺陷）。

## 2. 端到端故障注入：`scripts/demo-faults.sh`（异常模式）与 `--halt`（真杀 JVM）

两种模式结果相同，**5/5 通过，退出码 0**。基线：`committedOffset=9, sums={a:22,b:15,c:18}`。

```
OK   PENDING_WRITE   crashExit=99 afterCrash(epoch=1,off=4) -> recovered off=9 sums={'a': 22, 'b': 15, 'c': 18}
OK   STATE_WRITE     crashExit=99 afterCrash(epoch=1,off=4) -> recovered off=9 sums={'a': 22, 'b': 15, 'c': 18}
OK   COMMIT_RENAME  crashExit=99 afterCrash(epoch=1,off=4) -> recovered off=9 sums={'a': 22, 'b': 15, 'c': 18}
OK   TABLE_APPLY    crashExit=99 afterCrash(epoch=1,off=4) -> recovered off=9 sums={'a': 22, 'b': 15, 'c': 18}
OK   AFTER_COMMIT   crashExit=99 afterCrash(epoch=2,off=9) -> recovered off=9 sums={'a': 22, 'b': 15, 'c': 18}
result: 5 passed, 0 failed (halt=false   与 halt=true 相同)
```

- `crashExit=99`：注入崩溃确实终止了当次运行（异常模式 CLI 退出码 99；halt 模式 `Runtime.halt(99)`）。
- AFTER_COMMIT 是“提交完成之后”崩溃，故恢复前表已在 epoch 2/off=9，恢复为空操作；
  其余四点恢复前表都停在上一个已提交前缀 epoch 1/off=4。

## 3. 手动 CLI 全流程 + 磁盘现场

在 STATE_WRITE 点崩溃后（恢复前）磁盘文件：

```
output/committing/chk-1.committed      # epoch 1 已提交
output/committing/chk-2.pending        # epoch 2 输出已写但状态未发布 → 恢复时作废重放
state/latest.json                      # 仍是 epoch 1
state/latest.tmp                       # 未发布的状态临时文件（恢复时清理）
output/table.json  source/events.log
```

`peek`（只读、不恢复）：`appliedEpoch=1, committedOffset=4, sums={a:5,b:7,c:3}`。

重新运行（=重启+恢复）后：

```
output/committing/chk-1.committed
output/committing/chk-2.committed      # pending 已随重放后的新 epoch 提交，临时文件消失
state/latest.json  output/table.json  source/events.log
```

`table.json`：`appliedEpoch=2, lastConsumedOffset=9, sums={a:22,b:15,c:18}`，
与连续执行基线一致；`processedCount=10`（无重复计数）。

## 4. HTTP JSON 服务

手工 `curl` 验证（服务在后台启动）：追加 6 条 → `POST /drain` → 状态 `offset=5,
sums={a:5,b:7,c:9}`；安排 COMMIT_RENAME@epoch2 后追加第 7 条并 drain → 返回
`{"crashed":true,"at":"COMMIT_RENAME","epoch":2}`；下一个请求自动“重启恢复”，
再 drain → `offset=6, appliedEpoch=2, sums={a:5,b:7,c:109}`，无重复提交。
（该场景同样被自动化测试 `HttpServiceTest` 覆盖。）

## 5. 未通过项 / 已知限制

- 无未通过项：自动化测试 10/10、两个端到端演示各 5/5 全部通过。
- 已知设计限制（非失败项，见 README 第 6、7、8 节）：
  - exactly-once 仅覆盖受控本地汇总表；任意外部副作用会在恢复时重复（有专门测试固化该行为）。
  - 单线程、单分区、完整内存快照，定位是“小数据精确参考实现”。
  - 崩溃安全聚焦进程级（kill -9 / halt）；掉电级正确性还依赖文件系统写序，未做掉电注入。
