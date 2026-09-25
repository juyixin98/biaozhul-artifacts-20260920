# streamagg — 流式纠错聚合（纯后端）

按事件 ID 处理 **新增（ADD）/ 撤销（RETRACT）/ 更正（CORRECT）** 的事件流计算库，
维护每个键的 **总和（sum）与计数（count）**。支持乱序更正的依赖缓存、重复操作幂等、
禁止计数负漂移，并提供“最终事件账本重算”作为小数据精确参考实现。附带零依赖的
JSON 输入输出 HTTP 服务。纯后端，无前端，无外部消息系统。

## 特性与语义

| 需求 | 实现 |
|---|---|
| 按事件 ID 新增/撤销/更正 | `EventOp` + `StreamProcessor.submit()`；同一事件 ID 贯穿生命周期 |
| 每键总和与计数 | `KeyStats{count, sum}`，`BigDecimal` 精确十进制，无浮点误差 |
| 更正乱序到达可缓存依赖 | 版本号从 1 起；出现版本空洞时操作入 `pending` 缓存，前置版本补齐后**级联排空** |
| 先撤销后新增 | 无版本时引擎合成版本（RETRACT 自动占 v2、保留 v1 给未来的 ADD）；显式版本时同样缓存等待 |
| 重复操作幂等 | 两层：① `opId` 全局去重（客户端重试）；② 同事件同版本且内容相同判重。重复**不写日志、不改状态** |
| 陈旧消息冲突拒绝 | 同事件同版本但内容不同 -> `STALE_CONFLICT`，不应用 |
| 禁止计数负漂移 | 撤销不存在/未到达/已撤销事件均为**空操作**；计数一旦会低于 0 立即抛 `IllegalStateException` |
| 最终事件账本重算 | `StreamProcessor.recomputeFromLedger(ledger)`：独立于增量状态的参考实现；`reconcile()` 逐键比对 |
| 重放 | `replay()`：把原始输入日志在全新引擎上按序回放，比较聚合与账本是否一致 |
| 时间与调度可注入 | `Clock`/`Scheduler` 接口；生产用 `SystemClock`/`RealScheduler`，测试用 `ManualClock`/`ManualScheduler`（确定性） |
| JSON 输入输出 | JDK 内置 `com.sun.net.httpserver.HttpServer`；JSON 解析/序列化为手写，**整个项目零第三方依赖** |

### 版本规则

- 显式 `version`：从 1 开始严格递增，按版本定义事件顺序，与到达顺序无关。
- 不带 `version`：引擎按摄入顺序赋予合成版本。ADD 取当前最小未占版本；
  RETRACT/CORRECT 若事件生命周期尚未开始则从 v2 起占位（为将来的 ADD 保留 v1）。
- 对已存活事件再次 ADD 按 upsert（更正值/键）处理，计数不重复增加。
- CORRECT 可携带新 `key` 实现改键迁移（旧键减、新键增）；不带 key 则继承原键。
- 对已撤销事件的 CORRECT 视为按新值重新计入。

## 目录结构

```
src/streamagg/
  core/                 事件流计算库（可独立使用，不依赖 service/json）
    Clock.java, SystemClock.java, ManualClock.java
    Scheduler.java, RealScheduler.java, ManualScheduler.java, Cancellable.java
    OpType.java, EventOp.java, KeyStats.java, ApplyResult.java
    LedgerEntry.java, JournalEntry.java, ReconciliationReport.java
    StreamProcessor.java        # 核心引擎
  json/                 零依赖 JSON 解析器/序列化器
  service/              HTTP JSON 服务（HttpService）与入口（Main）
test/streamagg/tests/   31 个自动化测试（零依赖迷你测试框架）
samples/                请求样例 JSON
build.sh / test.sh / run.sh / demo.sh
```

## 环境要求

- JDK 17+（开发与验证使用 OpenJDK 21）；无需 Maven/Gradle，无需联网下载依赖。

## 构建 / 测试 / 运行

```bash
./build.sh       # 编译 src 与 test 到 build/
./test.sh        # 构建并运行全部 5 个测试套件
./run.sh [port] [reconcilePeriodMillis]   # 启动 HTTP 服务（默认 8080）
./demo.sh [port]                          # 自启服务，curl 演练全部验收场景后自动关闭
```

## HTTP 接口

| 方法/路径 | 说明 |
|---|---|
| `GET /health` | 健康检查，返回注入时钟的当前时间 |
| `POST /events` | 提交单条操作，见下 |
| `POST /events/batch` | 提交操作数组，逐条返回结果（单条错误不影响其余） |
| `GET /stats` | 全部键聚合；`?key=k` 查单键 |
| `GET /ledger` | 最终事件账本（仅存活事件） |
| `GET /journal` | 原始输入日志（提交版本、引擎规范版本、接收时间） |
| `GET /pending?eventId=e` | 某事件当前被乱序缓存的操作 |
| `POST /reconcile` | 增量状态 vs 账本重算，逐键对账 |
| `POST /replay` | 日志重放到全新引擎并与当前状态比较 |
| `POST /reset` | 清空全部状态 |

请求体字段：

