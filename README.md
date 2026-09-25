# streamagg — 流式纠错聚合（Streaming Retraction/Correction Aggregation）

纯后端 Java 项目：一个事件流计算库 + JSON HTTP 服务。按**事件 ID** 处理
**新增（ADD）/ 撤销（RETRACT）/ 更正（CORRECT）**，维护每个键（key）的
`sum`（值总和）与 `count`（存活事件数）。

- **零外部依赖**：只用 JDK（内置 `com.sun.net.httpserver.HttpServer`），JSON 解析/
  生成与测试框架均在仓库内手写，`javac` 直接构建，无需 Maven/Gradle、无需联网。
- **时间与调度可注入**：`Clock`、`Scheduler` 均为接口，生产用系统时钟/线程池调度，
  测试用 `ManualClock`/`ManualScheduler` 驱动虚拟时间，结果完全确定。
- **小数据精确参考实现**：从最终事件账本（resolved ledger）重放重算每键总和/计数，
  作为判定流式状态正确性的「神谕」（oracle）。

## 需求对照

| 需求 | 实现 |
| --- | --- |
| 按事件 ID 的新增、撤销、更正 | `StreamCorrectionEngine.ingest`，操作类型 `ADD/RETRACT/CORRECT` |
| 维护每键总和与计数 | `Aggregate { sum, count }`（`BigDecimal` 精确小数） |
| 更正乱序到达可缓存依赖 | 每事件 ID 的 1 起版本链：`v3` 先到则缓存，`v1/v2` 到齐后级联释放；无版本号时 RETRACT/CORRECT 先于 ADD 也会在队首缓存，等 ADD 释放 |
| 重复操作幂等 | 显式 `opId` 去重；无 `opId` 时按 (eventId, op, key, value) 结构指纹去重；重放返回 `DUPLICATE`，状态不变 |
| 禁止计数负漂移 | 聚合/参考重算中每次扣减后 `assertNoNegativeDrift()`，负值立即抛异常 |
| 先撤销后新增 / 多次更正 / 重放 | 单测与 HTTP 验收脚本覆盖，见下 |
| 与最终事件账本重算结果比较 | `verifyAgainstLedger()`（逐步）、`GET /v1/aggregates?verify=1`、`POST /v1/replay` |

## 目录结构

```
src/main/java/streamagg/
  time/       Clock/SystemClock/ManualClock、Scheduler/SystemScheduler/ManualScheduler
  json/       零依赖 JSON 解析器与写入器
  model/      IngestOp、Event、Aggregate、ResolvedOp、IngestResult、OutputSnapshot 等
  engine/     StreamCorrectionEngine（核心：缓存、幂等、账本、参考重算）
  server/     Codec、AggregateService、AggregateHttpServer
  Main.java   服务入口
src/test/java/streamagg/test/   自研微型测试框架 + 38 个测试用例
examples/     请求样例（curl --data @file 直接可用）
scripts/      build.sh / test.sh / demo.sh
docs/run-log.txt  最近一次真实端到端运行记录
```

## 构建与测试

需要 JDK 17+（开发与验证使用 JDK 21）。

```bash
scripts/build.sh          # javac 编译 main + test -> target/
scripts/test.sh           # 运行全部自动化测试（38 个）
```

启动服务：

```bash
java -cp target/classes streamagg.Main 8080   # 端口可省略，默认 8080
```

端到端验收（服务启动后，另开一个终端）：

```bash
scripts/demo.sh 8080       # 用 curl 顺序调用全部场景；输出见 docs/run-log.txt
```

## 语义说明

### 操作（operation）

```jsonc
{
  "opId": "evt-1",          // 可选：幂等令牌，全局去重
  "eventId": "order-100",   // 必填：事件 ID
  "op": "ADD",              // ADD | RETRACT | CORRECT
  "key": "books",           // ADD 必填；CORRECT 可改键；RETRACT 忽略
  "value": "29.90",         // ADD 必填（数字或数字字符串）；CORRECT 可改值
  "version": 1,             // 可选：每事件 ID 从 1 起的版本号
  "eventTime": "2026-09-23T10:00:00Z"  // 可选：epoch 毫秒或 ISO-8601
}
```

