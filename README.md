# 动态规则版本绑定（DRVB）— 纯后端事件流计算服务

一个**零外部依赖**的 Java 后端：事件过滤规则支持热更新；规则按**事件时间**
绑定到不可变版本；乱序 / 晚到事件使用其对应时间的历史版本；事件时间没有任何
版本覆盖时**明确拒绝，绝不静默套用最新规则**；历史版本只有在满足"回收前提"
后才能被回收，回收后落到该区间的晚到事件同样被明确拒绝。

- 语言/运行时：Java 21（仅用 JDK 标准库，无需 Maven/Gradle/任何第三方 jar）
- HTTP：JDK 内置 `com.sun.net.httpserver.HttpServer`
- JSON：自带极简解析/序列化（`drvb.json`）
- 时间与调度：**可注入**。`manual` 模式（默认，时间静止、由 `/admin/tick` 驱动，
  确定性可测）或 `wall` 模式（系统墙钟 + 单线程调度器）
- 无外部消息系统、无数据库、无前端

---

## 1. 快速开始

```bash
# 编译（需要 javac，JDK 17+；开发与验证使用 JDK 21）
./build.sh

# 运行全部自动化测试（29 个用例）
./test.sh

# 启动服务（默认 manual 时钟，端口由 --port 指定；0 = 系统分配空闲端口）
./run.sh --port=8080 --clock=manual --allowed-lateness=5000 --retention-horizon=60000

# 另一个终端：一键端到端演示（真实起服务 + curl，日志写入 docs/demo-output.log）
./demo.sh
```

手动调用示例（请求体见 [`examples/`](examples)）：

```bash
curl -s localhost:8080/health
curl -s -X POST localhost:8080/rules/publish \
  -H 'Content-Type: application/json' -d @examples/publish-v1.json
curl -s -X POST localhost:8080/events \
  -H 'Content-Type: application/json' -d @examples/event-single.json
```

