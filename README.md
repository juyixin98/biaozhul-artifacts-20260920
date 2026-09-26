# DST Rule Expander（夏令时规则展开服务）

把“每日当地时间规则”或 cron 规则，在**指定 IANA 时区内**展开为一串 **UTC 时刻**。
纯后端、JSON 输入输出、仅使用内置固定测试数据，**不依赖系统时钟、不发起网络请求、不做预约/考勤系统**。

重点解决 DST（夏令时）两类边界：

- **春季跳时（Gap，不存在的本地时间）**：如纽约 2026-03-08 `02:30` 这一分钟根本不存在。
- **秋季回拨（Overlap，重复的本地时间）**：如纽约 2026-11-01 `01:30` 会发生两次。

两类边界都支持四种可配置策略：`earlier`（选早）、`later`（选晚）、`skip`（跳过）、`error`（报错）。
每次响应都回带**时区数据库版本（TZDB）**、运行时版本以及区间内相关的跳时/回拨转换，保证结果可追溯。

---

## 1. 环境与构建

- JDK 17+（实际在 **OpenJDK 21.0.12.1** 上开发与运行）
- Maven 3.8+（仅构建期需要；运行期只需要 JRE）
- 依赖：Jackson 2.17.2（JSON）、JUnit 5 + AssertJ（测试，仅 test 作用域）

```bash
mvn -o clean verify          # 离线构建 + 跑测试 + 覆盖率门禁
mvn -q package -DskipTests   # 只打包，产出 target/dst-rule-expander-1.0.0.jar（fat jar）
```

产物 `target/dst-rule-expander-1.0.0.jar` 是含 Main-Class 的可执行 fat jar。

---

## 2. 命令行用法

```bash
# 展开：从文件读取请求
java -jar target/dst-rule-expander-1.0.0.jar expand request.json

# 展开：从标准输入读取，结果写到文件
cat request.json | java -jar target/dst-rule-expander-1.0.0.jar expand -o response.json

# 查看内置时区数据库版本
java -jar target/dst-rule-expander-1.0.0.jar tzdb

# 运行内置固定数据集（无需任何外部文件）
java -jar target/dst-rule-expander-1.0.0.jar demo spring      # 春季跳时
java -jar target/dst-rule-expander-1.0.0.jar demo fall        # 秋季回拨
java -jar target/dst-rule-expander-1.0.0.jar demo cross-year  # 南半球跨年
java -jar target/dst-rule-expander-1.0.0.jar demo error       # 触发报错策略
```

退出码：`0` 成功；`2` 请求合法但命中 `error` 策略 / 请求 JSON 非法；`64` 用法错误；`74` IO 错误；`1` 其它内部错误。

### `tzdb` 输出（实测）

```json
{
  "success" : true,
  "tzdbVersion" : "2026b",
  "javaVersion" : "21.0.12.1",
  "javaVendor" : "Ubuntu",
  "availableZoneCount" : 604
}
```

---

## 3. 请求格式

