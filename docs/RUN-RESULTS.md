# 实际运行记录

- 日期：2026-09-23
- 环境：Linux 6.8.0-90-generic (x86_64)
- JDK：Eclipse Temurin `17.0.13+11`（OpenJDK 17.0.13），免安装 tar 包
- 依赖：零第三方库（`jdeps --print-module-deps` = `java.base,jdk.httpserver`）

## 1. 构建

```
$ JAVAC=$JAVA_HOME/bin/javac JAR=$JAVA_HOME/bin/jar ./build.sh
built target/ordered-events.jar
```

`javac -Xlint:all` 无警告、无错误。

## 2. 自动化测试（./test.sh）

```
18 passed, 0 failed
```

完整清单（耗时来自真实运行）：

```
ok   - OrderingCoreTest.laterEventFinishesFirstButCommittedInSeqOrder (422ms)
ok   - OrderingCoreTest.timeoutOnHeadReleasesOrderWithFailurePlaceholder (112ms)
ok   - OrderingCoreTest.partitionsAreIndependent (506ms)
ok   - OrderingCoreTest.bufferCapRejectsExtraEventsWith429 (3ms)
ok   - OrderingCoreTest.inflightCapBoundsRunningEvents (150ms)
ok   - OrderingCoreTest.failedAttemptsAreRetriedThenPlaceholderCommittedInOrder (133ms)
ok   - OrderingCoreTest.timedOutAttemptIsRetriedAndSucceedsOnSecondAttempt (103ms)
ok   - OrderingCoreTest.cancelQueuedEventProducesNoOutputAndUnblocksTail (1032ms)
ok   - OrderingCoreTest.cancelRunningEventInterruptsAndSkipsItsSlot (101ms)
ok   - OrderingCoreTest.cancelFinishedEventIsConflictAndIdempotentOtherwise (21ms)
ok   - OrderingCoreTest.cancelIsIdempotent (50ms)
ok   - HttpApiTest.healthAndStatsAndPartitionLifecycle (273ms)
ok   - HttpApiTest.acceptanceTimeoutFirstLaterFinishesFirstOutputOrdered (847ms)
ok   - HttpApiTest.acceptanceBufferCapReturns429 (176ms)
ok   - HttpApiTest.acceptanceRetriesExhaustedThenFailureInOrder (174ms)
ok   - HttpApiTest.acceptanceCancelProducesNoResult (322ms)
ok   - HttpApiTest.errorsAreReportedAsJson (258ms)
ok   - HttpApiTest.globalStatsTrackInflightAndCommitted (625ms)
```

测试运行期间是并行提交 + 真实线程调度（非 mock），用轮询断言等待终态，
因此总耗时约 5 秒。

## 3. 启动与端到端验收（examples/acceptance-demo.sh 实测）

服务以默认参数启动（`inflightCap=8 bufferCap=16 timeoutMs=2000
maxAttempts=3 retryDelayMs=100`），以下为真实响应摘录。

### 验收点 A：首事件超时、后续先完成，输出仍有序

- seq0：`delayMillis=3000, timeoutMillis=800, maxAttempts=1`（会超时）
- seq1/seq2：50ms / 70ms 成功

提交后 200ms 观察（后续已先完成、队头仍在跑、输出为空）：

```
seq1 status = SUCCEEDED
seq0 status = RUNNING
GET /partitions/demo/results -> {"partition":"demo","results":[]}
```

注意 seq2 的 `completedAt=...2758`、seq1 的 `completedAt=...2730`，
都远早于 seq0 超时完成的 `...3444`；但最终输出严格按 seq 提交：

```json
{"seq":0,"eventId":"demo-0","outcome":"FAILURE",
 "error":"Exception: attempt timed out after 800ms","attempts":1,
 "completedAt":1790168683444,"committedAt":1790168683444}
{"seq":1,"eventId":"demo-1","outcome":"SUCCESS","attempts":1,
 "completedAt":1790168682730,"committedAt":1790168683445}
{"seq":2,"eventId":"demo-2","outcome":"SUCCESS","attempts":1,
 "completedAt":1790168682758,"committedAt":1790168683445}
```