启动参数：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--port` | `8080` | 监听端口，`0` 表示系统分配 |
| `--clock` | `manual` | `manual`（时间可注入）/ `wall`（系统时间） |
| `--start-time` | `1000000` | manual 时钟起始处理时间（毫秒） |
| `--allowed-lateness` | `5000` | 水位线容忍乱序时长 L；水位线 = 最大事件时间 − L |
| `--retention-horizon` | `60000` | 历史版本额外保留视窗 H；回收闸门 = 水位线 − H |
| `--auto-reclaim-ms` | `0`（关） | >0 时注册周期性自动回收任务 |

---

## 2. 核心语义（验收口径）

### 2.1 版本不可变 + 按事件时间绑定生效区间

规则版本（`RuleVersion`）一经发布：谓词树、JSON 规约、创建时间均不可修改
（发布时深拷贝 + 编译，外部再改入参不影响已发布版本）。

"改规则"从不修改旧版本，而是**发布新版本并追加一条生效绑定**。绑定按事件时间
构成**左闭右开**区间：

```
绑定 b[i] 对事件时间 t 生效  ⇔  b[i].from ≤ t < b[i+1].from
最后一条绑定向右无限延伸
```

事件按自身携带的 `eventTime` 解析版本，与到达（处理）时间无关——因此乱序、
晚到事件天然命中对应历史版本。新绑定的 `effectiveFrom` 必须**严格大于**当前
最大边界，历史区间永远不可被重开（返回 `409 BAD_EFFECTIVE_FROM`）。

### 2.2 拒绝静默套用最新规则

- 事件时间早于第一条绑定（没有任何版本覆盖该时间）：`status=REJECTED`，
  `rejectReason=MISSING_RULE_VERSION`。**不会**回退去用最新版本。
- 版本表为空时，一切事件都以同一原因拒绝。

拒绝是**业务结果**：HTTP 状态码仍为 200，逐条结果中带状态；批量接口给出
`rejectedCount`。入参/规约错误才是 4xx（见错误码表）。

### 2.3 热更新与回滚

- `POST /rules/publish`：发布新版本 + 新生效边界。
- `POST /rules/rollback`：让**已存在**版本从新边界起重新生效。回滚 = 追加一条
  `operation=ROLLBACK` 的绑定，旧绑定与历史区间全部保留（可审计）。回滚到当前
  已生效版本返回 `409 ALREADY_CURRENT`；回滚目标不存在/已回收返回
  `409 VERSION_NOT_FOUND`。

### 2.4 水位线与晚到事件

`watermark = maxObservedEventTime − allowedLateness`，只随成功解析出版本的
事件推进、绝不倒退。`eventTime < watermark` 的事件标记 `late=true`，但**照常
处理**，并使用其事件时间对应的历史版本（晚到不丢弃、不改判）。

### 2.5 历史版本回收前提（GC precondition）

一个**非当前**存活版本 v 只有同时满足以下条件才允许回收：

1. v 的所有生效区间都已闭合（不存在向右无限的区间）；
2. v 的最晚区间结束时刻 `endMax ≤ 水位线 − retentionHorizon`（回收闸门）。

含义：在"还可能有需要 v 的晚到事件到来"之前不得回收。边界用 `≤` 精确判定。
前提不满足时，`POST /reclaim {"mode":"one",...}` 返回
`409 RECLAIM_NOT_ELIGIBLE`；当前版本永远不可回收（条件 1 恒不满足）。

回收后：规则体被删除、版本成为**墓碑**（绑定记录保留用于审计）。此后解析到该
区间的事件返回 `status=REJECTED, rejectReason=RECLAIMED_RULE_VERSION`，
而不是悄悄改用最新规则。

`mode=eligible` 扫描并回收所有满足前提的版本；可注册成周期任务
（`--auto-reclaim-ms`，或服务内 `enableAutoReclaim`），manual 模式下在
`/admin/tick` 时确定性补跑。

---

## 3. HTTP API

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| GET | `/health` | 健康检查、时钟模式、当前处理时间与水位线 |
| GET | `/state` | 全量只读状态：版本、墓碑、绑定区间、回收资格、最近结果 |
| POST | `/rules/publish` | 发布不可变新版本并绑定生效边界 |
| POST | `/rules/rollback` | 追加回滚绑定（指向已存在版本） |
| POST | `/events` | 提交单条事件（对象）或批量事件（数组） |
| GET | `/results?status=&version=&limit=` | 查询处理结果 |
| POST | `/reclaim` | `mode=check/one/eligible` 回收前提检查 / 回收 |
| POST | `/admin/tick` | 仅 manual：`advanceTo`/`advanceBy` 推进时间并补跑周期任务 |
| GET | `/admin/scheduler` | 周期任务状态（名称、周期、运行次数） |

### 事件

```json
{
  "eventId": "evt-1",          // 必填
  "eventTime": 2500,           // 必填，业务/事件时间（毫秒），决定版本
  "type": "payment",           // 可选
  "payload": { "amount": 150 } // 可选，谓词在此求值；另注入 eventId/eventTime/type
}
```

单条响应（批量时外层为 `{count, rejectedCount, results:[...]}`）：

```json
{
  "watermark": 2400,
  "result": {
    "eventId": "late-v1", "eventTime": 500, "processingTime": 90000,
    "watermarkBefore": 2400, "watermarkAfter": 2400, "late": true,
    "status": "MATCHED",            // MATCHED | FILTERED_OUT | REJECTED
    "matched": true,
    "ruleVersionId": "v1", "bindingSeq": 1
  }
}
```

### 谓词规约

叶子：`eq/ne/gt/gte/lt/lte`、`in/notIn`、`contains/startsWith/endsWith`、
`isnull`；组合：`and/or/not`；常量 `true/false`。字段支持点分嵌套路径
（`user.tier`）。比较类算子遇字段缺失/null/类型不符时为 `false`（不抛异常）；
非法规约在发布时即被拒绝（`400 INVALID_RULE`），不会产生半成品版本。完整示例见
[`examples/publish-v3-compound.json`](examples/publish-v3-compound.json)。

### 错误码

| HTTP | error | 触发场景 |
| --- | --- | --- |
| 400 | `INVALID_JSON` / `BAD_REQUEST` / `INVALID_RULE` | 请求体或谓词规约非法 |
| 409 | `BAD_EFFECTIVE_FROM` | 新生效边界未严格大于当前最大边界 |
| 409 | `VERSION_EXISTS` / `VERSION_NOT_FOUND` | 版本 id 重复 / 回滚目标不存在或已回收 |
| 409 | `ALREADY_CURRENT` | 回滚目标已是当前版本 |
| 409 | `RECLAIM_NOT_ELIGIBLE` | 不满足历史版本回收前提 |
| 404 | `NOT_FOUND` | 路由不存在 |

业务拒绝（`MISSING_RULE_VERSION` / `RECLAIMED_RULE_VERSION`）不出现在这里——
它们是 200 响应里的逐事件 `status`，由调用方按结果处理。

---

## 4. 代码结构

```
src/drvb/
├── json/        零依赖 JSON 解析/序列化
├── time/        Clock（Wall/Manual）、Scheduler（Executor/Manual）
├── model/       Event、ProcessResult、RejectReason
├── rule/        Predicate 接口、Predicates 参考求值器（小数据、精确）
├── version/     RuleVersion、Binding、RuleVersionTable（区间绑定）、
│                Watermark、ReclamationService（回收前提）
├── stream/      EventProcessor（逐条精确的事件流参考算子）
├── service/     DynamicRuleService（用例门面，聚合上述组件）
├── http/        RuleHttpServer + DTO 映射（JSON in/out）
├── Main.java    入口（参数解析、组装注入、启动）
└── tests/       自研测试运行器 + 29 个测试用例
```

设计取舍与时间/晚到/回收的形式化说明见 [`docs/DESIGN.md`](docs/DESIGN.md)。

---

## 5. 验收场景如何对应到测试

| 验收要求 | 自动化测试 |
| --- | --- |
| 交错规则更新 × 乱序事件，晚到用历史版本 | `AcceptanceScenarioTest.interleavedUpdatesAndOutOfOrderEventsUseHistoricalVersions` |
| 边界时刻（左闭右开） | `RuleVersionTableTest.boundaryTimestampsResolveExactInterval`、`AcceptanceScenarioTest`（1000/3000 边界） |
| 回滚版本且历史区间不变 | `RuleVersionTableTest.rollbackAppendsBindingAndKeepsHistoryImmutable` |
| 缺失版本拒绝、不套用最新 | `...missingVersionIsRejectedNeverSilentlyLatest`、`RuleVersionTableTest.eventBeforeFirstBinding...` |
| 历史版本回收前提（闸门、边界、当前版本、回滚后多区间） | `ReclamationTest`（5 例） |
| 回收后晚到事件明确拒绝 | `...reclaimedVersionIsTombstone...`、`AcceptanceScenarioTest.rollbackAndGarbageCollectionPreconditions` |
| 版本不可变 | `...publishedVersionsAreImmutableObjects` |
| 时间/调度可注入 | `TimeTest`（ManualClock/ManualScheduler 确定性补跑） |
| JSON in/out 全链路 | `HttpApiTest`（真实 HTTP 回环，含 400/409 错误码与 tick 驱动自动回收） |

实际运行命令、结果与未通过项的如实记录见 [`docs/RUNLOG.md`](docs/RUNLOG.md)。
