# 区间集合代数（Interval Set Algebra）

时间/版本规则计算的**纯后端**服务：对区间集合执行**并集 ∪、交集 ∩、差集 −、补集 ¬**，
提供 JSON 输入输出，使用本地固定测试数据。支持开闭端点、无穷边界、单点区间；
结果规范化后表示唯一并稳定排序；反向区间被拒绝。

本项目**不是**预约或考勤系统，不做排班、不做前端。

- Java 21（仅用标准库 + Jackson 做 JSON）
- 构建：Maven（离线可构建，依赖均在本地 `~/.m2`）
- 测试：JUnit 5，50 个测试用例，含有限小域穷举与布尔代数恒等式验证

---

## 1. 构建与运行

```bash
# 编译 + 运行全部测试 + 覆盖率检查（行覆盖门槛 80%）+ 打包
mvn -o package

# 三种运行方式
java -jar target/interval-set-algebra-1.0.0.jar --demo                 # 内置固定数据
java -jar target/interval-set-algebra-1.0.0.jar examples/requests/01-time-all.json   # 请求文件
cat examples/requests/02-version-intersection.json | java -jar target/interval-set-algebra-1.0.0.jar   # 标准输入
```

`-o` 为 Maven 离线模式；联网时可去掉。

## 2. JSON 契约

请求：

```json
{
  "domain": "time",
  "timeZone": "Asia/Shanghai",
  "operation": "all",
  "a": [ {"lower": "2026-01-01T00:00:00", "upper": null, "lowerOpen": false} ],
  "b": [ {"lower": "2026-03-31T23:59:59", "upper": "2026-03-31T23:59:59"} ]
}
```

| 字段 | 说明 |
|---|---|
| `domain` | `time`（默认，ISO-8601 时间）或 `version`（整数版本号） |
| `timeZone` | 仅 time 域；不带偏移量的日期时间按此时区解释，缺省 `Asia/Shanghai` |
| `operation` | `union` / `intersection` / `difference`(A−B) / `complement`(¬A) / `all` |
| `a`、`b` | 区间数组；`complement` 只用 `a` |
| `lower`/`upper` | time 域为 ISO 字符串；version 域为整数；`null`/缺省/`"-inf"`/`"+inf"`/`"*"` 表示无穷 |
| `lowerOpen`/`upperOpen` | 是否开端，缺省 `false`（闭端） |

响应：`ok`、`domain`、`timeZone`、`operation`、`tzVersion`、`result`（或 `all` 时的 `results`）。
输出区间按下端点稳定升序、互不相交、已合并。出错时 `{"ok": false, "error": "..."}`，进程内处理不抛栈。

## 3. 区间语义

- 四种开闭组合：`[l,r]`、`[l,r)`、`(l,r]`、`(l,r)`。
- **单点区间** `[x,x]` 合法（两端皆闭），输出带 `"singleton": true`。
- **无穷边界**：`(-∞,+∞)` 为全集；无穷端点的“闭”没有意义，统一归一化为开。
- **反向区间** `lower > upper` 直接拒绝，返回 `ok=false`。
- **退化空区间** `(x,x)`、`[x,x)`、`(x,x]`、`(-∞,-∞)` 拒绝。
- **规范化（保证唯一）**：丢弃重复、按下端点排序、合并所有重叠或“相触且该点至少属于一方”的段；
  仅当两侧都排除相触点（右开 + 左开，如 `[1,3)` 与 `(3,5]`）时保留为两段。
  合并时外层区间决定外边界的开/闭（例如 `[1,4) ∪ [1,5] = [1,5]`，不会错误变成右闭取并）。

## 4. 时区数据库版本

服务本身不做时区换算，但在每个响应的 `tzVersion` 中如实记录数据版本基线：

```json
"tzVersion": {
  "jvmTzdbVersion": "2026b",
  "javaVersion": "21.0.12.1",
  "jvmDefaultZone": "Asia/Shanghai",
  "osTzdataVersion": "2026c",
  "zoneIdCount": 604
}
```

- `jvmTzdbVersion`：JVM 内置 tzdb（`java.time.zone.ZoneRulesProvider`）。
- `osTzdataVersion`：操作系统 tzdata（读取 `/usr/share/zoneinfo/+VERSION` 或 `tzdata.zi`，不可用为 `unknown`）。
- 当前机器两版本不同（JDK 21 自带 2026b，系统包为 2026c），属环境事实，如实记录。

