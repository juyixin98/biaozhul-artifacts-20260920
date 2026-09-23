# 变更流表状态重建（CDC Change-Stream State Rebuild）

纯 Java 后端服务：消费带事务边界的变更日志（CDC 事件流），重建出与“源事务解释器”
一致的最终表状态。**无界面**，只提供 HTTP 接口。

- **零第三方依赖**：仅用 JDK 17 标准库（含内置 `com.sun.net.httpserver.HttpServer`）。
- **事务边界**：BEGIN / DATA / COMMIT / ROLLBACK；COMMIT 前数据不可见，ROLLBACK 整体丢弃。
- **源位置（pos）**：每条事件携带从 1 开始严格递增的位置；含表名、主键、整行旧值/新值。
- **幂等**：重复位置按内容比对，一致则幂等返回，不一致返回 409。
- **缺口不跳过**：收到 pos 越过下一个期望值时进入 `PAUSED`，未来事件只缓冲不落盘，
  缺口补齐后自动按序排空并恢复 `RUNNING`。
- **崩溃恢复**：事件先 fsync 落 WAL 再改内存状态；重启后重放 WAL，
  半事务（已 BEGIN 未 COMMIT）恢复为 open 且仍不可见，等待补 COMMIT/ROLLBACK；
  WAL 末尾半条损坏记录自动截断修复。

---

## 1. 依赖与环境

| 项 | 要求 |
|---|---|
| JDK | **17 或更新**（开发/实测：OpenJDK 17.0.20） |
| 构建工具 | **不需要** Maven/Gradle，直接 `javac`；脚本见 `build.sh` |
| 第三方依赖 | **无**（见 `deps.lock`，依赖列表为空） |
| 运行命令 | `curl`（演示脚本用到） |

确认环境：

```bash
java -version    # 需要 17+
```

---

## 2. 构建与启动

```bash
# 编译主代码与测试到 build/（首次会自动创建）
./build.sh

# 启动（默认端口 8080、数据目录 ./data、主键列 id）
./run.sh

# 或带参数：端口 / 数据目录 / 主键列名
./run.sh 9090 ./mydata id
# 也支持环境变量 PORT / DATA_DIR / PK_COLUMN
PORT=9090 DATA_DIR=./mydata ./run.sh
```

等价的手工命令：

```bash
javac -d build/classes $(find src/main/java -name '*.java')
java -cp build/classes cdcrebuild.Main 8080 ./data id
```

启动成功后：

```
变更流表状态重建服务已启动
  监听端口 : 8080
  数据目录 : /.../data
  主键列   : id
```

---

## 3. 运行测试

```bash
./build.sh        # 如尚未编译
./test.sh         # 运行全部 34 个自动化测试，失败时退出码非 0
```

测试为自研零依赖框架（`src/test/java/cdcrebuild/test/`），含四个套件：

| 套件 | 内容 |
|---|---|
| 核心引擎（16） | 提交可见性、回滚、主键变更、字符串主键、幂等/冲突、缺口暂停与补齐、交错事务、约束冲突、参数校验、删后重插 |
| 重启/半事务/WAL（4） | 跨重启半事务补 COMMIT/ROLLBACK、WAL 半条尾部截断修复、重启后幂等 |
| 随机对账（5） | 5 个固定种子生成合法随机流（交错事务/主键变更/回滚/多表），制造乱序缺口并在 3 个切点重启，最终与源事务解释器逐键逐字段对账 |
| HTTP 端到端（9） | 真实 JDK HttpServer + HttpClient，覆盖全部状态码与验收场景 |

最近一次实测结果记录在 [`docs/TEST-RESULTS.md`](docs/TEST-RESULTS.md)。

---

## 4. HTTP 接口

所有请求/响应均为 `application/json; charset=utf-8`。

### 4.1 `POST /v1/events` —— 投递一条事件

事件结构：

```json
{
  "pos": 12,
  "txId": "tx-001",
  "type": "BEGIN | DATA | COMMIT | ROLLBACK",

  "table": "users",                 // 仅 DATA
  "op": "INSERT | UPDATE | DELETE", // 仅 DATA
  "old": { "id": 1, "name": "a" },  // UPDATE/DELETE 必填（整行旧值）
  "new": { "id": 9, "name": "b" },  // INSERT/UPDATE 必填（整行新值）
  "pkChanged": true                 // 仅 UPDATE；主键是否改变，必须与 old/new 主键一致
}
```

