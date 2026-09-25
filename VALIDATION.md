# 验证记录（VALIDATION）

本文件如实记录实际执行过的命令与结果。环境：

- OS：Linux 6.8.0-90-generic (x86_64)，16 核
- JDK：OpenJDK 21.0.12.1（`/usr/lib/jvm/java-21-openjdk-amd64`）
- Maven：3.9.9（环境中无预装，从 Apache 官方归档下载并解压到 `/tmp/apache-maven-3.9.9`）
- 依赖：Jackson 2.17.2、JUnit 5.10.2（本机 `~/.m2` 已有缓存；shade 插件首次在线拉取）

> 以下命令中 Maven 用绝对路径调用；若 Maven 已在 PATH 上，直接用 `mvn` 即可。

## 1. 自动化测试

命令：

```bash
/tmp/apache-maven-3.9.9/bin/mvn -o clean test
```

结果（`mvn clean test` 完整输出摘要）：

```
Tests run: 3,  ... s -- com.example.tjoin.StalledSideTest
Tests run: 12, ... s -- com.example.tjoin.IntervalJoinOperatorTest$Boundaries
Tests run: 0,  ... s -- com.example.tjoin.IntervalJoinOperatorTest
Tests run: 3,  ... s -- com.example.tjoin.ReferenceDifferentialTest
Tests run: 6,  ... s -- com.example.tjoin.WatermarkCleanupTest
Tests run: 10, ... s -- com.example.tjoin.HttpServiceIntegrationTest
Tests run: 4,  ... s -- com.example.tjoin.ManualClockTest
Tests run: 1,  ... s -- com.example.tjoin.SystemSchedulerTest
Tests run: 8,  ... s -- com.example.tjoin.BatchRunnerScenarioTest
Tests run: 4,  ... s -- com.example.tjoin.BufferBoundTest
Tests run: 51, Failures: 0, Errors: 0, Skipped: 0
BUILD SUCCESS
```

**51 个测试全部通过，0 失败 0 错误 0 跳过。** 无任何未通过项。

测试与验收点对应：

| 验收要求 | 测试/证据 |
|---|---|
| 按键双流区间连接，含时间边界 | `IntervalJoinOperatorTest` + 嵌套 `Boundaries`（lower/upper 各逐毫秒验证，零宽区间） |
| 两侧独立水位线决定状态清理 | `WatermarkCleanupTest`（单侧推进只清对侧、阈值边界精确、显式注入水位线） |
| 模拟一侧停滞，不提前丢弃可匹配记录 | `StalledSideTest`（左右两个方向）、HTTP 用例 `leftStallAndRecovery`/`rightStallAndRecovery`、`scenario-stalled-side.json` |
| 水位线推进与边界 | `WatermarkCleanupTest`、HTTP 用例 `watermarkCleanupAndLate`、`scenario-watermark-late.json` |
| 限制缓冲 | `BufferBoundTest`、HTTP 用例 `bufferCapReject`/`dropOldestOverHttp`、`scenario-buffer-cap.json` |
| 允许重复值、每对仅一次、唯一事件 ID 去重 | `duplicateValuesAllowed`、`duplicateIdIgnored`、`pairEmittedOnce`、HTTP 用例 `duplicateIdsAndValues` |
| 小数据精确参考实现 | `ReferenceIntervalJoin` + `ReferenceDifferentialTest`（200 组随机 + 全负区间 + 同刻密集） |
| 时间/调度可注入 | `ManualClockTest`（确定性）+ `SystemSchedulerTest`（墙钟冒烟）；服务与全部场景只用 ManualClock |
| JSON 输入输出服务 | `HttpServiceIntegrationTest`（真实内嵌 HTTP 服务器，10 个端到端用例） |

## 2. 离线 JSON 场景

命令：

```bash
/tmp/apache-maven-3.9.9/bin/mvn -o -q package
for s in basic stalled-side buffer-cap watermark-late; do
  java -cp target/dual-stream-interval-join-1.0.0.jar \
       com.example.tjoin.json.BatchRunner examples/scenario-$s.json \
       > examples/output/batch-$s.json
done
```

结果（`examples/output/batch-*.json` 中 `final.metrics` 摘要）：

| 场景 | pairsEmitted | 关键指标 |
|---|---|---|
| basic | 3 | duplicatesDropped=1（R2 同 ID 重传） |
| stalled-side | 2 | leftBuffered=3（停滞期间保留）、leftIdle=true、rightIdle=false |
| buffer-cap | 1 | bufferRejected=1、oldestEvicted=0 |
| watermark-late | 1 | rightStateCleaned=2、lateDropped=1 |

## 3. HTTP 端到端演示

命令：

```bash
MAVEN_CMD=/tmp/apache-maven-3.9.9/bin/mvn PORT=18177 bash scripts/demo.sh
```

结果：脚本正常退出（`DEMO_OK`），17 个 JSON 应答全部 `"ok":true`；
完整逐请求应答保存在 `examples/output/transcript.log`。其中实测到：

- A3 输出 3 对，`R3@22001`（超过上界 1ms）不匹配；A4 同 ID 重传 `R2` 返回
  `DUPLICATE`，不产生新对。
- B4 推进到处理时间 8000 后 `leftIdle=true,rightIdle=true`；B5 时左侧 3 条记录
  全部仍在缓冲；B6 停滞的右侧恢复，`R2@10000` 与 `L1@5000` 成功配对。
- C3 超容量事件返回 `BUFFER_FULL`，`leftBuffered` 仍为 2。
- D3 注入左水位线 20000，`cleaned=2`，恰在边界的 `RC@20000` 保留；D4 边界事件
  匹配成功，`LL@19999` 返回 `LATE`。

## 4. 开发过程中发现并修复的问题（如实记录）

这些问题都在开发期间通过测试暴露并修复，最终 `clean test` 全绿：

1. **停滞侧水位线策略**：初版在侧空闲时把其水位线撤回为 `Long.MIN_VALUE`，
   导致缓冲永不清理。修正为“冻结在最后已知值”——既不前进（不错杀可匹配记录）
   也不撤回（陈旧记录仍回收）。修正依据：恢复侧不可能再带来早于冻结水位线且
   非迟到的事件。
2. **清理后重复投递识别**：初版只在缓冲中识别事件 ID，记录被水位线清理后重传
   会被当作新记录。改为作业生命周期内永久记住已接纳 ID（被拒事件不占 ID）。
3. **JDK HttpServer context 前缀匹配**：context 最初注册在 `/api/v1/jobs`，
   而 JDK HTTP 服务器按最长**字面前缀**匹配，作业 id（如 `j-stall2`）会让请求
   路径在 `/api/v1/` 处与 context 分叉导致 404。改为注册在 `/api/v1/` 并在
   处理器内做显式路由。
4. 若干测试期望值编写错误（清理为严格小于阈值导致的边界保留、区间距离方向、
   非递减事件时间约束下的迟到事件），均经推演后改正；算子实现本身行为正确。
5. 一个源文件曾混入 1 个 NUL 字节导致编辑匹配失败，已清除并全树扫描确认无残留。

## 5. 已知限制

- 作业与去重状态存于单进程内存，无持久化（需求范围内）。
- 无前端；仅提供库、HTTP/JSON 服务、离线 JSON 运行器与自动化测试。
