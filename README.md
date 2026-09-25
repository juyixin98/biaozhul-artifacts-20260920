# event-order-reconstruction

事件有效顺序重建 —— 纯后端 Java 服务（CLI）。给定一组事件及其**偏序依赖**、**时间区间**、**版本号**，
计算一个确定性的合法拓扑顺序；当约束不可满足时，输出最小可读的冲突链（反例）。

不做预约、不做考勤、无前端。输入输出均为 JSON。

## 排序规则

三类规则共同构成有向图上的边 `from → to`（from 必须排在 to 之前）：

| 规则 | 触发条件 | 说明 |
|------|----------|------|
| EXPLICIT | 请求中声明的依赖 `{"before": "A", "after": "B"}` | 原样采纳 |
| VERSION | 同一 `stream` 上版本号不同 | 低版本排在前 |
| TIME | 区间 A 结束时间 ≤ 区间 B 开始时间 | 按 Instant 精确比较，跨时区安全 |

**时间区间重叠不制造因果关系**：两区间相交时不产生任何边，两个事件的相对顺序保持自由
（最终输出由确定性 tie-break 决定，并置 `multipleValidOrders: true`）。

## 确定性

拓扑排序使用 Kahn 算法 + 事件 id 字典序最小优先的 tie-break。输出是输入的纯函数：
同一输入在任何机器、任何一次运行都得到同一顺序。`multipleValidOrders` 为 `true` 表示
约束本身允许多个合法线性扩展（Kahn 某一步存在多个可发射节点）。

## 冲突链（反例）

图中存在环时判定 `UNSATISFIABLE`，输出**一个**环作为最小可读冲突链：链中每条边都带
`kind`（EXPLICIT / VERSION / TIME）和人类可读的 `reason`，首尾相接闭合。例如显式依赖
`P before Q` 与时间证据 `Q 结束 ≤ P 开始` 互相矛盾时，冲突链同时引用这两条边。

## 时区数据库

输入时间戳接受 ISO-8601 偏移形式（`2026-01-01T09:00:00+08:00`）和命名时区形式
（`2026-01-01T09:00:00+08:00[Asia/Shanghai]`），统一归一化为 Instant 比较。
每次响应的 `diagnostics.tzdbVersion` 记录 JVM 当前使用的 IANA 时区数据库版本
（本机实测为 `2026b`）。

## 构建与运行

```bash
mvn test                 # 运行自动化测试（JUnit 5）
mvn package -DskipTests  # 产出可执行 shaded jar

# 从文件读取请求，结果写 stdout
java -jar target/event-order-reconstruction-1.0.0.jar examples/ok.json

# 或从 stdin 读取
cat examples/ok.json | java -jar target/event-order-reconstruction-1.0.0.jar
```

退出码：`0` = 正常计算（含 UNSATISFIABLE）；`1` = 输入无法读取 / JSON 畸形 / 校验失败。

## 请求格式

```json
{
  "events": [
    { "id": "E1", "interval": { "start": "2026-01-01T09:00:00+08:00", "end": "2026-01-01T10:00:00+08:00" } },
    { "id": "E3", "version": { "stream": "doc-a", "value": 1 } },
    { "id": "E5" }
  ],
  "dependencies": [
    { "before": "E1", "after": "E3", "reason": "可选的人类可读理由" }
  ]
}
```

- `id`：必填、非空、唯一。
- `interval`：可选；`start`/`end` 为 ISO-8601 时间戳，`end >= start`。
- `version`：可选；同一 `stream` 内 `value` 不得重复。
- `dependencies[].before/after`：必须引用已声明的事件 id。

## 响应格式

```json
{
  "status": "OK | UNSATISFIABLE | INVALID_INPUT",
  "order": ["E1", "E2"],
  "multipleValidOrders": false,
  "conflicts": [
    { "type": "CYCLE",
      "chain": [ { "from": "X", "to": "Y", "kind": "EXPLICIT", "reason": "..." } ],
      "message": "circular ordering constraints: X -> Y -> X" }
  ],
  "errors": [],
  "diagnostics": { "tzdbVersion": "2026b", "eventCount": 2, "edgeCount": 1 }
}
```

## 请求样例（固定测试数据，`examples/`）

| 文件 | 场景 | 期望 |
|------|------|------|
| `ok.json` | 时间边 + 显式依赖 + 版本边构成唯一链 | `OK`，`[E1,E2,E3,E4]`，`multipleValidOrders=false` |
| `cycle.json` | X→Y→Z→X 循环依赖 | `UNSATISFIABLE`，3 边闭环冲突链 |
| `time-conflict.json` | 显式依赖 P→Q 与时间证据 Q→P 矛盾 | `UNSATISFIABLE`，2 边冲突链同时引用两类规则 |
| `multi-order.json` | A、B 区间重叠（无因果），均先于 C | `OK`，`[A,B,C]`，`multipleValidOrders=true` |

## 实际运行记录

环境：OpenJDK 21.0.12.1，Maven（/usr/bin/mvn），Linux 6.8.0-90-generic，IANA tzdb `2026b`。

| 命令 | 结果 |
|------|------|
| `mvn test` | **首次失败**：默认 maven-compiler-plugin 3.1 不支持 `release` 属性（"Source option 5 is no longer supported"）。在 `pom.xml` 固定 `maven-compiler-plugin` 3.13.0 后通过 |
| `mvn test`（修复后） | `Tests run: 10, Failures: 0, Errors: 0, Skipped: 0`，BUILD SUCCESS |
| `mvn package -DskipTests` | 生成 `target/event-order-reconstruction-1.0.0.jar`（shaded，含 Jackson） |
| `java -jar ... examples/ok.json` | `OK`，`order=[E1,E2,E3,E4]`，exit 0 |
| `java -jar ... examples/cycle.json` | `UNSATISFIABLE`，冲突链 `X -> Y -> Z -> X`，exit 0 |
| `java -jar ... examples/time-conflict.json` | `UNSATISFIABLE`，冲突链含 EXPLICIT `P→Q` 与 TIME `Q→P` 两条边，exit 0 |
| `java -jar ... examples/multi-order.json` | `OK`，`order=[A,B,C]`，`multipleValidOrders=true`，exit 0 |
| stdin 管道输入 | 与文件输入输出一致，exit 0 |
| 未知事件依赖（`A→NOPE`） | `INVALID_INPUT`，错误 `dependency references unknown event: 'NOPE'`，exit 1 |

未通过项：仅上述首次编译失败（构建配置问题，已修复）；修复后全部 10 个测试通过，无遗留失败项。

## 项目结构

```
src/main/java/com/eventorder/
  Main.java                  CLI 入口（文件参数或 stdin → stdout）
  io/JsonCodec.java          JSON 序列化边界（Jackson）
  model/                     EventInput / DependencyInput / OrderRequest / Edge / Conflict / OrderResult
  engine/GraphBuilder.java   校验 + 三类规则建边（重叠区间不建边）
  engine/CycleFinder.java    确定性 DFS 找环，输出带理由的冲突链
  engine/TopoSorter.java     Kahn + 字典序 tie-break，检测多合法顺序
  engine/OrderEngine.java    编排：校验 → 建图 → 找环 → 排序
  engine/TimeParser.java     ISO-8601（偏移/命名时区）→ Instant
  engine/TzdbInfo.java       读取 JVM tzdb 版本
src/test/java/com/eventorder/OrderEngineTest.java   10 个验收测试
examples/                    固定测试数据（4 个场景）
```
