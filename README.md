# 动态规则版本绑定（Dynamic Rule Version Binding）

纯后端的事件流过滤服务：过滤规则可**热更新**，规则版本**不可变**，
对事件的绑定**完全按事件时间（event time）**进行；乱序/迟到事件使用其
事件时刻对应的**历史版本**；缺失版本时**拒绝**事件，绝不静默套用最新规则。

- Java 21，无消息中间件、无 Web 框架（HTTP 使用 JDK 内置
  `com.sun.net.httpserver`，JSON 使用 Jackson）。
- 处理时间（`TimeSource`）与维护调度（`Scheduler`）均可注入，测试与
  参考运行使用 `SimClock` + `ManualScheduler`，结果**逐位可复现**。
- 小数据精确参考实现：内存存储、线性扫描、二分版本查找；并发以单一
  监视器保护，语义优先于吞吐。

## 1. 核心语义（不变量）

1. **版本时间线只增不改（append-only）。**
   - 首个版本经 `bootstrap` 安装，生效时间归一化为 `−∞`，覆盖全部历史。
   - 之后每个版本的 `effectiveFrom` 必须**严格大于**上一版本起点，因此
     版本区间互不重叠、顺序确定。允许**预先注册**未来生效的版本：
     事件在时刻 `t` 只会看到 `effectiveFrom <= t` 的版本，任何更新都
     无法改变已关闭时间区间的答案。
2. **版本不可变。** 版本一经发布，其规则内容永不修改。回滚不是修改，
   而是**追加一个新版本**，复制历史版本的规则（新版本号、相同
   SHA-256 内容校验和，接口返回 `checksumsMatch: true`）。
3. **按事件时间绑定，区间左闭右开。** 事件绑定到
   `effectiveFrom <= eventTime` 的最新版本；恰好在边界时刻
   `t == effectiveFrom` 属于新版本，`t − 1ms` 属于旧版本。
4. **迟到事件使用历史版本，而非最新版本。** 水位（watermark）为
   `max(已见事件时间) − watermarkDelay`；事件时间低于水位即为迟到，
   但只要不晚于 `watermark − allowedLateness` 仍被接受，并用历史版本求值。
5. **缺失/已回收版本显式拒绝，绝不静默兜底：**
   - 引导前事件 → `MISSING_VERSION`；
   - 事件所需版本已被垃圾回收 → `VERSION_RECLAIMED`；
   - 事件晚于迟到宽限边界（版本仍保留）→ `TOO_LATE`。
   版本解析先于迟到判断，因此即使事件既超宽限、版本又已回收，也会
   精确报告 `VERSION_RECLAIMED`。
6. **历史版本回收必须同时满足两个前提（缺一不可）：**
   - 时间前提：后继版本的起点已到达/越过 `watermark − allowedLateness`
     （该旧区间不可能再有迟到事件落入）；
   - 引用前提：保留期内的处理结果不再引用该版本（结果需保持对产生
     它的确切规则内容可审计）。
   最新版本永不回收；维护周期先按处理时间清理过期结果，再在同一周期内
   回收因此解除引用的版本。

## 2. 构建与运行

需要 JDK 21；本仓库不含 Maven Wrapper，以下命令中的 Maven 路径请替换
为本机的 `mvn`（开发机使用 `~/tools/apache-maven-3.9.9/bin/mvn`）。

```bash
mvn compile                         # 编译
mvn test                            # 31 个自动化测试
mvn package -DskipTests             # 生成 target/*.jar 与 target/lib/

# 确定性演示（无需启动 HTTP，直接打印验收场景）
mvn dependency:build-classpath -Dmdep.outputFile=/tmp/cp.txt -q
java -cp "target/classes:$(cat /tmp/cp.txt)" com.example.drvb.demo.AcceptanceDemo

# HTTP 服务（墙钟 + 后台维护线程）
java -cp "target/dynamic-rule-binding-1.0.0.jar:target/lib/*" \
  com.example.drvb.server.HttpServerMain --port=8080

# HTTP 服务（模拟时钟 + 手动维护，可复现走查）
java -cp "target/dynamic-rule-binding-1.0.0.jar:target/lib/*" \
  com.example.drvb.server.HttpServerMain \
  --port=8080 --clock=sim --scheduler=manual \
  --watermark-delay=0 --allowed-lateness=3600000 --result-retention=86400000

# 一键端到端走查（自动选择空闲端口，输出 JSON 交互全过程）
scripts/walkthrough.sh
```

