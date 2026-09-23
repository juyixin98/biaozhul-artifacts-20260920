# 变更流表状态重建（Change-Stream Table State Reconstruction）

纯后端服务：消费一条带**事务边界**的变更日志（change stream / CDC 日志），
把每张表的当前状态**重建**出来，通过 JDK 内置 HTTP Server 提供接口。

* **纯后端、无界面、零第三方依赖**：只用 JDK（`com.sun.net.httpserver.HttpServer`、
  `java.nio`）。JSON 解析/序列化、持久化、测试框架全部手写，无需 Maven/Gradle、无需联网。
* 每条变更记录：**表、主键（支持复合主键）、旧值、新值、源位置（position）**。
* 事务语义：
  * **事务提交前不可见**——DATA 只按事务缓存，COMMIT 才整体生效，ROLLBACK 丢弃；
  * **源位置幂等**——重复投递已消费位置直接忽略，绝不重复生效；
  * **缺口暂停而非跳过**——只接受紧邻的下一个位置，出现缺口进入 `PAUSED`，
    更大的位置先持久化到“滞留区”等待，缺口补齐后自动恢复 `RUNNING`；
  * **先持久化后改内存（WAL）**——每条事件先 `append + fsync` 再应用，
    崩溃后重放日志恢复，包括**跨重启的半事务**（未 COMMIT 的事务重启后仍 OPEN、仍不可见）。
* 内置**源事务解释器**参考实现，`GET /reconcile` 可随时核对消费器重建的表与源解释结果完全一致。

---

## 1. 环境与依赖

| 项 | 要求 | 本机实测 |
|---|---|---|
| JDK | Java 17+ | OpenJDK 17.0.20.1 |
| 第三方 jar | **无** | 0 个 |
| 构建工具 | 不需要（直接 `javac`） | — |

依赖锁定见 [`dependencies.lock`](dependencies.lock)。因为没有任何外部坐标，
该文件锁定的是 JDK 版本与“仅使用 `java.base`”这一事实。

## 2. 目录结构

```
src/com/example/cdc/
  Main.java       入口（启动 WAL 恢复 + HTTP 服务）
  ApiServer.java  JDK HttpServer 路由
  Engine.java     消费引擎：事务边界/幂等/缺口暂停/WAL 重放 + 源事务解释器与对账
  Wal.java        append-only 预写日志（每条 fsync；启动修复崩溃残行）
  Event.java      事件模型与校验（DATA/COMMIT/ROLLBACK；INSERT/UPDATE/DELETE）
  Json.java       零依赖 JSON 解析/序列化
test/com/example/cdc/
  RunTests.java   测试入口
  Test.java       迷你断言框架
  Ev.java         事件构造器
  EngineTest.java 引擎语义 + 随机故障注入对账（14 个）
  HttpTest.java   HTTP 端到端 + 真实重启（6 个）
build.sh  test.sh  run.sh  demo.sh
```

## 3. 构建 / 测试 / 启动

```bash
./build.sh          # 编译到 out/（等价: javac -d out $(find src -name '*.java')）
./test.sh           # 编译并运行全部 20 个自动化测试
./run.sh            # 启动，默认端口 8080、数据目录 ./data
./run.sh 9090 /tmp/cdc-data        # 自定义端口与数据目录
# 也可用环境变量：CDC_PORT / CDC_DATA_DIR
```

Windows / 无 bash 时的等价命令：

```bat
javac -d out src\com\example\cdc\*.java
java -cp out com.example.cdc.Main 8080 data
```

一键端到端演示（自动起停服务、curl 覆盖全部关键场景）：

```bash
./demo.sh           # 可选传端口: ./demo.sh 18080
```

## 4. 数据模型（事件格式）

投递 `POST /events`，请求体是事件数组（也支持 `{"events":[...]}`）。
三种事件共用一条**全局连续位置**序列 `position`（从 1 开始，逐 1 递增）：

```jsonc
// 行变更
{"position":1,"type":"DATA","txn":"T1","table":"users","op":"INSERT",
 "pk":[10],                       // 主键=JSON数组，支持复合主键 ["a",1]
 "oldPk":null,                    // UPDATE 改主键时给旧主键，其余省略
 "oldValues":{"...": "..."},      // 可选：变更前列值
 "newValues":{"id":10,"name":"alice"}}
{"position":2,"type":"COMMIT","txn":"T1"}
{"position":3,"type":"ROLLBACK","txn":"T1"}   // ABORT 视为 ROLLBACK
```

* `op` ∈ `INSERT | UPDATE | DELETE`；`type` ∈ `DATA | COMMIT | ROLLBACK`。
* **主键变更**用 `UPDATE` + `oldPk` 表达：删旧主键行、写新主键行，事务内原子完成。
* 行内容以 `newValues` 为准（建议包含主键列）；UPDATE 在已有行上做列合并。

