# 乱序去重与墓碑（Out-of-Order Dedup & Tombstones）

纯后端的事件流去重计算库 + JSON HTTP 服务。**零外部依赖、零外部消息系统**：
仅用 JDK（内置 `HttpServer`），手写 JSON 解析与测试框架。时间与调度均可注入，
提供小数据量的精确参考实现，并以差分测试与随机化不变量测试核对承诺范围内无重复输出。

- 语言/运行时：Java（JDK 17+ 语法，开发与验证使用 **OpenJDK 21**）
- 构建：`javac`/`jar` 直构，无 Maven/Gradle
- 前端：无（按要求不做）

---

## 1. 它解决什么问题

事件流常出现：**乱序到达、重复投递、机器时钟回退、消费者重启、迟到很久的旧副本**。
系统需要用**有界内存**判断“这个事件以前见过吗”，并诚实地说出“在什么范围内我能保证去重”。

核心设计选择：

1. **事件 ID 与事件时间分离**。身份是 `(key, id)`；`eventTime` 是事件自带的业务时间，
   去重判定只基于事件时间与水位线，不依赖处理机墙钟（墙钟可回退）。
2. **墓碑（Tombstone）** 记录某身份首次被接受时的事件时间与载荷指纹；水位线推进后
   按时间窗口释放，保证状态有界。
3. **承诺范围（dedup guarantee window）**：
   `eventTime >= watermark − allowedLateness` 且未被硬上限强制淘汰覆盖。
   范围内的重复**保证抑制**；范围外的极迟重复**不做承诺**——事件透传，但带显式标志
   `dedupGuaranteed=false` 与判定 `UNGUARANTEED`，下游可观察、可分流、可审计。
4. **双保险有界状态**：时间窗口淘汰 + 墓碑数量硬上限（超限强制淘汰最旧墓碑，并抬高
   `forcedEvictionHorizon`，把受影响时间区间也诚实标记为不可保证）。
5. **时钟/调度可注入**：`Clock`（系统/手动）与 `TaskScheduler`（系统线程/确定性调度器）
   都是接口，测试不依赖真实线程与墙钟。
6. **重启恢复**：快照（墓碑、水位线、计数器、强制淘汰下界、序列号）原子落盘，
   新进程从同一快照文件恢复，承诺连续。

---

## 2. 快速开始

```bash
./build.sh          # 编译到 build/，打包 build/dedup.jar（只需 JDK）
./test.sh           # 运行全部自动化测试（21 个用例）
./run.sh 8080       # 启动服务（默认 manual 水位线，快照在 state/snapshot.json）
```

另开终端：

```bash
# 首次事件 -> ACCEPT
curl -sS -X POST localhost:8080/events -H 'Content-Type: application/json' \
  -d '{"id":"e1","eventTime":10000,"payload":{"v":1}}'

# 同 ID 不同载荷 -> SUPPRESS 且 payloadMismatch=true
curl -sS -X POST localhost:8080/events -H 'Content-Type: application/json' \
  -d '{"id":"e1","eventTime":10000,"payload":{"v":2}}'

# 推进水位线
curl -sS -X POST localhost:8080/watermark -H 'Content-Type: application/json' \
  -d '{"watermark":15001}'

# 极迟重复 -> UNGUARANTEED + dedupGuaranteed=false（透传，不静默去重也不静默重复）
curl -sS -X POST localhost:8080/events -H 'Content-Type: application/json' \
  -d '{"id":"e1","eventTime":10000,"payload":{"v":1}}'

curl -sS localhost:8080/stats
```

一键端到端脚本（起停服务、重启恢复、全部判定现场可见）：

```bash
PORT=19091 ./examples/demo.sh          # 完整输出另存于 docs/demo-output.txt
./examples/curl-examples.sh            # 各端点 curl 速查
```

> 若 8080 被占用，用 `--port` 或 `PORT=xxxx` 指定其他端口。

---

## 3. HTTP API

