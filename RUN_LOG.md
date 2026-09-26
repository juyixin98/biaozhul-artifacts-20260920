# 运行记录（RUN_LOG）

> 执行时间：2026-09-25，平台：Linux 6.8.0 x86_64 / Ubuntu 24.04
> JDK：OpenJDK 21.0.12.1；Maven：3.x（`/usr/bin/mvn`）；tzdb：**2026b**

## 1. 构建

```bash
mvn -q package
```

结果：**BUILD 成功**（退出码 0），产物 `target/dst-rule-expander-1.0.0.jar`
（shade 胖 jar，约 2.4 MB）。

构建过程中实际遇到并修复的问题（如实记录）：

1. Maven 默认 `maven-compiler-plugin:3.1` 使用 `--source 5`，JDK 21 拒绝
   （`Source option 5 is no longer supported`）→ 在 `pom.xml` 显式锁定
   `maven-compiler-plugin:3.13.0` + `<release>21</release>`。
2. 代码误用 `ZoneOffsetTransition#getDateTime()`（该方法不存在）→ 改为
   `getDateTimeBefore()`。

## 2. 自动化测试

```bash
mvn -q test
```

结果：**全部通过，0 失败、0 错误、0 跳过**，共 28 个用例：

| 测试类 | 用例数 | 结果 |
|--------|--------|------|
| `DstExpanderTest` | 19 | 全部通过 |
| `MainTest` | 5 | 全部通过 |
| `JsonRoundTripTest` | 4 | 全部通过 |

覆盖内容：春季跳时 4 种策略 + 缺口边界时刻；秋季回拨 4 种策略；跨年区间；
UTC 排序；规则级/结果级去重及 `stats` 计数；无 DST 地区；JSON 线格式往返；
未知字段与非法策略拒绝；CLI 退出码；响应携带 tzdb 版本。

未通过项：无。

## 3. 样例实际运行

```bash
for f in spring-forward fall-back cross-year gap-error; do
  java -jar target/dst-rule-expander-1.0.0.jar samples/$f.json
done
```

完整响应已保存在 `samples/output/`，结果摘要：

| 样例 | 退出码 | 关键验证 |
|------|--------|----------|
| `spring-forward.json` | 0 | 2026-03-08 02:30 不存在 → `GAP_LATER`，UTC `07:30Z`（与前一天相同 UTC，落地当地 03:30 EDT）；03-07 为 `07:30Z`，03-09 恢复 `06:30Z` |
| `fall-back.json` | 0 | 2026-11-01 01:30 两次出现 → `OVERLAP_EARLIER` 取 `05:30Z`（EDT 第一次） |
| `cross-year.json` | 0 | 2026-12-31 → 2027-01-02 共 3 天 × 2 规则 = 6 条，均 NORMAL，EST 下 00:30→05:30Z、09:00→14:00Z |
| `gap-error.json` | 2 | 命中缺口 + `ERROR` → JSON `error` 指明切换点 02:00，退出码 2 |

补充抽查：

- `overlapPolicy=LATER`：2026-11-01 01:30 → `06:30Z`，`OVERLAP_LATER`，符合预期；
- stdin 管道输入（`cat ... | java -jar ...`）正常；
- 每个响应均含 `"tzdbVersion" : "2026b"`，时区数据库版本可追溯；
- `ZoneRulesProvider.getVersions("UTC").lastKey()` 经 jshell 独立确认为 `2026b`。

## 4. 已知限制

- 未内置 HTTP 层（需求为纯后端 + JSON 输入输出），以 CLI/stdin 提供服务边界；
- 最大展开区间 3662 天，防止误用导致超大输出；
- 不内置私有 tzdata，跟随 JDK；跨版本复现需固定 JDK 版本并以响应中的
  `tzdbVersion` 为准。