## 5. 测试策略与验收对应

| 验收点 | 测试 |
|---|---|
| 穷举有限小域端点关系 | `ExhaustiveTest.exhaustivePairs`：域 `{0..7}` 上 **153** 个合法区间（4 种开闭 + 单点 + 四种无穷半线 + 全集）两两组合，**23,409** 对，每对在截断全集 `{-10..17}` 上与逐点 `BitSet` 真值核对 ∪∩−¬ |
| 多区间组合 | `exhaustiveSetsOfThree`：22 个采样区间的三元组合，**10,648** 组 |
| 代数恒等式 | `AlgebraLawsTest`：同一律/支配律/双重否定/补集律/交换/结合/分配/德摩根/吸收/幂等/`A−B=A∩¬B`；另枚举 `{0..4}` 上全部 32 个点集 |
| 单点区间 | `IntervalTest.singletonValid`、补集样例 03、恒等式集合 A/B/C 含 `[6,6]` |
| 无限范围 | 穷举区间含无穷半线与全集；`IntervalSetTest.infiniteMergeToUniverse`、补集测试 |
| 规范化唯一 | `normalizationIsUnique`（同一集合三种输入写法结果相同）、`idempotent`、`containmentMerge` |
| 稳定排序 | `outputStableSortedAndDisjoint`、`stableOrder` |
| 拒绝反向区间 | `IntervalTest.rejectsReverse`、JSON 样例 05 返回 `ok=false` |
| JSON / 时间解析 | `JsonServiceTest`（8）、`IntervalCodecTest`（6，含时区零点、Z/偏移一致、无穷词法） |
| CLI | `AppTest`（4）：stdin / 文件 / `--demo` / 空输入退出码 2 |
| 时区版本 | `TzVersionTest`（2） |

覆盖率（JaCoCo，`target/site/jacoco/index.html`）：**行 93.3%，分支 87.2%**，BUNDLE 行覆盖门槛 80%。

## 6. 实际运行记录

构建与测试命令均在本机实际执行（2026-09-25，Asia/Shanghai）。

```bash
$ mvn -o package
Tests run: 50, Failures: 0, Errors: 0, Skipped: 0
BUILD SUCCESS
```

```bash
$ java -jar target/interval-set-algebra-1.0.0.jar examples/requests/05-reverse-rejected.json
{"ok":false,"error":"反向区间被拒绝: lower 9 > upper 2"}
```

5 个请求样例（`examples/requests/`）的实际输出已保存在 `examples/expected/`，
重新构建后逐字节 `diff` 无变化（输出稳定可复现）。

**开发过程中如实记录的未通过项（均已修复，最终全绿）：**

1. 初版规范化把“相触”合并条件写成“两侧都闭才合并”，且包含关系合并时右边界误用 OR，
   导致 `[1,4) ∪ [1,3]` 错成 `[1,4]`、`(-∞,1) ∪ [1,4)` 未合并。穷举测试与恒等式测试
   一次性暴露（首次运行 40 个测试 9 处失败）。已改为“相触点至少属于一方即合并；
   外边界由更外层区间决定”，重跑通过。
2. 修复后另有 2 处**测试期望本身写错**（对开闭相邻语义判断错误，以及时区毫秒差符号写反），
   已按正确语义修正，非产品代码缺陷。
3. 最终状态：50/50 通过，无未通过项；JaCoCo 行覆盖 93.3% ≥ 80% 门槛。

## 7. 目录结构

```
src/main/java/dev/intervals/
  model/    Endpoint、Edge、Interval、IntervalSet（规范化）
  domain/   IntervalAlgebra（∪ ∩ − ¬）
  json/     Domain、IntervalCodec、JsonService
  tz/       TzVersion（时区数据库版本）
  App.java  CLI 入口
src/test/   对应 JUnit 5 测试 + algebra/ 穷举与恒等式
examples/   requests/ 5 个请求样例；expected/ 实际输出
```

## 8. 范围说明

只做区间集合代数与 JSON 计算后端。不包含预约、考勤、排班、持久化、网络服务、前端。
