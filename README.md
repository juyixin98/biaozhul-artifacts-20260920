# 双流时间连接（Dual-Stream Interval Join）— 纯后端

零外部依赖的 Java 事件流计算库 + JSON HTTP 服务。实现**按键双流区间连接**（interval
join）：两侧事件各自携带事件时间，当且仅当键相同且右事件时间戳落在左事件时间戳的
`[L+lowerBound, L+upperBound]` 区间内时输出一对事件。

- 仅需 **JDK 21**（编译/运行/测试全部使用 `javac`/`java`，无 Maven/Gradle、无第三方库、无外部消息系统）。
- **时间与调度可注入**：`Clock` / `ProcessingTimeService` 同时提供真实实现与手动（虚拟时间）实现。
- 提供**小数据精确参考实现**（朴素 O(n²)、不做提前清理），自动化测试用随机流与其差分对拍。

---

## 1. 目录结构

```
src/com/tjoin/
  core/      事件流计算库（无 I/O 依赖）
    Event.java                 事件（唯一 id、侧别、键、事件时间戳、值）
    StreamSide.java            LEFT / RIGHT
    JoinConfig.java            区间上下界、闭/开边界、每键缓冲上限
    JoinPair.java              一次输出（leftId,rightId 决定相等性）
    IntervalJoinOperator.java  核心算子：状态、独立水位线清理、去重、容量保护
    ReferenceIntervalJoin.java 小数据精确参考实现（oracle）
    Collector.java             输出回调
    JoinMetrics.java           接收/输出/重复/迟到/过期/陈旧水位线计数
    BufferCapacityExceededException.java
  time/      可注入时间与调度
    Clock.java / SystemClock.java / ManualClock.java
    ProcessingTimeService.java / SystemProcessingTimeService.java / ManualProcessingTimeService.java
    WatermarkGenerator.java / BoundedOutOfOrdernessWatermarks.java
    PeriodicWatermarkAssigner.java
  service/   JSON 输入输出服务
    Json.java                  零依赖 JSON 解析/序列化
    JoinSimulation.java        请求驱动的模拟（steps 模式 / simple 模式）
    JoinHttpServer.java        JDK HttpServer：GET /health，POST /join
    Main.java                  启动入口
  demo/Demo.java               命令行验收演示（5 个场景）
test/com/tjoin/                零依赖迷你测试框架 + 40 个自动化测试
samples/                       请求样例 JSON
scripts/                       build.sh / test.sh / run-demo.sh / run-server.sh / smoke.sh
```

## 2. 快速开始

```bash
# 编译（输出到 build/classes）
bash scripts/build.sh

# 运行全部自动化测试（40 个）
bash scripts/test.sh

# 命令行验收演示（无需起服务）
bash scripts/run-demo.sh

# 端到端冒烟测试（自动起停服务、发送全部样例并校验）
bash scripts/smoke.sh

# 启动 HTTP 服务（默认 8080；传 0 由系统分配端口）
bash scripts/run-server.sh 8080
```

### HTTP 调用示例

```bash
curl -s http://localhost:8080/health

curl -s -X POST http://localhost:8080/join \
  -H 'Content-Type: application/json' \
  -d @samples/stalled-side.json
```

## 3. 请求格式

两种模式二选一（提供 `steps` 时走 steps 模式）。

### 3.1 `steps` 模式：显式控制事件/水位线交错顺序

```json
{
  "config": {"lowerBound": 0, "upperBound": 5, "maxBufferedPerSide": 100},
  "steps": [
    {"type": "event", "side": "LEFT",  "id": "L1", "key": "k", "ts": 10, "value": "v10"},
    {"type": "event", "side": "LEFT",  "id": "L2", "key": "k", "ts": 12},
    {"type": "watermark", "side": "LEFT", "watermark": 1000},
    {"type": "event", "side": "RIGHT", "id": "R1", "key": "k", "ts": 15},
    {"type": "watermark", "side": "RIGHT", "watermark": 16}
  ]
}
```

