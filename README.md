# 双流时间连接（Dual-Stream Event-Time Interval Join）

纯后端事件流计算库 + JSON 输入输出服务。对**两条独立事件流**按键（key）做事件时间
**区间连接**，两侧各自维护独立水位线并据此清理状态；时间与调度器完全可注入；附带
小数据**精确参考实现**用于差分校验。不依赖任何外部消息系统（无 Kafka/Pulsar 等）。

- 语言/构建：Java 21、Maven（唯一运行期依赖：Jackson；HTTP 使用 JDK 内置
  `com.sun.net.httpserver.HttpServer`）
- 连接规则：左事件 `(k, tL)` 与右事件 `(k, tR)` 当且仅当
  `lowerBound ≤ tR − tL ≤ upperBound`（含边界）时输出一对
- **允许重复值**，但靠唯一事件 ID 去重；**每对事件只输出一次**
- 时间可注入（`Clock` / `Scheduler` / `ManualClock`），服务端处理时间只能通过
  API 推进，因此所有演示与测试都是确定性的，没有墙钟不确定性

---

## 1. 构建与运行

需要 JDK 21 与 Maven 3.8+。

```bash
mvn package            # 编译 + 测试 + 打包（跳过测试用 -DskipTests）
```

产物：`target/dual-stream-interval-join-1.0.0.jar`（可执行 fat jar）。

### 1.1 启动 JSON HTTP 服务

```bash
java -jar target/dual-stream-interval-join-1.0.0.jar --port 8080
curl -s http://127.0.0.1:8080/health
```

服务内每个作业使用 **ManualClock**，处理时间从 0 开始、只随请求推进。

### 1.2 离线 JSON 场景（不起服务）

```bash
java -cp target/dual-stream-interval-join-1.0.0.jar \
     com.example.tjoin.json.BatchRunner examples/scenario-stalled-side.json
```

输出该场景逐步结果（每步状态/输出/淘汰/清理）与最终状态。
`examples/scenario-*.json` 即四个验收场景。

### 1.3 一键端到端演示

```bash
PORT=18099 bash scripts/demo.sh
```

自动构建、起服务、依次驱动四个验收场景（边界/去重、一侧停滞、缓冲限制、
水位线边界+迟到），完整应答记录在 `examples/output/transcript.log`。

### 1.4 运行测试

```bash
mvn test
```

---

## 2. 核心语义

### 2.1 区间连接与“每对仅一次”

- 匹配只看 key 与事件时间差，区间边界**闭区间**：`distance == lowerBound` 与
  `distance == upperBound` 都算匹配（测试
  `IntervalJoinOperatorTest.Boundaries` 逐毫秒验证）。
- 每对 `(leftId, rightId)` 在作业生命周期内只输出一次：
  - 新事件到达时只扫描对侧缓冲状态（自身尚未入缓冲），因此天然不会与自己配对；
  - 算子另维护已输出对集合 `emittedPairs` 兜底，任何路径都不会重复输出。
- **重复值合法**：相同 key、时间戳、value 的两条事件是不同记录，分别参与匹配
  （见 `duplicateValuesAllowed` 与 HTTP 用例 `duplicateIdsAndValues`）。
- **重复投递（相同事件 ID）被忽略**，返回 `DUPLICATE`。ID 在作业生命周期内一直
  被记住——即使该事件已因水位线清理离开缓冲，重传仍被识别（ID 不复用）。
  被拒绝的迟到/缓冲满事件不占用其 ID，可用同一 ID 合法重试。

### 2.2 时间与调度可注入

| 组件 | 作用 |
|---|---|
| `time.Clock` | 当前处理时间（`SystemClock` 或 `ManualClock`） |
| `time.Scheduler` | 定时器（`SystemScheduler` 或 `ManualClock` 自身） |
| `time.ManualClock` | 时间冻结直到 `advanceTo/advanceBy`，到期定时器按时间戳、注册顺序同步触发 |

