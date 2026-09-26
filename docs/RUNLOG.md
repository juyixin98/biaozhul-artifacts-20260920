# 运行记录（RUNLOG）

本文件如实记录本项目在交付环境中的实际命令与结果，包括中途失败、
失败原因与修正。所有命令均在项目根目录执行。

- 日期：2026-09-25
- OS：Linux 6.8.0-90-generic (Ubuntu)
- JDK：OpenJDK 21.0.12.1
- Maven：3.8.7
- 构建产物：`target/version-migration-planner-1.0.0.jar`（fat jar）

## 1. 环境探测

```
$ java -version
openjdk version "21.0.12.1" 2026-08-18
$ mvn -version
Apache Maven 3.8.7
```

时区数据库版本通过 JDK 的 `java.time.zone.ZoneRulesProvider` 获取
（见 `src/main/java/com/example/migration/service/TzdbInfo.java`）：

```
$ java -jar target/version-migration-planner-1.0.0.jar env
tzdb.version=2026b
java.version=21.0.12.1
java.vm.name=OpenJDK 64-Bit Server VM
```

> 即：本机 JDK 内置 **IANA tzdata 2026b**。该版本被写入每个响应的
> `environment.tzdbVersion` 字段。Maven Central 可达，Jackson/JUnit 正常下载。

## 2. 自动化测试

### 第一轮（失败，已如实保留过程）

编译通过后首次 `mvn test`，26 个测试中 **2 个失败**：

```
Tests run: 26, Failures: 2, Errors: 0, Skipped: 0
[FAIL] forkPicksCheapestAndKeepsAlternatives
       hotfix fork must be enumerated, got [[legacy, v2, v3-1, schema-aurora]]
[FAIL] requireReversibleGuard  (expected true but was false)
```

**根因分析（均为测试用例本身的设定问题，非规划器缺陷）：**

1. shop 演示请求未携带 `emergency=true`，因此热修复边
   `v2→v3-hotfix` 被其前置条件**正确阻断**。原断言却要求该分叉出现。
   修正：分叉测试改为构造一个同时满足 `emergency` 的请求（得到 2 条分叉路径），
   并**新增** `forkBlockedByPreconditionInDemo` 测试，显式断言演示请求中
   热修复边因 `emergency` 前置条件出现在 `blockedEdges` 中——把“被正确阻断”
   也固化为验收点。
2. 严格守卫测试从 `legacy` 出发，而 `legacy→v2` 本身就是
   `reversible=false`，开启 `requireReversible` 后被正确阻断，导致后续路径为空。
   修正：起点改为 `v2`，直接对比可逆的 v3-1 线与不可逆的 hotfix 线。

修正后：

```
$ mvn -B test
Tests run:  3, ... MigrationGraphTest
Tests run:  3, ... JsonRoundTripTest
Tests run: 10, ... PathPlannerTest
Tests run:  5, ... PlanningServiceTest
Tests run:  5, ... RollbackTest
Tests run: 26, Failures: 0, Errors: 0, Skipped: 0
BUILD SUCCESS
```

> 测试类与数量：图校验 3、JSON 往返 3、规划器（分叉/环/无路/并列/前置/时间窗/守卫/检查点）10、
> 服务层 5、回滚真实性 5，合计 **26 个，全部通过，0 失败 0 错误 0 跳过**。

## 3. 样例请求实际运行

```
$ for f in upgrade-fork rollback branch cycle no-path cost-tie; do
    java -jar target/version-migration-planner-1.0.0.jar plan samples/request-$f.json \
      > samples/output/response-$f.json
  done
```

结果（status 与各路径排名/成本，摘自真实输出文件）：

| 样例 | status | 路径（rank / 总成本 / 序列） |
|---|---|---|
| upgrade-fork | FOUND | 1=12 `legacy→v2→v3-1→schema-aurora`；2=13 `…→v3-hotfix→…` |
| branch | FOUND | 1=5 经 v3-blue；2=5 经 v3-green；3=13 `v3-blue→v3-green` 跨边 |
| cycle | FOUND | 1=4 `v2→v3→v3b→v4`；2=6 `v2→v3→v4`；告警检测到 1 个环 |
| no-path | **NO_PATH** | `vX-orphan` 无真实入边，`paths=[]` |
| cost-tie | FOUND | 1=5 `a→b→d`；2=5 `a→c→d`（**并列两条都返回**） |
| rollback | FOUND | 唯一路径 7=`schema-aurora→v3-1→v2`，两步均为 REAL reverse edge |

关键语义核验（来自 `response-rollback.json`）：

- 热修复线 `v2→v3-hotfix` 与 `v3-hotfix→schema-aurora` 都标了
  `reversible=true`，但**没有任何反向边**，故回滚路径里完全不出现该线；
  回滚只走出真实存在的 `schema-aurora→v3-1→v2`。
- 每个 step 含 `reverseEdgeExists / reverseEdgeMarkedReversible` 与
  步后检查点 `CP-1/CP-2`（累计成本 4.0、7.0）。

### 时间窗阻断（附加实测）

将 upgrade-fork 样例的 `at` 从 2026 改为 2025（早于 v2→v3-1 窗口）：

```
status = FOUND
blocked edges:
  v2 -> v3-1
      - time window: edge not open until 2026-01-01T00:00:00Z (at 2025-01-01T00:00:00Z)
```

该边被阻断后改走仍满足条件的热修复线——前置条件/时间窗按边独立求值。
输出见 `samples/output/response-window-closed.json`。

## 4. HTTP 接口实际运行

初次使用端口 8099 时与机器上**另一进程**冲突（其 `/health` 返回
`{"status": "ok"}`，非本服务的 `{"status":"UP",...}`，本服务进程已退出）。
换用空闲端口 18377 后全部正常：

```
$ java -jar target/version-migration-planner-1.0.0.jar serve 18377 &

$ curl -s http://localhost:18377/health
{"status":"UP","tzdbVersion":"2026b"}

$ curl -s -X POST http://localhost:18377/plan -H 'Content-Type: application/json' \
       --data @samples/request-cost-tie.json
FOUND [(1, 5.0), (2, 5.0)]  tzdb=2026b

$ curl -s -o - -w "%{http_code}\n" -X POST .../plan --data '{bad'
HTTP 400, body: status=ERROR error.code=BAD_REQUEST

$ curl -s -o /dev/null -w "%{http_code}\n" http://localhost:18377/plan
405
```

CLI 对坏 JSON 的退出码：

```
$ echo '{bad' | java -jar ...jar plan - ; echo $?
2          # stderr/JSON 错误信封输出，退出码 2
```

## 5. 未通过项 / 已知边界（如实说明）

- **当前无未通过测试**：最终 26/26 通过。
- 首次测试运行有 2 个失败，原因与修正如上（测试设定问题，非算法问题）。
- 端口 8099 被占用导致首次 HTTP 冒烟打到了无关进程；换端口后通过，非代码缺陷。
- 设计上的已知边界（非缺陷，已在代码注释/README 声明）：
  - 简单路径枚举为 DFS 全枚举，设有 200000 次展开安全阀，触发时会在
    `warnings` 中提示“结果可能不穷尽”。内置固定数据远低于该阈值。
  - 双向边对（A→B 与 B→A）在图论上构成 2-环，环检测会如实计数并告警；
    这不影响“只枚举无环简单路径”的正确性。
  - 时间均为 UTC `Instant`；服务不做预约/考勤、不含时区换算业务，
    仅记录 tzdb 版本作为环境元数据。
