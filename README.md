# 事件时间滚动窗口计数服务（Event-Time Tumbling Window Counter）

纯后端服务，基于 **Java + JDK 内置 HttpServer**（零第三方依赖）。接收带分区的事件流与
**显式水位线（watermark）**，按事件时间维护左闭右开的滚动计数窗口；全局水位取活跃分区
水位的最小值，迟到容忍期结束后将超期事件写入**侧输出（side output）**。服务完全由输入
驱动、不读取系统时钟，因此同一条事件/水位序列永远产生完全相同的结果（确定性，可重放）。

---

## 1. 语义说明（与 Flink EventTime 窗口对齐）

| 概念 | 规则 |
|---|---|
| 窗口划分 | 固定大小 `windowSize`，窗口 `[k·size, (k+1)·size)`，**左闭右开**；事件按 `floor(eventTime / size)` 归属（支持负时间戳） |
| 分区水位 | 每个分区由调用方显式上报，单调不减；更低的上报值被钳制（返回 `clamped:true`） |
| 窗口驱动 | 窗口的触发/修订/最终关闭**只由本分区自己的水位**决定，不受其他快/慢分区影响（对齐 Flink 按 key 分区窗口的行为） |
| 全局水位 | 所有**活跃且已初始化**（至少上报过一次水位）分区水位的**最小值**，且全局水位本身单调不减；用于跨分区的"晚到/侧输出"判定 |
| 窗口触发 | 当**本分区水位** `>= windowEnd` 时输出一次 `fire`（初步计数），水位戳记为本分区水位 |
| 迟到判定 | `eventTime < 全局水位` 即为晚到；是否丢弃还要看本分区窗口是否已过容忍期 |
| 迟到容忍 | 在本分区水位 `>= windowEnd + allowedLateness` 之前到达的晚到事件仍计入窗口；若窗口已 fire，则立即再输出一版 `update`（`revision` 递增） |
| 最终关闭 | 当本分区水位 `>= windowEnd + allowedLateness` 时输出 `final`（最终计数），并清除窗口状态；若 fire 与 final 同一步满足（容忍期为 0 或水位跳变），只输出一条 `final`。**final 具有终结性**：之后任何落入该窗口的新事件一律进侧输出，窗口不会被"复活"或再次 final，关闭历史不可变 |
| 批量原子性 | `/events`、`/watermarks` 的批量请求先整体解析校验，再在同一把锁内顺序执行；任一条非法（坏字段/小数/越界）整体返回 400 且不留任何状态，可安全重试 |
| 侧输出 | 晚于全局水位、且本分区窗口已过容忍期（最终关闭）的事件不计数，写入侧输出，状态 `late_dropped` |
| 去重 | 同一 `(partition, eventId)` 只计数一次；重复事件状态为 `duplicate`，**即使原窗口已关闭也仍能识别**，且不会进入侧输出。去重集合不随窗口清除 |
| 空闲分区 | 可显式标记为 `idle`，空闲分区不参与全局水位最小值；向其发送事件或水位自动恢复（`wasIdle:true`），全局水位不会因恢复而回退 |
| 未初始化分区 | 只发过事件、从未上报水位的分区不参与全局 min，其事件不做迟到判定，其窗口也不会被任何水位触发；首次上报水位时按该水位处理自己的窗口 |

时间戳为任意 `long`（示例用小整数；生产可传 epoch 毫秒）。

---

## 2. 依赖与启动

- **JDK 17+**（在 Temurin OpenJDK **17.0.20.1** 上实测通过），无需 Maven/Gradle，无需联网下载任何 jar。
- 若 `java`/`javac` 不在 PATH，设置 `JAVA_HOME` 指向 JDK 即可，例如：
  ```bash
  export JAVA_HOME=$HOME/jdk17
  ```

### 编译 / 测试 / 启动 / 演示

```bash
./scripts/compile.sh                 # javac 编译到 out/
./scripts/run-tests.sh               # 编译并运行全部 22 个自动化测试
./scripts/run.sh [port] [windowSize] [allowedLateness]   # 启动服务（默认 8080 / 10 / 2）
./scripts/demo.sh [port]             # 自动起服务 + curl 跑确定性序列 + 停止
```

也支持环境变量：`PORT` / `WINDOW_SIZE` / `ALLOWED_LATENESS`（命令行参数优先）。

### 手动启动与请求样例

