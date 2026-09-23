# 多源水位线协调（Multi-Source Watermark Coordination）

纯后端项目：一个事件流水位线计算库 + 一个 JSON 输入输出服务。
**无前端、无外部消息系统、无第三方运行时依赖**（仅 JDK 21；测试用 JUnit 5，由 Maven 自动获取）。
时间与调度均可注入，测试用虚拟时钟确定性驱动；另提供一个“小数据精确参考实现”作为标准答案交叉校验。

---

## 1. 解决什么问题

多个来源（分区/分片）并行产生带事件时间的事件时，需要一个**全局水位线**告诉下游：
“事件时间不超过 w 的事件都已到齐，可以放心输出/关闭窗口”。难点：

- 全局水位线 = 各活跃分区水位线的 **min**，一个停滞分区会永久拖住全局进度 → 需要**空闲检测**；
- 分区暂停后恢复、重放旧数据时，不能让已经推进的全局水位线**倒退**；
- 恢复分区补送的旧事件必须明确进入**迟到通道**，不能被当新数据处理。

本项目给出可测试、可注入时间的精确实现，并通过虚拟时钟验证上述全部场景。

## 2. 核心语义（精确定义）

设分区 p 已观察到的最大事件时间为 `maxTs(p)`，乱序等待宽度为 `B(boundMs)`：

| 概念 | 定义 |
|---|---|
| 分区本地水位线 | `wmLocal(p) = maxTs(p) − B` |
| 恢复基线 `floor(p)` | 分区从 IDLE/PAUSED 恢复那一刻的全局水位线 |
| 分区有效水位线 | `wmEff(p) = max(wmLocal(p), floor(p))` |
| **全局水位线** | 所有 `ACTIVE` 分区 `wmEff` 的 **min**，并对时间取**单调包络**（只增不减） |
| 空闲判定 | `now − 最后事件处理时间 ≥ idleTimeoutMs`（**边界含等于**） |
| 准时 / 迟到 | 事件时间 `t > w` 准时；`t ≤ w` 迟到（**边界含等于**），w 为全局水位线 |
| 恢复旧事件 | 分区此前收过事件、处于 IDLE/PAUSED，重新接入后注入且迟到 → `RECOVERED_OLD` |

**为什么全局水位线不会因恢复而倒退：** 分区恢复时把自己的有效基线抬到当时的全局水位线
`floor = max(floor, globalWatermark)`。于是它在“追上全局进度”之前不会重新参与拉低 min；
本地水位线 `wmLocal` 仍如实反映该分区数据，快照中同时暴露 `localWatermarkMs` 与
`effectiveWatermarkMs` 两个值，便于审计。全局值本身还额外有一道单调闸门，双保险。

**全部分区都空闲/暂停时：** 全局水位线**保留**最近值（不清空、不倒退），用 `activeCount=0` 表达“暂无活跃分区”。

**迟到判定锚定全局水位线**（不是分区本地进度）：快分区收到一条低于自身历史最大值、
但仍高于全局线的乱序事件，仍算准时。

## 3. 模块结构

```
src/main/java/com/example/wm/
├── time/                        # 可注入的时间与调度
│   ├── Clock.java               #   时钟接口
│   ├── SystemClock.java         #   系统墙钟（生产）
│   ├── VirtualClock.java        #   手动虚拟时钟，只能前进不能后退（测试/模拟）
│   ├── Scheduler.java / ScheduledTask.java
│   ├── SystemScheduler.java     #   ScheduledExecutorService 实现（生产）
│   └── ManualScheduler.java     #   由测试线程按虚拟时钟手动触发（确定性）
├── model/                       # StreamEvent / WatermarkConfig / 各种结果与快照 record
│   ├── PartitionStatus.java     #   ACTIVE / IDLE / PAUSED
│   ├── Classification.java      #   ON_TIME / LATE（含边界说明）
│   └── LateReason.java          #   NORMAL / RECOVERED_OLD
├── engine/
│   └── WatermarkCoordinator.java# ★ 增量维护的生产级协调器（全部规则的实现）
├── reference/
│   └── ReferenceCoordinator.java# ★ 小数据精确参考实现：保存完整操作日志，
│                                  每次从空状态平铺重放，O(n²)，作为 oracle
├── json/                        # 零依赖手写 JSON 解析/序列化
└── service/
    ├── SimulationService.java   # JSON 脚本 → 虚拟时钟确定性重放 → JSON 响应
    └── HttpService.java         # JDK HttpServer：POST /simulate、GET /health；也可 CLI 批处理
```