```json
{
  "zoneId": "America/New_York",
  "fromDate": "2026-03-01",
  "toDate": "2026-11-30",
  "rules": [
    { "ruleId": "standup", "type": "daily", "localTime": "02:30" },
    { "ruleId": "weekday-nine", "type": "cron", "cron": "0 9 * * 1-5" }
  ],
  "gapPolicy": "earlier",
  "overlapPolicy": "later",
  "sort": true,
  "deduplicate": true,
  "limit": 100000
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `zoneId` | 是 | IANA 时区 ID，如 `America/New_York`、`Europe/Berlin`、`Australia/Sydney`、`UTC` |
| `fromDate` / `toDate` | 是 | 本地日期闭区间，`yyyy-MM-dd` |
| `rules` | 是 | 至少一条；`ruleId` 非空且不可重复 |
| `rules[].type` | 是 | `daily` 或 `cron` |
| `… localTime` | daily 必填 | `HH:mm` 或 `HH:mm:ss` |
| `… cron` | cron 必填 | 五段 cron：`分 时 日 月 周`（见下） |
| `gapPolicy` | 否 | `earlier`(默认) / `later` / `skip` / `error` |
| `overlapPolicy` | 否 | `earlier` / `later`(默认) / `skip` / `error` |
| `sort` | 否 | 默认 `true`，按 UTC 时刻升序 |
| `deduplicate` | 否 | 默认 `true`，合并相同 UTC 时刻 |
| `limit` | 否 | 返回条数上限，默认 100000，硬上限 1000000 |

### cron 语法（分钟粒度）

`minute hour day-of-month month day-of-week`

- 数字，支持 `*`、列表 `a,b,c`、区间 `a-b`、步进 `*/n`、`a-b/n`、`a/n`；
- 月份支持 `JAN..DEC`，星期支持 `SUN..SAT`；星期 `0` 与 `7` 均为周日；
- 当“日”和“周”都受限时，采用 Vixie cron 的 **OR 语义**（满足其一即可）。

---

## 4. Gap / Overlap 语义（命名基于 UTC 时间轴上的“早 / 晚”）

以纽约（2026 年）为例。

### 春季跳时 Gap：本地 `02:00(-05:00)` 直接跳到 `03:00(-04:00)`，转换发生在 `07:00Z`

请求本地 `02:30`：

| 策略 | 采用偏移 | UTC 结果 | 含义 |
|---|---|---|---|
| `earlier`（默认） | 跳时**后** -04:00 | `06:30Z`（转换之前） | 把不存在的时间当作已进入夏令时，落在更早的 UTC 时刻 |
| `later` | 跳时**前** -05:00 | `07:30Z`（转换之后） | 按冬令时解读，落在更晚的 UTC 时刻 |
| `skip` | — | 不产出 | 记入 `skipped`，`reason=GAP_SKIPPED` |
| `error` | — | 整体失败 | 错误码 `GAP_ENCOUNTERED`，退出码 2 |

### 秋季回拨 Overlap：本地 `02:00(-04:00)` 回拨到 `01:00(-05:00)`，转换发生在 `06:00Z`

请求本地 `01:30`（该刻钟出现两次）：

| 策略 | 采用偏移 | UTC 结果 | 含义 |
|---|---|---|---|
| `earlier` | 回拨**前** -04:00（夏令时，第一次） | `05:30Z` | 取重复时间的第一次 |
| `later`（默认） | 回拨**后** -05:00（冬令时，第二次） | `06:30Z` | 取重复时间的第二次 |
| `skip` | — | 不产出 | 记入 `skipped`，`reason=OVERLAP_SKIPPED` |
| `error` | — | 整体失败 | 错误码 `OVERLAP_ENCOUNTERED`，退出码 2 |

> 每条 occurrence 的 `localDateTime` 保留**请求的本地挂钟时间**（即使它在 gap 中不存在），
> `offsetSeconds` 记录实际采用的偏移，`kind` 标记 `NORMAL/GAP_EARLIER/GAP_LATER/OVERLAP_EARLIER/OVERLAP_LATER`。

---

## 5. 排序与去重

- **排序**：`sort=true` 时按 `(UTC 时刻, ruleId, localDateTime)` 升序；`false` 时保留
  “按请求中的规则顺序、规则内按时间”的遇到顺序（encounter order）。
- **去重**：在排序**之前**，按 UTC 时刻合并，保留**遇到顺序中的第一条**（即请求里靠前的规则优先）。
  `duplicateCount` 给出被合并条数。`deduplicate=false` 时同一 UTC 时刻会保留多条。

---

## 6. 时区数据库可追溯

响应中的 `zoneRules` 字段：

- `tzdbVersion`：内置 IANA TZDB 版本（实测 `2026b`）；偏移型 ID（如 `+02:00`）记为 `fixed-offset:+02:00`；
- `javaVersion`：运行时 Java 版本；
- `relevantTransitions`：请求日期范围内相关的 `GAP`/`OVERLAP` 转换，含 UTC 时刻与前后偏移（秒）。

未来年份（如 2026）的转换由 zone 的 recurring transition rules 生成，历史转换来自显式列表，两者合并去重。

---

## 7. 样例

`samples/` 下有 10 个固定请求，`samples/outputs/` 下有对应的实际响应（已生成、随仓库交付）。

| 样例 | 场景 |
|---|---|
| 01–04 | 春季 Gap 的 earlier / later / skip / error 四种策略 |
| 05–07 | 秋季 Overlap 的 earlier / later / skip |
| 08 | 悉尼跨年（本地 `2027-01-01T00:00` → `2026-12-31T13:00Z`） |
| 09 | 柏林 cron：工作日 9 点、周一三五 18:30、周一 10 点每 15 分钟 |
| 10 | UTC 下 daily 与 cron 同一时刻的去重 |

一键运行全部样例（自动比对期望退出码，并打印 TZDB 版本）：

```bash
./run-samples.sh
```

实测输出：

```
OK   01-spring-gap-earlier            exit=0
OK   02-spring-gap-later              exit=0
OK   03-spring-gap-skip               exit=0
OK   04-spring-gap-error              exit=2
OK   05-fall-overlap-earlier          exit=0
OK   06-fall-overlap-later            exit=0
OK   07-fall-overlap-skip             exit=0
OK   08-cross-year-sydney             exit=0
OK   09-cron-weekdays-berlin          exit=0
OK   10-dedup-same-instant            exit=0
  "tzdbVersion" : "2026b",
  "javaVersion" : "21.0.12.1",
