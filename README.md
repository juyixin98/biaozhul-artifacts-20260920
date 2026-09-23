# txflow：事务性流快照（Transactional Stream Snapshot）

单机流算子**检查点服务**。算子状态、输入偏移和输出提交标记构成一个**一致快照**；
输出端是一个**本地事务接收器（local transactional file sink）**，通过
“暂存 → 原子发布 → 提交标记”协议保证：在任意阶段注入硬崩溃并重放后，
接收器的**可见输出无遗漏、无重复**。

- 纯后端，无界面
- **零第三方依赖**：只用 JDK（HTTP 用 JDK 内置 `com.sun.net.httpserver.HttpServer`）
- Java 17，`javac` 直接编译，无需 Maven/Gradle/联网

> **保证范围（重要）**：exactly-once 语义**只覆盖本服务内置的本地文件接收器**
> （数据目录 `sink/` 下的文件）。它**不**对任意外部系统（数据库、消息队列、
> 远程 HTTP、其它进程写入的文件）提供事务保证——对端不参与同一提交协议时，
> 网络层面的重复发送无法消除。本服务也不向任何外部系统投递数据。

---

## 1. 目录结构

```
src/com/example/txflow/
  HttpApiServer.java   HTTP 接口（JDK HttpServer）+ main 入口
  Engine.java          处理引擎：微批事务、四个故障点、崩溃恢复对账
  Checkpoint.java      一致快照模型 + 原子快照存储（tmp+fsync+rename）
  LocalTxnSink.java    本地事务接收器：prepare/commit/recover + committed.log
  InputLog.java        append-only 输入日志（length-prefixed，offset=记录序号）
  WordCountOperator.java  有状态流算子（word count），每条输入一条输出
  FileIO.java          原子写/fsync 原语
  Json.java            内置极简 JSON 解析/输出
  Crash.java           Runtime.halt 故障注入
tests/                 进程内测试 + 真实子进程崩溃恢复测试
scripts/build.sh       编译
scripts/run.sh         启动服务
scripts/test.sh        编译并运行全部测试
scripts/examples.sh    curl 请求样例
dependencies.lock      依赖锁定（零第三方依赖，仅 JDK 17）
```

## 2. 构建与启动

需要 JDK 17+（仅 `javac`/`java`，无其它依赖）。

```bash
# 编译 -> out/
bash scripts/build.sh

# 启动（默认 127.0.0.1:8080，数据目录 ./txflow-data）
bash scripts/run.sh

# 可选环境变量
TXFLOW_HOST=127.0.0.1 TXFLOW_PORT=8080 TXFLOW_DATA=./txflow-data bash scripts/run.sh
```

启动时会**自动执行一次恢复对账**（正常启动为空操作），日志示例：

```
[txflow] 启动恢复完成: {"sink":{"committedTxns":2,"actions":[]}, ...}
[txflow] 监听 http://127.0.0.1:8080  数据目录=/.../txflow-data
```

手动请求样例：

```bash
bash scripts/examples.sh                 # 默认 http://127.0.0.1:8080
BASE=http://127.0.0.1:8080 bash scripts/examples.sh
```

## 3. HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET  | `/health` | 健康检查，响应含保证范围声明 |
| POST | `/inputs` | 追加输入记录，body `{"text":"a b c"}`，返回 `offset` |
| POST | `/process` | 处理一个微批 = 一个接收器事务。body `{"maxRecords":N,"failPoint":"..."}` |
| POST | `/recover` | 显式恢复对账（幂等；启动已自动执行） |
| GET  | `/state` | 快照视图：输入总数、lastCommittedOffset、committedCount、hasPrepared、状态 |
| GET  | `/outputs` | 接收器**已提交可见**输出（每行一个 JSON，带 `inputOffset`） |
| GET  | `/markers` | `committed.log` 提交标记列表（txnId/endOffset/sha256） |

### 请求样例

```bash
curl -s -X POST localhost:8080/inputs -H 'Content-Type: application/json' -d '{"text":"a b a"}'
# {"appended":true,"offset":0}

curl -s -X POST localhost:8080/process -H 'Content-Type: application/json' -d '{"maxRecords":10}'
# {"processed":1,"beginOffset":0,"endOffset":0,"txnId":0,"state":{"counts":{"a":2,"b":1}}}

curl -s localhost:8080/state
curl -s localhost:8080/outputs
curl -s localhost:8080/markers
```

