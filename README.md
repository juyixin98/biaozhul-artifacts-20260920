# 双时态记录查询后端（Bitemporal Record Query）

纯 Java 后端：实现**业务有效时间（valid time）/ 系统记录时间（recorded/transaction time）双时态表**的规则计算，
提供 JSON 输入输出，使用本地固定测试数据。修订只追加、保留全部历史，支持双维 as-of 查询，所有区间采用**半开边界 `[from, to)`**。

- 业务域：项目成员的**部门 / 岗位归属**历史。明确**不是**预约系统，也**不是**考勤系统。
- 无前端、无数据库、无网络服务；一个可执行 JAR 从文件或标准输入读一个 JSON 请求，向标准输出写一个 JSON 响应。
- 运行时记录 JDK 内置 IANA 时区数据库（TZDB）版本（`info` 操作）。

## 1. 环境与构建

| 项 | 实测版本 |
|---|---|
| JDK | OpenJDK 21.0.12.1 |
| Maven | 系统自带（本地仓库已有依赖，亦可访问 Maven Central） |
| Jackson | 2.17.2（databind + datatype-jsr310） |
| JUnit | Jupiter 5.10.2 |

```bash
mvn verify          # 编译 + 45 个测试 + JaCoCo 覆盖率门槛（行覆盖 ≥ 80%）
mvn package -DskipTests   # 只打包，产出 target/bitemporal-records-1.0.0.jar（fat jar）
```

运行（文件参数或 stdin 管道均可）：

```bash
java -jar target/bitemporal-records-1.0.0.jar samples/02-asof-baseline.json
echo '{"op":"info"}' | java -jar target/bitemporal-records-1.0.0.jar
```

成功响应信封：`{"ok": true, ...}`；业务 / 输入错误：`{"ok": false, "error": {"message": ...}}`，进程退出码 `1`。

## 2. 双时态模型

每条物理记录在双时态平面上占一个矩形：

| 字段 | 含义 |
|---|---|
| `validFrom` / `validTo` | 业务有效时间，半开 `[vf, vt)`；`validTo = null` 表示正无穷 |
| `recordedFrom` / `recordedTo` | 系统记录时间，半开 `[rf, rt)`；`recordedTo = null` 表示“当前版本” |
| `department` / `role` | 负载（部门、岗位） |
| `rowId` | 物理行号，只追加、单调递增 |

**半开边界约定（关键）**

- 点查询条件：`vf ≤ businessDate < vt` 且 `rf ≤ observationDate < rt`。
- 区间相接（一个的 `to` 等于另一个的 `from`）**不算重叠**，因此相邻版本可无缝拼接、不重不漏。
  例：`[2025-01-01, 2025-07-01)` 与 `[2025-07-01, +∞)` 在 `2025-07-01` 当天由后者负责。

**修订保留历史（append-only）**：追溯修订从不 UPDATE/DELETE。它在事务时刻 `T`：

1. 把受影响的当前版本行的 `recordedTo` 关闭为 `T`（旧行原样保留）；
2. 旧有效区间扣除目标区间后的残余，拆成 1~2 条续存行，`recordedFrom = T`；
3. 目标区间整体写一条新事实行，`recordedFrom = T`。

于是：用修订前的观察时刻查询永远重现旧认知；用修订后的观察时刻查询看到重述后的新认知。

两种写入模式：

- **INSERT**：只在业务时间轴空白处登记；与任一当前版本（非零长度）重叠即**拒绝**。
- **CORRECTION**：追溯修订（重述历史），允许覆盖已有版本，旧版本关闭保留 + 拆分续存。

事务规则：一个 `commit` 可含多笔变更，共享同一事务时间戳；先全量校验、后统一落库，任一不通过整笔回滚；
事务时间必须严格晚于上一次提交；同一事务中同一实体的目标有效区间不得相交。

## 3. 固定测试数据（种子事务时间 2026-01-01）

