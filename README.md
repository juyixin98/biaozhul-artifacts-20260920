# monotonic-timeout — 墙钟截止时间 → 单调计时器转换服务（纯后端）

把"人类日历时间的截止时刻"转换为"不受校时影响的单调超时"，并保证：

1. **转换只发生在安排/重启恢复的瞬间**；
2. **运行期间墙钟（NTP/管理员/夏令时）向前或向后调整，都不改变已安排的超时**；
3. **重启后单调刻度失效，必须依据持久化的墙钟截止瞬间重新计算**；
4. **绝不用两种时钟混算同一个时长**。

本项目只做时间/版本规则计算，提供 JSON 输入输出，使用本地固定测试数据；
**不是预约系统，也不是考勤系统；不含任何前端或 HTTP 服务。**

---

## 1. 背景与核心模型

系统里有两种时间，语义完全不同：

| | 墙钟 wall clock | 单调计时器 monotonic timer |
|---|---|---|
| 含义 | 人类日历时间（`Instant`） | 流逝计时（`System.nanoTime()` 风格纳秒刻度） |
| 会被调整吗 | 会：NTP 校时、管理员拨钟、夏令时 | **不会**（速率可能漂移，但不会跳变） |
| 用途 | 仅在转换边界表达截止时间、持久化、展示 | 运行期间所有剩余时间/到期判断 |
| 能否跨 JVM | 能（UTC 时间线绝对点） | **不能**（纪元随 JVM 死亡而失效） |

唯一合法的跨域运算（只做一次）：

```
安排瞬间：  duration  = deadlineWall − nowWall          # 两个墙钟瞬间之差
            monoDeadlineTick = nowTick + duration        # 映射到单调刻度
运行期间：  remaining = monoDeadlineTick − nowTick       # 只用单调刻度
重启恢复：  丢弃旧单调刻度；用持久化的 deadlineWall 与新时钟再做一次上面的转换
```

错误示范（本项目刻意防止）：用 `Instant.now()` 的差值做运行期到期判断（校时会误判），
或把 `nanoTime()` 刻度持久化后在新 JVM 里使用（数值已无意义）。

### 时区数据库（TZDB）版本

每次转换结果都记录所用的 IANA tzdata 版本（来自 JDK 内置 `ZoneRulesProvider`，
文件为 `$JAVA_HOME/lib/tzdb.dat`），用于审计"这条超时是按哪一版夏令时规则算的"。
本机实测版本见下文运行记录（`2026b`）。

---

## 2. 规则（本地固定测试数据）

规则目录硬编码在 `RuleCatalog`，按 `ruleId + version` 选取，支持三种形态：

| ruleId | 版本 | 类型 | 含义 |
|---|---|---|---|
| `brief-absolute` | `1` / `2` | 绝对截止瞬间 | v2 特意落在美东夏令时结束（fall-back）当天 |
| `grace-two-minutes` | `1` | 相对时长 | 自安排起 2 分钟（单调语义） |
| `daily-1730-newyork` | `1` | 每日本地时刻 | 纽约 17:30，按 TZDB 解析夏令时偏移 |
| `daily-1800-shanghai` | `1` | 每日本地时刻 | 上海 18:00 |

每日规则：当天本地时刻已过（含恰好相等）则顺延到次日。

---

## 3. 构建与测试

要求 JDK 21+、Maven 3.8+（依赖均为常用库；加 `-o` 可离线构建）。

```bash
mvn clean verify          # 编译 + 64 个测试 + JaCoCo 80% 覆盖率门禁
mvn package -DskipTests   # 产出可执行 fat jar: target/monotonic-timeout-1.0.0.jar
```

测试报告：`target/surefire-reports/`；覆盖率报告：`target/site/jacoco/index.html`。

---

## 4. 使用方式（JSON 输入输出）

入口：`com.example.monotime.App`。无 HTTP，所有输出都是 stdout 上的 JSON，
统一封装为 `{ "success": ..., "data": ..., "error": ... }`；错误以非零退出码返回。

```bash
# 直接用 Maven
mvn -q exec:java -Dexec.args="info"

# 或用 fat jar
java -jar target/monotonic-timeout-1.0.0.jar <命令> [选项]
```

