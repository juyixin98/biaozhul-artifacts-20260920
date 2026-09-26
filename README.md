# 双时态记录查询（Bitemporal Records）纯后端

用 Java 实现的**时间/版本规则计算后端**：数据行同时携带**业务有效时间**（valid time）
与**系统记录时间**（system time / transaction time）两条时间轴，修订只追加、不改写历史，
支持沿两个维度的 as-of 查询。提供 JSON 标准输入/输出命令行接口，使用本地固定测试数据。

- 不是预约系统，也不是考勤系统；记录内容为通用的不透明字符串负载。
- 无前端、无外部数据库、无网络服务；状态保存在本地一个 JSON 快照文件中。
- 记录运行时 IANA 时区数据库（tzdb）版本。

## 1. 概念模型

### 1.1 双时态表

每个版本行（`TemporalRecord`）有：

| 字段 | 含义 |
|---|---|
| `recordId` | 业务记录标识 |
| `versionId` | 同一 recordId 内单调递增的版本行号 |
| `data` | 不透明字符串负载（示例中是一段 JSON） |
| `validFrom` / `validTo` | **业务有效时间**半开区间 `[from, to)`，`validTo=null` 表示至今 |
| `systemFrom` / `systemTo` | **系统记录时间**半开区间 `[from, to)`，`systemTo=null` 表示当前仍有效 |
| `txnId` | 写入该行的事务 |

### 1.2 半开区间

所有区间统一为 **左闭右开 `[from, to)`**：

- `t == from` 命中；`t == to` 不命中；
- 相邻区间 `[a,b)` 与 `[b,c)` **不重叠**，可以无缝拼接；
- 不允许零长度或倒置区间。

### 1.3 修订如何保留历史（bitemporal correction）

`revise`（追溯修订）**从不 UPDATE/DELETE 旧行**：

1. 与目标业务区间相交的所有“当前版本行”（`systemTo=null`）在提交时刻 `t` 被**封口**
   （复制一份 `systemTo=t` 的不可变副本，旧行保留）；
2. 这些行在目标区间之外的**左残段、右残段**以原值重新写为系统时间新行；
3. 目标区间写入一行新值。

于是：

- 在 `systemAt < t` 观察，看到的是旧信念；
- 在 `systemAt >= t` 观察同一业务时刻，看到修正值；
- 窗口之外的业务时间仍是原值。

特例：同一事务内“刚插入即被修订”的行，其 `systemFrom` 等于本次提交时刻，
封口会产生 `[t,t)` 零长度区间——该行从未在任何观察时刻可见，因此直接丢弃，
不会进入库中或提交结果。

### 1.4 as-of 双维查询

```
row 命中  ⇔  systemFrom ≤ systemAt < systemTo   （系统轴：当时数据库相信什么）
          ∧  validFrom  ≤ validAt  < validTo    （业务轴：描述的是什么时候的事实）
```

## 2. 环境与构建

- JDK 21（实际运行：OpenJDK 21.0.12，内置 **IANA tzdb 2026b**）
- Maven 3.x（依赖：Jackson 2.17.2；测试：JUnit 5.10.3）

```bash
mvn test                 # 编译 + 全部自动化测试 + JaCoCo 报告
mvn package -DskipTests  # 产出可执行 fat jar: target/bitemporal-records-1.0.0.jar
```

覆盖率报告：`target/site/jacoco/index.html`。

## 3. 命令与 JSON 协议

CLI 从标准输入读一个 JSON（无 body 的命令除外），向标准输出写统一信封：

```json
{ "success": true, "data": { ... } }
{ "success": false, "error": { "code": "OVERLAP_REJECTED", "message": "...", "recordId": "..." } }
```

退出码：`0` 成功；`2` 业务错误（`VALIDATION_ERROR` / `OVERLAP_REJECTED` /
`RECORD_NOT_FOUND`）；`1` 内部错误（如本地库文件损坏）。

状态文件默认 `./bitemporal-db.json`，可用环境变量 `BITEMPORAL_DB` 覆盖。

| 命令 | stdin body | 说明 |
|---|---|---|
| `seed` | 无 | 载入本地固定种子数据（要求库为空，先 `reset`） |
| `reset` | 无 | 清空全部版本行 |
| `info` | 无 | tzdb 版本、默认时区、时区数量、记录列表 |
| `commit` | `TransactionRequest` | 原子提交一个事务（可含多个变更） |
| `query` | `QueryRequest` | as-of 双维查询 |
| `history` | `{"recordId":"..."}` | 某记录全部版本行（含已封口历史） |
| `batch` | `{"steps":[...]}` | 单进程内顺序执行多步，便于一体化复现 |