即：先完成的 seq1/seq2 被缓冲，直到 seq0 的超时占位提交后才按
0、1、2 顺序提交（三者 `committedAt` 同一毫秒，`completedAt` 体现乱序完成）。

### 验收点 B：缓冲上限

`bufferCap=2` 的分区提交两个长事件后，第三次提交：

```
HTTP 429
{"error":{"status":429,"message":"partition buffer full: cap=2 outstanding=2"}}
```

在途上限由测试 `inflightCapBoundsRunningEvents` 验证：cap=2、提交 4 个事件，
150ms 后 `inFlight=2, buffered=2, outstanding=4`。

### 验收点 C：重试耗尽后的顺序

`fail=true, maxAttempts=3, retryDelayMillis=100` 的事件后紧跟一个成功事件：

```json
{"seq":0,"eventId":"retry-0","outcome":"FAILURE",
 "error":"Exception: injected processing failure on attempt 3","attempts":3,
 "committedAt":1790168683762}
{"seq":1,"eventId":"retry-1","outcome":"SUCCESS","attempts":1,
 "completedAt":1790168683527,"committedAt":1790168683762}
```

成功事件虽在 `...3527` 就处理完，但等到失败占位在 `...3762` 提交后才提交，
顺序保持 0(FAILURE) → 1(SUCCESS)。

### 验收点 D：取消后不再提交结果

取消一个 10s 的运行中事件（100ms 后取消）：

```json
{"id":"cxl-0","status":"CANCELLED","attempts":1,
 "cancelReason":"demo cancel","finishedAt":1790168683931}
```

分区结果只有 seq1，cxl-0 从未出现：

```json
{"partition":"cxl","results":[
  {"seq":1,"eventId":"cxl-1","outcome":"SUCCESS","attempts":1,
   "committedAt":1790168683931}]}
```

取消后全局统计（submitted=9，committed=8，差额即被取消且不占输出位的事件；
429 拒绝的提交不计入 submitted）：

```json
{"inflightCap":8,"partitions":4,"submitted":9,"committed":8,
 "outstanding":0,"inFlight":0,"buffered":0}
```

## 4. 过程中修复过的问题（如实记录）

首轮测试 14 通过 / 4 失败，均为测试代码或测试准备问题，非服务逻辑缺陷：

1. 失败占位事件终态断言写成 COMMITTED；实际设计为 FAILED（成功才是
   SUCCEEDED→COMMITTED，失败与成功使用不同终态，输出条目用 outcome 区分）。
   已统一状态机文档与断言。
2. HTTP 验收用例里超时窗口仅 ~40ms，轮询可能错过瞬时的 SUCCEEDED 中间态；
   已把窗口扩大到约 700ms（head delay 3s / timeout 800ms，tail 40~60ms）。
3. 一个核心用例在自建 service 上漏建分区（404 partition not found），已补建。
4. 错误处理用例直接把含空格的非法分区名放进 URI，客户端先报
   URISyntaxException；改为发送百分号编码的 `/partitions/bad%20name`。

## 5. 未完成项 / 限制

- 仅内存态：重启丢失，无持久化；`eventsById` 无 TTL/淘汰。
- 取消/超时依赖处理器协作响应中断（默认处理器已正确响应；自定义处理器
  若吞掉中断则无法立即生效）。
- 结果输出未分页、未压缩；消费方应使用 `sinceSeq` 增量拉取。
- 未做鉴权 / TLS（定位为本地演示服务，监听地址与端口可配）。
- 未提供 Maven/Gradle 构建；按“仅 JDK、锁定零依赖”的要求使用 `javac` 脚本。