```

---

## 8. 自动化测试与实测结果

```bash
mvn -o clean verify
```

实测（本环境，TZDB 2026b / JDK 21）：

- **63 个测试全部通过，0 失败 0 错误 0 跳过**；`BUILD SUCCESS`，JaCoCo 门禁 `All coverage checks have been met.`
- JaCoCo 覆盖率（`target/site/jacoco/index.html`）：
  - 行覆盖 **86.5%**（门禁阈值 80%）
  - 指令覆盖 87.2%，分支覆盖 77.7%

测试分布：

- `LocalTimeResolverTest`（11）：gap/overlap 的全部策略、边界刻钟、南半球回拨；
- `CronExpressionTest`（22）：通配/列表/区间/步进/月份星期名/OR 语义/非法表达式；
- `ExpansionServiceTest`（17）：春跳、秋回、跨年、排序、去重开关、计数、limit、TZDB 追溯、校验错误码；
- `CliEndToEndTest`（13）：文件/stdin/`-o`、错误信封、退出码、`tzdb`、四个内置 demo。

---

## 9. 错误响应

错误统一为 JSON 信封，写到 stderr（使用 `-o` 时写入该输出文件），退出码 2：

```json
{ "success" : false, "code" : "GAP_ENCOUNTERED", "message" : "Non-existent local time ..." }
```

错误码：`INVALID_JSON`、`MISSING_ZONE`、`UNKNOWN_ZONE`、`MISSING_DATE_RANGE`、`INVALID_DATE`、
`INVALID_DATE_RANGE`、`DATE_RANGE_TOO_LARGE`、`NO_RULES`、`MISSING_RULE_ID`、`DUPLICATE_RULE_ID`、
`UNSUPPORTED_RULE_TYPE`、`MISSING_LOCAL_TIME`、`MISSING_CRON`、`INVALID_POLICY`、`INVALID_RULE`、
`INVALID_LIMIT`、`GAP_ENCOUNTERED`、`OVERLAP_ENCOUNTERED`。

---

## 10. 项目结构

```
src/main/java/com/dstexp/
  cli/Main.java              # 命令行入口：expand / tzdb / demo
  json/JsonMappers.java      # 共享 Jackson mapper
  model/                     # 请求/响应 DTO、策略枚举
  cron/CronField,CronExpression.java   # 五段 cron 解析与本地时刻枚举
  schedule/RulePlanner.java           # 规则 -> 本地挂钟时间
  schedule/LocalTimeResolver.java     # 本地时间 -> UTC，处理 gap/overlap
  service/ExpansionService.java       # 校验、展开、排序、去重、转换收集
src/main/resources/demo/     # 4 个内置固定数据集（打进 jar）
samples/                     # 10 个请求样例 + outputs/ 实际响应
src/test/java/com/dstexp/    # 63 个 JUnit5 测试
```

## 11. 非目标（Non-goals）

- 不是定时任务调度器：只做“规则 → UTC 时刻”的离线展开，不会到点触发任何动作；
- 不是预约 / 考勤系统，不持久化任何业务数据；
- 无前端、无 HTTP 服务（JSON 经文件 / stdin / stdout 交换）；
- cron 仅支持分钟粒度的五段表达式，不支持秒、年、`L`/`W`/`#` 等扩展。
