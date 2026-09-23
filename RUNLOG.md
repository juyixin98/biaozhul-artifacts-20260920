# 运行记录（RUNLOG）

- 机器/环境：Linux 6.8.0-90-generic x86_64，Go 1.22.2 linux/amd64
- 记录时间：2026-09-23
- 依赖：仅 Go 标准库，无外部网络/数据库/集群依赖
- 以下全部为实际执行输出（可由 `./accept.sh` 一键复现）。

## 1. 环境

```
$ go version
go version go1.22.2 linux/amd64
```

## 2. 静态检查

```
$ go vet ./...
（无输出，通过）
```

## 3. 自动化测试

```
$ go test -race -count=1 ./...
?   	twopcsim/cmd/twopcsim	[no test files]
?   	twopcsim/internal/twopc	[no test files]
ok  	twopcsim/internal/runner	1.134s
ok  	twopcsim/internal/sim	1.015s
ok  	twopcsim/internal/wal	1.023s
```

`go test -v` 用例明细（全部 PASS）：

| 包 | 用例 | 验证内容 |
|---|---|---|
| runner | TestHappyPathCommitsAllThree | 干净网络 3/3 提交 |
| runner | TestDeterministic | 同种子两次运行 trace 逐条一致 |
| runner | TestLossyNetworkConsensus | 30% 丢包+重复+乱序下靠重发达成一致 |
| runner | TestVoteNoAbortsAll | 一张否决票 ⇒ 全员中止 |
| runner | TestCrashMatrixNoPartialCommit（6 子用例） | 协调者/参与者在 START、COMMIT、ABORT、PREPARED 等各阶段崩溃重启，无部分提交且裁决正确 |
| runner | TestBlockingWhenCoordinatorDownForever | 提交点后协调者永久不可用 ⇒ 3 参与者持锁阻塞、无自行回滚 |
| runner | TestBlockingBeforeDecision | 决议前协调者永久不可用 ⇒ 经典阻塞，磁盘无决议 |
| runner | TestCrossProcessResumeCommits | 第二个进程复用数据目录后 blocked → committed(3/3) |
| runner | TestDurableStateReconstructedFromWAL | 宕机节点状态可由 WAL 重放重建为 prepared/持锁 |
| sim | TestDeterministicReplay / TestDeterministicRng / TestCancelNode | 确定性调度、确定性随机、崩溃撤销定时器+在途消息 |
| wal | TestAppendReplayAndReopen / TestCorruptLineReported | fsync 追加、重启重放、resume 追加、损坏检测 |

测试过程中出现并修复的两个真实问题（如实记录）：
1. 早期版本崩溃只撤销“接收方拥有”的事件，导致协调者在 Hook 返回前已入队的
   GLOBAL_COMMIT 仍在崩溃后送达，05 号场景错误地全员提交。修复：调度器新增
   发送方标记与 `CancelNode`，崩溃时同时撤销其在途消息。
2. 跨进程 resume 的第二个进程启动时没有重放历史 WAL（仅“崩溃重启”会重放），
   导致裁决为空。修复：`runner.New` 末尾对每个节点执行一次 WAL 重放
   （全新目录为 no-op）。

## 4. 构建

```
$ go build -o bin/twopcsim ./cmd/twopcsim
（成功，无输出）
```

## 5. 12 个场景验收（`./accept.sh` 输出）

```
PASS  01-happy-path -> committed
PASS  02-lossy-network -> committed
PASS  03-coord-crash-after-start -> committed
PASS  04-coord-crash-after-commit -> committed
PASS  05-coord-down-forever-blocked -> blocked
PASS  06-coord-down-before-decision-blocked -> blocked
PASS  07-participant-crash-prepared -> committed
PASS  08-participant-crash-at-commit -> committed
PASS  09-vote-no-abort -> aborted
PASS  10-coord-crash-after-abort -> aborted
PASS  11-multi-txn-duplicates -> committed      (txn-A; 同场景 txn-B -> aborted)
PASS  12-participant-crash-before-prepared-timeout-abort -> aborted
PASS  全部场景无部分提交
PASS  阻塞证据断言（3 参与者持锁、周期询问、无自行回滚）
PASS  跨进程恢复 blocked -> committed
================ 全部验收通过 ================
```

## 6. 阻塞证据（真实报告摘录，未改写）

场景 05（协调者 fsync COMMIT 后、发 GLOBAL_COMMIT 前永久不可用）：

```
verdict: blocked | coordinatorAvailable: False | coordinatorDecision: "commit"
  participant-1: state=prepared lockHeld=true  preparedTick=8  waitedTicks=212 queriesSent=26 lastQueryTick=216
  participant-2: state=prepared lockHeld=true  preparedTick=7  waitedTicks=213 queriesSent=26 lastQueryTick=215
  participant-3: state=prepared lockHeld=true  preparedTick=6  waitedTicks=214 queriesSent=26 lastQueryTick=214
  crash: nodeId=coordinator trigger=hook:coordinator.commit.appended crashed=true restarted=false
```

场景 06（协调者在决议形成前 tick 9 永久不可用）：

```
verdict: blocked | coordinatorAvailable: False | coordinatorDecision: "unknown"
  participant-1/2/3 全部 state=prepared lockHeld=true，waitedTicks 212~214，各 26 次询问，无人作答
  crash: nodeId=coordinator trigger=atTick:9 crashed=true restarted=false
```

含义：参与者在 PREPARED 后持锁等待了整个观察窗口（~212 tick）、发出 26 轮
DECISION_REQUEST，trace 中不存在任何参与者自行 abort 事件——证明“准备成功后
不自行回滚”，也证明系统在协调者不可用时**确实阻塞**，而非伪称可用。

## 7. 阻塞可解除（跨进程恢复，真实输出）

```
进程1裁决=blocked（协调者永久不可用）
进程2（同一 dataDir、fresh:false、协调者恢复）裁决=committed, committed=['participant-1','participant-2','participant-3']
```

协调者重放 WAL 看到 `COORD_COMMIT` 后重发 GLOBAL_COMMIT，3 个参与者收敛提交，
无部分提交。说明阻塞是“等待权威决议恢复”，而非数据损坏。

## 8. 未通过项 / 已知限制

- 无未通过项：`go vet`、`go test -race`、12 场景裁决、无部分提交检查、
  阻塞证据检查、跨进程恢复全部通过（`accept.sh` 退出码 0）。
- 已知范围限制（设计如此，非缺陷）：
  - 2PC 本身在协调者不可用且无存活决议持有者时阻塞，本项目保留并显式验证该属性，
    未实现 3PC/Paxos 等非阻塞协议。
  - 仅模拟单协调者（无协调者选举/备份协调者）。
  - 资源锁以布尔状态建模，不涉及具体数据项；不做前端。
