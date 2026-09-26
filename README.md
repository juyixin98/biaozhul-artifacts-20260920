# Interval Set Algebra（区间集合代数）纯后端服务

用 Java 21 实现的**时间 / 版本规则计算后端**。对区间集合执行**并集（union）、交集（intersection）、差集（difference）、补集（complement）**，支持**开 / 闭端点**与**无穷边界**，输出**规范化后唯一**且**稳定排序**的结果，并在构造时**拒绝反向区间**。

- 纯后端：只有命令行（stdin / 文件 JSON 输入，stdout JSON 输出），**无任何前端、无 HTTP 服务、无预约或考勤逻辑**。
- 固定测试数据：随 jar 打包的 classpath 资源 `data/datasets.json`，不访问网络与外部数据库。
- 记录时区数据库版本：每次响应都带 JRE 内置 tzdata 版本、操作系统 tzdata 版本与 Java 运行时信息。

---

## 1. 环境与构建

要求 JDK 21 与 Maven 3.8+。

```bash
mvn -o package         # 离线构建（依赖已在本地仓库）；会先跑全部测试
```

产物：`target/interval-set-algebra-1.0.0.jar`（shade 后的可执行 fat jar）。

常用命令：

```bash
mvn -o test             # 只跑测试
mvn -o package -DskipTests   # 只打包不跑测试
java -jar target/interval-set-algebra-1.0.0.jar help
```

> 注：本环境依赖（JUnit 5.10.3、Jackson 2.17.2、各 Maven 插件）均已存在于本地 `~/.m2`，故使用 `-o` 离线构建。去掉 `-o` 亦可从 Maven 中央仓库正常构建。

---

## 2. 命令行用法

```bash
# 从 stdin 读一个请求 JSON
java -jar target/interval-set-algebra-1.0.0.jar < samples/01-union-time.json

# 从文件读
java -jar target/interval-set-algebra-1.0.0.jar --file samples/04-complement-time.json

# 列出内置固定数据集
java -jar target/interval-set-algebra-1.0.0.jar datasets

# 打印时区数据库版本信息
java -jar target/interval-set-algebra-1.0.0.jar timezone
```

退出码：`0` 成功；`1` 请求被拒绝 / 非法（响应体 `success=false`）；`2` 命令行用法错误。

---

## 3. 请求 / 响应 JSON 契约

### 3.1 请求

```json
{
  "domain": "time",
  "dataset": "time-unbounded",
  "sets": {
    "A": [ { "lower": "...", "lowerOpen": false, "upper": "...", "upperOpen": true } ]
  },
  "expression": { "op": "union", "left": { "set": "A" }, "right": { "set": "B" } }
}
```

| 字段 | 说明 |
|---|---|
| `domain` | `"time"` 或 `"version"`，必填 |
| `dataset` | 可选，引用内置固定数据集的 id；其内的命名集合进入作用域 |
| `sets` | 可选，显式提供命名集合（会与数据集同名键合并、覆盖） |
| `expression` | 必填，表达式树 |

区间对象字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `lower` / `upper` | string 或 null | 下端点 / 上端点；`null` 或缺省表示该端为无穷 |
| `lowerOpen` | bool | `true` = 开（排除下端点），`false`/缺省 = 闭（包含） |
| `upperOpen` | bool | `true` = 开（排除上端点），`false`/缺省 = 闭 |

端点记号：

- **time 域**：ISO-8601，接受 instant（`2024-03-31T22:00:00Z`）、偏移（`...+02:00`）、带时区（`...+02:00[Europe/Paris]`）；统一归一到 **UTC 时间线**的 `Instant`，输出规范为 UTC、尾随 `Z`。
- **version 域**：任意非空字符串，按**固定词法序**（Unicode 码元序）比较。不做语义化版本解析，需要 `v10 > v9` 时请用零填充记号（`v09`、`v10`）。

表达式节点三选一：

- 叶子：`{ "set": "A" }`
- 一元：`{ "op": "complement", "arg": <node> }`
- 二元：`{ "op": "union", "left": <node>, "right": <node> }`

操作符及别名：

| 规范名 | 别名 | 元数 | 含义 |
|---|---|---|---|
| `union` | `or` | 2 | A ∪ B |
| `intersection` | `and` | 2 | A ∩ B |
| `difference` | `minus`, `except` | 2 | A − B |
| `complement` | `not` | 1 | 全集 − A（绝对补集，含无穷区间） |

### 3.2 响应

成功：

```json
{
  "success": true,
  "domain": "time",
  "dataset": null,
  "result": {
    "intervals": [ { "lower": null, "lowerOpen": false, "upper": "...", "upperOpen": true } ],
    "intervalCount": 1,
    "empty": false
  },
  "error": null,
  "timezone": {
    "jreTzDataVersion": "2026b",
    "osTzDataVersion": "2026c",
    "javaVersion": "21.0.12.1",
    "javaVendor": "Ubuntu",
    "availableZoneCount": 604,
    "sampleZones": [ "UTC", "Europe/Paris", "..." ]
  }
}
```

失败：`success=false`，`result=null`，`error={ "code", "message" }`；`timezone` 字段始终存在。

错误码：`invalid_request`、`unknown_domain`、`unknown_dataset`、`unknown_set_ref`、`unknown_operation`、`invalid_endpoint`、`reversed_interval`、`invalid_expression`、`arity_mismatch`、`internal_error`。

---

## 4. 规范化与唯一性（关键语义）

内部用**切割（Cut）全序**表示端点。对任意值 `v`：

```
-∞  <  BELOW(v)（闭端点）  <  v  <  ABOVE(v)（开端点）  <  +∞
```

四种经典端点形式由此被精确表达：`[a,b]=(BELOW(a), ABOVE(b))`、`(a,b)=(ABOVE(a), BELOW(b))`、`[a,b)`、`(a,b]`。

