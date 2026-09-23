# 运行记录（RUNLOG）

本文件如实记录本项目在开发机上的实际运行情况。所有命令均可通过
`./run_tests.sh` 复现。

- 日期：2026-09-23
- 系统：Linux 6.8.0-90-generic x86_64（Ubuntu 24.04）
- Go：`go version go1.22.2 linux/amd64`（位于 `/usr/lib/go-1.22/bin`）
- 工作目录：`/home/admin/Downloads/biaozhul/opp22/b`

> 说明：环境默认 `PATH` 中没有 `go`，运行时使用
> `export PATH=/usr/lib/go-1.22/bin:$PATH`（`run_tests.sh` 已自动处理）。

---

## 1. 构建

```
$ go build -o bin/fls ./cmd/fls
（无输出，退出码 0）

$ go vet ./...
（无输出，退出码 0）
```

## 2. 自动化测试

命令：`go test -race ./...`，退出码 **0**。共 **24 个测试函数全部通过**
（`go test -v` 实测，含竞态检测）：

```
--- PASS: TestFenceTokensIncreasePerAcquisition
--- PASS: TestAcquireConflictWhileHeld
--- PASS: TestExpiredLeaseAllowsNewFence
--- PASS: TestRenewStaleFenceRejected
--- PASS: TestRenewAfterExpiryRejected
--- PASS: TestDuplicateAcquireIsIdempotent
--- PASS: TestRenewExtendsExpiry
--- PASS: TestReentrantAcquireSameFence
ok  fencinglease/internal/lock
--- PASS: TestFencedResourceRejectsOlderFence
--- PASS: TestFencedResourceRejectsSameFence
--- PASS: TestFencedResourceRejectsZeroFence
--- PASS: TestUnfencedResourceAcceptsStaleToken
ok  fencinglease/internal/resource
--- PASS: TestExampleScenarios
--- PASS: TestStaleFenceNeverAccepted
--- PASS: TestFuzzManySeeds            # 25 个随机种子，围栏均不回退
--- PASS: TestEndToEndPauseScenario
ok  fencinglease/internal/runner
--- PASS: TestTimersFireInTimeOrder
--- PASS: TestRNGDeterministicAcrossInstances
--- PASS: TestNetworkDropRule
--- PASS: TestNetworkDelayRule
--- PASS: TestNetworkDuplicateRule
--- PASS: TestNetworkNthMatchTargetsOneOccurrence
--- PASS: TestPauseHoldsEventsAndFreesOnResume
--- PASS: TestExplicitResumeFlushesEarly
--- PASS: TestCancelTimer
ok  fencinglease/internal/sim
```

（`cmd/fls`、`internal/node`、`internal/proto` 无独立测试文件，其行为由
`runner` 集成测试覆盖。）

## 3. 场景运行结果

命令：`./bin/fls run examples/<name>.json`

| 场景 | 退出码 | 断言 | 结果文件 |
|---|---|---|---|
| renew-lost | 0 | 7 | `examples/output/renew-lost.result.json` |
| pause-expired | 0 | 6 | `examples/output/pause-expired.result.json` |
| late-message | 0 | 5 | `examples/output/late-message.result.json` |
| duplicate | 0 | 4 | `examples/output/duplicate.result.json` |
| fuzz | 0 | 内置 fence_monotonic | `examples/output/fuzz.result.json` |
| **no-fence（对照）** | **1（预期）** | fence_monotonic **失败** | `examples/output/no-fence.result.json` |

`no-fence` 是故意关闭围栏校验的对照实验，其失败正是要展示的结论：

```
FAIL: no-fencing-baseline-fails
  - fence_monotonic: resource acct: fence 1 after 2 at t=25 (A)
```

即 B 以 fence=2 写入后，僵尸 A 又以旧 fence=1 写入并被接受，顺序被破坏。
`run_tests.sh` 把该退出码 1 视为**预期通过**。

### 3.1 续约丢失（renew-lost）关键 trace

```
t= 1 lockserver lock.granted        fence=1
t= 7 res-1      resource.accepted   fence=1 high=1 value=a-while-held
t= 9 network    net.delay            (首次续约被滞留，deliver_at=21)
t=16 A          client.lock_lost    fence=1 reason=local_deadline_reached
t=17 lockserver lock.granted        fence=2            # B 接管
t=21 lockserver lock.renew_rejected fence=1 reason=fencing_token_stale
t=21 res-1      resource.accepted   fence=2 high=2 value=b-wins
t=22 res-1      resource.rejected   fence=1 high=2 reason=fence_too_old value=a-zombie
```

