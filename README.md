# 版本迁移路径规划（version-migration-planner）

纯后端服务：在 **schema 版本有向图** 上规划迁移路径。图的每条边标记
**成本（cost）**、**可逆性（reversible）**、**前置条件（preconditions）**
与**时间窗（validFrom / validTo）**。服务在满足全部条件的边中寻找路径，
输出有序步骤（steps）与检查点（checkpoints）。

> 核心约束：**是否可迁移只由“是否存在一条真实声明的有向边”决定**，
> 绝不根据版本号的数字大小、词法先后做任何推断。版本 id 是不透明字符串
> （`legacy`、`v3-1`、`schema-aurora`、`vX-orphan` …）。
>
> **回滚必须存在真实逆边。** 边上的 `reversible=true` 只是供应商给的元数据，
> 它**不会凭空合成一条反向边**。从 B 回滚到 A，当且仅当图中真实存在
> B→A 的边（或由这种边组成的路径）且满足前置条件/时间窗。

本服务只做“时间/版本规则计算”，使用**本地固定测试数据**，输出/输入均为
JSON；响应中记录运行环境的 **IANA 时区数据库（tzdata/tzdb）版本**。
不包含任何预约或考勤逻辑，也没有前端页面。

---

## 1. 技术栈与构建

- Java 21（record、switch 表达式、`java.net.http` 之外仅用 JDK 内置 HttpServer）
- Maven 3.8+，依赖：Jackson 2.17（databind + jsr310，用于 JSON 与 `Instant`）
- JUnit 5（自动化测试）
- 无数据库、无网络数据、无前端框架

```bash
mvn -B test          # 编译并运行全部自动化测试
mvn -B package       # 生成可执行 fat jar: target/version-migration-planner-1.0.0.jar
```

## 2. 运行方式

```bash
JAR=target/version-migration-planner-1.0.0.jar

java -jar $JAR env                       # 打印 tzdb / java 环境信息
java -jar $JAR scenarios                 # 列出内置固定数据场景
java -jar $JAR demo shop                 # 直接运行内置场景（输出即标准请求 JSON）
java -jar $JAR plan samples/request-cost-tie.json   # 从文件读取请求
cat req.json | java -jar $JAR plan -     # 从标准输入读取请求
java -jar $JAR serve 8080                # 启动 JSON HTTP 服务
```

HTTP 接口（无任何前端）：

- `GET  /health` → `{"status":"UP","tzdbVersion":"2026b"}`
- `POST /plan`，请求体为一个 `PlanRequest` JSON，返回 `PlanResponse` JSON。
  正常 200；请求体无法解析返回 400；非 POST 返回 405。

```bash
curl -s -X POST http://localhost:8080/plan \
  -H 'Content-Type: application/json' \
  --data @samples/request-cost-tie.json
```

## 3. 请求 / 响应格式

### 请求 `PlanRequest`

| 字段 | 类型 | 说明 |
|---|---|---|
| `from` / `to` | string | 起点 / 目标版本 id（不透明字符串） |
| `at` | ISO-8601 instant | 评估时间窗所用时刻（UTC），省略取当前时刻 |
| `mode` | `UPGRADE` \| `ROLLBACK` | 模式（默认 UPGRADE；两种模式都只走真实边） |
| `attributes` | object | 与边前置条件匹配的请求属性 |
| `requireReversible` | bool | 严格守卫：UPGRADE 时排除 `reversible=false` 的边；ROLLBACK 时额外要求对端正向边显式标记 `reversible=true` |
| `maxPaths` | int | 返回候选简单路径上限（1–50，默认 3） |
| `graph.nodes` | string[] | 可选，显式声明节点（边端点也会自动纳入） |
| `graph.edges` | Edge[] | **有向边集合，是可迁移性的唯一来源** |

### 边 `Edge`

| 字段 | 说明 |
|---|---|
| `from` / `to` | 有向边方向。**只在这个方向可迁移** |
| `cost` | 有限正数（停机分钟、风险分等），用于排序 |
| `reversible` | 仅信息性标记；**不会创建反向边** |
| `preconditions` | 属性 → 期望值，或允许值数组；属性缺失/不匹配则边被阻断 |
| `validFrom` / `validTo` | 可选时间窗，相对请求的 `at` 判断 |
| `description` | 说明文本 |