- `config`
  - `lowerBound` / `upperBound`：连接区间（含），`lowerBound <= upperBound`；
  - `lowerInclusive` / `upperInclusive`：边界是否闭（默认都为 `true`）；
  - `maxBufferedPerSide`：**每个键、每侧**缓冲事件条数上限，`0`/缺省为不限；
    超限返回 HTTP 422。
- 步骤 `{"type":"event", ...}`：事件；`{"type":"watermark","side":...,"watermark":...}`：
  推进该侧水位线（`"wm"` 是其别名）。
- 事件字段：`id`（同键侧别内唯一）、`side`（`LEFT`/`RIGHT`）、`key`、`ts`
  （`timestamp` 亦可）、`value`（任意 JSON）。

### 3.2 `simple` 模式：给两侧事件数组，服务按 (ts, 侧别, 原下标) 确定性回放

```json
{
  "config": {"lowerBound": -2, "upperBound": 2},
  "left":  [{"id":"L1","key":"a","ts":10,"value":{...}}],
  "right": [{"id":"R1","key":"a","ts":10,"value":{...}}],
  "outOfOrderness": 0,
  "advanceSide": "RIGHT",
  "advanceWatermarkTo": 100
}
```

### 响应

```json
{
  "ok": true,
  "results": [ {"key":"k","left":{...},"right":{...},"tsDiff":5} ],
  "trace":   [ {"type":"event|watermark","side":"...","emitted":N,
                "buffered":{"left":..,"right":..},
                "watermarks":{"left":..,"right":..}, ...} ],
  "watermarks": {"left": .., "right": .., "output": ..},
  "buffered":   {"left": .., "right": ..},
  "metrics": {"leftReceived":..,"rightReceived":..,"duplicates":..,
              "lateDropped":..,"emitted":..,"leftExpired":..,"rightExpired":..,
              "staleWatermarks":..}
}
```

`trace` 逐步记录每一步后的两侧缓冲与水位线，便于核对“什么时候该清理、什么时候不该”。

错误：请求格式/语义问题 → HTTP 400；缓冲超上限 → HTTP 422；错误方法 → 405。

## 4. 语义与设计要点

### 4.1 连接条件

`L.ts + lowerBound <= R.ts <= L.ts + upperBound`（边界可配为开区间）。
每个事件到达时与对侧**已缓冲**事件做范围匹配（`TreeMap` 子图），立即输出全部命中对。
因此无论左事件还是右事件先到，结果集合相同。

### 4.2 两侧独立水位线驱动状态清理（不提前丢弃可匹配记录）

设两侧水位线 `wmL`、`wmR`（初始 `Long.MIN_VALUE`，非单调推进被忽略并计数）：

- 左事件 `L` 的删除条件：右水位线已越过它可能匹配的最大右时间戳，
  即 `wmR - L.ts > upperBound`（上界开时为 `>=`）。**由右水位线驱动**。
- 右事件 `R` 的删除条件：未来左事件（时间戳 `>= wmL`）已不可能落入区间，
  即 `R.ts - wmL < lowerBound`（下界开时为 `<=`）。**由左水位线驱动**。

结论：**一侧停滞时，另一侧水位线不动，任何仍可匹配的记录都不会被删除。**
此外，新事件在与对侧缓冲匹配后，还会立刻按当前对侧水位线做一次清理
（例如在左水位线已经很远后才到达的右事件，匹配完现存左事件后立即过期，
不必等待下一次水位线步骤）。下游水位线取 `min(wmL, wmR)`。

### 4.3 重复值允许，但每对事件仅输出一次

