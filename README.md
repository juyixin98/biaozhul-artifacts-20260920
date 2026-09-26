# 事件有效顺序重建(event-order-reconstruction)

纯后端 Java 服务:给定一组事件、显式偏序依赖、可选时间区间与可选版本号,
重建有效事件顺序。JSON 输入 / JSON 输出,本地固定测试数据,无前端、无数据库,
不做预约或考勤。

## 语义规则(重要)

1. **排序只由显式依赖决定。** `dependencies` 中的 `before -> after` 是唯一的排序
   约束来源,构成偏序。输出顺序是该偏序的**字典序最小线性扩展**(Kahn 算法 +
   按事件 id 的最小堆),因此完全确定,与输入顺序、哈希顺序无关。
2. **时间区间是可行性约束,不是排序约束。** 每个事件可带时间窗
   `[earliest, latest]`(两端均可缺省)。对每条依赖 `u -> v` 要求
   `t(u) <= t(v)`,且每个事件必须落在自己的时间窗内。引擎按拓扑序做最早时间
   传播(PERT 式):若传播到某事件的下界超过其 `latest`,则约束不可满足,
   并沿传播路径输出冲突链。
3. **时间区间重叠不制造因果关系。** 两个事件时间窗重叠(或完全不重叠)都不会
   产生任何依赖边;没有显式依赖时,两个方向都是合法顺序(见
   `samples/04-overlap-no-causality.json`,输出 `validOrderCount = 2`)。
4. **版本规则(可选)。** `options.enforceVersionOrder = true` 时,若依赖两端
   事件都带 `version`,要求 `version(before) <= version(after)`,违反即
   `VERSION_CONTRADICTION`。任一端无版本号则该边豁免。版本号只用于校验,
   不产生排序边。
5. **冲突检测顺序:循环 → 时间 → 版本。** 存在循环依赖时不做时间/版本检查
   (无拓扑序可言),只报告循环。
6. **计数与枚举。** `validOrderCount` 为合法顺序总数:事件数 ≤ 20 时用子集 DP
   精确计算,超过则返回 `-1`(不计算)。`enumeratedOrders` 按字典序列出具体
   顺序,上限 `options.maxEnumeratedOrders`(默认 100,最大 10000)。
7. **时间戳格式。** `earliest` / `latest` 必须是带偏移量的 ISO-8601
   (如 `2026-01-01T09:00:00Z` 或 `2026-01-01T17:00:00+08:00`);不带偏移量的
   本地时间会被拒绝,以保证结果确定。所有计算在 `Instant` 上进行,时区不影响
   结果。

## 时区数据库版本

每条响应都记录 `tzdbVersion` —— 运行引擎的 JDK 内置 IANA 时区数据库版本
(经 `java.time.zone.ZoneRulesProvider` 读取)。该版本仅作记录与可复现性声明,
不参与任何计算。

本仓库交付时的实测环境:

| 项 | 值 |
|---|---|
| JDK | OpenJDK 21.0.12.1 |
| JDK 内置 tzdb(响应中的 `tzdbVersion`) | **2026b** |
| 操作系统 tzdata 包(仅供参考) | 2026c-0ubuntu0.24.04.1 |
| 系统默认时区 | Asia/Shanghai |

## 输入格式

```json
{
  "requestId": "可选,回显",
  "events": [
    {
      "id": "a",
      "earliest": "2026-01-01T09:00:00Z",
      "latest": "2026-01-01T10:00:00Z",
      "version": 1
    }
  ],
  "dependencies": [
    { "before": "a", "after": "b", "reason": "可选备注" }
  ],
  "options": {
    "enforceVersionOrder": false,
    "maxEnumeratedOrders": 100
  }
}
```

- `events`:至少一个;`id` 必填且唯一;`earliest` / `latest` / `version` 均可缺省。
- `dependencies`:可缺省或为空;端点必须是已声明事件;允许自环(会报为循环);
  重复边报错。
- 未知字段、错误类型(如 `"id": 42`)都会被拒绝并返回错误信封。

## 输出格式

```json
{
  "requestId": "回显",
  "tzdbVersion": "2026b",
  "satisfiable": true,
  "order": ["a", "b"],
  "validOrderCount": 1,
  "enumeratedOrders": [["a", "b"]],
  "dependencyChecks": [
    { "before": "a", "after": "b", "satisfied": true }
  ],
  "conflicts": []
}
```

- `satisfiable = false` 时 `order` 为 `null`,`conflicts` 非空,每条冲突是
  **最小可读冲突链**:
  - `CYCLE`:循环路径,首尾同一事件,如 `["a","b","c","a"]`;对每个非平凡强
    连通分量,报告经过其字典序最小节点的最短环。
  - `TIME_CONTRADICTION`:时间下界传播路径,如 `["a","b","c"]`,`detail`
    说明哪条链把哪个下界推过了哪个 `latest`;单事件自身 `earliest > latest`
    时链长为 1。
  - `VERSION_CONTRADICTION`:`[before, after]` 二元链。
- `dependencyChecks` 逐条回显每条依赖的验证结果(可满足时全部 `satisfied: true`)。

## 构建与运行

```bash
# 运行测试(57 个)
mvn test

# 打包可执行 jar(含依赖)
mvn package

# 从文件读取请求
java -jar target/event-order-reconstruction-1.0.0.jar samples/01-multiple-legal-orders.json

# 从标准输入读取
cat samples/02-circular-dependency.json | java -jar target/event-order-reconstruction-1.0.0.jar
```