### 故障注入

`POST /process` 的 `failPoint` 在处理流水线上的四个有序边界注入
`Runtime.getRuntime().halt(17)`（**不执行 shutdown hook、不清理缓冲**，模拟掉电 / kill -9）：

| failPoint | 崩溃位置 |
|---|---|
| `AFTER_PROCESS` | 记录已在内存处理完，状态**尚未落盘** |
| `AFTER_STATE_PERSIST` | 原子快照已写入（含 prepared：状态+偏移+输出摘要），接收器**未提交** |
| `AFTER_OUTPUT_PREPARE` | 输出已暂存并原子发布、快照已登记 prepared，接收器**未提交**（与上一位置紧邻，覆盖“输出准备”与“提交”之间） |
| `AFTER_COMMIT` | 接收器提交标记已落盘（输出**已可见**），提交点快照**尚未提升** |

```bash
curl -s -X POST localhost:8080/process -d '{"maxRecords":2,"failPoint":"AFTER_COMMIT"}'
# 连接中断，进程退出码 17；随后重新 bash scripts/run.sh 即自动恢复
```

## 4. 一致性快照与提交协议

### 数据布局

```
txflow-data/
  input.log                 输入：[8字节长度][JSON] 帧序列，append+fsync
  checkpoint.json           一致快照（整体原子替换，另有 .tmp 仅在写入瞬间存在）
  sink/
    staging/txn-N.out       事务私有暂存（对外不可见）
    outputs/txn-N.out       原子发布的输出文件（rename 自 staging）
    committed.log           提交标记：每行 "txnId,endOffset,sha256"，append+fsync
```

**可见性不变量**：一条输出可见 ⟺ 它在 `outputs/` 中**且**在 `committed.log` 有标记。
`committed.log` 是接收器可见性的唯一权威。

### 处理一个微批（一个接收器事务）的有序步骤

```
1. 处理：从 lastCommittedOffset+1 读记录，在状态副本上逐条计算并收集输出
   ── 故障点 AFTER_PROCESS
2. 输出准备：sink.prepare() 写 staging（fsync）→ 原子 rename 到 outputs/
3. 状态落盘：快照原子写入 prepared{txnId, begin/endOffset, 输出文件名+SHA-256,
   stateAfter}（tmp + fsync + rename + 父目录 fsync）
   ── 故障点 AFTER_STATE_PERSIST / AFTER_OUTPUT_PREPARE
4. 接收器提交：committed.log 追加标记（append+fsync，重复提交幂等）
   ── 故障点 AFTER_COMMIT
5. 提升提交点快照：lastCommittedOffset=endOffset、committedCount++、prepared=null，
   再原子写一次快照
```

步骤 2~5 的顺序使**状态、输入偏移、输出提交标记**三者被对齐到同一个快照版本：
快照要么看不到 prepared（旧提交点，接收器也无该事务标记），要么看到完整 prepared
（输出文件已就位、摘要可校验），不存在撕裂中间态。

### 崩溃恢复（启动时自动执行）

接收器先自行对账：删除 `staging/` 残留；删除 `outputs/` 中**无提交标记**的文件
（它们在崩溃窗口中发布过但从未可见）；有标记却缺文件则**拒绝启动并报错**，绝不静默补写。

然后对齐快照：

- **快照 `prepared=null`**：快照版本与接收器一致，无需动作；
  启动后从 `lastCommittedOffset+1` 继续。
- **快照有 prepared、接收器已有标记**（AFTER_COMMIT 窗口）：校验标记的 endOffset/摘要后
  **直接提升快照**，输出不重放、不重复。
- **快照有 prepared、接收器无标记**（AFTER_STATE_PERSIST / AFTER_OUTPUT_PREPARE 窗口）：
  **丢弃 prepared**，快照回到上个提交点；未提交输出文件已被接收器隐藏；
  该段记录下次 `/process` 从输入日志**重放**并生成新事务（新 txnId、新暂存文件）。
- **AFTER_PROCESS 窗口**：什么都没落盘，等价于该微批从未发生，直接重放。

因为（a）提交标记 append 幂等、（b）未标记输出一律不可见、（c）重放起点严格取自
`lastCommittedOffset+1`，接收器可见输出在任意崩溃组合下均满足
**每个 inputOffset 恰好一条可见输出**。

## 5. 自动化测试

```bash
bash scripts/test.sh
```

