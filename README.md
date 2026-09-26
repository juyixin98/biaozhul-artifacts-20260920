# migration-planner — 版本迁移路径规划（纯后端）

在 **schema 版本有向图** 上规划迁移路径的纯后端服务（Java 21，无 Web 框架，JDK 内置
HTTP 服务器 + Jackson）。输入输出均为 JSON，图数据来自本地固定测试夹具
`src/main/resources/graph.json`。

## 核心设计

- **节点是不透明字符串**：版本号（`v1`、`schema-10`…）仅作标识，**绝不**根据版本
  数字大小推断可迁移性或迁移方向。图中有 `schema-10 -> schema-9` 的单向边专门用于
  验证这一点（`schema-9 -> schema-10` 无路）。
- **边**携带：`cost`（必须为正数）、`reversible`（作者声明，仅元数据）、
  `preconditions`（请求上下文中的布尔标志，如 `maintenance_window`）。
- **回滚必须存在真实逆边**：检查点的 `rollback.available=true` 当且仅当图中存在
  `to -> from` 的真实反向边。`reversible=true` 但无逆边时（夹具中的 `e10`），加载期
  产生告警，检查点明确报告 `available=false`。
- **前置条件过滤**：不满足前置条件的边不参与搜索；`NO_PATH` 错误的
  `details.edgesBlockedByPreconditions` 会列出被条件阻挡的边。
- **成本并列（tie）**：Dijkstra 求出最小成本后，在"紧致边"子图上枚举所有等成本
  路径（上限保护），按节点 id 字典序**确定性**选择主路径，其余列入
  `alternatives`，并置 `tieBrokenDeterministically=true`。
- **环**：边成本为正，最短路搜索与枚举天然免疫环（夹具含 `v3->v4->v5->v3` 环）。
- **时区数据库版本**：每次响应携带 `tzdbVersion`（来自
  `java.time.zone.ZoneRulesProvider`，本机 JVM 为 `2026b`）。

## 构建与测试

```bash
mvn test          # 28 个自动化测试
mvn package       # 生成 target/migration-planner-1.0.0.jar
```

## 运行

```bash
# 服务器模式（--port 0 表示随机端口，启动日志打印实际端口）
java -cp "target/migration-planner-1.0.0.jar:<jackson jars>" com.migration.planner.Main --port 8080

# CLI 模式：读取请求 JSON，把计划 JSON 打印到 stdout
java -cp "target/migration-planner-1.0.0.jar:<jackson jars>" com.migration.planner.Main --cli samples/plan-cost-tie.json

# 自定义图文件
java -cp ... com.migration.planner.Main --port 8080 --graph path/to/graph.json
```

## HTTP API

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/plan` | 请求体为 PlanRequest；200 返回计划，422 返回 `NO_PATH`/`UNKNOWN_VERSION`，400 返回 `INVALID_REQUEST` |
| GET  | `/health` | 服务状态、图规模、`tzdbVersion` |
| GET  | `/graph` | 当前加载的图与告警 |

### 请求样例（`samples/`）

```json
{
  "from": "v1",
  "to": "v6",
  "context": {"maintenance_window": true, "backup_verified": true},
  "maxAlternatives": 16
}
```

```bash
curl -X POST http://127.0.0.1:8080/plan -H 'Content-Type: application/json' \
     -d @samples/plan-cost-tie.json