### 乱序与依赖缓存

- **带版本号**：每个事件 ID 必须按 1,2,3,… 解析。`v3` 先到 -> `BUFFERED`；
  当 `v2` 到达时若 `v1` 已在，则 `v2` 与缓存的 `v3` 在同一次调用中级联 `drained`。
- **无版本号**：按到达顺序解析；RETRACT/CORRECT 先于对应 ADD 到达时缓存在链首，
  ADD 到达后插到最前并依次释放（即「先撤销后新增」：ADD 立刻又被自己的撤销抵消，
  净聚合为零）。
- 同一版本重复且内容一致 -> `DUPLICATE`；内容不同 -> `CONFLICT`。
- `v2 RETRACT` 终止链之后，更高版本的操作 -> `CONFLICT`（不会被永久挂起）；
  重新 `ADD` 同一事件 ID 会开启一条新链。

### 幂等

- 显式 `opId`：同一令牌永远只生效一次，无论重放多少次、顺序如何。
- 无 `opId`：结构指纹去重（同 eventId + 同操作 + 同键值的重复 ADD/RETRACT 折叠）。

### 事件账本与参考重算

每次**真正解析**（而非缓存）的生命周期变更都以 `ResolvedOp` 追加到只追加账本，
记录旧键/旧值与新键/新值。`recomputeFromLedger` 从空状态重放该账本独立重算，
是流式增量状态的对照基准；两者逐键比较，任何差异都会被报告。

## HTTP 接口

| 方法与路径 | 说明 |
| --- | --- |
| `POST /v1/events/ingest` | 摄入单条操作，返回 `APPLIED/BUFFERED/DUPLICATE/CONFLICT/INVALID/LATE` |
| `POST /v1/events/batch` | 按数组顺序摄入一批，返回逐条状态与汇总计数 |
| `GET  /v1/aggregates?verify=1` | 每键 sum/count；`verify=1` 附带账本重算校验结果 |
| `GET  /v1/events` | 当前存活事件 + 仍在缓存等待依赖的操作 |
| `GET  /v1/ledger?limit=N` | 已解析事件账本（全局解析顺序） |
| `POST /v1/replay` | body 带 `operations`：把一批原始操作灌入全新引擎做 what-if 重放；body 为 `{}`：直接对当前账本做精确重算。均返回与重算结果的逐键比对 |
| `POST /v1/emit` | 显式产出一次输出快照 |
| `POST /v1/admin/reset` | 清空全部状态 |
| `GET  /health` | 存活检查 |

### 快速试一下

```bash
# 撤销先于新增（无版本号）：先缓存，ADD 到达后 ADD+RETRACT 一起释放
curl -s -X POST localhost:8080/v1/events/ingest -H 'Content-Type: application/json' \
  -d '{"eventId":"o1","op":"RETRACT","opId":"u1"}'
curl -s -X POST localhost:8080/v1/events/ingest -H 'Content-Type: application/json' \
  -d '{"eventId":"o1","op":"ADD","key":"books","value":"29.90","opId":"a1"}'

# 结果
curl -s 'localhost:8080/v1/aggregates?verify=1'
```

更多样例见 [`examples/`](examples/)（每个文件一个可直接发送的 JSON body）。

## 时间与调度注入

```java
var clock = new ManualClock();
var scheduler = new ManualScheduler(clock);
var engine = StreamCorrectionEngine.builder().clock(clock).build();
scheduler.scheduleAtFixedRate(Duration.ofSeconds(5), Duration.ofSeconds(5),
        engine::emitSnapshot);
engine.ingest(...);
scheduler.advance(Duration.ofSeconds(20)); // 虚拟时间推进，触发 4 次快照，无真实等待
```

## 验收与最近一次运行

- 自动化测试：**38/38 通过**（JSON、引擎、时间/调度、HTTP 端到端）。
- 端到端验收脚本：16 个场景全部成功（先撤销后新增、版本化乱序、多次更正、
  键迁移、重放幂等、冲突拒绝、账本重算一致）。
- 实际执行的命令与原始输出保存在 [`docs/run-log.txt`](docs/run-log.txt)。