```bash
./scripts/run.sh 8080 10 2

# 健康检查
curl -s http://localhost:8080/health

# 单条事件
curl -s -X POST http://localhost:8080/events \
  -H 'Content-Type: application/json' \
  -d '{"partition":"a","eventId":"e2","eventTime":2}'

# 批量乱序事件（样例文件：examples/events.simple.json）
curl -s -X POST http://localhost:8080/events \
  -H 'Content-Type: application/json' \
  --data @examples/events.simple.json

# 推进分区 a 的水位到 10：[0,10) 立即 fire
curl -s -X POST http://localhost:8080/watermarks \
  -H 'Content-Type: application/json' \
  -d '{"partition":"a","watermark":10}'

# 批量水位（样例文件：examples/watermarks.batch.json）
curl -s -X POST http://localhost:8080/watermarks \
  -H 'Content-Type: application/json' \
  --data @examples/watermarks.batch.json

# 标记 / 恢复空闲分区（idle:false 恢复）
curl -s -X POST http://localhost:8080/partitions/idle \
  -H 'Content-Type: application/json' \
  --data @examples/partitions.idle.json

# 查询
curl -s http://localhost:8080/snapshot                 # 全量状态
curl -s http://localhost:8080/windows?partition=a      # 某分区窗口（fire/final 状态）
curl -s http://localhost:8080/side-output              # 侧输出
curl -s http://localhost:8080/duplicates               # 重复事件记录
curl -s http://localhost:8080/emissions                # 全部窗口输出（fire/update/final 序列）
curl -s http://localhost:8080/partitions               # 各分区水位/空闲状态
curl -s http://localhost:8080/config

# 清空状态（可顺带改窗口参数）
curl -s -X POST http://localhost:8080/reset -H 'Content-Type: application/json' -d '{}'
```

完整的真实请求/响应流水见 [`examples/demo-output.txt`](examples/demo-output.txt)（由
`scripts/demo.sh` 实际运行生成）。

---

## 3. HTTP 接口一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET  | `/health` | 健康检查 |
| GET  | `/config` | 窗口参数、全局水位、分区数 |
| POST | `/events` | 接入事件。单条 `{partition,eventId,eventTime}` 或批量 `{"events":[...]}` |
| POST | `/watermarks` | 设置分区水位。单条 `{partition,watermark}` 或批量 `{"watermarks":[...]}` |
| POST | `/partitions/idle` | `{partition, idle}` 标记/恢复空闲分区 |
| GET  | `/partitions` | 所有分区的水位、`initialized`、`idle` 及窗口 |
| GET  | `/windows`、`/windows?partition=p` | 全部/指定分区窗口（含 state：open/fired/final） |
| GET  | `/side-output` | 超容忍期事件列表 |
| GET  | `/duplicates` | 重复事件列表 |
| GET  | `/emissions` | 窗口输出流水（fire / update / final，带触发水位与 revision） |
| GET  | `/snapshot` | 全量状态快照（确定性对比用） |
| POST | `/reset` | 清空状态；body 可选 `windowSize` / `allowedLateness` |

错误返回统一为 `{"error":..., "message":...}`，状态码：400（坏 JSON / 缺字段）、404（路径
不存在）、405（方法不允许）、500。

### 典型响应字段

- 事件接入：`status` ∈ `on_time | late_accepted | duplicate | late_dropped`，
  以及所属 `windowStart/windowEnd`、处理前后的 `globalWatermarkBefore/After`；
  若晚到事件修订了已触发窗口，`emitted` 中会带一条 `update`。
- 水位推进：`requestedWatermark / appliedWatermark / clamped / advanced / emitted`，
  窗口输出中的 `watermark` 是**触发该窗口的本分区水位**（精确刻画关闭时间）。
- 窗口输出：`{type, partition, windowStart, windowEnd, count, revision, watermark}`。
- 侧输出/重复记录同时记录 `atGlobalWatermark`（晚到判定依据）与
  `atPartitionWatermark`（容忍期判定依据）。

---

## 4. 确定性验收序列（scripts/demo.sh 的实际结果）

窗口=10，迟到容忍=2。以下结果来自真实运行（完整响应见
`examples/demo-output.txt`）。排放序列、侧输出、重复记录都可用
`examples/demo-output.txt` 中的 JSON 精确复核：

