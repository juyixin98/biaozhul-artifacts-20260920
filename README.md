# DST Rule Expander（夏令时规则展开）

纯后端小服务（CLI）：把**每日当地时间规则**在指定 IANA 时区内展开为一组 **UTC 时刻**。
不包含预约/考勤语义，不包含前端；所有测试数据为本地固定数据，不依赖外部服务。

- 语言/构建：Java 21 + Maven
- 时区判定：JDK 内置 `java.time.ZoneRules`（IANA tz database）
- JSON：Jackson 2.17

## 背景：缺口（gap）与重叠（overlap）

以 `America/New_York`（2026 年，美国现行规则）为例：

- **春季跳时（gap）**：`2026-03-08 02:00` 时钟拨到 `03:00`，当地时间 `02:30` **不存在**。
- **秋季回拨（overlap）**：`2026-11-01 02:00` 时钟拨回 `01:00`，当地时间 `01:30` **发生两次**（一次 EDT、一次 EST）。

两种情况分别由 `gapPolicy` / `overlapPolicy` 配置，取值一致：

| 策略 | 含义 |
|------|------|
| `EARLIER` | 选**较早**的 UTC 时刻。gap：按跳时**后**偏移解释（落在缺口前）；overlap：取**第一次**出现（旧偏移） |
| `LATER`   | 选**较晚**的 UTC 时刻。gap：按跳时**前**偏移解释（落在缺口后）；overlap：取**第二次**出现（新偏移） |
| `SKIP`    | 跳过该条，不出现在 `occurrences`，记录到 `skipped` |
| `ERROR`   | 整个展开中止，返回 JSON 错误，退出码 2 |

策略可省略：`gapPolicy` 默认 `LATER`，`overlapPolicy` 默认 `EARLIER`。

## 构建

```bash
mvn -q package
```

产物：`target/dst-rule-expander-1.0.0.jar`（shade 胖 jar）。

## 运行

```bash
# 从文件读取
java -jar target/dst-rule-expander-1.0.0.jar samples/spring-forward.json

# 从 stdin 读取
cat samples/fall-back.json | java -jar target/dst-rule-expander-1.0.0.jar
```

退出码：`0` 成功；`2` 请求非法或命中 `ERROR` 策略；`1` I/O 或 JSON 语法错误。

### 请求字段

| 字段 | 必填 | 说明 |
|------|------|------|
| `zoneId` | 是 | IANA 时区名，如 `America/New_York`、`Asia/Shanghai` |
| `startDate` | 是 | 起始日期（含），`YYYY-MM-DD` |
| `endDate` | 是 | 结束日期（含），`YYYY-MM-DD`；区间上限 3662 天 |
| `rules` | 是 | 规则数组；`time` 为当地时间 `HH:mm`，`label` 可选 |
| `gapPolicy` | 否 | `EARLIER` / `LATER` / `SKIP` / `ERROR`，默认 `LATER` |
| `overlapPolicy` | 否 | 同上，默认 `EARLIER` |

未知字段会被拒绝（便于发现拼写错误）。

### 响应要点

- `tzdbVersion`：JVM 使用的 IANA 数据库版本（如 `2024a`），每次展开可追溯。
- `occurrences`：按 `utc` **升序排序**；完全相同的 `(utc, rule, label)` 行**去重**。
  - `resolution`：`NORMAL` / `GAP_EARLIER` / `GAP_LATER` / `OVERLAP_EARLIER` / `OVERLAP_LATER`
  - `requestedLocal`：规则请求的当地时间；`resolvedLocal`：该 UTC 时刻实际显示的当地时间。
- `skipped`：`SKIP` 策略丢弃的条目及原因（`GAP_SKIPPED` / `OVERLAP_SKIPPED`）。
- `stats`：`inputRules`、`uniqueRules`、`duplicateRulesRemoved`、`days`、`occurrences`、
  `duplicatesRemoved`、`skipped`，用于核对排序/去重行为。

## 样例

`samples/` 下含固定请求：

- `spring-forward.json` — 2026-03-07~09，规则 02:30，覆盖春季跳时（`gapPolicy=LATER`）。
- `fall-back.json` — 2026-10-31~11-02，规则 01:30，覆盖秋季回拨（`overlapPolicy=EARLIER`）。
- `cross-year.json` — 2026-12-31~2027-01-02 跨年，两条规则，策略 `SKIP`。
- `gap-error.json` — 命中缺口且 `gapPolicy=ERROR`，返回错误、退出码 2。

预期关键值（America/New_York）：

| 日期 | 当地规则 | 策略 | UTC |
|------|----------|------|-----|
| 2026-03-07 | 02:30 | NORMAL | 07:30Z（EST, UTC-5） |
| 2026-03-08 | 02:30 | GAP_LATER | **07:30Z**（按 EST 解释，落地为当地 03:30 EDT） |
| 2026-03-09 | 02:30 | NORMAL | 06:30Z（EDT, UTC-4） |
| 2026-11-01 | 01:30 | OVERLAP_EARLIER | **05:30Z**（第一次，EDT） |

> 说明：跳时当天规则按 `LATER` 展开后 UTC 与前一天同为 07:30Z，输出中通过 `resolution`、
> `requestedLocal`/`resolvedLocal` 区分；相邻天出现相同 UTC 是 DST 的真实结果，不属于“重复行”。

## 测试

```bash
mvn test
```

JUnit 5 用例（固定数据，无网络依赖）覆盖：

- 春季跳时：`EARLIER`/`LATER`/`SKIP`/`ERROR` 四种策略 + 缺口边界时刻；
- 秋季回拨：四种策略；
- 跨年区间；
- 排序（乱序规则按 UTC 输出）、规则级与结果级去重及计数；
- 无夏令时地区（`Asia/Shanghai`）；
- JSON 线格式往返、未知字段/非法策略拒绝；
- CLI 退出码契约；
- `tzdbVersion` 出现在响应中且形如 `YYYYx`。

## 时区数据库版本可追溯性

- 运行时版本：响应中的 `tzdbVersion`，等价于
  `java.time.zone.ZoneRulesProvider.getVersions("UTC").lastKey()`。
  也可直接在 JDK 21 上验证：

  ```bash
  printf 'System.out.println(java.time.zone.ZoneRulesProvider.getVersions("UTC").lastKey());\n/exit\n' | jshell -s
  ```

  本机实测输出 `2026b`。
- 如需覆盖 JDK 自带版本，可在启动时用 `-Djava.time.zone.DefaultZoneRulesProvider=...`
  或随 JVM 升级 tzdata；本项目不内置私有 tzdata，避免与 JDK 版本漂移。

## 目录结构

```
src/main/java/com/example/dstexpand/
  Main.java                 CLI 入口（stdin/文件 -> stdout JSON）
  engine/DstExpander.java   展开、gap/overlap 判定与策略、排序去重
  model/                    请求/响应/枚举（不可变记录式模型）
  tz/TzdbVersion.java       tzdb 版本读取
  json/Json.java            Jackson 配置
src/test/java/...           JUnit 5 测试
samples/                    固定 JSON 请求样例
```