两组测试，**零第三方测试框架**（自带约 40 行断言工具）：

1. **`InProcessTest`（47 个断言）**：JSON、输入日志 offset 语义、接收器 prepare 后不可见/
   commit 后可见/重复 commit 幂等/staging 与未提交文件回收、引擎正常路径、
   prepared 回滚重放、commit 后快照提升、空转。
2. **`CrashRecoveryTest`（154 个断言）**：对 `NONE` 与全部四个故障点各起**独立 JVM 子进程**，
   真实触发 `Runtime.halt(17)`，用同一数据目录重启两次，并附加一个“同一数据目录连续 20+ 次
   交替故障点崩溃”的压力场景。每次都机械核对：
   - 可见输出条数恰为输入条数；
   - `inputOffset` 集合恰为 `{0..N-1}`（无遗漏）；
   - 每个 offset 恰好出现一次（无重复）且顺序严格递增；
   - 最终 word-count 与无故障对照一致；
   - `committed.log` 与快照对账一致、无悬挂 prepared。
   同时断言了各故障点崩溃后**重启前的磁盘中间态**（例如 AFTER_COMMIT：标记已 3 条、
   快照仍 prepared=true），证明故障确实注入在指定窗口。

### 实测结果（2026-09-23，本机如实记录）

环境：Ubuntu 24.04 容器（无 root），JDK 为用户目录解压的
`OpenJDK 17.0.20.1 (17.0.20.1+1-1~24.04, amd64)`。

```
# 1/2 进程内测试：    47 通过, 0 失败
# 2/2 崩溃恢复测试： 154 通过, 0 失败
全部测试通过 ✅
```

覆盖到的崩溃窗口与重启前中间态（6 条输入，基线先提交 offset 0、1，
崩溃批处理 offset 2、3）：

| 故障点 | 重启前快照 prepared | committed.log 标记 | outputs 文件 | 重启后结果 |
|---|---|---|---|---|
| AFTER_PROCESS | false | 2 | 2 | 重放，6/6 无重复 |
| AFTER_STATE_PERSIST | true | 2 | 3（1 个未提交被隐藏） | 回滚重放，6/6 |
| AFTER_OUTPUT_PREPARE | true | 2 | 3（1 个未提交被隐藏） | 回滚重放，6/6 |
| AFTER_COMMIT | true | 3 | 3 | 提升快照，6/6 不重放 |
| NONE（对照） | false | 3 | 3 | 6/6 |

连续崩溃场景：同一数据目录经历 24 次 `halt(17)`（四个故障点循环）后，
最终仍为 6 条可见输出、offset `{0,1,2,3,4,5}`、计数 `a=8 b=6 c=3`。

## 6. 设计取舍 / 已知边界（未完成与不做的项）

- **保证范围有意收窄**：只对内置本地文件接收器负责。若将来需要对接外部系统，
  需要外部系统参与事务（幂等键 + 去重表，或 XA/两阶段提交），本项目未实现。
- **单机**：无多副本、无 leader 选举；数据目录在一块磁盘上。原子 rename 与 fsync
  的耐久性依赖底层文件系统正确实现（代码在容器卷上做了目录 fsync，个别文件系统
  可能静默忽略，但不影响同一机内崩溃语义）。
- **微批粒度**：一次 `/process` 处理 1..N 条记录、封装成**一个**接收器事务；
  崩溃重放的最小单位是微批（未提交微批整体重算，重复计算被“未标记不可见”屏蔽，
  不产生重复可见输出）。
- **无后台自动处理循环**：处理由 `POST /process` 同步触发（便于精确注入故障点）；
  加一个定时循环是直接的扩展，本项目刻意不做，以保持故障窗口可控、可测。
- **输入日志无截断/保留策略**：所有输入永久保留在 `input.log`；生产系统需要
  checkpoint 后分段与删除，本项目未实现。
- **输入追加途中崩溃**：`input.log` 尾部可能留下半帧，`InputLog.readAll()` 会显式
  报错而非静默截断（宁可拒绝启动，不丢数据）。当前测试未覆盖该极端情况，
  恢复方式为人工修复尾部。
- **HTTP 层**：服务默认只监听 127.0.0.1，无鉴权、无 TLS；定位为本地单机工具，
  不应直接暴露到非可信网络。
- **JSON 实现**：内置解析器只支持本服务使用的子集（对象/数组/字符串/数字/
  true/false/null），非通用 JSON 库。