退出码:`0` = 已处理(包括不可满足,响应 JSON 照常输出);`2` = 请求非法
(输出 `{"error": "...", "tzdbVersion": "..."}` 错误信封);`1` = I/O 或工具故障。

## 请求样例

| 文件 | 场景 | 预期 |
|---|---|---|
| `samples/01-multiple-legal-orders.json` | 多合法顺序 | `validOrderCount=3`,输出字典序最小序 |
| `samples/02-circular-dependency.json` | 循环依赖 | `CYCLE` 冲突链 |
| `samples/03-time-contradiction.json` | 矛盾时间约束 | `TIME_CONTRADICTION` 冲突链(跨 3 事件传播) |
| `samples/04-overlap-no-causality.json` | 时间窗重叠、无依赖 | 可满足,2 种顺序,不制造因果 |
| `samples/05-version-contradiction.json` | 版本规则矛盾 | `VERSION_CONTRADICTION` 冲突链 |

## 验收标准映射

| 验收项 | 实现与验证 |
|---|---|
| 循环依赖 | `CycleFinder`(Tarjan SCC + 分量内最短环);`CycleFinderTest`、`OrderEngineTest.cyclicDependenciesAreUnsatisfiableWithMinimalChain`、样例 02 |
| 矛盾时间约束 | `TimeWindowChecker`(最早时间传播);`TimeWindowCheckerTest`、样例 03 |
| 多合法顺序 | `LinearExtensions`(子集 DP 计数 + 字典序枚举);`LinearExtensionsTest`、`OrderEngineTest.multipleLegalOrdersAreCountedAndEnumerated`、样例 01 |
| 验证每条依赖 | 响应 `dependencyChecks` 逐条回显;`OrderEngineTest.everyDependencyIsVerified` |
| 最小可读冲突链 | 环:经过分量最小节点的最短环;时间:实际下界传播路径;版本:二元链;上述各测试断言链内容 |
| 时间区间重叠不制造因果 | `OrderEngineTest.overlappingWindowsDoNotCreateCausality` / `disjointWindowsDoNotCreateCausalityEither`、样例 04 |
| 记录时区数据库版本 | `TzdbVersion` + 每条响应的 `tzdbVersion` 字段;`OrderEngineTest.detectedTzdbVersionLooksLikeIanaRelease` |

## 项目结构

```
src/main/java/com/example/eventorder/
  Main.java                     CLI 入口(文件或 stdin -> stdout)
  model/                        请求/响应 DTO(record)
  json/JsonIO.java              JSON 边界(严格解析)
  engine/
    RequestValidator.java       输入校验(系统边界)
    DependencyGraph.java        依赖图(排序邻接表,确定性遍历)
    CycleFinder.java            循环检测 + 最短环链
    TimeWindowChecker.java      时间窗可行性(PERT 传播)+ 冲突链
    VersionRuleChecker.java     版本规则
    TopologicalOrderer.java     字典序最小拓扑序(Kahn + 最小堆)
    LinearExtensions.java       线性扩展计数(子集 DP)与枚举
    OrderEngine.java            编排
    TzdbVersion.java            tzdb 版本探测
src/test/java/...               57 个 JUnit 5 测试(固定测试数据 TestFixtures)
samples/                        5 个请求样例
```

## 实际运行记录

以下为本仓库交付前在交付环境(Linux 6.8.0-90-generic,OpenJDK 21.0.12.1,
Maven 3.8.7)的真实执行结果:

```text
$ mvn -o test
...
Tests run: 57, Failures: 0, Errors: 0, Skipped: 0
BUILD SUCCESS

$ mvn -o -q package -DskipTests && ls target/*.jar
target/event-order-reconstruction-1.0.0.jar            (可执行 shaded jar)
target/original-event-order-reconstruction-1.0.0.jar

$ for f in samples/*.json; do java -jar target/event-order-reconstruction-1.0.0.jar "$f"; done
01: satisfiable=true,  order=[compile,lint,package], validOrderCount=3
02: satisfiable=false, CYCLE 链 [index,ingest,normalize,index]
03: satisfiable=false, TIME_CONTRADICTION 链 [deploy-eu,smoke-test,announce]
04: satisfiable=true,  validOrderCount=2(重叠时间窗未产生因果)
05: satisfiable=false, VERSION_CONTRADICTION 链 [schema-v2,schema-v1]
全部响应 tzdbVersion="2026b",退出码均为 0
```

开发过程中曾出现并已修复的问题(如实记录):

1. 自环依赖最初只记入 `selfLoops` 集合、未计入入度,导致单独调用拓扑排序时
   自环图被误判为有序 —— 测试 `TopologicalOrdererTest.selfLoopIsNotOrdered`
   捕获,已修复(自环同时作为普通边参与入度计算)。
2. Jackson 默认把 JSON 数字 `42` 强转为字符串 `"42"` 绑定到 `id` 字段 ——
   测试 `JsonIOTest.rejectsWrongFieldTypes` 捕获,已通过 coercion 配置拒绝。
3. 线性扩展枚举的回溯回滚最初只恢复"被释放为 0"的后继入度,会漏恢复其余
   后继 —— 代码审查中发现,已修复为逐边回滚。
4. 自环节点同时属于更大强连通分量时,最短环搜索会错误地选中自边 —— 自查
   发现,已修复(分量内遍历时跳过自边),并有回归测试
   `CycleFinderTest.selfLoopOnSmallestSccNodeDoesNotMaskRealCycle`。

当前无未通过的测试项。