算子内部**不直接调用系统时钟、不创建线程**。库代码只依赖注入的 `Clock` 给输出
打处理时间戳、记录侧活跃度；空闲检测由调度器周期性触发（周期 100ms 处理时间）。

### 2.3 两侧独立水位线与状态清理

每侧一个独立的 **bounded-out-of-orderness** 水位线：

```
watermark(side) = maxSeenEventTime(side) − maxOutOfOrderness(side)
```

- 事件时间 `< 本侧水位线` 的事件判为**迟到**丢弃（恰好等于水位线不丢）。
- 也可通过 `POST .../watermark/{side}` 直接注入水位线。

缓冲记录只在“**不可能再匹配未来任何非迟到事件**”时才被清理（严格小于阈值才删，
边界记录保留）：

```
左记录 tL 在  tL < watermarkRight − upperBound  时清理
右记录 tR 在  tR < watermarkLeft  + lowerBound  时清理
```

**一侧停滞（idle）时的关键保证**：停滞侧的水位线**冻结在最后已知值**——
既不继续前进（不会把对侧仍可被“恢复事件”匹配的记录提前清掉），也不撤回为
−∞（确已无匹配可能的陈旧记录仍被回收，缓冲因此保持有界）。侧空闲由
*处理时间* 上的 `idleTimeoutMillis` 判定（注入时钟驱动），与事件时间无关；
恢复活动后空闲标志清除，水位线继续推进。

> 设计说明：冻结（而非撤回至 −∞）是可证明安全的——恢复侧若再带来早于冻结水位线
> 的事件，该事件对恢复侧自身而言就是迟到事件、本就应丢弃；所以冻结水位线不会
> 错杀任何可匹配记录。

### 2.4 缓冲限制

每侧独立配置 `maxBufferSize`（≤0 表示不限）与溢出策略：

- `REJECT`（默认）：超容量的新事件返回 `BUFFER_FULL`，**绝不删除任何已缓冲、
  仍可匹配的记录**；水位线清理自然释放容量后可再写入。
- `DROP_OLDEST`：淘汰该侧全局事件时间最小的记录（跨 key）腾位，淘汰计入
  `oldestEvicted` 指标。该策略可能丢失理论上仍可匹配的记录，因此为显式可选。

---

## 3. HTTP API

所有路径前缀 `/api/v1`，请求/响应均为 JSON。响应统一信封：
`{"ok":true,"data":...}` / `{"ok":false,"error":"...","message":"..."}`。

| 方法与路径 | 说明 |
|---|---|
| `POST /api/v1/jobs` | 创建作业 |
| `GET  /api/v1/jobs` | 列出作业 |
| `GET  /api/v1/jobs/{id}/status` | 双侧水位线、缓冲量、全部计数指标 |
| `DELETE /api/v1/jobs/{id}` | 删除作业 |
| `POST /api/v1/jobs/{id}/events/{left\|right}` | 推送一批事件 |
| `POST /api/v1/jobs/{id}/watermark/{left\|right}` | 显式推进某侧水位线 |
| `POST /api/v1/jobs/{id}/time` | 推进处理时间（`advanceTo`/`advanceBy`） |
| `GET  /health` | 健康检查 |

### 创建作业

```json
{
  "jobId": "acc",
  "lowerBound": 0,
  "upperBound": 10000,
  "left":  {"maxOutOfOrderness": 0, "idleTimeoutMillis": 5000,
            "maxBufferSize": 100, "overflowPolicy": "REJECT"},
  "right": {"maxOutOfOrderness": 0, "idleTimeoutMillis": 5000,
            "maxBufferSize": 100, "overflowPolicy": "REJECT"}
}
```

未提供 `left/right` 时，顶层的同名字段作为两侧默认配置。

### 推送事件

```json
{
  "processingTime": 2000,
  "events": [
    {"id": "R1", "key": "k", "eventTime": 15000, "value": "payment-1"}
  ]
}
```