### 响应 `PlanResponse`

- `status`：`FOUND` / `NO_PATH` / `ERROR`；`success` 布尔。
- `environment`：`tzdbVersion`（本机 JDK 内置 IANA tzdata 版本）、`javaVersion`、`dataSet`。
- `paths[]`：按 **总成本升序 → 步数升序 → 路径词法签名** 确定性排序；
  **成本相同的并列路径会同时返回**（不会被任意丢弃）。
- 每条路径含 `initialCheckpoint`、`steps[]`、`finalCheckpoint`。
  每个 step 都引用一条**真实存在的边**，并给出 `cost`、时间窗、
  `reverseEdgeExists`（反向是否真实存在边）、`reverseEdgeMarkedReversible`
  以及步后 `checkpoint`（`CP-n`、所在版本、累计成本）。
- `blockedEdges[]`：被前置条件/时间窗/守卫阻断的边及**逐条原因**。
- `warnings[]`：例如检测到有向环时提示“仅枚举无环简单路径”。

## 4. 验收场景与样例

| 场景 | 样例请求 | 验收点 |
|---|---|---|
| 分叉 fork | `samples/request-upgrade-fork.json` | 两条分叉都枚举；成本 12 的主线排第 1，13 的热修复支线第 2 |
| 菱形分叉 | `samples/request-branch.json` | blue/green 两分支 + 跨分支边，共 3 条简单路径 |
| 有向环 | `samples/request-cycle.json` | v3↔v3b 成环；枚举**必然终止**，只返回无环简单路径，并告警 |
| 无路可达 | `samples/request-no-path.json` | `vX-orphan` 虽“看起来更新”，但无真实入边 → `NO_PATH` |
| 成本并列 | `samples/request-cost-tie.json` | a→b→d 与 a→c→d 成本都为 5，**两条都返回**且顺序确定 |
| 真实逆边回滚 | `samples/request-rollback.json` | aurora→v3-1→v2 走真实逆边；热修复线虽标 reversible 但无逆边，不能回滚 |

对应真实运行输出保存在 `samples/output/response-*.json`；
完整命令与结果（含一次失败-修正的过程）见 `docs/RUNLOG.md`。

### 最小请求示例

```json
{
  "from": "a",
  "to": "d",
  "at": "2026-06-01T02:00:00Z",
  "attributes": {},
  "graph": {
    "edges": [
      {"from": "a", "to": "b", "cost": 2, "reversible": true},
      {"from": "a", "to": "c", "cost": 2, "reversible": true},
      {"from": "b", "to": "d", "cost": 3, "reversible": true},
      {"from": "c", "to": "d", "cost": 3, "reversible": true}
    ]
  }
}
```

## 5. 算法说明

- **约束求值**：对每条边独立计算全部不满足原因（前置条件、时间窗、严格守卫）。
- **路径枚举**：在“可用边”子图上做 DFS，维护访问集合，**只枚举简单路径**
  （不重复经过版本节点），因此即使存在任意有向环也保证终止；
  另设有展开次数安全阀（200000）防止组合爆炸。
- **环检测**：三色 DFS 找出可用边子图中的后向边，给出代表性环并写入告警。
- **确定性排序**：总成本 → 步数 → 词法路径签名。并列成本的路径全部保留。
- **回滚**：ROLLBACK 模式与 UPGRADE 使用同一个遍历器——区别只在于图中
  真实声明了哪些方向的边；`reversible` 标记不参与“造边”。可选严格守卫
  会额外审计对端正向边的可逆标记。

## 6. 目录结构

```
pom.xml
samples/                       # 手工编写的请求样例 + 真实运行输出
src/main/java/com/example/migration/
  Main.java                    # CLI 与内置 HTTP 服务入口
  model/                       # Edge / MigrationGraph / PlanRequest / PlanResponse
  service/                     # PathPlanner（核心算法）、PlanningService、TzdbInfo
  data/                        # FixedData 固定场景 + DemoRequests
  json/                        # Jackson 配置
src/test/java/...              # 26 个 JUnit 5 测试
docs/RUNLOG.md                 # 实际命令、结果与未通过项的如实记录
```