### 3.2 暂停超过租期（pause-expired）关键 trace

```
t= 1 lockserver lock.granted        fence=1
t= 4 A          node.paused         until=22 duration=18
t=13 lockserver lock.granted        fence=2            # A 冻结期间 B 接管
t=17 res-1      resource.accepted   fence=2 high=2
t=22 A          node.resumed        held_events=3
t=22 A          client.lock_lost    fence=1
t=23 lockserver lock.renew_rejected fence=1 reason=fencing_token_stale
t=25 res-1      resource.rejected   fence=1 high=2 reason=fence_too_old
```

### 3.3 消息迟到（late-message）关键 trace

```
t= 7 network    net.delay            (续约被推迟到 t=15)
t=11 A          client.lock_lost    fence=1
t=12 lockserver lock.granted        fence=2
t=15 lockserver lock.renew_rejected fence=1 reason=fencing_token_stale
t=16 res-1      resource.accepted   fence=2 high=2 value=b-new-holder
t=18 res-1      resource.rejected   fence=1 high=2 reason=fence_too_old value=a-late
```

三个场景结论一致：**旧持有者携带较小围栏号的提交全部被资源拒绝
（reason=fence_too_old），high-water 只随新围栏号增长。**

## 4. 随机压力（多种子）

`./bin/fls demo fuzz`（内置 10 个确定性种子，15% 丢包、8% 重复、8% 乱序、
一次 30 tick 暂停），整体 `ok: true`：

| seed | 接受 commits | 拒绝 rejections | 授予 grants | 结果 |
|---|---|---|---|---|
| 1 | 1 | 37 | 1 | ok |
| 2 | 2 | 68 | 2 | ok |
| 3 | 1 | 35 | 1 | ok |
| 4 | 2 | 73 | 2 | ok |
| 5 | 2 | 64 | 2 | ok |
| 6 | 1 | 41 | 1 | ok |
| 7 | 2 | 70 | 2 | ok |
| 8 | 2 | 78 | 3 | ok |
| 9 | 1 | 39 | 1 | ok |
| 10 | 1 | 29 | 2 | ok |

`TestFuzzManySeeds` 在此形状上额外跑种子 1–25，全部无围栏回退，且每种子
至少有 1 个 commit（防止断言空洞）。

## 5. 确定性验证

对同一场景连续运行两次并比较完整结果 JSON：

```
$ ./bin/fls run examples/fuzz.json --out /tmp/r1.json --quiet
$ ./bin/fls run examples/fuzz.json --out /tmp/r2.json --quiet
$ diff -q /tmp/r1.json /tmp/r2.json
（无差异）
```

## 6. 一键脚本

```
$ ./run_tests.sh
== go vet ==
== go test (race) ==
ok  fencinglease/internal/lock
ok  fencinglease/internal/resource
ok  fencinglease/internal/runner
ok  fencinglease/internal/sim
== build ==
== example scenarios ==
PASS  renew-lost
PASS  pause-expired
PASS  late-message
PASS  duplicate
PASS  fuzz
PASS  no-fence (correctly rejects with exit 1)
== built-in fuzz (10 seeds) ==
FUZZ PASS: 10 seeds
```

退出码 **0**。

## 7. 未通过项 / 已知限制（如实记录）

- **无未通过的测试**：`go test -race ./...` 全绿，所有要求场景按预期通过。
- **对照场景 `no-fence` 以退出码 1 结束是设计行为**，不是缺陷；它演示关闭
  围栏校验时数据会被旧持有者破坏，`run_tests.sh` 与 `TestExampleScenarios`
  均显式断言该结果。
- 开发过程中修正过的两个真实问题（已在最终代码中解决）：
  1. 初版 renew-lost 场景用两条都带 `nth:1` 的丢弃规则，实际只有一条生效，
     租约从未过期；改为“把首次续约延迟到过期之后到达”，并修正了脚本动作与
     RPC 响应同刻时的时序（提交动作后移到授予响应之后）。
  2. fuzz 工作循环最初在“未持有锁/已丢锁”时停止，无法产生僵尸写；改为在
     整个配置步数内持续提交，丢锁后继续用旧围栏号写，由资源拒绝，从而真正
     冲击围栏不变量。
- 已知边界：锁服务/资源不暂停（仅客户端节点暂停）；无真实网络与持久化；
  RNG 为自带 PCG，刻意不依赖标准库随机流以保证跨版本可复现。
