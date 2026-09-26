# 运行记录（RUN_LOG）

本文件记录在交付环境中的**实际**命令与结果。所有命令均真实执行；无未通过项被隐瞒。

## 环境

| 项 | 值 |
|---|---|
| 操作系统 | Linux 6.8.0-90-generic (Ubuntu 24.04) |
| JDK | OpenJDK 21.0.12.1（2026-08-18 构建） |
| Maven | 3.8.7 |
| JDK 内置 IANA tzdb 版本 | **2026b**（每个响应的 `tzdbVersion` 字段） |
| 依赖 | Jackson 2.17.2、JUnit 5.10.2、maven-surefire 3.2.5、maven-compiler 3.13.0 |

## 自动化测试

命令：

```bash
mvn -o clean test
```

结果（2026-09-25 实际输出）：

```
TemporalParserTest ........... Tests run: 4,  Failures: 0, Errors: 0, Skipped: 0
RecurrenceServiceTest ........ Tests run: 5,  Failures: 0, Errors: 0, Skipped: 0
RecurrenceExpanderTest ....... Tests run: 21, Failures: 0, Errors: 0, Skipped: 0
Results: Tests run: 30, Failures: 0, Errors: 0, Skipped: 0
BUILD SUCCESS
```

**30/30 通过，0 失败、0 错误、0 跳过。**

测试覆盖的验收点：

- **31 日**：MONTHLY 从 1 月 31 日起，LAST_VALID_DAY 落 2/29（闰年）、4/30、6/30；
  平年链路 2023-12-31 → 2024 各月；SKIP 策略只保留 31 日月份。
- **闰年**：2024-02-29 起按月 LAST_VALID_DAY（平年回退 28 日）、按 interval=12 + SKIP
  仅在 2024/2028/2032 产出。
- **次数与截止同时限制**：两组用例分别验证 count 更严格、until 更严格（含边界）。
- **空区间**：窗口在序列之后 / 之前均返回 success=true、空 occurrences，不报错。
- **过大展开上限**：无界规则超 maxExpansions 返回 EXPANSION_LIMIT_EXCEEDED；
  maxExpansions=0 / 10001 返回 VALIDATION_ERROR；超大 interval（100001）被拒绝。
- 另有 DST（America/New_York 春令时）、跨时区偏移量转换、非法 JSON、非法枚举/时区等。

### 开发过程中出现过的失败（已修复，如实记录）

1. 首次 `mvn test`：本机 Maven 默认选用缓存中的 maven-compiler-plugin 3.1（不支持
   Java 21 release，报 "Source option 5 is no longer supported"）。
   **修复**：在 pom.xml 显式锁定 compiler 插件 3.13.0。
2. 修复 1 后编译失败：`TimeZone.getTZDataVersion()` 在该 JDK 上不可见
   （符号不存在）。**修复**：改用文档化 API
   `ZoneRulesProvider.getVersions("UTC").lastKey()` 读取 tzdb 版本。
3. 首次测试运行 29 个中 17 个失败，均为**测试断言的时间字符串格式**问题而非引擎逻辑：
   `ZonedDateTime.toString()` 会附 `[UTC]` 区域名，且零秒输出差异。
   **修复**：测试统一用 `DateTimeFormatter.ISO_OFFSET_DATE_TIME` 断言，与 JSON 输出格式一致。
   修复后 29/29 通过。

## 样例实际运行

命令（对 `samples/` 全部 8 个文件）：

```bash
mvn -o -q package -DskipTests
java -cp "target/recurrence-backend-1.0.0.jar:<jackson 2.17.2 三个 jar>" \
     com.example.recur.Main samples/<file>.json
```

`./run.sh samples/<file>.json` 已验证可自动构建 classpath 并得到相同结果。

### 01-daily-count.json —— DAILY + count（Asia/Shanghai）

```json
{
  "success" : true, "tzdbVersion" : "2026b", "zoneId" : "Asia/Shanghai",
  "occurrenceCount" : 5,
  "occurrences" : [ "2024-03-01T09:00:00+08:00", "2024-03-02T09:00:00+08:00",
    "2024-03-03T09:00:00+08:00", "2024-03-04T09:00:00+08:00",
    "2024-03-05T09:00:00+08:00" ]
}
```

### 02-monthly-31st-leap.json —— 31 日遇闰年 2 月（LAST_VALID_DAY）

```json
{
  "success" : true, "tzdbVersion" : "2026b", "zoneId" : "UTC",
  "occurrenceCount" : 6,
  "occurrences" : [ "2024-01-31T00:00:00Z", "2024-02-29T00:00:00Z",
    "2024-03-31T00:00:00Z", "2024-04-30T00:00:00Z",
    "2024-05-31T00:00:00Z", "2024-06-30T00:00:00Z" ]
}
```

### 03-count-and-until.json —— count=5 与 until 同给，count 先到

```json
{
  "success" : true, "tzdbVersion" : "2026b", "zoneId" : "UTC",
  "occurrenceCount" : 5,
  "occurrences" : [ "2024-01-01T00:00:00Z", "2024-01-02T00:00:00Z",
    "2024-01-03T00:00:00Z", "2024-01-04T00:00:00Z", "2024-01-05T00:00:00Z" ]
}
```

### 04-empty-window.json —— 空区间（窗口在 2025 年，序列在 2024 年 1 月）

```json
{ "success" : true, "tzdbVersion" : "2026b", "zoneId" : "UTC",
  "occurrenceCount" : 0, "occurrences" : [ ] }
```

### 05-unbounded-limit.json —— 无界规则 + maxExpansions=5

```json
{ "success" : false, "tzdbVersion" : "2026b",
  "errorCode" : "EXPANSION_LIMIT_EXCEEDED",
  "message" : "展开结果达到上限 5，请缩小范围（count/until/windowEnd）或提高 maxExpansions（最大 10000）" }
```

### 06-feb29-skip-leap-years.json —— 2/29 + SKIP + interval=12，仅闰年

```json
{
  "success" : true, "tzdbVersion" : "2026b", "zoneId" : "UTC",
  "occurrenceCount" : 3,
  "occurrences" : [ "2024-02-29T00:00:00Z", "2028-02-29T00:00:00Z",
    "2032-02-29T00:00:00Z" ] }
```

### 07-weekly-dst.json —— 跨美国春令时（2024-03-10 换日）

```json
{
  "success" : true, "tzdbVersion" : "2026b", "zoneId" : "America/New_York",
  "occurrenceCount" : 5,
  "occurrences" : [ "2024-03-04T09:00:00-05:00", "2024-03-11T09:00:00-04:00",
    "2024-03-18T09:00:00-04:00", "2024-03-25T09:00:00-04:00",
    "2024-04-01T09:00:00-04:00" ] }
```

墙上时间保持 09:00，UTC 偏移量由 −05:00 变为 −04:00，符合本地墙上时间语义。

### 08-max-expansions-too-large.json —— 999999 超硬顶 10000

```json
{ "success" : false, "tzdbVersion" : "2026b",
  "errorCode" : "VALIDATION_ERROR",
  "message" : "maxExpansions 必须在 1..10000 之间，收到: 999999" }
```

## 未通过项 / 已知限制

- 截至本次记录：**无未通过的测试或样例**（30/30 测试通过，8/8 样例输出符合预期）。
- 已知范围限制（设计如此，非缺陷）：仅 DAILY/WEEKLY/MONTHLY；不支持 YEARLY、BYDAY、
  BYSETPOS、WKST 等；MONTHLY 的锚点是 start 的"日"，不支持"第几个星期几"；
  输出上限硬顶 10000。详见 README"明确不做的事"。
