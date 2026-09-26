# monotonic-timeout — 墙钟截止时间 → 单调计时器转换服务

纯后端服务（Java 21，JSON over HTTP，无前端、无数据库、无预约/考勤业务）。
核心职责只有一件事：**把人类日历意义上的“墙钟截止时间（wall-clock deadline）”在安排瞬间换算成
单调计时器（monotonic timer）上的触发点，之后只按单调时间计时。**

- 墙钟（wall clock）：`Instant`，人类日历时间，可被 NTP / 管理员向前或向后调整。
- 单调钟（monotonic clock）：进程内只读递增的计时读数（生产环境为 `System.nanoTime()`），
  不受校时影响，**重启后原点改变、读数失效**。

> 一句话不变量：**转换之后，墙钟再怎么调都不改变已安排的超时；重启之后必须用墙钟截止时间重新换算。**

---

## 1. 为什么需要它（缺陷模型）

用“两次墙钟读数之差”来计时长，在墙钟被调整时会出错：

- NTP 向前校时 3 小时 → 本应 10 分钟后触发的超时被误判为“早已过期”；
- NTP 向后校时 3 小时 → 同一个超时被白白延后 3 小时。

本服务的做法是把两类时钟严格分开，**唯一允许的“跨时钟”运算只发生在转换瞬间**：

```
安排时:   remaining_ns = deadlineWall - clock.wall()      // 仅此一处用墙钟差求时长
          fireMonoNs   = clock.monoNanos() + remaining_ns // 立刻锚定到单调读数
运行中:   到期判据      = clock.monoNanos() >= fireMonoNs   // 只看单调钟，绝不读墙钟差
重启后:   单调读数作废 → 删除旧 fireMonoNs → 用当前时钟对持久化的 deadlineWall 重新执行上面的换算
```

服务同时上报 IANA 时区数据库（tzdb）版本，因为本地时刻→UTC 瞬时的换算（尤其 DST）依赖它。

---

## 2. 技术栈与目录

- Java 21、JDK 内置 `com.sun.net.httpserver.HttpServer`（无第三方 Web 框架）
- Jackson 2.17.2（含 `jackson-datatype-jsr310`，ISO-8601 时间）
- JUnit 5（27 个测试）、JaCoCo（行覆盖率门禁 80%）
- Maven Shade 打单一可执行 fat jar

```
src/main/java/com/example/timeout/
  clock/    TimeoutClock 接口；SystemTimeoutClock(nanoTime)；VirtualClock(测试/演示，可校时)
  core/     TimeoutRegistry：墙钟→单调转换、到期判定、重启重算；TimeoutEntry / TimeoutStatus
  rule/     DeadlineRule(sealed：absolute|duration|dailyLocal|versionTtl) + DeadlineRuleEngine
  store/    TimeoutStore / JsonTimeoutStore（原子写；只持久化墙钟字段）
  service/  TimeoutService、ServiceManager（原地模拟重启）
  api/      TimeoutHttpServer、请求解析与边界校验、JSON 视图、统一响应信封
  tz/       TzdbInfo（读取 tzdb 版本）
  App.java  启动入口、固定测试数据
src/test/…  单元测试 + 真实 HTTP 端到端验收测试
examples/    请求样例、curl 演示脚本、真实响应样例
```

---

## 3. 构建与测试

需要 JDK 21 与 Maven。

```bash
mvn -o verify          # 离线编译 + 27 个测试 + JaCoCo 覆盖率门禁（需要联网时去掉 -o）
mvn -o package -DskipTests
java -jar target/monotonic-timeout.jar --mode virtual --port 8088 --store data/timeouts.json
```

启动参数：`--mode virtual|system`（默认 virtual）、`--port`、`--store`、
`--start-wall <ISO-8601>`（仅虚拟模式）、`--no-seed`、`-h`。

- **virtual**：虚拟时钟，可通过接口向前/向后校时、推进时间，用于演示与固定测试数据。
- **system**：真实系统墙钟（UTC）+ `System.nanoTime()`；接口拒绝一切校时操作（HTTP 409）。

---

## 4. HTTP 接口