响应 `status` 字段：

- `DURABLE`（HTTP 200）：顺序事件已 fsync 落盘并应用。
- `DUPLICATE`（HTTP 200）：重复位置且内容一致，幂等成功。
- `BUFFERED`（HTTP 202）：属于缺口之后的未来事件，已缓冲，消费暂停。

错误：`400` 结构非法；`409` 语义冲突（未 BEGIN 的 DATA、重复位置内容不一致、
主键冲突、目标行不存在等）。

### 4.2 查询接口

| 方法与路径 | 说明 |
|---|---|
| `GET /healthz` | 存活探针 |
| `GET /v1/status` | 消费状态：state、lastDurablePos、nextExpectedPos、highestSeenPos、lastVisiblePos、firstMissingPos、gapError、bufferedCount、openTxIds、各表行数、WAL 字节数 |
| `GET /v1/tables` | 所有表的**已提交**快照（未提交数据不出现） |
| `GET /v1/tables/{table}` | 单表快照 |
| `GET /v1/tables/{table}/row/{key}` | 按主键查行；`key` 是 JSON 编码，数字直接写 `1`，字符串写 URL 编码后的 `"alice"`（如 `%22alice%22`） |
| `GET /v1/log?from=&to=` | 已结束事务（COMMIT/ROLLBACK）审计，按结束位置过滤，含每条 DATA 的表/主键/旧值/新值/位置 |

---

## 5. 请求样例（curl）

完整可运行脚本：[`examples/demo.sh`](examples/demo.sh)（自动启停、含一次真实重启）：

```bash
./examples/demo.sh 18090
```

手工样例：

```bash
BASE=http://localhost:8080

# 事务 1：插入两行后提交
curl -s -XPOST $BASE/v1/events -H 'Content-Type: application/json' \
  -d '{"pos":1,"txId":"t1","type":"BEGIN"}'
curl -s -XPOST $BASE/v1/events -H 'Content-Type: application/json' \
  -d '{"pos":2,"txId":"t1","type":"DATA","table":"users","op":"INSERT","new":{"id":1,"name":"alice"}}'
curl -s -XPOST $BASE/v1/events -H 'Content-Type: application/json' \
  -d '{"pos":3,"txId":"t1","type":"DATA","table":"users","op":"INSERT","new":{"id":2,"name":"bob"}}'
curl -s -XPOST $BASE/v1/events -H 'Content-Type: application/json' \
  -d '{"pos":4,"txId":"t1","type":"COMMIT"}'

# 事务 2：回滚（id=3 永远不可见）
curl -s -XPOST $BASE/v1/events -H 'Content-Type: application/json' \
  -d '{"pos":5,"txId":"t2","type":"BEGIN"}'
curl -s -XPOST $BASE/v1/events -H 'Content-Type: application/json' \
  -d '{"pos":6,"txId":"t2","type":"DATA","table":"users","op":"INSERT","new":{"id":3,"name":"carol"}}'
curl -s -XPOST $BASE/v1/events -H 'Content-Type: application/json' \
  -d '{"pos":7,"txId":"t2","type":"ROLLBACK"}'

# 主键变更：id=1 -> id=10
curl -s -XPOST $BASE/v1/events -H 'Content-Type: application/json' \
  -d '{"pos":8,"txId":"t3","type":"BEGIN"}'
curl -s -XPOST $BASE/v1/events -H 'Content-Type: application/json' \
  -d '{"pos":9,"txId":"t3","type":"DATA","table":"users","op":"UPDATE",
       "old":{"id":1,"name":"alice"},"new":{"id":10,"name":"alice2"},"pkChanged":true}'
curl -s -XPOST $BASE/v1/events -H 'Content-Type: application/json' \
  -d '{"pos":10,"txId":"t3","type":"COMMIT"}'

# 幂等：重复 pos=10 且内容相同 -> DUPLICATE
curl -s -XPOST $BASE/v1/events -H 'Content-Type: application/json' \
  -d '{"pos":10,"txId":"t3","type":"COMMIT"}'

# 缺口：直接发 pos=13（缺 12）-> 202 BUFFERED + state=PAUSED
curl -s -XPOST $BASE/v1/events -H 'Content-Type: application/json' \
  -d '{"pos":11,"txId":"t4","type":"BEGIN"}'
curl -s -XPOST $BASE/v1/events -H 'Content-Type: application/json' \
  -d '{"pos":13,"txId":"t4","type":"DATA","table":"users","op":"INSERT","new":{"id":4,"name":"dave"}}'
curl -s $BASE/v1/status

# 补齐 pos=12，缓冲的 13 自动排空，恢复 RUNNING
curl -s -XPOST $BASE/v1/events -H 'Content-Type: application/json' \
  -d '{"pos":12,"txId":"t4","type":"DATA","table":"users","op":"INSERT","new":{"id":5,"name":"erin"}}'

# 查询
curl -s $BASE/v1/tables/users
curl -s "$BASE/v1/log?from=1&to=14"
```