| # | 输入 | 本分区/全局水位 | 关键结果 |
|---|---|---|---|
| 1 | 批量事件 a: t=2,7,5（乱序） | -∞ | 3 条均 on_time，`[0,10)` count=3 |
| 2 | a 水位→10 | a=10 | `[0,10)` **fire@a_wm=10，count=3**（触发时间精确等于窗口末端） |
| 3 | a 晚到事件 t=6 | a=10 | t=6<全局水位10 → late_accepted，**update count=4 revision=2** |
| 4 | a 未来事件 t=15 | a=10 | on_time，`[10,20)` count=1 |
| 5 | a 水位→12 | a=12 | `[0,10)` **final@a_wm=12，最终计数 4**（容忍期 end+2） |
| 6 | a 事件 t=1 | a=12 | t=1<全局水位 且窗口已过容忍期 → **late_dropped 侧输出** |
| 7 | a 重复 e7（时间戳改成 999） | a=12 | **duplicate**，不计数、不进侧输出 |
| 8 | b 事件 t=103,115（b 未上报水位） | b 未初始化 / 全局 12 | on_time；b 未初始化，不被高水位误伤，窗口不被触发 |
| 9 | b 水位→100 | b=100，全局 12 | `[100,110)` 末端 110 未到，无输出；全局仍被 a 的 12 限住 |
| 10 | a 水位→22 | a=22，全局 22 | **a 的窗口只随 a 自己推进**：`[10,20)` final@22；慢分区 b 不延迟 a（全局 min 升为 22） |
| 11 | b 水位→110 | b=110，全局 22 | **b 的窗口只随 b 自己推进**：`[100,110)` fire@110 count=1；快分区 a 不提前关 b |
| 12 | b 置空闲；a 水位→130 | 全局 130 | 空闲 b 被排除出全局 min，全局水位涨到 130 |
| 13 | b 恢复，补缓存事件 t=108 | b=110 | `wasIdle:true`；窗口已 fire 但容忍期内 → **late_accepted update count=2 rev=2**（恢复后容忍期内数据不丢） |
| 14 | b 水位→112 | b=112 | `[100,110)` **final count=2**；全局水位因单调性保持 130（恢复的慢分区不拉回全局） |
| 15 | b 事件 t=105 | b=112 | 窗口已最终关闭 → **late_dropped 侧输出** |
| 16 | b 水位→130 | b=130 | `[110,120)` 的 fire(120)/final(122) 被水位跳变一次越过，**直接 final count=1** |

最终排放日志 8 条，顺序与计数确定：

```
fire    a [0,10)    count=3 rev=1 @wm=10
update  a [0,10)    count=4 rev=2 @wm=10
final   a [0,10)    count=4 rev=2 @wm=12
final   a [10,20)   count=1 rev=1 @wm=22
fire    b [100,110) count=1 rev=1 @wm=110
update  b [100,110) count=2 rev=2 @wm=110
final   b [100,110) count=2 rev=2 @wm=112
final   b [110,120) count=1 rev=1 @wm=130
```

侧输出 2 条（a 的 t=1、b 恢复后超期的 t=105）；重复记录 1 条（e7）。

### 覆盖的验收点

- **乱序**：t=2,7,5 乱序接入、全局晚到但容忍期内的 t=6 修订（用例 1 / 3 / 13）。
- **窗口边界与关闭时间**：左闭右开（t=10 归 `[10,20)`）、fire 恰好发生在**本分区水位**=窗口末端、
  final 恰好发生在 end+lateness；水位跳变时 fire/final 合并；负时间戳 floorDiv 归属
  （`negativeTimestampFloorDiv`）。
- **窗口独立性**：快分区不被慢分区延迟、慢分区不被快分区提前关闭
  （`windowTimingDrivenByOwnPartitionWatermark` + 演示 9–11）。
- **空闲分区恢复**：空闲时被排除出全局 min、全局水位借快分区前进、恢复后容忍期内晚到数据
  仍可修订、更陈旧数据进侧输出、全局水位不回退
  （`idlePartitionExcludedThenResumes` / `idleResumeLateAndSideOutput` + 演示 12–15）。
- **重复事件**：同 id 不计数、窗口 purge 后仍识别、不污染侧输出、按分区作用域
  （`duplicatesNotCountedEvenAfterPurge`）。
- **确定性**：同一序列重放两次快照逐字节相等（`deterministicReplay`）。

---

## 5. 自动化测试

测试与主程序一起由 `./scripts/run-tests.sh` 编译执行（零依赖自研 runner，失败退出码 1）：

- `test/tumbling/EngineTest.java` — 13 个引擎级语义/回归用例（纯状态机，不起网络）。
- `test/tumbling/JsonTest.java` — 4 个 JSON 解析用例（前导零、超 long 整数、小数拒绝、嵌套/转义）。
- `test/tumbling/HttpTest.java` — 5 个端到端用例（真实起 Server + Java HttpClient 发
  HTTP，覆盖单条/批量、批量原子性、空闲、重复、查询、reset、400/404/405 错误路径）。

### 实际运行结果（本机如实记录）