| 命令 | 说明 |
|---|---|
| `info` | TZDB 版本、JDK 版本、tzdb.dat 路径 |
| `catalog` | 列出固定规则目录 |
| `schedule --id <超时ID> --rule <规则ID> [--version <版本>] [--store <目录>]` | 用当前时钟安排并落盘 |
| `status --id <超时ID> [--store <目录>]` | 查询（本命令即新 JVM，按**重启恢复**语义重算） |
| `list [--store <目录>]` | 列出持久化记录（仅墙钟域） |
| `scenario --file <场景.json>` 或 stdin | 确定性执行校时/流逝/重启脚本（验收主入口） |
| `request --file <请求.json>` 或 stdin | 通用 JSON 信封：`{"command":"scenario|info|catalog|...", ...}` |
| `help` | 命令文档 |

### 4.1 场景脚本格式（确定性虚拟时钟）

```json
{
  "name": "...",
  "description": "...",
  "initialWallClock": "2026-09-25T09:00:00Z",
  "initialMonotonicNanos": 1000000000,
  "steps": [ { "op": "...", ... } ]
}
```

`SimulatedClock` 让墙钟与单调刻度**正交可控**：校时只跳墙钟，流逝只推单调刻度。

| op | 字段 | 含义 |
|---|---|---|
| `schedule` | `timeoutId`, `ruleId`, `version` | 安排超时（真实写入 JSON 持久化） |
| `status` | `timeoutId` | 查询剩余/是否过期 |
| `delete` | `timeoutId` | 从内存与持久化删除 |
| `advance` | `durationIso`(如 `PT90S`) 或 `nanos` | 真实流逝，**只推进单调计时器** |
| `adjustWallClock` | `forwardIso` / `backwardIso` / `instant` | 校时，**只动墙钟** |
| `restart` | `newMonotonicNanos`(缺省 0)、`newWallClockInstant`(缺省不变) | 模拟 JVM 重启：清空内存、纪元重置、从持久化恢复重算 |

每一步都返回操作前后的双时钟快照，便于直接核对验收点。

### 4.2 内置请求/场景样例（`examples/`）

| 文件 | 验收点 |
|---|---|
| `request-info.json` / `request-catalog.json` / `request-schedule.json` | 通用请求：环境信息、规则目录、安排 |
| `request-scenario-envelope.json` | `{"command":"scenario",...}` 信封 |
| `scenario-clock-adjustment.json` | **向前/向后校时不改变已安排超时**；只有单调流逝消耗剩余 |
| `scenario-already-expired.json` | **安排时截止已过**：立即过期，时长为负，拨回墙钟也不改变结论 |
| `scenario-restart-recovery.json` | **持久化恢复**：重启纪元重置，按墙钟截止瞬间重算单调死线 |
| `scenario-dst-fall-back.json` | 夏令时结束日按 TZDB 规则解析本地截止时刻 |

运行：

```bash
java -jar target/monotonic-timeout-1.0.0.jar scenario \
     --file examples/scenario-clock-adjustment.json --store data/store-demo
java -jar target/monotonic-timeout-1.0.0.jar request < examples/request-scenario-envelope.json
```

---

## 5. 持久化：刻意不存单调刻度

每条超时落盘为 `<store>/<id>.json`，**只有墙钟域与审计字段**：

```json
{
  "timeoutId": "acc-r",
  "ruleId": "grace-two-minutes",
  "ruleVersion": "1",
  "scheduledAtInstant": "2026-09-25T09:00:00Z",
  "deadlineInstant": "2026-09-25T09:02:00Z",
  "scheduledTzdbVersion": "2026b"
}
```

`monotonicDeadlineTickNanos`、`scheduledAtTickNanos`、`durationAtScheduleNanos`
**在结构上不可能被保存**（落盘投影为独立的 `PersistedTimeout` 记录）。
因为 nanoTime 纪元跨 JVM 无意义，保存只会诱导误用——这正是"重启后必须重新计算"的护栏。