```json
{
  "eventId": "evt-1001",   // 必填，事件 ID
  "op": "ADD",             // 必填：ADD | RETRACT | CORRECT
  "key": "product-42",     // ADD 必填；CORRECT 可填（改键）；RETRACT 不填
  "value": 19.99,          // ADD/CORRECT 必填（精确十进制）；RETRACT 必须为空
  "version": 1,            // 可选；省略时由引擎合成
  "opId": "uuid"           // 可选；携带后同 opId 重发整体幂等
}
```

返回中的提交状态：`APPLIED`（已应用，可能伴随缓存级联）、`BUFFERED`（乱序已缓存）、
`DUPLICATE`（重复，幂等忽略）、`STALE_CONFLICT`（同版本冲突，拒绝）。

### 快速体验

```bash
./run.sh 8080 &
curl -s localhost:8080/health
curl -s -X POST localhost:8080/events -H 'Content-Type: application/json' \
  -d @samples/01-add.json
curl -s -X POST localhost:8080/events -H 'Content-Type: application/json' \
  -d @samples/02-correct.json
curl -s -X POST localhost:8080/events -H 'Content-Type: application/json' \
  -d @samples/03-retract.json
curl -s -X POST localhost:8080/reconcile -d '{}'
```

样例文件：`samples/01-add.json`、`02-correct.json`、`03-retract.json`、
`04-batch.json`、`05/06`（先撤销后新增）。

## 作为库直接使用

```java
Clock clock = new ManualClock(0L);                       // 时间可注入
Scheduler scheduler = new ManualScheduler();            // 调度可注入
var engine = new StreamProcessor(clock, scheduler, 100); // 100ms 周期对账

engine.submit(EventOp.retract("e1", 2L, null));         // BUFFERED（v1 缺失）
engine.submit(EventOp.add("e1", "k", new BigDecimal("10"), 1L, null)); // 级联撤销
engine.submit(EventOp.add("e1", "k", new BigDecimal("30"), 3L, null)); // 重新计入
engine.submit(EventOp.correct("e1", "k", new BigDecimal("31"), 4L, "c-4"));

KeyStats s = engine.statsOf("k");                        // count=1, sum=31
ReconciliationReport r = engine.reconcile();             // r.consistent() == true
r = engine.replay();                                     // 日志重放同样一致
```

## 测试

| 套件 | 内容 |
|---|---|
| `EngineTest` (15) | 先撤销后新增（显式/合成版本）、多次更正、乱序缓存级联、opId 与同版本幂等、冲突拒绝、防负漂移、改键、撤销后复活、BigDecimal 精度、对账与重放 |
| `PropertyTest` (2) | **200 个随机种子**生成含版本跳跃/重发/撤销先行的事件流，每一步都用独立参考账本重算比对；结束后补齐版本空洞并再做对账+重放。另有 50 次同 opId 重发只计一次的测试 |
| `ClockSchedulerTest` (3) | 手动时钟、日志时间戳来源、手动调度器确定性触发周期对账 |
| `JsonTest` (4) | BigDecimal 精度、转义/中文往返、非法 JSON 拒绝、pretty 往返 |
| `ServiceTest` (7) | 真实 HTTP 端到端：健康检查、400 校验、验收全场景、批量、对账/重放、重置、404 |

## 实际运行记录（2026-09-23，OpenJDK 21，Linux x86_64）

以下为本机真实执行的命令与结果（非推断）。

1. `./build.sh` —— 编译通过（修复了开发过程中的 3 个编译问题：`Map.merge` 方法引用签名、
   测试 helper 的 `long/Long` 装箱、测试框架对受检异常的支持）。
2. `./test.sh` —— **5 个套件全部通过：31/31**。
   首次运行曾有 3 个失败，均已定位并修复，复测全绿：
   - 属性测试 1 例失败：原因是**测试参考侧 harness 的记账 bug**（用级联返回的“最后应用
     版本”代替操作自身版本作 key，补洞时漏记参考账本），引擎本身在每一步比对中均正确；
     修复参考侧后通过；
   - `JsonTest` 2 例失败：测试断言笔误（数组元素数 4 误写 3）与错误信息文案期望不符；修正后通过。
3. `./run.sh 18099` + `curl` 真实演练：先撤销（`BUFFERED`）后新增（级联后聚合为 0）、
   opId 重复提交（`DUPLICATE`）、新增/更正/撤销、批量改键迁移、乱序 v3/v2 更正级联到最终
   值 3、撤销未知事件计数不为负、非法输入返回 400、`/reconcile` 与 `/replay` 均
   `consistent: true`。
4. `./demo.sh 18077` —— 脚本化端到端演练全部输出符合预期。

**未通过项：无。** 当前工作树下所有自动化测试与人工 curl 演练均通过；无遗留 TODO
影响验收（服务为内存态，重启即清空，这是有意的范围裁剪）。

## 设计取舍

- **内存态、单引擎单线程语义**：所有公开方法加锁，线程安全；不引入存储与外部消息系统，
  贴合“小数据精确参考实现”的定位。
- **BigDecimal 全程精确**：JSON 数字直接解析为 BigDecimal，输出 `toPlainString()`。
- **对账是独立第二实现**：账本重算不复用增量更新代码路径，避免“用同一套逻辑自证”。
- **周期对账可注入调度**：默认关闭（`POST /reconcile` 手动触发），需要时传
  `reconcilePeriodMillis` 或在构造引擎时注入 `Scheduler`。
