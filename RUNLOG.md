# 实际运行记录（RUNLOG）

本文如实记录本次开发中的构建、测试与手工运行命令及其结果，包括修复过程。

## 环境

- OS：Linux 6.8.0-90-generic (Ubuntu 24.04)
- JDK：OpenJDK 21.0.12.1（`java -version` 实测）
- 无 Maven / Gradle；测试用 JUnit 5 `junit-platform-console-standalone-1.11.4.jar`
  （由 `test.sh` 从 Maven Central 下载到 `lib/`，无其他外部依赖）
- 无外部消息系统；无前端。

## 构建

```bash
./build.sh
# -> 主代码编译完成 -> target/classes（javac --release 21，无错误）
```

## 自动化测试

```bash
./test.sh
# 等价于：
# javac -d target/classes --release 21 @target/main-sources.txt
# javac -d target/test-classes -cp target/classes:<junit.jar> --release 21 @target/test-sources.txt
# java -jar <junit.jar> execute -cp target/classes -cp target/test-classes --scan-classpath
```

**最终结果：119 tests found / 119 started / 119 successful / 0 failed / 0 skipped。**

测试按包分布：

| 测试类 | 覆盖点 |
|---|---|
| `FractionTest` | 精确分数约分、半整数、long 边界不溢出 |
| `ExactQuantileContractTest` | R-7 中位数/分位数、奇偶、重复值、负值、插值、空集合、非法 q |
| `DifferentialVsSortingReferenceTest` | **与每窗全排序差分对照**：200 组随机增删序列 + 50 组随机窗口 × 9 个分位点 |
| `TrailingWindowQuantilesTest` | **过期删除逐拍维护计数**、全重复整批过期、同时间戳成组退出、负值、空窗、乱序入窗 |
| `SlidingWindowQuantileOperatorTest` | pane 分片/共享、空窗补齐、负时间戳网格对齐、同时间戳、迟到丢弃、watermark 单调 |
| `TimeAbstractionsTest` | MockClock/ManualScheduler/可注入 watermark |
| `StreamingQuantileJobTest` | 注入时间与调度的端到端确定性作业 |
| `JsonParserWriterTest` | 长整数不丢精度、坏 JSON 拒绝、回环 |
| `BatchQuantileServiceTest` / `QuantileHttpServerTest` | 服务逻辑与真实 loopback HTTP E2E（含 400/405） |

### 开发过程中发现并修复的真实缺陷（如实记录）

首版代码并非一次通过，测试过程中暴露并修复了以下问题：

1. **flush 到极大 watermark 时无限生成窗口导致 OOM**。
   最初 `advanceWatermark(Long.MAX_VALUE)` 只靠 watermark 条件停循环，最后一个有数据的
   窗口之后会无限产出空窗。修复：窗口起点一旦超过“曾出现过的最右 pane 起点”即终止。
2. **pane 被过早删除导致滑动窗口计数错误**。窗口触发后立刻删除其首个 pane，
   但该 pane 还属于随后触发的其他窗口，造成少数据。修复：触发窗口后只清理
   `paneStart <= 当前窗口起点` 的 pane，其余保留给后续窗口（`TreeMap.headMap(...).clear()`）。
3. 若干**测试期望值本身错误**，经按 R-7 定义手算后更正（例如
   `[1,2,2,2,5]` 在 q=0.75 时 p=3 取 v[3]=2，而非最初误写的 2.75）；
   生产代码（TreeMap 与全排序参考）在这些用例上本来就一致。
4. `Json` 的类型辅助最初只接受解析模型记录；批处理服务内部直接构造的是
   原生 `Map/List/Number`，已让辅助同时接受两种表示。

## 手工运行（端到端）

### 批处理 CLI

```bash
java -cp target/classes com.example.quantiles.service.Main batch samples/request-basic.json
```

关键输出（节选）：第一窗 `[0,10)` 有 `[1,2,3,4]`，中位数 `2.5`；
第二窗 `[10,20)` 有 `[10,20,30]`，中位数 `20`。完整结果见
`samples/response-basic.json`。

`samples/request-duplicates-same-ts-and-empty.json` 在一次响应中同时验证：
全重复（窗口 `[0,100)` 四个 `7` -> `7`）、同时间戳、以及两个中间空窗
（`count=0, empty=true, values=[null]`，见
`samples/response-duplicates-same-ts-and-empty.json`）。

负数时间戳（stdin）：

```bash
printf '{"windowSizeMillis":10,"events":[{"timestampMillis":-15,"value":-3},{"timestampMillis":-11,"value":-7},{"timestampMillis":-13,"value":-5}]}' \
 | java -cp target/classes com.example.quantiles.service.Main batch /dev/stdin
```

窗口按 floorDiv 对齐为 `[-20,-10)`，3 个值 `[-7,-5,-3]` 中位数 `-5`。

### HTTP 服务

```bash
java -cp target/classes com.example.quantiles.service.Main serve --port=8080
```

实测（服务在真实后台进程中启动，`GET /health` 就绪后）：

- `GET /health` -> 200 `{"status":"ok",...}`
- `POST /quantiles`（`--data @samples/request-*.json`）-> 200，结果与批处理一致
- 非法 JSON -> `400`；缺少必填字段 -> `400`；对 `/quantiles` 用 GET -> `405`

## 未通过项 / 已知限制

- 最终**无未通过测试**（119/119）。
- 分位数结果以精确分数表示；`toCanonicalString()` 对整数/半整数等简单结果给出精确十进制，
  对一般分母用 `BigDecimal` 精确十进制展开（分母来自分位参数的十进制表示与 `(n-1)`）。
- 批处理接口在喂完所有事件后一次性把 watermark 推到无穷，因此批处理响应中的
  `lateDroppedCount` 恒为 0；迟到丢弃语义由流式算子
  （`SlidingWindowQuantileOperator` / `StreamingQuantileJob`）在逐事件 watermark
  推进下体现，并由对应单元测试覆盖。