持久化健壮性：`timeoutId` 在边界严格校验（仅 1–128 位 `[A-Za-z0-9._-]`，拒绝分隔符与
`.`/`..`，不做可能碰撞的有损替换）；写入走"同目录临时文件 + 原子 move"，
崩溃不会留下半截记录；恢复时单个损坏文件会被跳过并告警，不拖垮其余记录。
`scenario`/`request` 命令默认写入与生产存储隔离的 `data/store-scenario/`，
且场景运行只重置脚本自身涉及的 id，绝不清空目录。

---

## 6. 验收点 → 测试/样例对照

| 验收要求 | 自动化测试 | 场景样例 |
|---|---|---|
| 墙钟向前校时不提前超时 | `MonotonicConverterTest.forwardWallClockAdjustmentDoesNotExpireScheduledTimeout` | `scenario-clock-adjustment.json` |
| 墙钟向后校时不推迟超时 | `MonotonicConverterTest.backwardWallClockAdjustmentDoesNotChangeScheduledTimeout` | 同上 |
| 只有单调流逝消耗剩余 | `...monotonicElapseChangesRemainingAndWallAdjustmentAfterwardsDoesNot`、`ScenarioRunnerTest.wallAdjustmentsDoNotChangeTimeoutButMonotonicElapseDoes` | 同上 |
| 已过期（及恰好相等）截止 | `...marksAlreadyExpiredDeadlineAtScheduleTime`、`...marksDeadlineExactlyAtScheduleTimeAsExpired` | `scenario-already-expired.json` |
| 重启后重新计算/持久化恢复 | `...recoverAfterRestartReanchorsToNewMonotonicEpochUsingWallDeadline`、`...restartRecomputesMonotonicDeadlineFromPersistedWallInstant`、`JsonTimeoutStoreTest.persistsOnlyWallClockFieldsNeverMonotonicTicks` | `scenario-restart-recovery.json` |
| 不混用两种时钟度量时长 | 模型分离（`ClockPort` 两读 + 仅边界转换）；`SimulatedClockTest` 正交性四例 | 全部场景 |
| 夏令时/TZDB 版本 | `DeadlineCalculatorTest.*FallBack*`、`TzdbInfoTest`、`MonotonicConverterTest.recordsTzdbVersionOnEveryScheduledTimeout` | `scenario-dst-fall-back.json` |
| 跨 JVM 恢复 | `AppTest.scheduleThenListThenStatusRoundTripAcrossProcesses`（+ 下文三进程实测） | — |

---

## 7. 实际运行记录（2026-09-25，本机如实记录）

环境：OpenJDK `21.0.12.1`（Ubuntu）、Maven 3.8.7、Linux x86_64；TZDB `2026b`。
完整原始日志见 `run-output/`（该目录不入库），关键结果如下。

### 7.1 干净构建与测试

命令：`mvn -B -ntp -o clean verify` → 退出码 **0**，`BUILD SUCCESS`。

```
Tests run: 64, Failures: 0, Errors: 0, Skipped: 0
All coverage checks have been met.
JaCoCo instruction coverage: 89.3%   (门禁 80%)
```

各测试类（11 个）全部 0 失败：api 3、scenario 8、persistence 8、
domain（DeadlineCalculator 7 + MonotonicConverter 9 + RecoverAndStatus 2）、
tzdb 2、app 13、data 5、clock 4、math 3。

### 7.2 场景实测关键数值（`run-output/*.json`）

- **校时**：安排 120s 超时后，墙钟向后 `PT3H`、向前 `P2D`，`remaining` 始终 **120.0s**；
  `advance PT90S` 后 **30.0s**；再 `PT30S` 后 **expired=true**。
- **已过期**：11:00Z 安排截止 10:05Z，`duration=-3300s`、`alreadyExpired=true`；
  再把墙钟向后拨 10 天仍 **expired=true**。
- **重启恢复**：安排截止 09:02Z；单调流逝 90s 后重启（新纪元 8,000,000,000，墙钟 09:01:30Z），
  恢复后墙钟截止不变、单调死线 = 新紀元 + 30s = **38,000,000,000**、`remaining=30s`；
  之后向后校时 1 小时仍 30s；单调流逝 30s 后 **expired=true**。
