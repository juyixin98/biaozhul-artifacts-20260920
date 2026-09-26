# 运行记录（RUNLOG）

如实记录本项目开发与验收过程中实际执行的命令、结果与未通过项。未删除任何失败记录。

环境：OpenJDK 21.0.12.1、Apache Maven 3.8.7、Linux 6.8.0-90-generic。
时区数据库版本：JDK 内置 IANA tzdb **2026b**（由 `ZoneRulesProvider.getVersions("UTC").lastKey()` 读取，每个响应的 `tzdbVersion` 字段输出）。

## 1. 编译与测试

### 第一次构建（失败，已修复）

```
mvn -q -B test
```

结果：**失败**。默认 maven-compiler-plugin 3.1 不认识 `release 21`：

```
[ERROR] Source option 5 is no longer supported. Use 8 or later.
[ERROR] Target option 5 is no longer supported. Use 8 or later.
```

修复：在 `pom.xml` 显式声明 `maven-compiler-plugin` 3.13.0。

### 第二次构建（1 个测试失败，已修复）

```
mvn -q -B test
```

结果：**44 个测试中 1 个失败**：

```
[ERROR] RequestParserTest.rejectsNonPositiveCount:153 expected: <INVALID_COUNT> but was: <INVALID_INTEGER>
```

原因：JSON 解析层对 `count: 0` 先抛出通用的 `INVALID_INTEGER`，而领域语义应返回 `INVALID_COUNT`。
修复：`RequestParser.optionalPositiveInt` 增加按字段传入错误码的参数。

### 第三次构建（通过）

```
mvn -B test
```

结果：**全部通过，无未通过项**：

```
Tests run: 44, Failures: 0, Errors: 0, Skipped: 0
BUILD SUCCESS
```

各测试类：`RecurrenceExpanderTest` 18、`RecurrenceRuleTest` 7、`WeeklyExpansionTest` 4、
`RequestParserTest` 8、`MainEndToEndTest` 7。

### 评审后加固（第四次构建，通过）

代码评审发现两点并当场修复：

1. **健壮性缺陷**：`dtStart` 接近 `LocalDate.MIN` 且窗口在遥远未来时，逐日迭代次数接近无限，
   会造成请求挂死。修复：展开器增加周期数兜底（5,000,000），超出抛 `EXPANSION_TOO_LARGE`，
   并新增回归测试 `rejectsPathologicallyLongSeriesBeforeWindow`。
2. **冗余代码**：`anchorAfterEnd` 两个分支完全相同，删除，循环条件直接使用 `cursor.isAfter(hardEnd)`。

```
mvn -B test
```

结果：**45/45 通过，无未通过项**（新增 1 个兜底测试）。

## 2. 打包

```
mvn -q -B package -DskipTests
```

结果：成功，产出 `target/recurrence-expander-1.0.0.jar`（约 2.3 MB，含 Jackson 的可执行 shaded jar）。

## 3. 样例请求实际运行

```
for f in examples/request-*.json; do java -jar target/recurrence-expander-1.0.0.jar "$f"; done
```

全部 7 个样例运行成功（`request-limit-exceeded.json` 按预期返回错误，退出码 2）。
实际响应已保存到 `examples/output/*.response.json`。要点：

| 样例 | 退出码 | 结果 |
|---|---|---|
| request-daily | 0 | 5 个日期（COUNT=5） |
| request-monthly-31-skip | 0 | 7 个日期，2025 年无 31 日的月份被跳过 |
| request-monthly-31-clamp | 0 | 12 个日期，2 月回落到 2025-02-28 |
| request-leap-year | 0 | 6 个日期，含 2024-02-29，COUNT=6 截断 |
| request-weekly-byday | 0 | 6 个日期，隔周 MO/WE/FR，COUNT 与 UNTIL 双限制下 COUNT 先生效 |
| request-empty-window | 0 | `count: 0`，空数组 |
| request-limit-exceeded | 2 | `EXPANSION_LIMIT_EXCEEDED` |

## 4. 补充手工验证（stdin）

```
cat <<'EOF' | java -jar target/recurrence-expander-1.0.0.jar
{ "requestId": "both-limits",
  "rule": { "dtStart": "2026-01-01", "freq": "DAILY", "count": 100, "until": "2026-01-04" },
  "windowStart": "2026-01-01", "windowEnd": "2026-12-31" }
EOF
```

结果：退出码 0，输出 4 个日期（2026-01-01..2026-01-04）——COUNT=100 与 UNTIL 同时存在时 UNTIL 先生效，符合预期。

```
... "maxExpansions": 10001 ...
```

结果：退出码 2，`INVALID_MAX_EXPANSIONS: maxExpansions must not exceed 10000`——超过硬上限被拒绝，符合预期。

## 5. 未通过项汇总

- 首次编译失败（compiler 插件版本过旧）——已修复。
- `RequestParserTest.rejectsNonPositiveCount` 一次失败（错误码语义不符）——已修复。
- 评审发现病态超长序列可致挂死——已修复并加回归测试。
- 最终状态：**45/45 测试通过，7/7 样例按预期运行，无遗留未通过项**。