## 5. HTTP 接口与请求样例

| 方法/路径 | 说明 |
|---|---|
| `POST /events` | 投递事件数组；返回 `accepted/duplicates/bufferedInGap/state/watermark/notes` |
| `GET /status` | 状态：`RUNNING/PAUSED`、水位、下一个期望位置、滞留位置、打开事务 |
| `GET /tables` | 所有表的**已提交**当前行 |
| `GET /tables/{name}` | 单表当前行 |
| `GET /changes?table=xx` | 已提交变更台账（表/主键/旧值/新值/DATA 位置/提交位置） |
| `GET /txns/{id}` | 事务是否 OPEN、已缓存几条 DATA（用于观察提交前不可见） |
| `GET /reconcile` | 用源事务解释器重算并与当前表深比较，返回 `match` 与差异 |
| `POST /reset` | 清空内存状态与 WAL |
| `GET /health` | 健康检查 |

curl 样例：

```bash
# 1) 一个事务：插入 + 改主键（10 -> 11），提交前查不到
curl -s localhost:8080/events -H 'Content-Type: application/json' -d '[
 {"position":1,"type":"DATA","txn":"T1","table":"users","op":"INSERT","pk":[10],
  "newValues":{"id":10,"name":"alice"}},
 {"position":2,"type":"DATA","txn":"T1","table":"users","op":"UPDATE","pk":[11],
  "oldPk":[10],"newValues":{"id":11,"name":"alice2"}}
]'
curl -s localhost:8080/tables/users          # => []  （提交前不可见）
curl -s localhost:8080/txns/T1               # => {"open":true,...}

# 2) 提交：旧主键行消失，新主键行可见
curl -s localhost:8080/events -d '[{"position":3,"type":"COMMIT","txn":"T1"}]'
curl -s localhost:8080/tables/users          # => [{"id":11,"name":"alice2"}]

# 3) 回滚事务：数据不留痕
curl -s localhost:8080/events -d '[
 {"position":4,"type":"DATA","txn":"T2","table":"users","op":"INSERT","pk":[12],
  "newValues":{"id":12,"name":"bob"}},
 {"position":5,"type":"ROLLBACK","txn":"T2"}]'

# 4) 幂等：重发历史事件，全部 duplicates，不产生新行
curl -s localhost:8080/events -d '[
 {"position":1,"type":"DATA","txn":"T1","table":"users","op":"INSERT","pk":[10],
  "newValues":{"id":10,"name":"alice"}},
 {"position":3,"type":"COMMIT","txn":"T1"}]'

# 5) 缺口：跳过 6 直接发 8 -> PAUSED，8 不生效
curl -s localhost:8080/events -d '[{"position":8,"type":"DATA","txn":"T3",
 "table":"users","op":"INSERT","pk":[13],"newValues":{"id":13,"name":"carol"}}]'
curl -s localhost:8080/status                # state=PAUSED, nextExpected=6

# 6) 补齐 6、7；8 自动排空进入 OPEN 的 T3，再 COMMIT 9
curl -s localhost:8080/events -d '[
 {"position":6,"type":"DATA","txn":"T9","table":"orders","op":"INSERT","pk":["o1"],
  "newValues":{"amount":100}},
 {"position":7,"type":"COMMIT","txn":"T9"}]'
curl -s localhost:8080/events -d '[{"position":9,"type":"COMMIT","txn":"T3"}]'

# 7) 跨重启半事务：发 10、11 不提交，重启进程
curl -s localhost:8080/events -d '[
 {"position":10,"type":"DATA","txn":"T4","table":"users","op":"INSERT","pk":[20],
  "newValues":{"id":20,"name":"dave"}},
 {"position":11,"type":"DATA","txn":"T4","table":"users","op":"INSERT","pk":[21],
  "newValues":{"id":21,"name":"erin"}}]'
# kill 后重新 ./run.sh（同一数据目录）
curl -s localhost:8080/txns/T4               # 重启后仍 open=true, dataCount=2
curl -s localhost:8080/tables/users          # 仍看不到 T4
curl -s localhost:8080/events -d '[{"position":12,"type":"COMMIT","txn":"T4"}]'

# 8) 对账：最终表必须与源事务解释器一致
curl -s localhost:8080/reconcile             # => {"match":true,...}
curl -s localhost:8080/changes?table=users   # 已提交变更台账
```

## 6. 关键语义与实现说明