| 实体 | 部门/岗位 | 业务有效区间 |
|---|---|---|
| E001 张伟 | Engineering / Dev | `[2025-01-01, 2025-07-01)` |
| E001 张伟 | Engineering / TechLead | `[2025-07-01, +∞)` |
| E002 李娜 | Sales / Rep | `[2025-01-01, 2025-10-01)` |
| E002 李娜 | Sales / Manager | `[2025-10-01, +∞)` |
| E003 王芳 | Finance / Analyst | `[2025-03-01, +∞)` |

每次 CLI 调用都从这份固定种子重新装载（无状态进程），结果可复现。
需要“提交→查询→再提交→再查询”的链路时，用 `scenario` 操作在单次调用内按序执行多个步骤（共享同一存储）。

## 4. JSON 协议

| `op` | 必需字段 | 说明 |
|---|---|---|
| `info` | — | TZDB 版本、Java 版本、默认时区、种子与语义说明 |
| `asOf` | `entityId, businessDate, observationDate` | 双维点查询，命中一条或 `record: null` |
| `asOfAll` | `businessDate, observationDate` | 全部实体的点查询 |
| `history` | `entityId, observationDate` | 该观察时刻看到的实体完整有效时间线 |
| `commit` | `transactionDate, changes[]` | 一个事务提交多笔变更 |
| `snapshot` | — | 导出全部物理行（含已关闭历史行）与提交日志 |
| `scenario` | `steps[]`（`commit` / `asOf` / `history`） | 单进程内按序执行，复现跨观察时刻链路 |

单个 change：`{entityId, department, role, validFrom, validTo?(null=开放), mode}`。
日期一律 ISO `yyyy-MM-dd`。样例见 [`samples/`](samples/)。

## 5. 验收用例与手算结果

### 5.1 追溯修订 + 不同观察时刻（`samples/06-scenario-retroactive.json`）

事务：**2026-02-15** 对 E001 提交 CORRECTION：`[2025-03-01, 2025-09-01)` 实为 Platform/SRE。

手算：旧行 Dev `[01-01,07-01)` 与 TechLead `[07-01,+∞)` 在记录维关闭为 `[2026-01-01,2026-02-15)`；
有效时间轴重述为 Dev `[01-01,03-01)`、SRE `[03-01,09-01)`、TechLead `[09-01,+∞)`，新行记录区间自 2026-02-15 起。

| 查询（业务日期 2025-05-01） | 观察时刻 | 手算结果 | 程序结果 |
|---|---|---|---|
| 修订前 | 2026-01-15 | Engineering / Dev（rowId 1） | ✅ Dev（rowId 1，旧行 recordedTo 已关闭但仍可被旧观察时刻命中） |
| 修订后回看旧观察时刻 | 2026-01-15 | 历史不可变，仍是 Dev | ✅ Dev |
| 修订后 | 2026-06-01 | Platform / SRE（rowId 8） | ✅ SRE / Platform（rowId 8） |

半开边界核验：业务日期 `2025-09-01` 在 2026-06-01 观察得到 **TechLead**（SRE 区间不含终点）。
`history` 在 2026-06-01 返回三段，端点首尾相接：`[01-01,03-01)` / `[03-01,09-01)` / `[09-01,+∞)`。

### 5.2 重叠拒绝（`samples/04-insert-overlap-rejected.json`）

对 E001 以 INSERT 提交 `[2025-06-01, 2025-08-01)`，与 Dev `[2025-01-01,2025-07-01)` 及 TechLead `[2025-07-01,+∞)` 重叠：

```json
{ "ok": false,
  "error": { "message": "INSERT rejected: overlapping current record (rowId=1) ... (use CORRECTION to restate history)" } }
```
退出码 `1`，物理行数不变。对照：INSERT `[2024-06-01, 2025-01-01)`（与既有区间仅端点相接）被接受（测试 `acceptsAbuttingHalfOpenInterval`）。

### 5.3 同一事务多变更（`samples/05-multi-change-tx.json`）

事务 **2026-03-01** 含两笔，共享同一时间戳：