- `processingTime` 可选：把注入时钟推进到该时刻（单调），从而可观察空闲超时。
- 响应 `data` 含 `emittedCount`、`emitted`（本次新产生的连接对）、逐事件
  `items[].status`（`ACCEPTED/DUPLICATE/LATE/BUFFER_FULL`）、`evictedId`、
  `cleaned` 以及完整 `status`。
- 水位线在未初始化时序列化为 `null`。

更多可直接导入 IDE/REST 客户端的样例见 [`examples/requests.http`](examples/requests.http)，
curl 版本见 `examples/http/*.json` + `scripts/demo.sh`。

### 离线场景文件格式（BatchRunner）

```json
{
  "config": { "lowerBound": 0, "upperBound": 10000, "left": {...}, "right": {...} },
  "steps": [
    {"type":"event","side":"left","processingTime":1000,
     "event":{"id":"L1","key":"k","eventTime":10000,"value":42}},
    {"type":"watermark","side":"right","processingTime":2000,"watermark":9000},
    {"type":"advanceTime","to":8000},
    {"type":"tick"}
  ]
}
```

---

## 4. 代码结构

```
src/main/java/com/example/tjoin/
├── model/       事件、连接结果、配置、指标、处理状态、缓冲溢出策略、侧枚举
├── time/        Clock/Scheduler 接口，SystemClock/SystemScheduler，ManualClock
├── watermark/   WatermarkGenerator（每侧独立：BOO 水位线 + 处理时间空闲检测）
├── state/       SideState（key→时间桶→事件；区间扫描、水位线清理、最旧淘汰）
├── join/        IntervalJoinOperator（核心算子）
├── ref/         ReferenceIntervalJoin（O(n·m) 精确参考实现）
├── json/        BatchRunner（离线 JSON 场景）
└── server/      Main + JoinHandlers + JobRegistry + JoinJob（JSON HTTP 服务）
src/test/java/   51 个 JUnit 5 测试（见下）
examples/        scenario-*.json、http/*.json 请求体、requests.http、output/ 实跑输出
scripts/demo.sh  端到端演示
```

---

## 5. 测试

| 测试类 | 覆盖内容 |
|---|---|
| `IntervalJoinOperatorTest`（含 `Boundaries` 嵌套） | 两种到达顺序、含边界的逐毫秒边界、重复值、重复 ID、每对一次、扇出 |
| `WatermarkCleanupTest` | 双侧清理阈值、边界精确清理、显式水位线、迟到丢弃、BOO 乱序容忍、两侧水位线独立 |
| `StalledSideTest` | 一侧停滞时不提前丢弃可匹配记录、恢复后仍匹配、空闲超时边界 |
| `BufferBoundTest` | REJECT 不丢可匹配记录、DROP_OLDEST 跨 key 淘汰、清理释放容量 |
| `ReferenceDifferentialTest` | 200 组随机场景与暴力参考实现差分（含全负区间、同刻密集重复值） |
| `ManualClockTest` | 注入时钟/定时器的顺序、链式自调度、禁回拨 |
| `SystemSchedulerTest` | 墙钟调度器冒烟（触发与关闭） |
| `BatchRunnerScenarioTest` | 随仓库提供的 4 个场景的验收断言 |
| `HttpServiceIntegrationTest` | 对真实内嵌 HTTP 服务做端到端验收（10 个用例） |

随机差分测试中，两侧事件各自按事件时间非递减生成（因此无迟到、无清理副作用），
再随机交错喂入，输出集合必须与参考实现完全一致。

---

## 6. 范围与已知限制

- 单进程内存实现，作业存于内存，重启不保留（需求明确无需外部消息系统/持久化）。
- 已接纳事件 ID 与已输出对集合为作业级内存结构，长期运行作业需外部去重存储；
  缓冲中的事件本身受 `maxBufferSize` 与水位线清理双重约束。
- 空闲检测分辨率为处理时间 100ms 的内部 tick；也可在每次请求后由服务端主动
  执行一次检查，因此 HTTP 场景的空闲判定不依赖 tick 粒度。
- 时间戳为 64 位毫秒；水位线未初始化用 `Long.MIN_VALUE`（JSON 中为 `null`）。