所有运算走**同一条切割扫描线**：把所有操作数的端点收集为排序去重的切割序列（恒含 ±∞），逐原子区域判定各操作数是否覆盖，再用布尔谓词决定并 / 交 / 差 / 补。因此：

- 相邻覆盖自动合并：`[1,2] ∪ (2,3] = [1,3]`；
- 单点区间保留为 `[v,v]`，而 `(v,v)`、`[v,v)`、`(v,v]` 为空被丢弃；
- 被挖去单点会正确分裂：`[1,3] − {2} = [1,2) ∪ (2,3]`；
- 无穷运行段输出 `null` 端点；
- 结果恒为**互不相交、无缝则合、按下端点排序**的唯一规范表示，故相等性与 JSON 序列化稳定、确定。

**反向区间**（有限下端点值 > 上端点值，如 `[5,2]`）在 `Interval` 构造时立即抛出 `reversed_interval`；含无穷端的区间永不构成反向。

---

## 5. 时区数据库版本记录

`timezone` 字段同时记录两个独立来源（二者可能不同，本机即不同）：

- **JRE tzdata**：JDK 内嵌、供 `java.time` 使用的 IANA 库，经 `ZoneRulesProvider.getVersions(...)` 读取，本机为 **2026b**；
- **OS tzdata**：操作系统时区库，Linux 下解析 `/usr/share/zoneinfo/tzdata.zi` 的 `# version` 头，本机为 **2026c**；文件缺失时降级为 `"unknown"`。

不调用任何外部命令；另记录 Java 版本、厂商、可用时区数量与若干样例时区。`timezone` 子命令可单独查看。

---

## 6. 内置固定数据集（`src/main/resources/data/datasets.json`）

| id | domain | 用途 |
|---|---|---|
| `time-adjacency` | time | 相邻 / 重叠维护窗口，混合开闭端点，验证合并 |
| `time-dst-europe-2024` | time | 跨欧洲夏令时跳变的本地时区窗口，验证 UTC 归一 |
| `time-unbounded` | time | 半开 / 无界可用区间，验证补集与差集 |
| `version-rollout` | version | 零填充版本标签上的分阶段可用性，含单点 |

---

## 7. 请求样例

`samples/` 目录：

| 文件 | 演示点 |
|---|---|
| `01-union-time.json` | time 域并集，相邻合并、稳定排序 |
| `02-intersection-time.json` | time 域交集 |
| `03-difference-dataset.json` | 引用固定数据集做差集（含无界） |
| `04-complement-time.json` | 绝对补集，输出两端无穷 |
| `05-version-nested.json` | version 域嵌套表达式 |
| `06-dst-zoned-time.json` | 带时区端点归一 UTC，开闭边界取空 |
| `07-singleton-and-unbounded.json` | 单点区间与无界区间合并 |
| `08-error-reversed.json` | 反向区间被拒绝（`success=false`） |

---

## 8. 自动化测试与验收对照

运行 `mvn -o test`（结果见 `RUNLOG.md`）。共 **67** 个测试，`mvn -o clean test` 的 JaCoCo 覆盖率：**指令 90.2% / 分支 80.6% / 行 88.8%**（报告 `target/site/jacoco/index.html`），均高于 80% 下限。

验收要求 → 测试：

- **穷举有限小域端点关系**：`BoundaryRelationExhaustiveTest` 枚举 3 值宇宙的全部 8×8 切割对，分类断言「反向拒绝 / 退化空 / 非空且为连续原子段」，并对所有非空区间两两校验并交差；`IntervalAlgebraExhaustiveTest` 在 7 个原子单元格（2³=128 个点集）上做 128×128 全组合，用独立的 `BitSet` 神谕交叉验证。
- **代数恒等式**：`IntervalAlgebraIdentityTest` 穷举验证德摩根、分配律、双重补 involution、A−B=A∩Bᶜ、吸收 / 恒等 / 零元 / 排中 / 矛盾、交换律。
- **单点区间**：`IntervalTest.singletonIntervalIsExactlyOnePoint`、服务层 `singletonVersionPointSurvives`。
- **无限范围**：`unboundedIntervalsContainWithoutBound`、补集服务测试、穷举宇宙首尾无界单元格。
- **稳定排序 / 唯一**：`outputIsNormalizedAndSorted`、交换律的规范化相等、CLI 双跑 `diff` 确定性校验。
- **拒绝反向区间**：模型与服务 / CLI 多层测试。

神谕 `FiniteUniverse`（测试专用）是与扫描线完全独立的实现：把有限宇宙切成 `(-∞,v1), {v1}, (v1,v2), …, (vk,+∞)` 共 `2k+1` 个原子单元格，集合即 BitSet，集合运算用 `| / & / and-not / flip`，用以独立印证生产算法。

---

## 9. 代码结构

```
src/main/java/com/example/intervals/
  model/      Cut, Interval, IntervalSet      # 切割全序、不可变区间与集合
  algebra/    IntervalAlgebra                 # 切割扫描线：并交差补 + 规范化
  engine/     Domain, TimeDomain, VersionDomain, Domains
              Operation, ExpressionEvaluator, IntervalService
  json/       请求/响应/区间 DTO + Jackson、IntervalConverter
  fixed/      DatasetRepository（classpath 固定数据）
  tz/         TimeZoneInfo（JRE/OS tzdata 版本）
  cli/        Main（stdin/文件/子命令）
src/main/resources/data/datasets.json         # 内置固定测试数据
src/test/java/...                             # 67 个测试，含穷举与恒等式
samples/                                      # 8 个请求样例
```

设计遵循不可变（运算返回新对象，不修改输入）、显式错误处理与边界校验，核心类均远小于 800 行。