所有请求/响应均为 JSON。监听 `127.0.0.1`。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/events` | 发送单事件 `{id,key?,eventTime,payload?}` |
| POST | `/events/batch` | 发送一批 `{events:[...]}`，按顺序处理 |
| POST | `/watermark` | manual 模式推进水位线 `{watermark}`；回退返回 `advanced:false` |
| POST | `/tick` | bounded 模式触发一次周期水位线推进 |
| GET  | `/outputs?sinceSeq=N&drain=bool` | 读取已发射（ACCEPT/UNGUARANTEED）事件 |
| GET  | `/stats` | 可观察计数与当前水位线、状态规模 |
| POST | `/checkpoint` | 立即快照落盘 |
| POST | `/admin/reset` | 清空内存与快照 |
| GET  | `/health` | 健康检查 |

`eventTime` 支持整数 epoch 毫秒，或 ISO-8601 字符串（如 `2026-09-23T10:00:00Z`）。

### 判定结果（每条事件返回，也出现在 `/outputs` 中）

```json
{
  "result": {
    "decision": "SUPPRESS",          // ACCEPT | SUPPRESS | UNGUARANTEED
    "emitted": false,                // 是否向下游发射（SUPPRESS 不发射）
    "dedupGuaranteed": true,         // 关键可观察标志：是否在承诺范围内
    "payloadMismatch": true,         // 同 ID 但载荷与首次副本不同
    "key": "user-a", "id": "e1", "eventTime": 10000,
    "watermark": 12000,
    "reason": "承诺范围内重复，且载荷与首次副本不同"
  }
}
```

三种判定：

- **ACCEPT**：承诺范围内首次见到 → 写墓碑并发射，`dedupGuaranteed=true`。
- **SUPPRESS**：承诺范围内识别出重复 → 不发射，`dedupGuaranteed=true`；
  若载荷指纹不同，`payloadMismatch=true`（身份以 ID 为准，载荷差异只做标记）。
- **UNGUARANTEED**：墓碑已按水位线释放、或被硬上限淘汰覆盖 → **无法保证**去重，
  事件**透传发射**，`dedupGuaranteed=false`。系统不会假装能去重它。

### 启动参数

```
--port N                  监听端口（默认 8080）
--allowed-lateness-ms N   允许迟到窗口（默认 5000）
--max-tombstones N        墓碑硬上限（默认 100000）
--watermark-mode MODE     manual（默认，POST /watermark）| bounded（Flink 风格 BoundedOutOfOrderness）
--out-of-orderness-ms N   bounded 模式的最大乱序间隔（默认 2000）
--tick-period-ms N        bounded 模式自动 tick 周期（默认 1000）
--state-file PATH         快照文件路径（默认 state/snapshot.json）
```

---

## 4. 语义与边界（重要）

给定水位线 `W` 与迟到窗口 `L`，承诺下界 `H = W − L`：

- 事件 `e` 满足 `e.eventTime ≥ H` 且其墓碑存活（锚点 `≥ H`、未被强制淘汰）→
  身份重复**保证**被抑制。
- 墓碑锚点取该身份“**最后一次在承诺窗口内被见到**”的 eventTime：窗口内出现更晚的
  乱序副本会把墓碑寿命顺延，避免“刚刚还在窗口内见过、转瞬间墓碑却被释放”的漏去重。
- `e.eventTime < H`（极迟）或其身份被硬上限强制淘汰 → `UNGUARANTEED`。
  这是**有意的设计边界**：有界状态不可能无限记住所有历史 ID。系统把边界做成
  **可观察的显式信号**，而不是静默地产生重复输出。
- 水位线单调不减：时钟回退（处理机 NTP 回跳、手动提交更小水位线、bounded 模式下
  旧事件时间）都不会拉低水位线。

### “同 ID 不同载荷”如何处理

ID 是身份。同一 `(key,id)` 的副本即使载荷不同也按重复抑制；同时通过载荷的规范化
SHA-256 指纹（对象键排序、数字去尾零，故 `{"a":1,"b":2}` 与 `{"b":2,"a":1.0}` 同指纹）
检测差异并置 `payloadMismatch=true`，便于发现上游的版本冲突/重试改写问题。

---

## 5. 重启恢复

- 每次水位线前进、以及收到 `SIGTERM` 优雅关闭时自动 `checkpoint`；也可 POST `/checkpoint`。
- 快照先写 `snapshot.json.tmp` 再**原子 rename** 覆盖 `snapshot.json`，不会产生半截文件。
- 快照包含：全部存活墓碑（含锚点、序列号、载荷指纹）、水位线、强制淘汰下界、
  bounded 模式内部最大事件时间、全部计数器。
- 新进程启动时若快照存在则自动恢复；恢复后窗口内已知 ID 的副本继续被抑制，
  计数在历史值上递增。

---

## 6. 项目结构

```
src/main/java/dedup/
  json/Json.java                 零依赖 JSON 解析/序列化/规范化指纹
  core/
    Event.java                   事件（id 与 eventTime 分离）
    CompositeKey.java            (key,id) 复合身份
    Tombstone.java               墓碑（锚点 eventTime、seq、载荷指纹）
    DedupResult.java             三态判定 + 可观察标志
    Deduplicator.java            算子接口
    BoundedTombstoneDeduplicator.java  有界墓碑生产算子
    ReferenceDeduplicator.java   小数据精确参考实现（无硬上限，语义对齐，供差分）
    Stats.java                   计数器
  watermark/
    WatermarkGenerator.java
    ManualWatermarkGenerator.java
    BoundedOutOfOrdernessWatermarks.java
  time/
    Clock.java                   系统时钟 / 手动时钟
    TaskScheduler.java
    SystemTaskScheduler.java     守护线程调度
    DeterministicScheduler.java  确定性调度（测试，无真实线程）
  state/
    SnapshotStore.java / FileSnapshotStore / InMemorySnapshotStore
    SnapshotData.java            快照 JSON 结构与恢复
  stream/
    StreamProcessor.java         编排：水位线 × 去重 × 输出缓冲 × 快照
    OutputEntry.java
  server/DedupHttpServer.java    JDK HttpServer JSON 服务