启动参数：`--port`、`--clock=system|sim`、`--scheduler=wall|manual`、
`--watermark-delay=<ms>`、`--allowed-lateness=<ms>`、
`--result-retention=<ms>`、`--maintenance-period=<ms>`、
`--sim-start=<epochMillis>`。`eventTime`/`effectiveFrom`/`watermark`
均接受 epoch 毫秒数或 ISO-8601 字符串（`2026-09-23T10:00:00Z`）。

## 3. HTTP 接口

| 方法 路径 | 说明 |
|---|---|
| `POST /admin/bootstrap` | 安装首个不可变版本（`effectiveFrom` 被忽略并归一化为 −∞） |
| `POST /admin/rules/versions` | 追加热更新版本（区间必须严格后移；可预注册未来版本） |
| `POST /admin/rules/rollback` | 以历史版本规则内容追加新版本（`sourceVersionId`、`newVersionId`、`effectiveFrom`） |
| `GET  /admin/rules/versions` | 版本时间线（含校验和） |
| `POST /events` | 送入单个事件；处理结果 200 返回（含 `ACCEPTED`/`REJECTED`） |
| `POST /events/batch` | 按请求数组顺序送入多个事件（调用方控制交错顺序） |
| `GET  /results?from=&to=` | 按事件时间区间查询已接受结果（ISO 时间或毫秒） |
| `GET  /results/rejected` | 被拒绝事件及原因 |
| `POST /admin/watermark` | 显式推进水位（空闲源/管理用途，单调不倒退） |
| `POST /admin/maintenance` | 执行一次：清理过期结果 + 回收历史版本 |
| `GET  /admin/gc-preview` | 只读查看回收资格（边界、引用集合、可回收列表） |
| `POST /admin/tick` | 手动模式触发一次维护调度 |
| `POST /admin/clock` | 模拟模式设置/推进处理时间（`set` / `advanceMillis`） |
| `GET  /admin/stats`、`GET /health` | 计数配置与存活检查 |

错误状态码：`400` 请求格式错误、`404` 回滚源版本不存在/未引导发布、
`409` 重复版本/重复引导、`422 EFFECTIVE_TIME_IN_PAST` 版本区间重叠或倒退。
注意：事件被规则体系拒绝（缺失/超期/回收）是**正常处理结果**，HTTP 为
200 且体中 `status=REJECTED`，与 4xx 的请求错误区分。

规则条件（`condition`）支持：
`all`/`any`/`not` 逻辑；`eq`/`ne`/`gt`/`gte`/`lt`/`lte`（数值比较
自动按 BigDecimal 提升，`eq` 兼容字符串数字）；`contains`/`startsWith`/
`endsWith`/`matches`（整串正则）；`in`；`exists`/`notExists`；
`alwaysTrue`。字段支持点路径（`user.tier`、`items.0.id`）。

请求样例见 [`examples/`](examples/)：引导、发布、单事件/批量、回滚、
水位推进、时钟设置与维护。

## 4. 代码结构

```
src/main/java/com/example/drvb/
  core/      纯领域库（零 Jackson 依赖）：Event, Rule, Condition(sealed),
             RuleVersion(不可变+SHA-256), RuleRegistry(时间线/解析/回收),
             JsonCanonical(校验和规范化)
  time/      TimeSource(SystemClock/SimClock), Scheduler(Wall/Manual)
  stream/    WatermarkTracker, RuleBindingEngine(绑定/拒绝/维护),
             IngestResult, ResultStore(+内存实现, 版本引用集合)
  server/    JDK HttpServer + Jackson 编解码
  demo/      AcceptanceDemo：确定性验收场景
src/test/    31 个 JUnit 5 测试（核心语义、水位调度、HTTP 黑盒集成）
examples/    请求 JSON 样例；scripts/walkthrough.sh 端到端走查
```