```
编译完成 → out/
  ✓ EngineTest.outOfOrderWithinWatermark
  ✓ EngineTest.halfOpenBoundaryZeroLateness
  ✓ EngineTest.negativeTimestampFloorDiv
  ✓ EngineTest.idlePartitionExcludedThenResumes
  ✓ EngineTest.idleResumeLateAndSideOutput
  ✓ EngineTest.duplicatesNotCountedEvenAfterPurge
  ✓ EngineTest.partitionsIndependent
  ✓ EngineTest.windowTimingDrivenByOwnPartitionWatermark
  ✓ EngineTest.watermarksMonotonic
  ✓ EngineTest.deterministicReplay
  ✓ EngineTest.purgedWindowIsNotResurrected
  ✓ EngineTest.extremeTimestampOverflowRejected
  ✓ EngineTest.returnedRecordsAreDefensiveCopies
  ✓ JsonTest.rejectsLeadingZeros
  ✓ JsonTest.rejectsOutOfRangeInteger
  ✓ JsonTest.lngRejectsFractions
  ✓ JsonTest.parsesNestedAndUnicode
  ✓ HttpTest.ingestWatermarkAndCountOverHttp
  ✓ HttpTest.idleDuplicateAndQueriesOverHttp
  ✓ HttpTest.allWindowsCarryPartitionAndState
  ✓ HttpTest.batchIsAtomicOnValidationError
  ✓ HttpTest.errorPaths

通过 22 个，失败 0 个。
```

`scripts/demo.sh` 端到端演示实际退出码 0，响应流水已保存到
`examples/demo-output.txt`。

---

## 6. 项目结构

```
├── README.md                     本文档
├── dependencies.lock             依赖锁定（零第三方依赖 + 已验证 JDK 版本）
├── scripts/
│   ├── compile.sh                javac 编译到 out/
│   ├── run-tests.sh              编译 + 跑全部测试
│   ├── run.sh                    启动服务
│   └── demo.sh                   确定性端到端演示（自动起停）
├── src/tumbling/
│   ├── Main.java                 入口（端口/窗口参数）
│   ├── Server.java               JDK HttpServer 路由与 JSON 处理（单线程串行）
│   ├── WindowEngine.java         窗口/水位/侧输出/去重 核心状态机
│   └── Json.java                 手写 JSON 解析与序列化
├── test/tumbling/
│   ├── TestRunner.java           零依赖测试框架（@Test 注解 + 反射）
│   ├── EngineTest.java           13 个引擎语义/回归用例
│   ├── JsonTest.java             4 个 JSON 解析用例
│   └── HttpTest.java             5 个 HTTP 端到端用例
├── examples/
│   ├── events.simple.json        批量事件请求样例（小整数时间戳）
│   ├── events.batch.json         批量事件请求样例（epoch 毫秒时间戳）
│   ├── watermarks.batch.json     批量水位请求样例
│   ├── partitions.idle.json      空闲分区请求样例
│   └── demo-output.txt           demo.sh 的真实请求/响应流水
└── out/                          编译产物（脚本生成，不入库）
```

---

## 7. 设计备注与已知限制

- 状态保存在**内存**中，`/reset` 或重启即清空；未做持久化（本任务只要求纯后端计数服务）。
- 去重集合与侧输出/排放日志不设容量上限与 TTL，超长运行会持续增长；生产化时应加
  状态 TTL / RocksDB 式后端。本实现规模面向功能验收。
- HTTP 服务单线程串行处理请求，保证到达顺序即处理顺序；需要吞吐时可换有界线程池，但
  同分区的事件/水位仍需保证有序。
- 一个刻意的设计选择：**窗口只由本分区水位驱动，全局水位只做晚到/侧输出判定**。这与
  Flink 在 keyBy 之后每个键独立持有分区窗口的行为一致，可避免"慢分区延迟快分区关窗"或
  "快分区提前关掉慢分区窗口"两类错误；全局 min 的意义保留在跨分区的迟到判定与
  空闲分区协调上。
- 时间戳单位对引擎透明（小整数或 epoch 毫秒均可），窗口参数与其同单位。
- 输入严格校验：事件时间/水位必须是整数（拒绝 `15.9` 这类会改变窗口归属的小数）、
  拒绝 JSON 前导零与超 `long` 整数；时间戳接近 `long` 边界导致窗口末端溢出时返回 400，
  不产生静默错误的负窗口。
- 所有对外返回的记录（排放、侧输出、重复、快照）均为防御性拷贝，外部在 JVM 内修改返回值
  不会污染引擎内部状态。
- 未完成/未覆盖项：鉴权、TLS、监控指标、持久化、容器镜像均不在本次范围内。