`TransactionRequest`：

```json
{
  "txnId": "fix-q1-level",
  "committedAt": "2026-03-01T09:00:00Z",
  "changes": [
    { "op": "revise", "recordId": "emp-1001",
      "data": "{\"level\":\"L2\",\"team\":\"Platform\"}",
      "validFrom": "2026-01-15T00:00:00Z", "validTo": "2026-03-01T00:00:00Z" }
  ]
}
```

- `op`：`insert`（默认；与当前版本业务时间重叠即拒绝）或 `revise`（追溯修订）。
- `committedAt` 可省略（取当前时钟）；若提供，必须**严格晚于**上次提交时刻
  （系统时间只增、半开）。所有示例时间均为 UTC（`Z`）。

`QueryRequest`：用 `validAt` + `systemAt` 分别指定两个维度；或用单个
`observationTime` 同时作为两者。`recordId` 可省略表示全库。

请求样例见 [`samples/`](samples/)。

## 4. 固定测试数据与手算验收

种子事务 `seed-txn` 于系统时间 **2026-01-01T00:00:00Z** 入库：

| recordId | 业务有效区间（UTC） | data |
|---|---|---|
| emp-1001 | `[2026-01-01, 2026-04-01)` | `{"level":"L3","team":"Platform"}` |
| emp-1001 | `[2026-04-01, 2026-07-01)` | `{"level":"L4","team":"Platform"}` |
| emp-1001 | `[2026-07-01, +∞)` | `{"level":"L4","team":"Infra"}` |
| emp-1002 | `[2026-01-01, +∞)` | `{"level":"L2","team":"Data"}` |

### 场景 A：追溯修订

`fix-q1-level` 在系统时间 **2026-03-01T09:00:00Z** 把业务区间
`[2026-01-15, 2026-03-01)` 修订为 `{"level":"L2","team":"Platform"}`。

原 Q1 行被切成 3 个当前行（v4 左残段 / v6 新值 / v5 右残段），旧 v1 封口保留。

**手算不同观察时刻对业务点 `validAt = 2026-02-15` 的查询结果：**

| systemAt（观察时刻） | 命中版本 | level | 说明 |
|---|---|---|---|
| 2026-02-15T12:00:00Z（修订前） | v1 | **L3** | 当时数据库只知道旧值 |
| 2026-03-01T09:00:00Z（恰为提交时刻） | v6 | **L2** | `systemFrom` 包含，提交瞬间即可见 |
| 2026-03-02T00:00:00Z（修订后） | v6 | **L2** | 修正值 |
| 2026-03-01T08:59:59Z（提交前 1 秒） | v1 | **L3** | 半开右端：差 1 秒仍是旧信念 |

修订后（systemAt=2026-03-02）沿业务轴逐点核对：

| validAt | level | 来源 |
|---|---|---|
| 2026-01-10 | L3 | 左残段 `[01-01,01-15)` |
| **2026-01-15T00:00** | **L2** | 新值窗口起点（包含） |
| 2026-02-15 | L2 | 新值窗口内 |
| **2026-03-01T00:00** | **L3** | 新值窗口终点（排除）→ 右残段 |
| 2026-05-01 | L4/Platform | Q2 行未被触及 |
| 2026-10-01 | L4/Infra | Q3 开放行未被触及 |

### 场景 B：重叠拒绝

对 emp-1001 插入业务区间 `[2026-02-01, 2026-05-01)`：与 v1、v2 真正相交 →
返回 `OVERLAP_REJECTED`，退出码 2，库不变。
而 `[...,2026-01-01)` 与首段 `[2026-01-01,...)` 仅在被排除端点相接，**允许**插入。

### 场景 C：同一事务多变更

`multi-changes-demo`（一个事务）依次：插入 emp-1003 的 jan、feb 两段，再把
`[02-10,02-20)` 修订为 `feb-fixed`。后一个变更能看到同事务前一个变更的结果；
最终落库 4 行（jan / feb 左残段 / feb-fixed / feb 右残段）。