**双实现对照**是本项目正确性的核心：增量引擎为 O(1)/事件，参考实现刻意朴素、
完全按定义重放，测试把同一条随机操作序列同时喂给两者，逐步比对全部可观测状态。

## 4. JSON API

### `POST /simulate`

请求是一段带显式处理时间的操作脚本，响应给出逐步结果、最终快照和迟到事件列表。
相同请求永远得到相同响应（虚拟时钟，确定性）。

顶层字段：

| 字段 | 含义 | 默认 |
|---|---|---|
| `boundMs` | 乱序等待宽度 B，分区水位线 = maxEventTime − B | 0 |
| `idleTimeoutMs` | 空闲超时（毫秒，边界含等于） | 不限 |
| `startTimeMs` | 虚拟时钟起点 | 0 |
| `includeLateEvents` | 响应是否含迟到事件明细 | true |
| `actions` | 操作数组，**必填**，按数组顺序执行 | — |

action 类型：

| type | 字段 | 说明 |
|---|---|---|
| `register` | `partition` | 预注册分区（ACTIVE，尚无水位线时不约束 min） |
| `advance` | `durationMs` | 虚拟时钟前进一段（不做检测） |
| `advanceTo` | `time` | 虚拟时钟前进到绝对时间 |
| `event` | `partition`, `eventTime`, `payload?`, `time?` | 注入事件；先把时钟推进到可选 `time`；自动恢复空闲/暂停分区 |
| `tick` | `time?` | 在当前（或指定）处理时间执行空闲超时检测并重算全局水位线 |
| `pause` | `partition`, `time?` | 显式暂停（等价于退出 min 聚合） |
| `resume` | `partition`, `time?` | 显式恢复（无事件），同样抬升恢复基线 |

每个 event 步骤返回：`accepted`、`classification`(ON_TIME/LATE)、`reason`(NORMAL/RECOVERED_OLD/null)、
`resumedFromIdle`、`previousGlobalWatermark`、`globalWatermark`、`timedOutPartitions`。

### `GET /health`

返回 `{"ok": true, "service": "multi-source-watermark"}`。

错误均返回 JSON：参数/格式错误 HTTP 400（`bad_request`），方法错误 405。

### 命令行批处理（不开服务）

```bash
./mvnw -q exec:java -Dexec.mainClass=com.example.wm.service.HttpService \
  -Dexec.args="run examples/01-pause-resume-no-regression.json out.json"
# 第二个参数可省（打印到 stdout）；文件名用 - 表示从 stdin 读
```

## 5. 请求样例

`examples/` 下四个样例，覆盖全部验收点；`examples/output/` 保存了实际运行的完整响应。

| 文件 | 场景 |
|---|---|
| `01-pause-resume-no-regression.json` | a 暂停→快分区 b 独自推进→a 恢复重放旧事件（RECOVERED_OLD，全局保持 4900 不倒退）→a 追上后重新参与 min |
| `02-idle-boundary.json` | t=99 仍 ACTIVE、t=100 恰好边界 IDLE；IDLE 释放 min；全空闲保留全局值；恢复旧事件迟到 |
| `03-extreme-fast-partition.json` | fast 冲到 10 万，全局被 slow 钉在 10；暂停 slow 后才顶到 100000 |
| `04-late-boundary.json` | B=10 时水位线 90；t=90 迟到（含等于）、t=91 准时 |

