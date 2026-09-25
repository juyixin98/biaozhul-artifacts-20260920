# 运行记录（RUNLOG）

本机：Linux 6.8（amd64），Go 1.22.2，仅标准库、无外网依赖。
以下命令均在仓库根目录实际执行，输出为本机真实结果。

## 1. 环境与构建

```
$ go version
go version go1.22.2 linux/amd64

$ go build ./...
# 无输出（成功）

$ go vet ./...
VET_OK（无告警）

$ gofmt -l .
# 空（全部文件已格式化）
```

## 2. 自动化测试

```
$ go test -count=1 ./...
?   drfscheduler/cmd/drfscheduler   [no test files]
ok  drfscheduler/pkg/httpapi        0.167s
ok  drfscheduler/pkg/scheduler      1.305s
```

竞态检测 + 重复执行（确定性校验）：

```
$ go test -race -count=3 ./...
ok  drfscheduler/pkg/httpapi        1.450s
ok  drfscheduler/pkg/scheduler      22.050s
```

开发期间还多次运行 `go test -race -count=10 ./...`，全部通过。

20 个测试函数全部 PASS、0 FAIL：

1. TestHTTPHealthAndNotFound
2. TestHTTPSubmitRejectsUnknownTenantAndOversize
3. TestHTTPSchedulingScenario（HTTP 端到端：大任务阻塞→释放→排空，校验不超配）
4. TestHTTPBatchSubmissionIsSimultaneous（批量=同时刻到达；坏元素整批回滚）
5. TestHTTPCapacityShrinkBelowUseRejected
6. TestFakeClockFiresInDeadlineOrder
7. TestFakeClockSameDeadlineFiresInRegistrationOrder
8. TestFakeClockTimerDoesNotFireBeforeDeadline
9. TestFakeClockStop
10. TestFakeClockTimerArmedWithinAdvance
11. TestRandomizedNoOvercommitAndDRF（12 随机种子 × 300 步：全程不超配 + FIFO）
12. TestClassicDRFHandComputed（9 CPU/18 GB 手算序列 + 释放后补调度）
13. TestBigTaskBlocksSmallTasks（大任务阻塞、INSUFFICIENT_RESOURCES、释放启动）
14. TestSmallTasksArrivingContinuously（小任务持续到达，5ms/10ms 释放点）
15. TestWeightedDRF（1:2 权重精确序列 4:6、精确分数份额）
16. TestDeterministicReplay（重放逐位一致）
17. TestPerTenantFIFOHeadBlocking（TENANT_QUEUE_FULL）
18. TestMemoryDominantDimension（内存主导维）
19. TestValidationAndCapacityGrowth（422/400 校验、扩容解阻塞）
20. TestCancelQueuedTask（取消事件）

## 3. HTTP 服务实际运行（仿真模式）

构建并启动（本机 18080/18099 端口被无关进程 `vccsim` 占用，改用系统分配端口）：

```
$ go build -o /tmp/drfscheduler ./cmd/drfscheduler
$ /tmp/drfscheduler -addr 127.0.0.1:<port> -capacity-cpu 9000 -capacity-mem 18000 \
    -mode sim -events-log /tmp/drf-events.jsonl

$ curl /healthz
{"mode":"simulation","ok":true}
```

运行 `scripts/demo.sh`（退出码 0）。关键快照（真实返回）：

- 批量提交 7 个任务后 t=0：`running=5 queued=2`，`used={9000 cpu,14000 MiB}`，
  `free={0 cpu,4000 MiB}`，运行集合恰为 **A1 B1 A2 B2 A3**，A4/B3 排队。
- 推进 50ms（B2 结束）：`completed=1 running=5 queued=1`，B3 补入（B 份额落后）。
- 再推进 50ms（A1/A2/A3 结束）：`completed=4 running=3 queued=0`，A4 补入。
- 推进 500ms：`completed=7 running=0 queued=0`，`used={0,0}`。

`-events-log` 落盘的 JSON Lines 事件顺序（`seq`/时间戳为实际输出）：

```
 1..7 TASK_SUBMITTED A1 A2 A3 A4 B1 B2 B3
 8 TASK_STARTED A1
 9 TASK_STARTED B1
10 TASK_STARTED A2
11 TASK_STARTED B2
12 TASK_STARTED A3
13 TASK_WAITING A4  reason=INSUFFICIENT_RESOURCES
14 TASK_WAITING B3  reason=INSUFFICIENT_RESOURCES
15 TASK_FINISHED B2  SUCCESS   @0.050s
16 TASK_STARTED B3             @0.050s
17..19 TASK_FINISHED A1 A2 A3 SUCCESS @0.100s
20 TASK_STARTED A4             @0.100s
21..23 TASK_FINISHED A4 B1 B3 SUCCESS @0.600s
```

与 README 第 1.4 节手算表逐位一致；全程 `used.cpu<=9000`、`used.mem<=18000`。

错误路径实测：

```
POST /v1/tasks 未知租户            -> 404
POST /v1/tasks 请求 999999 CPU     -> 422（task request exceeds cluster capacity ...）
POST /v1/tasks 非法 JSON           -> 400
PUT  /v1/cluster/capacity 非法值    -> 400
```

## 4. 实时模式 + 真实进程执行器实测

```
$ /tmp/drfscheduler -mode real -executor=process -capacity-cpu 2000 -capacity-mem 2000
GET  /healthz                                  -> {"mode":"realtime","ok":true}
POST /v1/clock/advance                         -> 400（endpoint supported only with a FakeClock）
POST /v1/tasks spec="echo ...; sleep 0.3"      -> 202 RUNNING
（0.8s 后）GET /v1/tasks/p1                    -> state COMPLETE
```

子进程通过环境变量收到 `DRF_TASK_ID / DRF_CPU_MILLICPU` 等配额信息。

## 5. 未通过项 / 已知边界（如实记录）

- 最终交付状态：**无未通过测试**（20/20，`-race` 干净）。
- 开发过程中发现并修复的两个非平凡正确性问题，记录以备复查：
  1. **跨维份额比较**：两租户主导维可能不同（一个 CPU、一个内存），不能直接
     对两个不同物理量的分母交叉相乘。现统一缩放到公共刻度
     `capCPU*capMem`，并用 big.Int/溢出回退保证精确。
  2. **FakeClock 的“到达时刻”与“触发该时刻定时器”必须原子对外**：否则调度
     goroutine 可能在“时间已到终点、完成回调尚未投递”的窗口启动新任务。
     现 `Now()` 在分组派发期间阻塞，消除该竞态（`-race` 与 10× 重复验证）。
- 设计上刻意不做的事：不可抢占（无驱逐）；不做持久化（事件日志只追加、
  状态在内存）；ProcessExecutor 不做 cgroup 级真实资源隔离，只把配额通过
  环境变量传给子进程（这是“可替换执行器”的演示实现，非容器隔离）。
- 端口说明：本机 18080/18099 被其他进程占用，记录中的运行使用系统分配端口；
  默认 `-addr=127.0.0.1:8080` 在干净环境可直接使用。
