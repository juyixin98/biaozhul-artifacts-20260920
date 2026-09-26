# 周期规则有限展开（Recurrence Rule Finite Expansion）

纯后端 Java 服务：输入一条周期规则和一个时间窗口，输出窗口内的具体日期序列（JSON 入 / JSON 出）。
只做**有限展开**，不做预约系统、不做考勤系统、不做前端、不连接数据库，测试数据全部为本地固定数据。

- Java 21、Maven、Jackson（JSON）、JUnit 5
- 日期使用 `java.time.LocalDate`（ISO-8601，纯日期，不含时分秒、不含时区换算）
- 每个响应都记录运行 JDK 内置的 **IANA 时区数据库版本**（`tzdbVersion` 字段，本机为 `2026b`）

## 明确支持的规则子集（重要）

本项目**有意只实现一个明确子集，不宣称完整兼容 RFC 5545 / iCalendar RRULE**。

| 字段 | 取值 | 说明 |
|---|---|---|
| `rule.freq` | `DAILY` / `WEEKLY` / `MONTHLY` | 日、周、月三种频率；不支持 YEARLY、HOURLY、SECONDLY 等 |
| `rule.interval` | 正整数，默认 `1` | 相邻两个周期间隔（每 N 天 / 每 N 周 / 每 N 月） |
| `rule.count` | 正整数，可选 | 从 `dtStart` 起最多产生多少个实例（含窗口外的，语义见下） |
| `rule.until` | `YYYY-MM-DD`，可选 | 截止日期，**含当天** |
| `rule.byDay` | `MO..SU` 数组，**仅 WEEKLY** | 每个生效周内的星期；省略时等于 `dtStart` 所在星期 |
| `rule.monthEndPolicy` | `SKIP`（默认）/ `CLAMP`，**仅 MONTHLY** | 目标日在当月不存在时的显式策略 |

月末不存在日期的策略是**显式**的，不做隐式猜测：

- `SKIP`：该月不产生实例。如 1 月 31 日起的月规则，2 月（无 31 日）整体跳过。
- `CLAMP`：落到当月最后一天。如 1 月 31 日 → 非闰年 2 月 28 日；2024-02-29 → 2025-02-28。

**不支持（明确排除）**：YEARLY、`BYMONTHDAY` 数字表达式、`BYSETPOS`、`WKST`（固定按周一开始周）、
`EXDATE/RDATE`、UTC/DATE-TIME 与夏令时换算、复数规则集合等。

### 关键语义

1. **COUNT 从 dtStart 开始计数**，窗口只是"取景框"。窗口外的前序实例照样消耗 count。
2. **COUNT 与 UNTIL 可同时给出**，二者中先到达的先生效（交集语义）。
3. MONTHLY 锚定每月 1 日再解析目标日，因此短月（2 月）之后**不会发生日期漂移**
   （例如 1/31 → 2/28 → 3/31，而不是错误地变成 3/28）。
4. WEEKLY 的周固定从周一开始；`dtStart` 的星期必须包含在 `byDay` 中，否则首个实例未定义，直接校验报错。

## 有限展开与安全上限

展开始终有界，避免无限/超大输出：

- 必须提供 `windowStart` / `windowEnd`（含端点），只返回窗口内日期。
- `maxExpansions`：窗口内实例数上限。默认 `1000`，硬上限 `10000`，超过硬上限直接拒绝请求；
  实际展开数超过给定上限时返回 `EXPANSION_LIMIT_EXCEEDED` 错误。
- 窗口为空（窗口内没有任何实例）是正常结果：返回 `count: 0` 和空数组，不算错误。

## 构建与运行

```bash
mvn package              # 编译 + 跑测试 + 打包（跳过测试用 -DskipTests）
java -jar target/recurrence-expander-1.0.0.jar examples/request-daily.json   # 文件入参
cat request.json | java -jar target/recurrence-expander-1.0.0.jar            # 标准输入
```

退出码：`0` 成功；`2` 请求错误（校验失败 / 超限 / JSON 非法），错误仍以 JSON 打印到标准输出。

## 请求格式

```json
{
  "requestId": "可选，原样回填",
  "rule": {
    "dtStart": "2025-01-31",
    "freq": "MONTHLY",
    "interval": 1,
    "count": 10,
    "until": "2025-12-31",
    "byDay": ["MO", "WE", "FR"],
    "monthEndPolicy": "SKIP"
  },
  "windowStart": "2025-01-01",
  "windowEnd": "2025-12-31",
  "maxExpansions": 1000
}
```

成功响应：

```json
{
  "ok": true,
  "requestId": "...",
  "tzdbVersion": "2026b",
  "count": 7,
  "occurrences": ["2025-01-31", "2025-03-31"]
}
```

错误响应：

```json
{
  "ok": false,
  "requestId": "...",
  "tzdbVersion": "2026b",
  "error": { "code": "EXPANSION_LIMIT_EXCEEDED", "message": "..." }
}
```

错误码：`MISSING_DTSTART`、`MISSING_FREQ`、`INVALID_FREQ`、`INVALID_INTERVAL`、`INVALID_COUNT`、
`UNTIL_BEFORE_DTSTART`、`INVALID_DATE`、`INVALID_BYDAY`、`BYDAY_WEEKLY_ONLY`、
`DTSTART_NOT_IN_BYDAY`、`INVALID_MONTH_END_POLICY`、`MISSING_WINDOW`、`INVALID_WINDOW`、
`INVALID_MAX_EXPANSIONS`、`EXPANSION_LIMIT_EXCEEDED`、`EXPANSION_TOO_LARGE`、`INVALID_JSON`。

## 请求样例

`examples/` 目录：

| 文件 | 覆盖点 |
|---|---|
| `request-daily.json` | DAILY + COUNT |
| `request-monthly-31-skip.json` | **31 日**，短月 SKIP |
| `request-monthly-31-clamp.json` | **31 日**，短月 CLAMP 到月末 |
| `request-leap-year.json` | **闰年 2/29** 起的月规则 + COUNT |
| `request-weekly-byday.json` | WEEKLY + BYDAY + 隔周 + COUNT 与 UNTIL 双限制 |
| `request-empty-window.json` | **空区间** |
| `request-limit-exceeded.json` | **超过展开上限** |

## 测试

```bash
mvn test
```

45 个 JUnit 5 用例，AAA 结构，覆盖：日/周/月频率与间隔、31 日 SKIP/CLAMP、闰年 2/29 与非闰年回落、
COUNT+UNTIL 同时限制（两侧谁先生效各一用例）、窗口空区间、窗口外实例消耗 count、
展开上限触发、硬上限拒绝、病态超长序列兜底、各类非法输入校验、CLI 文件/stdin 端到端。

实际命令、输出与中途出现过的失败见 [RUNLOG.md](RUNLOG.md)。

## 项目结构

```
src/main/java/com/example/recurrence/
  core/Frequency.java, MonthEndPolicy.java, RecurrenceRule.java,
       RecurrenceExpander.java, ValidationException.java, ExpansionLimitExceededException.java
  json/RequestParser.java, ResponseWriter.java
  Main.java                    # CLI 入口
src/test/java/...              # 45 个测试
examples/                      # 固定请求样例
```