- **DST**：2026-11-01（美东 fall-back 日）纽约本地 17:30 截止正确解析为 **22:30Z**（UTC-5）。
- **持久化**：落盘文件不含任何 `monotonic*Tick*` / `durationAtSchedule*` 字段（grep 计数 0）。

### 7.3 真实跨 JVM 演示（三个独立 `java` 进程）

```bash
java -jar target/monotonic-timeout-1.0.0.jar schedule --id cross-jvm-1 \
     --rule daily-1800-shanghai --store data/cli-demo   # JVM A：安排，落盘
java -jar target/monotonic-timeout-1.0.0.jar list    --store data/cli-demo   # JVM B：仅读墙钟域
java -jar target/monotonic-timeout-1.0.0.jar status  --id cross-jvm-1 --store data/cli-demo  # JVM C：重算
```

实测：JVM A 的单调刻度（如 `32719539800365`，仅当次进程有效）**不在盘上**；
JVM C 启动后从墙钟截止瞬间重新锚定，正确给出剩余时间。

### 7.4 错误路径实测

未知命令 / 不存在的 id / 缺少 `--id`：均返回 `success:false` 的 JSON 并以退出码 2 结束。

### 7.5 独立代码评审与加固

完成后做了一次独立只读评审。评审确认四条时钟铁律（单次转换、运行期只看单调刻度、
重启重算、两域不混算）实现正确、路径穿越被拦截；同时发现并已修复：

- **HIGH**：`scenario` 旧实现会清空默认存储目录，可能误删生产记录 → 场景改用隔离目录
  `data/store-scenario/`，且只重置脚本自身涉及的 id（回归测试 `runDoesNotDeleteUnrelatedRecordsInSharedStore`，并经跨 JVM 实测）。
- **MEDIUM**：id 有损清洗会让不同 id 碰撞到同一文件互相覆盖 → 改为边界严格拒绝非法 id；
  写入改为临时文件 + 原子 move；单文件损坏不再拖垮整目录恢复。
- **LOW**：`status` 恢复与查询两次读时钟 → 新增 `MonotonicConverter.recoverAndStatus`
  在同一时钟快照完成；补齐裸参数/缺字段的人类可读错误；非预期错误详情改走 stderr；
  固定并文档化夏令时缺口/重叠策略；标注虚拟时钟非线程安全。

### 7.6 未通过项

无。构建、全部 64 个测试、覆盖率门禁、四个验收场景与跨 JVM 演示均通过。

---

## 8. 项目结构

```
pom.xml
examples/                 JSON 请求与四个验收场景脚本
src/main/java/com/example/monotime/
  App.java                CLI/JSON 入口（命令分发、统一响应封装）
  ClockPort.java          双时钟端口（墙钟 + 单调读数）
  SystemClockPort.java    生产时钟（Instant.now / System.nanoTime）
  SimulatedClock.java     确定性虚拟时钟（校时与流逝正交）
  SaturatingMath.java     纳秒刻度饱和算术（防溢出）
  tzdb/TzdbInfo.java      TZDB/JDK 版本探测
  domain/                 TimeRule(sealed)、DeadlineCalculator、
                          MonotonicConverter、ScheduledTimeout、TimeoutStatus
  data/RuleCatalog.java   本地固定规则（版本化）
  persistence/            TimeoutStore / PersistedTimeout(仅墙钟域) / JsonTimeoutStore
  scenario/               场景定义、步骤、执行器与报告（验收引擎）
  api/                    ObjectMapper 配置、统一响应封装
src/test/java/...         56 个 JUnit 5 测试，镜像主代码结构
```

## 9. 范围与限制

- 纯后端库/CLI：无前端、无网络服务、无数据库；规则为固定测试数据。
- `schedule/status` 单次命令使用真实时钟，主要用于演示转换与跨 JVM 恢复；
  **确定性的完整验收请使用 `scenario`**（虚拟时钟）。
- 每日规则若落在夏令时"跳空/重叠"本地时刻，采用 JDK `ZonedDateTime` 的默认归一化
  （跳空向前顺延、重叠取较早偏移）；本项目固定数据未使用此类歧义时刻。
- 时长用纳秒 long 饱和表示，超范围（约 ±292 年）钳制到 `Long` 边界，不静默溢出。