## 5. 验收场景与实际运行记录

### 5.1 确定性演示 `AcceptanceDemo`

规则时间线：v1(金额≥100，−∞) → v2(≥200，10:00) → v3(≥300，11:00)
→ 回滚 rb(≥100，14:00)；迟到宽限 60 分钟，结果保留 24 小时。
实际运行（2026-09-23，命令见 §2），关键输出：

- 引导前事件：`REJECTED ... reason=MISSING_VERSION`；引导后重放同事件
  绑定 v1 并命中。
- 边界：事件 @10:00:00.000 → `v=v2`；@09:59:59.999 → `v=v1 late=true`。
- 乱序迟到：11:30 处理 @11:00 金额 250 → v3 不命中（250<300）；
  @10:40 金额 250 → **历史 v2 命中**（若误用最新 v3 则不命中）。
- 宽限边界：@10:30（恰为水位 11:30 − 60m）仍接受；@10:29、@09:45
  → `TOO_LATE`。
- 回滚：`checksums ... (equals v1: true)`；@14:05 金额 150 在 rb 下命中，
  @11:20 金额 250 仍绑定不可变的 v3 区间且不命中。
- 回收前提：14:05 维护 `purged=0 reclaimed=0`；15:00 时间前提满足但
  结果仍引用版本，`reclaimed=0`；次日 15:30（过 24h 保留期）同一维护
  周期 `purged=13 reclaimed=3 removed=[v1, v2, v3] retained=[rb...]`；
  此后 @09:10 事件 `VERSION_RECLAIMED`。
- 最终计数：`accepted=10`，
  `rejected={MISSING_VERSION=1, TOO_LATE=2, VERSION_RECLAIMED=1, total=4}`。

### 5.2 HTTP 端到端走查 `scripts/walkthrough.sh`

实际运行 `EXIT=0`，16 个步骤全部成功，覆盖：引导前 `MISSING_VERSION`、
引导/重复引导、v2/v3 预注册、重复起点发布返回
`422 EFFECTIVE_TIME_IN_PAST`、边界绑定、批量乱序（白名单标志与嵌套
字段生效）、迟到事件绑定 v2、`TOO_LATE`、回滚 `checksumsMatch=true`、
按事件时间查询、GC 预览（引用集合钉住版本）、强制水位后维护仍
`versionsReclaimed=0`、推进时钟过保留期并手动 tick 后仅剩最新版本、
再送旧区间事件得 `VERSION_RECLAIMED`。完整 JSON 交互见运行时输出。

### 5.3 自动化测试

`mvn test` 实际结果：**31 个测试全部通过，0 失败 0 错误 0 跳过**
（`ConditionTest` 6、`RuleRegistryTest` 8、`WatermarkTrackerTest` 3、
`ManualSchedulerTest` 3、`RuleBindingEngineTest` 9、
`HttpServerMainTest` 2 黑盒 HTTP 集成）。

开发过程中如实在测试驱动下修正了两处设计/数据问题：

1. **发布闸门初版过严。** 最初要求 `effectiveFrom > watermark`，测试
   “规则更新与乱序事件交错”暴露其无法预注册未来版本、会错误拒绝合法
   热更新。正确的不可变不变量是“区间严格后移”（`effectiveFrom` 大于
   上一版本起点），水位只参与回收判定；已修正 `RuleRegistry.publish`
   并保留“区间重叠/倒退 → 422”的拒绝测试。
2. **拒绝原因优先级。** 初版先判迟到再解析版本，导致版本已回收的事件
   被误报 `TOO_LATE`。已调整为先做版本绑定（`MISSING`/`RECLAIMED`
   优先），再判宽限；补充了区分两种原因的测试。

验收演示的时间线参数（迟到窗口、保留期、各事件与维护的钟点）也经过
多轮实跑核对，使每步文字说明与真实输出严格一致。

## 6. 范围与非目标

- 无前端；无外部消息系统/数据库（可替换 `ResultStore` 与时间线存储扩展）。
- 参考实现面向小数据与语义精确：单监视器并发、内存保留；大批量场景可
  在保持同一套不变量的前提下替换存储与索引实现。
