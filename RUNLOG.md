# RUNLOG — 实际运行记录

记录在构建机上的真实命令与结果。日期：2026-09-25。

## 0. 环境

| 项 | 值 |
|---|---|
| OS | Linux 6.8.0-90-generic (Ubuntu 24.04) |
| JDK | OpenJDK 21.0.12.1（Ubuntu） |
| Maven | 3.8.7 |
| JRE 内置 tzdata | **2026b**（`ZoneRulesProvider.getVersions`） |
| OS tzdata | **2026c**（`/usr/share/zoneinfo/tzdata.zi`） |
| 可用时区数 | 604 |
| 依赖 | JUnit Jupiter 5.10.3、Jackson Databind 2.17.2（均在本地 `~/.m2`，故用 `-o` 离线构建） |

## 1. 干净构建 + 全部测试

命令：

```bash
mvn -o clean test
```

结果（摘要）：

```
Tests run: 67, Failures: 0, Errors: 0, Skipped: 0
BUILD SUCCESS   EXIT=0
```

逐类测试计数（全部通过）：

| 测试类 | 数量 |
|---|---|
| engine.TimeDomainTest | 4 |
| engine.IntervalServiceTest | 11 |
| engine.ExpressionEvaluatorTest | 10 |
| engine.VersionDomainTest | 2 |
| model.CutTest | 4 |
| model.IntervalTest | 8 |
| cli.MainTest | 9 |
| json.IntervalConverterTest | 3 |
| fixed.DatasetRepositoryTest | 3 |
| tz.TimeZoneInfoTest | 4 |
| algebra.BoundaryRelationExhaustiveTest | 2 |
| algebra.IntervalAlgebraIdentityTest | 5 |
| algebra.IntervalAlgebraExhaustiveTest | 2 |
| **合计** | **67** |

覆盖率（`mvn -o clean test` 产生的 JaCoCo，`target/site/jacoco/index.html`）：

```
classes=26  instruction=90.2%  branch=80.6%  line=88.8%
```

三项均高于 80% 下限。

## 2. 打包

命令：

```bash
mvn -o package -DskipTests
ls -1 target/*.jar
```

结果：

```
target/interval-set-algebra-1.0.0.jar            (fat jar, ~2.35 MB, 可执行)
target/original-interval-set-algebra-1.0.0.jar   (瘦 jar)
BUILD SUCCESS
```

## 3. 8 个请求样例实跑

命令：`java -jar target/interval-set-algebra-1.0.0.jar --file samples/<f>`

| 样例 | 退出码 | success | 结果区间数 | empty | error |
|---|---|---|---|---|---|
| 01-union-time.json | 0 | true | 2 | false | — |
| 02-intersection-time.json | 0 | true | 1 | false | — |
| 03-difference-dataset.json | 0 | true | 1 | false | — |
| 04-complement-time.json | 0 | true | 2 | false | — |
| 05-version-nested.json | 0 | true | 2 | false | — |
| 06-dst-zoned-time.json | 0 | true | 0 | **true** | — |
| 07-singleton-and-unbounded.json | 0 | true | 1 | false | — |
| 08-error-reversed.json | **1** | **false** | — | — | **reversed_interval** |

关键结果（`result.intervals`，已逐一人工核验）：

- **01 并集**：`[2024-01-01T00:00Z, 2024-02-15T00:00Z]`、`[2024-03-15T08:00Z, 2024-03-20T00:00Z)`
  —— A、B 在 `2024-02-01T00:00Z` 处闭/闭相接被合并；乱序输入仍稳定升序输出。
- **02 交集**：`[2024-03-01T00:00Z, 2024-06-01T00:00Z]`。
- **03 差集（固定数据集 time-unbounded，B−A）**：`(2024-01-01T00:00Z, 2025-06-01T00:00Z]`
  —— 两端各被 A 的闭端 / 开端削去，下端开、上端闭。
- **04 补集**：`(-∞, 2024-01-01T00:00Z)` 与 `(2024-12-31T00:00Z, +∞)`，两端点为开，含无穷边界。
- **05 version 嵌套（A ∪ (B∩C)）**：`[v01,v05)` 与单点 `[v07,v07]`。
- **06 DST**：`[]`（empty=true）。A 的巴黎本地窗口归一到 UTC 为 `[2024-03-30T23:30Z, 2024-03-31T01:30Z]`，
  B 为 `(2024-03-31T01:30Z, +∞)`；唯一可能接触点 `01:30Z` 在 B 中为开端点被排除，故交集为空——符合开闭端点语义。
- **07 单点 + 无界**：`(-∞, v07]`（单点 `{v07}` 与 `(-∞,v07)` 合并）。
- **08 反向区间**：退出码 1，响应
  `error.code = reversed_interval`，
  `message = "reversed interval: lower endpoint 2024-12-31T00:00:00Z is greater than upper endpoint 2024-01-01T00:00:00Z"`。

## 4. stdin / 子命令 / 确定性

```bash
cat samples/02-intersection-time.json | java -jar target/...jar        # 退出 0，success=true
java -jar target/...jar timezone
# {"jreTzDataVersion":"2026b","osTzDataVersion":"2026c",
#  "javaVersion":"21.0.12.1","javaVendor":"Ubuntu","availableZoneCount":604}
java -jar target/...jar datasets        # 列出 4 个内置数据集，退出 0
java -jar target/...jar help            # 退出 0
echo '' | java -jar target/...jar       # 非法 JSON → success=false, invalid_request，退出 1
```

确定性 / 稳定排序：对样例 01 连跑两次重定向到文件后 `diff`，输出逐字节相同：

```
DETERMINISTIC: identical outputs
```

## 5. 未通过项

**最终状态：无未通过项**（67/67 测试通过，8 个样例全部符合预期）。

开发过程中首次 `mvn -o test` 曾出现失败，均已定位并修复，如实记录如下：

1. **编译错误（3 处，已修复）**
   - `pom.xml`：shade 插件的 `<transformers>` 误放在 `<configuration>` 外 → 移入 `configuration`。
   - `Interval.all()`：泛型方法内菱形推断失败（推断为 `Object`）→ 加显式类型见证 `Cut.<T>negInfinity()`。
   - `Main`：对 `IOException` 调用了不存在的 `getOriginalMessage()` → 改捕 `JsonProcessingException`。
2. **测试期望写错，非生产代码缺陷（2 处，已更正断言）**
   - `[1,2] ∪ (2,3]` 正确结果是 `[1,3]`（闭端相接，3 被包含），原断言误写为 `[1,3)`。
   - 差集 `A−B` 第二段下界应为 `(2024-07-01, …)`（B 闭端之后），原断言误取 `2024-06-01`。
3. **服务层设计问题（1 处，已修复）**
   - `IntervalService.process` 最初对未知域 / 反向区间直接抛异常，导致服务层错误信封用例以 `IntervalException` 错误形式失败；
     已改为在边界统一捕获 `IntervalException` 并返回 `success=false` 信封（CLI 仍据此返回退出码 1）。

修复后重跑 `mvn -o clean test` 全绿，结果即第 1 节。

## 6. 复现实验的命令一览

```bash
mvn -o clean test
mvn -o package -DskipTests
java -jar target/interval-set-algebra-1.0.0.jar --file samples/01-union-time.json
java -jar target/interval-set-algebra-1.0.0.jar datasets
java -jar target/interval-set-algebra-1.0.0.jar timezone
# 覆盖率报告：target/site/jacoco/index.html
```
