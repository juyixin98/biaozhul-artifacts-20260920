# 滑动窗口精确分位数（Sliding-Window Exact Quantiles）

纯后端的事件流计算库 + JSON 输入输出服务。对**整数值**事件流，按**事件时间**
维护滑动窗口，输出窗口内的**精确中位数和指定分位数**。

- **精确，不是近似草图**：窗口内每个值都保留精确多重度（`TreeMap<Long, Long>` 计数），
  结果与“对窗口内全部元素全排序后取下标并插值”逐位一致。
  提供朴素的全排序参考实现并在随机差分测试中逐一对照。
- **无外部消息系统、零运行时依赖**：仅用 JDK（HTTP 用内置
  `com.sun.net.httpserver`，JSON 为手写小型解析/书写器）。
- **时间与调度可注入**：`Clock` / `Scheduler` / `WatermarkGenerator` 均为接口，
  测试使用 `MockClock` + `ManualScheduler` 做确定性重放。
- 语言级别 Java 21（record / sealed / switch 模式），无 Maven/Gradle 也可构建。

## 目录结构

```
src/main/java/com/example/quantiles/
  model/Event.java                 事件（事件时间戳 + 整数值）
  time/                            可注入的时钟、调度器、watermark 生成器
    Clock / SystemClock / MockClock
    Scheduler / ExecutorScheduler / ManualScheduler
    WatermarkGenerator             bounded-out-of-orderness watermark
  quantile/
    Fraction.java                  精确分数（BigInteger 分子，恒约分）
    QuantileAccumulator.java       精确多重集合接口
    TreeMapAccumulator.java        生产实现：TreeMap 计数，O(log U) 增删
    SortingReferenceAccumulator.java  小数据全排序精确参考实现（验收基准）
  window/
    WindowSpec / WindowResult.java 窗口配置与结果
    SlidingWindowQuantileOperator.java 事件时间滑动窗口（pane 分片 + watermark）
    TrailingWindowQuantiles.java   单尾随窗口 + 显式过期事件队列
    StreamingQuantileJob.java      组装 Clock/Scheduler/Watermark 的流式作业
  json/Json.java JsonParser.java JsonWriter.java JsonException.java
  service/
    BatchQuantileService.java      离线批处理（复用同一窗口算子）
    QuantileHttpServer.java        JDK HTTP 服务：GET /health, POST /quantiles
    Main.java                      CLI：batch <file> | serve [--port=N]
samples/                           请求样例
src/test/java/...                  JUnit 5 测试（差分对照、边界、HTTP E2E）
```

## 构建与测试

需要 JDK 17+（开发/验证使用 JDK 21）。无需 Maven/Gradle。

```bash
./build.sh           # 编译主代码到 target/classes
./test.sh            # 首次自动下载 JUnit5 standalone jar 到 lib/，编译并运行全部测试
./run-batch.sh samples/request-basic.json   # 离线计算
./run-server.sh 8080                         # 启动 HTTP 服务
```

## 精确定义（插值与重复值规则）

采用 R-7（Hyndman–Fan 第 7 种，与 R 的 `quantile` 默认、NumPy `linear`、
SQL `PERCENTILE_CONT` 一致）：

- 窗口内 n 个值升序排序为 `v[0] <= v[1] <= ... <= v[n-1]`（**重复值按多重度各占一个位置**）。
- 连续位置 `p = q * (n - 1)`（0 基）；令 `i = floor(p)`、`f = p - i`，
  则 `Q(q) = v[i] + f * (v[i+1] - v[i])`。
- 由此：
  - n 为奇数时，中位数是正中间的元素；
  - n 为偶数时，中位数是中间两个值的平均（差为奇数时结果为 `.5`）；
  - `q=0` 即最小值，`q=1` 即最大值；
  - 全重复数据在任何 q 下都等于该值（相邻次序位相同，插值不改变结果）。
