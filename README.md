# 周期规则有限展开后端（recurrence-backend）

纯后端、无前端、无预约/考勤业务的时间规则计算服务。输入一条周期规则（JSON），
在**有限上限**内展开实例序列（JSON）。使用本地固定测试数据，无数据库、无外部时间规则库。

> **范围声明（非完整 RFC 兼容）**：本项目只实现 RFC 5545（iCalendar RRULE）的一个
> **明确小子集**：频率仅 `DAILY / WEEKLY / MONTHLY`，支持 `INTERVAL`、`COUNT`、`UNTIL`
> 与月度月末策略。**不支持** `YEARLY`、`BYDAY`、`BYMONTHDAY`、`BYSETPOS`、`WKST`、
> `EXDATE/RDATE` 等，也不保证与完整 RFC 5545 实现语义一致。

## 技术栈

- Java 21（仅用 JDK `java.time`，时间运算不依赖第三方规则库）
- Jackson 2.17.2（JSON 序列化）
- JUnit 5.10.2（自动化测试）
- Maven 3.8+（构建），编译插件显式锁定 3.13.0、surefire 3.2.5

## 目录结构

```
pom.xml
run.sh                         # 一键编译并运行样例
samples/                       # 8 个本地固定请求样例
src/main/java/com/example/recur/
  Main.java                    # CLI 入口：文件参数或 stdin
  RecurrenceService.java       # JSON 编排与错误信封
  RecurrenceExpander.java      # 周期展开引擎（核心）
  TemporalParser.java          # ISO-8601 -> ZonedDateTime
  RecurrenceException.java     # 业务异常（errorCode）
  model/                       # record DTO 与枚举
src/test/java/com/example/recur/  # 30 个 JUnit 5 测试
```

## 请求 / 响应格式

请求字段：

| 字段 | 必填 | 说明 |
|---|---|---|
| `zoneId` | 否 | IANA 时区名，默认 `UTC` |
| `rule.start` | 是 | 首次实例时刻，ISO-8601：`yyyy-MM-dd`、`yyyy-MM-ddTHH:mm:ss` 或带偏移量 |
| `rule.frequency` | 是 | `DAILY` / `WEEKLY` / `MONTHLY` |
| `rule.interval` | 否 | 周期间隔，≥1，≤100000，默认 1 |
| `rule.count` | 否 | 次数上限（从 start 起计数） |
| `rule.until` | 否 | 截止时刻（**含边界**）；与 count 同时给出时取更严格者 |
| `rule.monthEndPolicy` | 否 | `LAST_VALID_DAY`（默认）/ `SKIP`，仅 MONTHLY 生效 |
| `windowStart` / `windowEnd` | 否 | 输出窗口（含边界），只过滤输出，**不改变 count 对完整序列的计数** |
| `maxExpansions` | 否 | 本次输出数量上限，1..10000，默认 5000 |

裸日期按 `zoneId` 当日 00:00 解释；不带偏移量的日期时间按 `zoneId` 墙上时间解释；
带偏移量（`Z` / `+08:00`）按同一绝对时刻转换到 `zoneId`。

月末不存在日期策略（**显式**，无隐式行为）：

- `LAST_VALID_DAY`：1 月 31 日起按月展开时，2 月落 29 日（闰年）/28 日（平年），4 月落 30 日；
- `SKIP`：跳过没有该日期的月份（跳过的月份不计入 count）。

成功响应：

```json
{
  "success": true,
  "tzdbVersion": "2026b",
  "zoneId": "UTC",
  "occurrenceCount": 6,
  "occurrences": ["2024-01-31T00:00:00Z", "..."]
}
```

错误响应（错误不抛到进程外，HTTP 场景可据此映射状态码）：

```json
{ "success": false, "tzdbVersion": "2026b",
  "errorCode": "EXPANSION_LIMIT_EXCEEDED", "message": "..." }
```

`errorCode`：`VALIDATION_ERROR` / `EXPANSION_LIMIT_EXCEEDED` / `INVALID_JSON`。

**时区数据库版本**：每个响应都带 `tzdbVersion`，取自 JDK 内置 IANA tzdata
（`ZoneRulesProvider.getVersions("UTC")` 最新键）。本机实测为 `2026b`（OpenJDK 21.0.12）。
若升级 JDK 或通过 `-Djava.time.zone.DefaultZoneRulesProvider` 替换 tzdata，该值随之变化。

## 构建与运行

```bash
# 运行全部自动化测试
mvn test

# 打包（跳过测试）
mvn package -DskipTests

# 运行样例（脚本自动构建 classpath；离线环境用 -o，脚本已自动回退）
./run.sh samples/02-monthly-31st-leap.json

# 或直接用 java（classpath 需含 jackson-databind/core/annotations 三个 jar）
java -cp "target/recurrence-backend-1.0.0.jar:<jackson-jars>" \
     com.example.recur.Main samples/01-daily-count.json

# 从标准输入读取
cat samples/01-daily-count.json | ./run.sh -
```

## 请求样例（samples/）

| 文件 | 验收点 |
|---|---|
| `01-daily-count.json` | DAILY + count，非 UTC 时区（Asia/Shanghai） |
| `02-monthly-31st-leap.json` | **31 日 + 闰年**：2024-02-29、4 月落 30 日（LAST_VALID_DAY） |
| `03-count-and-until.json` | **count 与 until 同时限制**，count 更严格 |
| `04-empty-window.json` | **空区间**：窗口与序列不相交，success=true、occurrenceCount=0 |
| `05-unbounded-limit.json` | **过大展开上限保护**：无 count/until 且 maxExpansions=5 |
| `06-feb29-skip-leap-years.json` | 2 月 29 日 + SKIP + interval=12，仅闰年产出（2024/2028/2032） |
| `07-weekly-dst.json` | 跨 DST（America/New_York），墙上 09:00 保持、偏移量 −05:00→−04:00 |
| `08-max-expansions-too-large.json` | maxExpansions=999999 被 VALIDATION_ERROR 拒绝 |

各样例的真实运行输出与测试运行记录见 [RUN_LOG.md](RUN_LOG.md)。

## 安全边界与终止性

- 无 `count`、无 `until`（窗口又未提前截断）的规则理论上无限；引擎以 `maxExpansions`
  （默认 5000，硬顶 10000）为输出上限，超出即 `EXPANSION_LIMIT_EXCEEDED`。
- `MONTHLY + SKIP` 下被跳过的周期不产出实例，另有"遍历周期数"防御性守卫
  （120012 个周期，与本次输出上限解耦），保证任何输入下循环必然终止；
  日期运算溢出（极端 interval/until）同样转为 `EXPANSION_LIMIT_EXCEEDED`。
- `interval` 限定 ≤100000，防止间隔×周期数在日期运算中溢出 `long`。
- 时间字段、时区名、枚举值、数值范围均在入口校验；非法 JSON 返回 `INVALID_JSON`。

## 明确不做的事

- 不提供 HTTP 服务与前端页面（仅 CLI / 可嵌入的 `RecurrenceService`）。
- 不做预约、排班、考勤等业务系统，不维护用户数据。
- 不宣称 RFC 5545 完整兼容；未列出的 RRULE 部件一律不支持（提交即 `VALIDATION_ERROR`）。