1. E002 CORRECTION `[2025-02-01,04-01)` = Marketing/Analyst → Rep 被切成 `[01-01,02-01)` 与 `[04-01,10-01)` 两段续存；
2. E003 INSERT `[2025-01-01,03-01)` = Finance/Junior，与原 Analyst `[03-01,+∞)` 端点相接。

手算与程序一致：`2025-03-01` → Marketing/Analyst，`2025-04-01`（半开边界）→ Sales/Rep；E003 在 `2025-02-28` → Junior、`2025-03-01` → Analyst。
原子性由测试 `oneRejectedChangeRollsBackWholeTransaction` 保证：合法 + 非法混在一个事务时全部不落库、提交日志不增加。

### 5.4 对修订的再次修订

测试 `secondCorrectionRestatesPreviousRestatement`：02-15 先把 `[03-01,09-01)` 重述为 SRE，
03-10 再把 `[05-01,07-01)` 重述为 Data/Eng。观察 2026-03-01（两次之间）仍看到 SRE；观察 2026-06-01 看到
Dev → SRE → Eng → SRE → TechLead 的完整重述链，证明每次修订本身也被历史化。

## 6. 自动化测试与实测结果

```
mvn verify
```

实测（2026-09-25，本机）：**Tests run: 45, Failures: 0, Errors: 0, Skipped: 0 — BUILD SUCCESS，
JaCoCo「All coverage checks have been met.」**

覆盖范围：

- `IntervalTest`（6）：半开包含/不包含端点、相接不重叠、开放端、非法区间；
- `SubtractAllTest`（6）：单切口/多切口/相接切口/开放端扣除的碎片几何；
- `BitemporalStoreTest`（20）：种子基线、追溯修订手算、旧行关闭保留、INSERT 重叠拒绝与相接接受、
  同事务多实体多变更、同实体多修订并集扣除、事务回滚、事务时间单调、二次修订、输入校验；
- `RequestServiceTest`（13）：JSON 协议各操作、错误信封、scenario 跨观察时刻链路、TZDB 版本格式。

JaCoCo 行覆盖率（`target/site/jacoco/index.html`）：

| 类 | 行覆盖 |
|---|---|
| 核心引擎 `BitemporalStore` | 136/142（94.9% 指令） |
| 协议层 `RequestService` | 128/137（94.0% 指令） |
| `Interval` / 其余模型与数据类 | 94.6% ~ 100% |
| **合计** | **333/385 行 = 86.5%**（指令 90.0%；CLI 入口 `Main` 为薄胶水层，已排除在 80% 门槛外，通过手动运行样例验证） |

各样例的实际运行记录见 [docs/RUN_LOG.md](docs/RUN_LOG.md)（含命令、退出码、未通过项说明）。

## 7. 范围与限制

- 时间粒度为**日期（LocalDate）**；不涉及时刻/时分秒与时区换算。TZDB 版本仅按要求**记录与上报**（`info`），
  不参与双时态计算；本机实测版本 **2026b**，Java 21.0.12.1，默认时区 Asia/Shanghai。
- 存储为进程内内存，每次 CLI 调用重新装载固定种子；`scenario` 提供单次调用内的有状态链路。未做持久化（YAGNI）。
- 同一实体同一业务日期在同一观察时刻至多一条当前版本，该不变量被 asOf 查询与每日唯一性测试显式校验。
- 无前端（按要求）。

## 8. 目录结构

```
pom.xml
samples/                 8 个请求样例
src/main/java/com/example/bitemporal/
  model/      Interval, BitemporalRecord, ChangeRequest, WriteMode
  engine/     BitemporalStore（核心规则）, CommitResult, CommitLogEntry, BitemporalException
  data/       SeedData（固定测试数据）
  json/       RequestService（协议分发）, RecordJson, TimeZoneInfo
  cli/        Main（stdin/文件 → JSON → stdout）
src/test/...            45 个 JUnit 5 测试
docs/RUN_LOG.md         实际运行记录
```