- 值（`value`）可以任意重复；事件身份由 `id` 决定。
- 同一 (键,侧别,id) 的重复投递只处理一次（含迟到事件的重放）。
- 去重记忆的淘汰与缓冲生命周期保持一致：事件仍在缓冲中时其 ID 一定保留；
  仅当该事件已迟到（`ts < 本侧水位线`）且不在缓冲中时才淘汰。
  因此“过期后重放同一 ID”不会重新入缓冲、不会产生第二次输出。
  每对 (leftId,rightId) 天然最多输出一次。

### 4.4 迟到与缓冲上限

- 迟到事件（`ts < 本侧水位线`）丢弃并计入 `lateDropped`；`ts == wm` 不算迟到。
- `maxBufferedPerSide` 按**每个键**限制单侧缓冲条数。一侧长期停滞时，
  对侧不断到达会使缓冲增长，达到上限即抛 `BufferCapacityExceededException`
  （HTTP 422），把“无限缓冲”变为显式、可观测的失败，而不是静默 OOM。

### 4.5 可注入时间与调度

- `Clock`：系统墙钟 / `ManualClock`（只在手动推进时前进）。
- `ProcessingTimeService`：真实单线程调度器 / `ManualProcessingTimeService`
  （虚拟时间，`advanceBy/advanceTo` 时触发沿途所有一次性/周期任务）。
- `BoundedOutOfOrdernessWatermarks`：`watermark = maxTs - maxOutOfOrderness`。
- `PeriodicWatermarkAssigner`：用注入的处理时间服务周期推进某侧水位线。
  `Demo` 场景 5 与 `TimeServiceTest` 在零真实等待下确定性验证周期水位线行为。

### 4.6 小数据精确参考实现与差分测试

`ReferenceIntervalJoin` 保存所有已接纳事件、暴力两两比较、集合去重，
不做任何提前清理。`ReferenceConsistencyTest` 随机生成约 700 组小数据场景
（多键、乱序、值重复、真实 ID 重放、两侧独立推进水位线、一侧长时间停滞、
闭/开边界），以相同驱动顺序喂给流式算子与参考实现，逐步断言输出集合一致、
每个配对恰好一次，并在流末检查缓冲完全清空。

## 5. 请求样例

| 文件 | 说明 |
|---|---|
| `samples/simple-join.json` | simple 模式基础连接（多键、边界内/外） |
| `samples/stalled-side.json` | 右流停滞 → 左记录保留；水位线 15/16 的边界清理 |
| `samples/exclusive-bounds.json` | 开区间：差恰为 ±2 不匹配，差 0 匹配 |
| `samples/duplicates.json` | 允许重复值；重复 ID 投递不产生重复配对 |
| `samples/buffer-capacity.json` | 停滞 + 缓冲上限 → HTTP 422 |

## 6. 验收关注点对照

- **一侧停滞**：`stalled-side.json`、`Demo` 场景 1、
  `IntervalJoinOperatorTest.stalledRightWatermarkKeepsLeftRecords` /
  `stalledLeftWatermarkKeepsRightRecords`、差分测试 stall 变体。
- **时间边界**：`exclusive-bounds.json`、`Demo` 场景 2、
  `boundariesInclusiveByDefault` / `exclusiveBoundariesExcludeExactEdges`、
  `rightWatermarkExpiresLeftOnlyAtCorrectPoint`（清理点在边界值的下一个单位）。
- **水位线推进**：`trace` 字段、`PeriodicWatermarkAssigner`、
  `TimeServiceTest.periodicAssignerDrivesOperatorWithInjectedTime`。
- **不提前丢弃可匹配记录**：独立水位线清理（4.2）+ 上述停滞用例。
- **限制缓冲**：`maxBufferedPerSide`、`buffer-capacity.json`、`Demo` 场景 4、
  `capacityLimitTriggersWhenSideStalls`。
- **重复值 / 每对一次**：`duplicates.json`、`Demo` 场景 3、
  `pairEmittedAtMostOnceAcrossManyRedeliveries`、
  `expiredRecordRedeliveryDoesNotRejoin`。