### 6.1 消费流程（`Engine.ingest`，全程同一把锁）
对请求数组逐条处理：
1. `position <= 水位` 或已在滞留区 → **重复**，忽略（计数 `duplicates`）；
2. `position == 水位+1` → 先做事务协议校验（如 COMMIT 未知事务会在落盘前 400 拒绝，
   避免毒化只能追加的 WAL），再 `WAL.append + fsync`，应用并**连续排空滞留区**；
3. `position > 水位+1` → `append + fsync` 后放入滞留区，状态置 `PAUSED`，**绝不跳过缺口**。

### 6.2 事务边界与可见性
DATA 按 `txn` 缓存在内存；COMMIT 时按到达顺序把该事务的所有行变更应用到表并写台账；
ROLLBACK 仅移除缓存。查询接口只读已提交的 `tables`，因此**提交前绝对不可见**。
交错事务（T1、T2 的 DATA 交替到达）按各自 txn 独立缓存、互不影响。

### 6.3 主键变更
`UPDATE` 带 `oldPk` 时：先从表中移除 `oldPk` 行，再把合并后的新行写入 `pk`。
主键用 JSON 数组的**规范化紧凑字符串**（`Json.write`）做 Map 键，天然支持复合主键。

### 6.4 持久化与崩溃恢复（WAL）
* 每条事件是 WAL 中一行 JSON（UTF-8），写完即 `FileChannel.force(false)`（fsync）。
* 启动 `Wal.open` 逐行校验：只有**以换行结尾且能解析**的行才算完整记录；
  末尾无换行/解析失败的残行（崩溃半写）直接截断，绝不跳过坏行继续。
* `Engine.recover` 重放：WAL 按**到达顺序**追加，缺口期间靠后的位置可能先落盘
  （物理行如 `…2,5,6,3,4`），因此重放前先按 `position` 排序恢复逻辑全序；
  连续前缀正常走事务生命周期（未结束事务自然恢复为 OPEN，即**半事务**），
  仍大于水位+1 的（崩溃前滞留区事件）重建进滞留区并恢复 `PAUSED`。

### 6.5 源事务解释器与对账（验收口径）
`Engine.interpret(log)` 是独立的参考实现：对位置严格 1..N 连续的源日志，
按事务边界（DATA 缓存 / COMMIT 应用 / ROLLBACK 丢弃）解释出每张表应有的行。
`GET /reconcile` 用 WAL 全量事件跑解释器，与在线引擎的表状态做 JSON 规范化深比较，
任何不一致都会列出表级差异。自动化测试即以此作为最终验收口径。

## 7. 验收点与测试对应关系

| 验收要求 | 覆盖测试 |
|---|---|
| 主键变更（含复合主键） | `主键变更…`、`复合主键…`、HTTP 全流程 |
| 事务回滚 | `事务回滚…`、`HTTP 回滚后查询为空…` |
| 提交前不可见 | `提交前不可见…`、`GET /txns` 用例 |
| 重复位置幂等 | `重复位置幂等…`、随机场景重发、HTTP 重发 |
| 缺口暂停而非跳过 | `位置缺口…`、`HTTP 缺口返回 PAUSED…` |
| 跨重启半事务 | `跨重启半事务…`、`HTTP 跨重启半事务…` |
| 跨重启重放 | `跨重启重放…`、随机场景中 7+ 次重启 |
| WAL 崩溃残行 | `WAL 半行…` |
| 与源事务解释器一致 | `随机故障注入…对账一致`、各 `/reconcile` 用例 |

随机故障注入场景（固定种子，可复现）：87 条源事件、24 个事务（约 1/5 回滚、
含 INSERT/UPDATE/DELETE/主键变更/交错事务），投递时随机制造位置缺口、随机重发历史、
随机重启进程，最终全部补齐后与解释器逐表对账。

## 8. 实测结果

见仓库根目录 `RUNBOOK.md`（记录本机实际运行 `./test.sh` 与 `./demo.sh` 的输出、
环境与未完成项）。

## 9. 边界与取舍（已知限制）

* 单节点、单实例嵌入式设计；HTTP 用单线程 executor，请求天然串行，未做分布式/复制。
* 状态全部在内存，WAL 只追加不压缩、无快照；超大日志下重放时间与 WAL 体积线性增长。
  （正确性不受影响，吞吐/容量未作为本次目标。）
* 位置是全局单调连续整数，不支持乱序提交后“永久跳过”；缺数据必须补齐（符合需求）。
* 对源端语义采取“宽容”应用：重复 INSERT 同主键按覆盖处理、UPDATE/DELETE 不存在的主键
  不报错——在线引擎与参考解释器采用同一套规则，保证对账口径一致。
* 无鉴权/TLS，仅适合本地或受信网络；`/reset` 会清空数据，勿暴露到公网。