- 结果以**精确分数**计算（分子 `BigInteger`、恒约分），整数输入的中位数等结果
  不带二进制浮点尾差；JSON 中整数输出为 `15`、半整数为 `15.5`，不会出现 `15.0`。
  一般 q（如 0.1）按十进制精确转分数后插值。

### 窗口语义

- 窗口为半开区间 `[windowStart, windowStart + windowSizeMillis)`；
  窗口起点对齐到滑动步长网格：`windowStart = floor(t / slide) * slide`
  （`floorDiv`，时间戳为负时网格仍连续正确）。
- `windowSizeMillis` 必须是 `windowSlideMillis` 的整数倍；翻转窗口取 `slide = size`。
- 一个事件只写入它所在的一个 **pane**（长度 = slide），窗口触发时合并其全部 pane；
  窗口触发后其最老 pane 立即删除。
- watermark = `maxTimestampSeen - allowedLatenessMillis`，单调不减；
  窗口在 `watermark >= windowEnd + allowedLateness` 时触发一次；
  迟到（所属窗口均已触发）的事件被丢弃并计入 `lateDroppedCount`。
- 输出序列从“首个事件所在 pane 能落入的最早窗口”开始，到“末个 pane 自身所属窗口”
  结束，中间没有事件的网格窗口也会输出（`count = 0, empty = true, values = [null,...]`）。

另有 `TrailingWindowQuantiles`：单个尾随窗口 `(now-size, now]` 的显式模型，
时间推进时逐条把过期事件从精确累积器中删除，`count()` 始终等于队列长度，
便于直接验证**过期删除正确维护计数**。

## HTTP 接口

### `GET /health`

```json
{ "status": "ok", "service": "sliding-window-exact-quantiles" }
```

### `POST /quantiles`

请求字段：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `windowSizeMillis` | long > 0 | 是 | 窗口长度 |
| `windowSlideMillis` | long > 0 | 否 | 滑动步长，默认等于窗口长度（翻转窗口）；必须整除 size |
| `quantiles` | double[] | 否 | 分位参数，每个 ∈ [0,1]；默认 `[0.5]`（中位数） |
| `allowedLatenessMillis` | long ≥ 0 | 否 | 允许迟到，默认 0 |
| `sortByTimestamp` | bool | 否 | 喂入算子前是否按时间戳排序，默认 true |
| `events` | `[{timestampMillis:long, value:long}]` | 是 | 事件数组，可为空 |

响应包含 `results`：每个网格窗口一项，字段
`windowStartMillis / windowEndMillis / count / empty / values`，
`values` 与请求 `quantiles` 同序；空窗口对应值为 `null`。

### curl 示例

```bash
curl -s http://localhost:8080/health
curl -s -X POST http://localhost:8080/quantiles \
  -H 'Content-Type: application/json' \
  --data @samples/request-basic.json
```

## 流式使用（时间/调度注入）

```java
WindowSpec spec = new WindowSpec(10_000, 5_000, List.of(0.5, 0.9), 1_000);
var job = new StreamingQuantileJob(
        spec,
        WatermarkGenerator.boundedOutOfOrderness(1_000),
        result -> { /* 处理触发窗口 */ });
// 生产：
job.start(new ExecutorScheduler(), new SystemClock(), 100);
// 测试：
job.start(new ManualScheduler(), new MockClock(0), 100);
```

## 验收点对照

- 与每窗全排序比较：`DifferentialVsSortingReferenceTest`（200 组随机增删序列 +
  50 组随机窗口，9 个分位点逐一比较 `TreeMapAccumulator` 与全排序参考实现）。
- 负值：累积器与窗口测试均覆盖（值和时间戳都含负数）。
- 全重复：`allDuplicatesAgree` / `allDuplicateValuesReturnThatValue`。
- 同时间戳：`sameTimestampEventsAllBelongToSamePanes` 及服务样例测试。
- 窗口为空：算子补空窗、尾随窗口 null 分位数、空事件请求均有测试。
- 过期删除计数：`TrailingWindowQuantilesTest.expiryKeepsAccumulatorCountConsistent`
  逐拍断言 `count()`，以及全重复批量过期后计数归零。