统一信封：`{ "success": true, "data": ..., "error": null }`，失败时
`{ "success": false, "data": null, "error": { "code", "message" } }`。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 存活状态、时钟类型、代际(generation) |
| GET | `/info` | 当前墙钟、单调读数、**tzdb 版本**、Java 版本、时区数量 |
| POST | `/timeouts` | 按规则安排超时（见下） |
| GET | `/timeouts` | 列出全部（只读视图，到期立即呈现 EXPIRED） |
| GET | `/timeouts/{id}` | 单个条目 |
| POST | `/clock/tick` | 仅 virtual：真实流逝 `{"seconds":n}`，两钟同步前进 |
| POST | `/clock/set-wall` | 仅 virtual：直接设置墙钟 `{"wall":"…Z"}`，单调读数不变 |
| POST | `/clock/advance-wall` | 仅 virtual：相对校时 `{"seconds":n}`，**n 可为负**，单调读数不变 |
| POST | `/admin/restart` | 模拟重启：单调钟归零，按持久化墙钟截止时间重算；可给 `{"wallNow":"…Z"}` |
| POST | `/persist` | 显式落盘（落盘前确认已到期条目） |

### 规则（请求体 `rule`）

| type | 字段 | 截止时间含义 |
|---|---|---|
| `absolute` | `deadline` (ISO-8601 UTC) | 给定 UTC 瞬时；过去则立即 EXPIRED |
| `duration` | `seconds` 或 `duration`(ISO-8601，如 `PT2M30S`) | 当前墙钟 + 时长 |
| `dailyLocal` | `localTime`(`HH:mm`)、`zone` | 该时区下一次该本地时刻（正确处理 DST gap/overlap） |
| `versionTtl` | `issuedAt`、`ttlSeconds` 或 `ttl` | **版本规则**：截止 = 版本时间戳 + TTL，与何时安排无关 |

### 响应中两类字段的区别（不要混用）

- `deadlineWall` / `convertedAtWall` / `firedAtWall`：**墙钟**，日历时间，可被校时改变，是持久化与重算依据。
- `fireMonoNanos` / `convertedAtMonoNanos` / `firedAtMonoNanos` / `remainingMonotonicNanos`：
  **单调读数（纳秒）**，只在当前进程内有意义，**从不落盘**，重启即重算。

---

## 5. 验收场景与复现步骤

启动服务后可直接运行两个脚本（已在本机真实执行，输出见第 7 节）：

```bash
./examples/demo_steering.sh   # 向前/向后校时不影响超时；真实流逝才到期；已过期截止立即 EXPIRED
./examples/demo_restart.sh    # 单调读数不落盘；重启归零并按墙钟重算；停机期间到期→重启即过期
./examples/curl_examples.sh   # 用 examples/requests/*.json 逐个演示四类规则
```

1. **向前校时**：安排 600s 超时 → `set-wall` 向前 +3h → 仍 SCHEDULED、剩余仍 600s。
2. **向后校时**：`advance-wall` -5h → 仍 SCHEDULED、剩余仍 600s。
3. **真实流逝到期**：`tick 599s` 未到期；再 `tick 1s` → EXPIRED，
   `firedAtMonoNanos == fireMonoNanos`，而 `firedAtWall` 是被拨慢后的墙钟读数（仅记录，不参与判定）。
4. **已过期截止**：安排一个过去的绝对截止 → 立即 EXPIRED，
   `remainingAtConversionNanos` 为负值，保留“迟到多久”。
5. **持久化恢复**：落盘 JSON 只含墙钟截止时间（`grep -i monoNanos` 命中 0 次）；
   重启后单调读数归零，用当前墙钟对每个截止时间重算；停机期间已到点的条目在重启时判为过期。
6. **不混用两种时钟计时长**：运行期剩余时间一律由
   `fireMonoNanos - monoNanos()` 得到，从不使用墙钟差值。

### DST 说明

`dailyLocal` 基于 `ZonedDateTime`：春季 gap（本地时刻不存在）按该时区规则顺延，
秋季 overlap（本地时刻出现两次）取较早偏移，结果确定。单测
`DeadlineRuleEngineTest.dailyLocalRuleSurvivesSpringForwardDstGap` 覆盖了
`America/New_York` 2026-03-08 的春令时跳变。

