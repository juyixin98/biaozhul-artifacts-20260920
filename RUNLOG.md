# 运行记录（RUNLOG）

- 环境：Go 1.22.2 linux/amd64，无外部依赖（仅标准库）
- 日期：2026-09-23
- 说明：以下命令与输出均为本机实际执行所得；未通过项在末尾如实列出。

## 1. 构建与静态检查

```
$ go build ./...
（无输出，退出码 0）

$ go vet ./...
VET_OK（无告警）

$ gofmt -l scheduler server cmd
（无输出：全部文件已格式化）
```

## 2. 自动化测试

### 2.1 竞态检测 + 重复 10 轮

```
$ go test -race -count=10 ./...
?   dagscheduler/cmd/server  [no test files]
ok  dagscheduler/scheduler   1.548s
ok  dagscheduler/server      1.135s
```

全部通过，`-race` 无任何数据竞争报告。

### 2.2 用例清单（`go test -v -race ./...`，全部 PASS）

scheduler 包（库）：

| 用例 | 覆盖的验收点 |
|---|---|
| TestValidateCycleDetection（self/two/three node） | 启动前环检测 |
| TestFindCyclePathIsClosedAndExact | 环路径精确重建 `a -> b -> c -> a` |
| TestValidateAcceptsDiamond | 菱形 DAG 合法 |
| TestValidateErrors（8 个子用例） | 空图/空 ID/重复 ID/缺 task_type/未知依赖/非法策略/负预算/负 backoff |
| TestSubmitRejectsUnknownTaskType | 未知执行器拒绝提交 |
| **TestDiamondAllSuccess** | **菱形依赖：top 先于两分支、bottom 最后** |
| **TestNodeNeverRunsConcurrently** | **根节点 4 次零退避重试 + 50 扇出叶子；并发计数 >1 即失败；attempts 恰为 [1,2,3,4]** |
| TestFailureSkipsDownstreamAllSuccess | failed 与 skipped 区分，且跳过沿链传递；被跳节点零执行 |
| TestAllFinishedRunsDespiteFailedDeps | all_finished 策略：上游失败仍运行 |
| TestMixedPolicies | 同一 DAG 内两种策略对照 |
| **TestRetryThenSucceedFakeClock** | **FakeClock 推进 10s backoff，3 次尝试成功，事件含 retry_wait/retrying** |
| TestRetryBudgetExhausted | 预算耗尽恰好 3 次尝试，节点 failed，last_error 有记录 |
| **TestCancelMarksPendingNodesAndFailsInFlightCtx** | **取消：运行中节点 ctx 中断→canceled，未启动下游 canceled 且零执行** |
| TestCancelDuringBackoffStopsTimer | backoff 等待中取消：定时器停止，推进时钟也不补发尝试 |
| TestCancelFinishedRunIsConflict | 对已结束 run 取消返回 ErrRunFinished（HTTP 409） |
| TestStructuredEventsAndSinks | 事件序列、全局递增 seq、外部 Sink 收到全部事件 |
| TestEventsUnknownRun | 未知 run 返回 ErrNotFound |
| **TestConcurrentSubmitsNeverRunANodeTwice** | **40 goroutine 并发提交同构 DAG（含首败零退避重试），无重复并发执行** |
| TestCloseCancelsInflightRun | Close 中断运行中节点、取消未启动节点、run 落定 canceled |

server 包（HTTP）：

| 用例 | 覆盖点 |
|---|---|
| TestHealth | GET /health |
| TestCreateGetListRun | 提交→轮询终态→查询→列表→事件 |
| TestCreateRejectsCycle | 环检测经 HTTP 返回 400 且错误信息含 cycle |
| TestCreateRejectsUnknownTask | 未知 task_type 返回 400 |
| TestUnknownRunReturns404 | 未知 run 返回 404 |
| TestMalformedJSONIs400 | 非法 JSON 返回 400 |
| TestCancelRunOverHTTP | HTTP 取消在 sleep 任务上落定 run/node = canceled |

## 3. 实际启动 HTTP 服务的端到端验收

构建二进制、在随机空闲端口启动（注：本机 18080/18099 已被其他进程占用，
故选用系统分配的空闲端口），依次提交各场景：

```
$ go build -o /tmp/dagscheduler_final ./cmd/server
$ DAGSCHEDULER_ADDR=127.0.0.1:41417 /tmp/dagscheduler_final

--- health:
{"status":"ok"}

--- 菱形（examples/diamond_success.json）
run: succeeded | nodes: {'extract_a': 'succeeded', 'extract_b': 'succeeded',
                        'merge': 'succeeded', 'prepare': 'succeeded'}

--- 失败重试（examples/retry_success.json，fail_for=2, max_attempts=4, backoff=200ms）
run: succeeded | flaky attempts: 3 status: succeeded

--- 失败传播（examples/failure_skip.json）
run: failed | nodes: {'broken': 'failed',
                      'lenient_branch': 'succeeded',   # policy=all_finished，照常执行
                      'skipped_branch': 'skipped'}     # policy=all_success，被跳过、零执行

--- 环检测（examples/cycle_rejected.json）
HTTP 400，body: {"error":"dependency cycle detected: a -> c -> b -> a"}

--- 取消（examples/cancel_demo.json，sleep 60s）
POST /runs/{id}/cancel {"reason":"acceptance"}
run: canceled | nodes: {'after': 'canceled', 'long': 'canceled'}
  long.last_error = "context canceled"（执行器正确响应了 ctx）
  after 从未执行

--- SIGINT 优雅关闭
dagscheduler: shutdown signal received
dagscheduler: stopped
```

`scripts/demo.sh`（完整 JSON 输出版）同样实际跑通，输出见会话记录；
事件日志样例（重试 run 的节选）展示了完整状态序列：

```
node_started(attempt 1) → node_retry_wait(backoff=200ms, error=...) →
node_retrying(attempt 2) → node_retry_wait → node_retrying(attempt 3) →
node_succeeded → run_succeeded
```

## 4. 未通过项 / 已知限制（如实记录）

- **无未通过的测试**：所有自动化用例与端到端场景均通过，`-race` 干净。
- 开发过程中修复过的问题（最终代码中均已解决，记录以保证可追溯）：
  1. 环检测错误信息最初把收尾节点重复输出（`... -> a -> a`），已重写路径
     重建逻辑并增加 `TestFindCyclePathIsClosedAndExact` 精确断言；
  2. 演示脚本最初用 sed 从响应中抽取 id，误取到节点的 `id` 字段（返回
     404），已改为用 python/jq 解析顶层 run id；
  3. 本机 18080、18099 端口被无关进程占用，首次启动失败（address already
     in use），改用系统分配的空闲端口，与项目代码无关。
- 设计内的范围限制（非缺陷）：
  - 纯内存存储：run 历史与事件日志不持久化，重启即清空；
  - HTTP 服务未内置鉴权/限流/TLS，定位为本地接口；
  - 节点间不传递产物数据（只有状态依赖）；
  - 固定并发度为“所有就绪节点并行”，未提供并发上限信号量；
  - backoff 为固定间隔，未实现指数/抖动策略（Clock 已抽象，可在执行器侧
    或后续版本扩展）。