一键重跑全部样例：

```bash
scripts/run-examples.sh
```

## 6. 构建与运行

要求 **JDK 21+**。仓库自带 Maven Wrapper，无需预装 Maven：

```bash
./mvnw compile                 # 编译
./mvnw test                    # 运行全部测试
./mvnw -q package              # 打包（跳过不需要的步骤可加 -DskipTests）

# 启动 HTTP 服务（端口可选，默认 8080）
./mvnw -q exec:java -Dexec.mainClass=com.example.wm.service.HttpService -Dexec.args="8080"

# 调用
curl -s localhost:8080/health
curl -s -X POST localhost:8080/simulate -H 'Content-Type: application/json' \
  --data @examples/01-pause-resume-no-regression.json
```

## 7. 自动化测试

```
src/test/java/com/example/wm/
├── WatermarkCoordinatorAcceptanceTest.java  验收：暂停/恢复不倒退、恢复旧事件迟到、
│                                            极端快分区被慢分区钉住、全程全局单调
├── IdleDetectionBoundaryTest.java           空闲边界 99/100/109/110、超时即恢复、全空闲保留
├── LateAndBoundTest.java                    迟到含等于、B 的语义、迟到锚定全局而非本地
├── reference/DifferentialFuzzTest.java      ★ 302 条随机操作序列 vs 参考实现逐步全量比对
├── time/VirtualClockAndSchedulerTest.java   虚拟时钟不可倒退、手动调度时序、周期 tick 驱动空闲
├── time/SystemSchedulerTest.java            真实调度器冒烟（触发/取消）
├── json/JsonRoundTripTest.java              JSON 解析/序列化/畸形输入
└── service/SimulationServiceTest.java       JSON 端到端验收链路、确定性、错误处理
```

### 实测结果（本机实际执行）

- 环境：Ubuntu，OpenJDK 21.0.12.1，Apache Maven 3.9.9（通过 `./mvnw` 自动获取）。
- 命令：`./mvnw clean test`
- 结果：**Tests run: 329, Failures: 0, Errors:0, Skipped: 0 — BUILD SUCCESS**
  （其中差分测试 `DifferentialFuzzTest` 302 次：200 条随机脚本 + 100 条带 tick 的随机脚本 + 2 个固定种子）。
- HTTP 服务实测（端口 47831）：`GET /health` 200；`POST /simulate` 场景 1 返回最终全局水位线 6900、
  迟到事件 `(a, 1500, RECOVERED_OLD)`；非法 `boundMs` 与畸形 JSON 返回 400 JSON 错误体；
  `GET /simulate` 返回 405。
- CLI：四个样例经 `scripts/run-examples.sh` 全部成功，响应在 `examples/output/`。

开发过程中差分测试确实暴露并修复了若干实现缺陷（如实记录，最终版本均已通过）：

1. `pause/resume` 操作最初没有像事件/tick 那样先做空闲超时检测，已补齐；
2. “从未收过事件就被暂停”的分区第一条事件被误报为 `resumedFromIdle`，已修正为仅在
   此前收过事件时才算恢复；
3. 手写 JSON 解析器一度把所有整数误判为 `double`（指数判断写错），已修复并由往返测试覆盖。

无未通过项。

## 8. 设计取舍说明

- **空闲只退出 min、不删除进度**：分区恢复后用“恢复基线抬升”而非重置/丢弃历史数据，
  兼顾全局单调性与数据可见性（快照同时给本地/有效水位线）。
- **迟到事件仍更新 maxTs**：与“watermark = max(eventTime) − B”的流式定义一致；迟到只影响分类路由。
- **检测惰性触发**：空闲状态在 `tick`、事件注入、暂停、恢复时检测，不依赖后台线程也能正确；
  生产部署可注入 `SystemScheduler` 周期调用 `tick()`。
- **确定性优先**：服务端模拟完全由请求里的处理时间驱动，便于回放、审计与作为教学/回归工具。