---

## 6. 持久化格式

`data/timeouts.json` 为 JSON 数组。**类型层面就无法写出单调字段**
（`StoredTimeout` record 只有墙钟/业务字段）：

```json
{
  "id": "restart-job",
  "label": "restart demo",
  "deadlineWall": "2026-09-25T09:10:00Z",
  "status": "SCHEDULED",
  "ruleSource": "DurationRule"
}
```

写入采用“临时文件 + 原子 rename”。重启恢复时单调字段以占位值读入，
必须经 `TimeoutRegistry.recover(...)` 重新换算，不允许直接信任。

---

## 7. 实际运行记录（命令与结果）

环境：Ubuntu，OpenJDK `21.0.12.1`，Maven 3.x，内置 **tzdb 版本 `2026b`**（604 个时区）。

- `mvn -o verify` → **BUILD SUCCESS**；Tests run: **27, Failures: 0, Errors: 0, Skipped: 0**；
  JaCoCo：**All coverage checks have been met**（行覆盖率门禁 80%，实测见下）。
- 覆盖率（`target/site/jacoco/jacoco.xml`）：
  LINE **83.9%**、INSTRUCTION 83.8%、METHOD 88.4%、CLASS 92.6%、BRANCH 71.0%。
- 真实启动 virtual 服务（端口 8088）并以 curl 走完第 5 节全部场景，输出存档于
  `logs/demo_steering.out`、`logs/demo_restart.out`、`logs/curl_examples.out`。关键结果：
  - 向前 +3h / 向后 -5h 校时后，600s 超时 `remainingMonotonicNanos` 恒为 `600000000000`；
  - `tick` 满 600s 时 EXPIRED，`firedAtMonoNanos = fireMonoNanos = 600000000000`；
  - 过去截止立即 EXPIRED，`remainingAtConversionNanos = -11400000000000`；
  - 落盘文件 `grep -ciE 'fireMono|monoNanos'` 命中 **0**；
  - `/admin/restart`（墙钟拨到 12:00）后 `generation=2, monoNowNanos=0`，
    截止 09:10 的任务重算为 EXPIRED；未到期重启剩余按墙钟差重算为 500s。
- **真实进程级重启**：`kill` 掉 JVM 后以 `--start-wall 2026-09-25T13:00:00Z` 重新启动，
  `monoNowNanos=0`；截止 12:10 的 `fresh-10m` 重算为 EXPIRED，截止 19:30Z 的每日任务仍 SCHEDULED。
- 边界校验：空 id/缺规则、负时长 → HTTP 400；重复 id → 400；未知端点 → 404；
  system 模式下 `POST /clock/tick` → HTTP 409。

### 开发过程中出现过、已修复的问题（如实记录）

1. 早期 `status()` 只在调用 `pollExpired()` 后才翻转，导致“单调时刻已到但尚未轮询”时
   状态仍显示 SCHEDULED。已改为状态由单调读数动态派生，`get/list` 返回只读过期视图，
   显式确认只发生在 `pollExpired()`/落盘，过期事件恰好投递一次。
2. Jackson 最初把 `Instant` 写成数字时间戳，导致持久化内容不含 ISO 字符串、断言失败；
   已禁用 `WRITE_DATES_AS_TIMESTAMPS`，统一 ISO-8601 文本。
3. 规则 record 的紧凑构造器最初对 null 抛 `NullPointerException`，与“边界抛
   `IllegalArgumentException`”的测试契约不符，已统一为 `IllegalArgumentException`。
4. 首次实现误把服务层 `get()/list()` 也触发过期确认，会提前消费过期事件；已改为只读。

当前未通过项：**无**（27/27 测试通过，覆盖率门禁通过，上述手工验收均符合预期）。

---

## 8. 明确不做的事

- 不做任何前端 / UI；仅 JSON HTTP 接口。
- 不做预约系统、考勤系统或任何业务排期；这里只处理“截止时间→计时器”的换算。
- 不把单调读数持久化、不跨进程复用单调值；system 模式不允许接口校时。