**原子性**：事务中任一变更被拒绝，整批不生效。例如先插入 emp-9999 一段、
再在同事务插入与之重叠的一段 → 第二条拒绝，emp-9999 查询结果为 0 行。

### 一键复现

```bash
export BITEMPORAL_DB=/tmp/demo-db.json
java -jar target/bitemporal-records-1.0.0.jar reset
java -jar target/bitemporal-records-1.0.0.jar seed
java -jar target/bitemporal-records-1.0.0.jar query  < samples/query-before-fix.json   # L3
java -jar target/bitemporal-records-1.0.0.jar commit < samples/commit-retro-fix.json
java -jar target/bitemporal-records-1.0.0.jar query  < samples/query-before-fix.json   # 仍 L3（旧观察时刻）
java -jar target/bitemporal-records-1.0.0.jar query  < samples/query-after-fix.json    # L2
java -jar target/bitemporal-records-1.0.0.jar commit < samples/commit-overlap-reject.json; echo $?  # 2
java -jar target/bitemporal-records-1.0.0.jar history < samples/history.json
# 或单进程内把 reset/seed/修订/两次查询/预期失败/history 一次跑完：
java -jar target/bitemporal-records-1.0.0.jar batch < samples/batch-demo.json
```

## 5. 时区数据库版本

`info` 命令与测试通过公开 API `java.time.zone.ZoneRulesProvider#getVersions`
读取 JDK 内置 IANA tzdb 版本（无反射、无 `--add-exports`）。本机实测：

```
IANA tzdb version=2026b (bundled with the running JDK), default zone=Asia/Shanghai, zone count=604
```

## 6. 实际运行记录

- 日期：2026-09-25；平台：Linux x86_64；JDK：OpenJDK 21.0.12；Maven：系统自带。
- `mvn clean test`：**Tests run: 53, Failures: 0, Errors:0, Skipped: 0 — BUILD SUCCESS**。
- JaCoCo：Instruction 89.3%、Branch 80.0%、Line 88.1%（均达到/超过 80% 要求）。
- `mvn package -DskipTests`：BUILD SUCCESS，产出
  `target/bitemporal-records-1.0.0.jar`（约 2.4 MB fat jar），`java -jar` 各命令实测通过。
- CLI 端到端实测结果与第 4 节手算表逐项一致（L3→L2、左右残段、Q2/Q3 不变、
  重叠拒绝退出码 2、事务回滚后 emp-9999 为 0 行、batch 中 continueOnError 行为）。

**未通过项（最终状态：无）。** 开发过程中出现过并已修复的问题，如实记录：

1. 初版测试把“修订窗口严格落在版本内部”误算为只产生 2 个新行（实际为
   左残段/新值/右残段 3 行），更正测试期望后通过；
2. 发现真实缺陷：同事务 insert→revise 会把刚插入行封口成非法 `[t,t)` 零长度区间，
   已修复为“从未可见的行直接丢弃”，并从提交结果 `written` 中撤回，新增回归测试。

## 7. 目录结构

```
pom.xml
samples/                         请求样例（commit/query/history/batch）
src/main/java/bitemporal/
  Main.java                      CLI 入口：stdin JSON → stdout JSON，退出码映射
  model/                         Interval / TemporalRecord / 请求与结果 DTO（record）
  store/BitemporalStore.java     双时态规则核心：事务、重叠拒绝、修订切分、as-of
  store/PersistentBitemporalStore.java  本地 JSON 快照持久化（原子写）
  store/SeedData.java            固定测试数据
  time/TzdbInfo.java             IANA tzdb 版本读取
  api/                           服务分发、batch、统一响应信封
  json/JsonMapper.java           Jackson（Instant ↔ ISO-8601）
src/test/java/bitemporal/        53 个 JUnit 5 测试（含子进程端到端）
```

## 8. 设计取舍

- **内存规则引擎 + JSON 文件快照**：双时态规则全部在 `BitemporalStore` 内实现并可独立
  单测；持久化只是整库快照，刻意不引入数据库，满足“本地固定数据”的要求。
- **不可变模型**：版本行与 DTO 均为 Java `record`；修订产出新副本，不就地修改历史。
- **显式 `committedAt`**：为手算/复算不同观察时刻提供确定性；系统时间保持严格递增。
- **负载不透明**：`data` 为字符串，引擎不解释其内容，避免与任何具体业务（预约/考勤等）耦合。