```

| 文件 | 场景 |
|------|------|
| `samples/plan-fork.json` | 分叉：v1→v3 选成本更低的 v2a 分支（成本 4 < 6） |
| `samples/plan-rollback-demo.json` | 无维护窗口时走 v2b 分支；检查点展示"有真实逆边才可回滚" |
| `samples/plan-cost-tie.json` | v1→v6 三条等成本（11）路径，确定性选择 + alternatives |
| `samples/plan-no-path.json` | v6 是汇点，v6→v1 返回 422 `NO_PATH` 及可达集诊断 |
| `samples/plan-unknown-version.json` | 未知版本返回 422 `UNKNOWN_VERSION` |

### 响应要点

- `steps[]`：有序迁移步骤（边 id、起止版本、成本、前置条件）。
- `checkpoints[]`：每步之后的检查点（到达版本、累计成本、回滚可用性与真实逆边 id）。
- `alternatives[]`：等成本备选路径；`tieBrokenDeterministically` 标记发生了并列。
- `warnings[]`：图加载期告警（如声明可逆但无真实逆边）。
- `tzdbVersion` / `graphId` / `graphVersion`：环境与时区数据库版本元数据。

## 测试覆盖（验收映射）

| 验收点 | 测试 |
|--------|------|
| 分叉选低成本分支 | `PathPlannerTest.forkChoosesLowestCostBranch` |
| 前置条件阻挡边 | `PathPlannerTest.unmetPreconditionExcludesEdge` |
| 环不死循环、不重复节点 | `PathPlannerTest.cycleDoesNotTrapPlanner` |
| 成本并列：确定性 + alternatives | `PathPlannerTest.costTieIsDeterministicAndReportsAlternatives`、`PlanServiceTest.costTieProducesAlternativesAndDeterministicChoice` |
| 无路（汇点/孤立节点） | `PathPlannerTest.noPathIsReportedWithDiagnostics`、`isolatedNodeHasNoPath` |
| 不按版本数字推断 | `PathPlannerTest.versionNumbersAreNotUsedToInferMigratability` |
| 回滚需真实逆边 | `PlanServiceTest.rollbackExistsOnlyWhenRealInverseEdgeExists`、`declaredReversibleWithoutInverseEdgeIsNotRollbackable` |
| 步骤与检查点 | `PlanServiceTest.checkpointsTrackCumulativeCost` |
| 时区数据库版本 | `TzdbInfoTest`、`PlanServiceTest.planCarriesTzdbAndGraphMetadata` |
| HTTP 端到端 | `HttpApiIntegrationTest`（7 个用例） |

## 实际运行记录（2026-09-25，本机 OpenJDK 21.0.12.1 / Maven 3.8.7）

| 命令 | 结果 |
|------|------|
| `mvn test` | **通过**：`Tests run: 28, Failures: 0, Errors: 0, Skipped: 0`，BUILD SUCCESS |
| `mvn package -DskipTests` | 通过，生成 `target/migration-planner-1.0.0.jar` |
| `java ... Main --port 18080` | **失败**：`BindException: Address already in use`（18080 被机器上另一个 java 进程占用，非本项目缺陷） |
| `java ... Main --port 18099` | **失败**：同上，端口被占用 |
| `java ... Main --port 0` | 通过，日志：`migration-planner listening on port 37365 (graph schema-migration-fixture v1.0.0)` |
| `curl /health` | 200：`{"status":"ok","nodeCount":10,"edgeCount":13,"tzdbVersion":"2026b"}` |
| `curl -d @samples/plan-cost-tie.json /plan` | 200：`totalCost=11`，5 步，2 条等成本 alternatives，`tieBrokenDeterministically=true` |
| `curl -d @samples/plan-fork.json /plan` | 200：走 v2a 分支，`totalCost=4` |
| `curl -d @samples/plan-rollback-demo.json /plan` | 200：检查点 v2b `rollback.available=false`（e3 无逆边），v3 `available=true, edgeId=e6` |
| `curl -d @samples/plan-no-path.json /plan` | 422：`NO_PATH`，`reachableVersions=["v6"]` |
| `curl -d @samples/plan-unknown-version.json /plan` | 422：`UNKNOWN_VERSION` |
| `java ... Main --cli samples/plan-cost-tie.json` | 通过，stdout 输出完整计划 JSON，exit=0 |

**未通过项**：无测试未通过；仅固定端口 18080/18099 因本机端口占用启动失败，改用
`--port 0` 后正常。

## 项目结构

```
src/main/java/com/migration/planner/
├── Main.java                  # 入口：服务器模式 / --cli 模式
├── model/                     # MigrationEdge, SchemaGraph, PlanRequest, MigrationPlan,
│                              # PlanStep, Checkpoint, RollbackInfo, AlternativePath, Precondition
├── plan/                      # PathPlanner（Dijkstra + 等成本枚举）, PlanBuilder, PlanException
├── graph/GraphLoader.java     # 夹具加载与校验（重复边 id、悬空端点、非正成本）
├── api/                       # PlanService, HttpApiServer（JDK 内置 HttpServer）
└── tz/TzdbInfo.java           # 时区数据库版本
src/main/resources/graph.json  # 固定测试图（分叉/环/汇点/孤立点/并列/声明可逆无逆边）
samples/                       # 请求样例
src/test/java/...              # 28 个 JUnit 5 测试
```