src/test/java/dedup/tests/       21 个自动化用例（迷你测试框架，零依赖）
examples/                        请求样例与 demo/curl 脚本
docs/                            测试与演示的真实运行记录
```

---

## 7. 测试

`./test.sh`（构建后运行 `dedup.tests.AllTests`，退出码 0/1）。覆盖：

- **验收 1 同 ID 不同载荷**：以 ID 抑制、`payloadMismatch`、规范化指纹、复合键隔离。
- **验收 2 时钟回退**：bounded 模式周期触发不产生倒退水位线；manual 模式拒绝回退；
  回退期间窗口内重复仍抑制。
- **验收 3 重启恢复**：内存存储与真实文件存储两条路径；墓碑/水位线/计数延续；
  bounded 内部最大值恢复；原子落盘无 `.tmp` 残留。
- **验收 4 极迟重复**：窗口边界逐点核对、墓碑释放计数、`UNGUARANTEED` 透传与标志、
  硬上限强制淘汰同路径。
- **差分测试**：300 条随机乱序流（约 1.9 万事件）同时喂给生产算子与参考实现，
  在未触硬上限时判定逐条一致。
- **不变量测试**：同一承诺窗口内已接受 ID 的后续副本必被抑制；`SUPPRESS` 必在承诺内；
  `UNGUARANTEED` 必带 `dedupGuaranteed=false`。
- **端到端**：真实起停 HTTP 服务（随机端口 + 文件快照），覆盖关闭→重启恢复全链路。

真实运行记录见 [`docs/test-output.txt`](docs/test-output.txt) 与
[`docs/demo-output.txt`](docs/demo-output.txt)。

---

## 8. 非目标与局限（如实说明）

- 单机、单实例、串行处理；没有 Kafka/Pulsar 等外部消息系统，也没有分布式副本/分片。
- 参考实现刻意无硬容量上限，仅供小数据量验收，不应直接用于无界流量。
- 硬上限触发后的去重是“尽力而为 + 显式标记”，不是精确一次。
- `/outputs` 是有界内存缓冲（默认 10000，溢出丢弃最旧并计数 `droppedOutputs`），
  不是持久投递队列；它用于观察/审计，承诺语义以每条事件返回的判定为准。