---

## 6. 关键语义（与验收点对照）

1. **事务提交前不可见**：DATA 只进入该事务的暂存区，`/v1/tables` 仅在 COMMIT 后反映；
   未提交时 `lastVisiblePos` 停在上一个已结束事务。
2. **主键变更**：UPDATE 且 `pkChanged=true` 时 old/new 主键必须不同，提交时
   **先删旧键再插新键**；旧键随后查行返回 404。
3. **事务回滚**：ROLLBACK 后暂存的所有变更丢弃，表不变；审计日志记录
   `committed=false` 与全部原始 DATA。
4. **重复位置幂等**：`pos <= lastDurablePos` 时用规范化 JSON（键排序）比对内容，
   一致返回 `DUPLICATE` 且不重复应用，不一致返回 409。
5. **发现缺口暂停而非跳过**：`pos > nextExpectedPos` 只进内存缓冲区并置 `PAUSED`，
   `firstMissingPos` 指明缺口起点；缺口事件到达后从缓冲区连续排空。
6. **跨重启半事务**：WAL 重放会重建 open 事务（`openTxIds` 可见），其数据仍不可见；
   之后可补 COMMIT（生效）或 ROLLBACK（丢弃）。
7. **与源事务解释器一致**：`ReferenceInterpreter`（测试代码）是无持久化、无乱序的
   参考实现；随机对账测试断言引擎最终快照与它逐键逐字段相同。

### 崩溃与写入顺序

每个顺序事件的处理是：**语义校验 → WAL 追加并 `fsync` → 更新内存表**。
WAL 帧格式 `[长度][CRC32][JSON 载荷][CRC32]`，打开时扫描，CRC/长度异常的尾部
（崩溃时写了一半）被截断到上一条完整记录，完整记录全部保留。

---

## 7. 目录结构

```
.
├── build.sh / test.sh / run.sh      # 纯 JDK 构建 / 测试 / 启动脚本
├── deps.lock                        # 依赖锁定（零第三方依赖 + JDK 版本）
├── README.md
├── docs/TEST-RESULTS.md             # 实测记录
├── examples/demo.sh                 # curl 端到端演示（含重启）
└── src
    ├── main/java/cdcrebuild
    │   ├── Main.java                # 入口
    │   ├── codec/Json.java          # 零依赖 JSON 解析/序列化/规范化
    │   ├── model/Event.java         # 事件模型与校验
    │   ├── engine/Wal.java          # CRC + fsync 的只追加日志与崩溃修复
    │   ├── engine/CdcEngine.java    # 事务状态机 / 幂等 / 缺口暂停 / 重放
    │   └── http/ApiServer.java      # JDK HttpServer 接口
    └── test/java/cdcrebuild/test    # 自研测试框架 + 34 个用例
```

---

## 8. 范围与未完成项（如实说明）

- 这是**单进程内存态 + 单文件 WAL** 的教学/验收实现：表状态在内存，重启靠重放 WAL，
  WAL 不做分段/压缩/快照（checkpoint）。超大数据集或长期运行需要加分段与快照，本项目未实现。
- 缺口期间的“未来事件”只缓存在**内存**：暂停状态下再次崩溃，这些未确认事件需要上游重发
  （它们本就未返回 DURABLE）；已 fsync 的连续事件不丢。
- pos 使用单个全局严格递增序列（未实现多分区/多源的位置合并）。
- 无鉴权/TLS/限流；HTTP 为内网使用设计。
- 并发模型为引擎单锁串行化（保证正确性），未做批量摄入或高吞吐优化。
- 校验假设上游流基本合法：提交时若出现主键冲突/目标行不存在等，返回 409 并停在该位置，
  需上游修正后重投（测试“INSERT 主键冲突在 COMMIT 时报错”演示了该行为）。
