# 事务性流快照（Transactional Streaming Snapshot）

单机流算子检查点服务：让 **算子状态、输入偏移、输出提交标记** 三者构成一个一致快照，
崩溃后重放，输出对接收器保证 **无遗漏、无重复可见输出（exactly-once 可见性）**。

纯后端，无界面。仅用 **JDK 标准库**（`com.sun.net.httpserver.HttpServer`），
**没有任何第三方依赖**（见 [`dependencies.lock`](dependencies.lock)），不需要 Maven/Gradle。

> 适用范围（重要）：输出是 **本地事务接收器**——prepare 暂存段与 commit 标记都在
> 本机同一目录。本服务的一致性保证 **只覆盖这个本地接收器自身**，
> **不是** 两阶段提交协调器，不对数据库、消息队列、远程服务等任意外部系统提供事务保证。

---

## 1. 它解决什么问题

一条输入的处理横跨四个会崩溃的阶段：

```
处理(内存)  →  输出 prepare(落盘)  →  状态+偏移快照(落盘)  →  输出 commit(标记)
```

任一步之间断电/被杀，重启后都可能产生：重复输出（commit 前以为没做、重放又做一次）、
丢失输出（做了但状态先于输出落盘）、状态与偏移错位。本项目用确定性事务 ID +
“prepare → 快照 → commit” 顺序 + 崩溃恢复协议，把这四种崩溃都收敛为 exactly-once。

### 协议（每个批次）

1. **输入先行落盘**：`input.log` 追加并 fsync，成功后才入队处理（输入不丢、可重放）。
2. **纯计算**：新状态、输出结果只在内存。
3. **prepare**：输出写暂存段 `sink/segments/txn-<offset>.rec`（fsync + 目录 fsync）。
4. **快照原子发布**：`{state, offset, pendingCommit=txn-<offset>}` 写临时文件 +
   fsync + 原子 rename + 切换 `current` 指针 + fsync 目录。指针只指向完整快照。
5. **commit**：向 `sink/commit.log` 追加一行事务 ID（fsync）。**完整一行**才算提交。

事务 ID 由输入偏移确定性派生（`txn-<offset>`），因此重放/恢复时提交天然幂等。

### 恢复协议（启动时自动执行）

1. `input.log` / `commit.log` 截掉末尾写了一半的行（无结尾 `\n`）。
2. 加载最新完整快照；其 `pendingCommit` 若未出现在 commit.log，则补写提交标记
   （它的暂存段在快照之前已落盘）；暂存段缺失则 **拒绝启动**，不猜测、不静默丢数据。
3. 其余 prepare 暂存段（既未提交也非快照悬空事务）一律 **回滚删除**。
4. 从 `snapshot.offset + 1` 开始重放 input.log。

崩溃点与后果：

| 崩溃点 | 磁盘现场 | 恢复动作 |
|---|---|---|
| 处理中 | 无新段、无新快照；输入已在 log | 从输入日志重放 |
| prepare 中 | 半个暂存段、无新快照 | 删除残段，重放 |
| 落盘后、commit 前 | 完整段 + 快照 pendingCommit + 无标记 | 补提交该悬空事务，继续重放 |
| commit 中 | 半行标记（无 `\n`） | 半行截掉=未提交，按 pending 路径补提交 |

算子语义：输入整数 `v`（偏移 `i`），状态 `state = Σ v`，输出记录
`{txn, offset, value, result=v²}`。

---

## 2. 目录与文件布局

```
src/txsnapshot/
  Main.java                 入口：服务模式 / --verify 只读校验模式
  ApiServer.java            JDK HttpServer 接口层（无第三方库）
  StreamEngine.java         单线程串行处理 + 提交协议 + 恢复重放
  CheckpointStore.java      状态/偏移/pendingCommit 一致快照（原子发布+指针）
  LocalTransactionalSink.java  本地事务接收器（prepare 段 + commit.log 标记）
  InputLog.java             输入 WAL（fsync，崩溃截尾）
  DurableFiles.java         原子 rename、fsync 文件/目录、只追加日志、半行截断
  Fault.java                故障注入点（HALT 杀进程 / EXCEPTION 进程内崩溃态）
  Json.java                 极简 JSON（零依赖）
  Verifier.java             只读一致性校验（不改文件）
test/test/                  自动化测试（零依赖断言框架 + 子进程驱动）
scripts/build.sh            编译
scripts/test.sh             编译并运行全部测试
scripts/demo.sh             端到端演示：故障注入→杀进程→重启→核对
dependencies.lock           锁定依赖：零第三方，JDK 17，java.base/jdk.httpserver
```

运行后的数据目录（默认 `./data`）：

```
data/
  input.log                     输入 WAL，每行 {"offset":N,"value":x}
  checkpoints/
    snapshot-N.json             一致快照（state/offset/pendingCommit/checksum）
    current -> snapshot-N.json  指向最新完整快照的符号链接（不支持时回退 current.txt）
  sink/
    commit.log                  只追加提交标记，每行一个事务 ID（完整行才算数）
    segments/txn-N.rec          prepare 暂存段（提交后保留为输出本体）
```

---

## 3. 环境与启动

**需要**：JDK 17 或更高（仅标准库）。无需联网、无需 Maven/Gradle、无需任何 jar。
本仓库在 **Eclipse Temurin JDK 17.0.20.1+1 (Linux x86_64)** 上实际编译、测试、演示通过。

```bash
# 编译（输出到 build/classes）
./scripts/build.sh

# 启动服务（默认 127.0.0.1:8080，数据目录 ./data）
java -cp build/classes txsnapshot.Main --port 8080 --data ./data --debug
#   --debug 会开启 /debug/fault 故障注入接口；生产可不带 --debug
```

脚本会自动发现 `JAVA_HOME`、PATH 中的 java，或仓库内 `.tools/jdk-*`。

### HTTP 接口与请求样例

```bash
# 健康检查
curl http://127.0.0.1:8080/health
#   {"status":"ok"}

# 摄入一条输入（同步：fsync 输入并处理/提交完成后返回）
curl -X POST http://127.0.0.1:8080/ingest \
  -H 'Content-Type: application/json' -d '{"value":10}'
#   {"offset":0,"committedOutputs":1}

# 查看状态（偏移/状态/快照 epoch/已可见输出数等）
curl http://127.0.0.1:8080/state
#   {"offset":0,"state":10,"epoch":1,"committedOutputs":1,"queued":0,
#    "nextInputOffset":1,"crashed":false}

# 查看所有已可见输出（按提交顺序）
curl http://127.0.0.1:8080/outputs
#   {"outputs":[{"txn":"txn-0","offset":0,"value":10,"result":100}]}

# 仅 --debug：武装一次性故障。下一条（或指定 offset 的）处理命中时触发
curl -X POST http://127.0.0.1:8080/debug/fault -H 'Content-Type: application/json' -d '{
  "point": "AFTER_STATE_PERSISTED",
  "mode":  "HALT",
  "offset": 3
}'
# point:  DURING_PROCESSING | DURING_PREPARE | AFTER_STATE_PERSISTED | DURING_COMMIT
# mode:   HALT（Runtime.halt 杀进程，模拟 kill -9/断电）
#         EXCEPTION（进程存活但引擎进入 crashed 态，等价于“本进程内已不可信，需重启”）
# offset: 可省略；省略时下一条任意偏移触发
```

### 只读校验器（不打开引擎、不修改任何文件）

```bash
java -cp build/classes txsnapshot.Main --verify --data ./data
# 退出码 0=一致；1=发现不一致；2=读取错误
```

校验：每个提交标记都有合法暂存段；提交标记无重复；可见输出偏移恰好为
`0..k-1`（无空洞、无重复可见输出）；快照偏移与提交数一致，或处于协议内合法的
“落盘后提交前”未决窗口。

---

## 4. 自动化测试与实际运行结果

```bash
./scripts/test.sh      # 编译主程序+测试并运行
```

测试在 **2026-09-23** 用 Temurin JDK 17.0.20.1+1 实跑，结果 **13/13 通过**：

```
PASS happy path: 5 inputs -> state, 5 visible outputs in order
PASS EXCEPTION fault at DURING_PROCESSING       then reopen + continue: exactly-once
PASS EXCEPTION fault at AFTER_STATE_PERSISTED   then reopen + continue: exactly-once
PASS EXCEPTION fault at DURING_COMMIT           then reopen + continue: exactly-once
PASS EXCEPTION fault at DURING_PREPARE          then reopen + continue: exactly-once
PASS crash-site on-disk shapes (torn marker / half segment / pending snapshot)
PASS reopen twice after crash: idempotent, still exactly-once
PASS subprocess Runtime.halt at DURING_PROCESSING      then recover: exactly-once
PASS subprocess Runtime.halt at AFTER_STATE_PERSISTED  then recover: exactly-once
PASS subprocess Runtime.halt at DURING_COMMIT          then recover: exactly-once
PASS subprocess Runtime.halt at DURING_PREPARE         then recover: exactly-once
PASS HTTP end-to-end: ingest -> fault(EXCEPTION) -> restart -> outputs
PASS verifier CLI: FAIL on crashed dir, PASS after recovery
passed=13 failed=0
```

说明：
- **四个故障点**各用两种方式注入：进程内 `EXCEPTION`（崩溃态，要求重开）与
  独立子进程 `Runtime.halt(0)`（真实杀进程，父进程在同目录恢复）——后者最接近 kill -9。
- 每个场景恢复后都断言：可见输出偏移恰好是 `0..n-1` 各一次、`result=value²`、
  `state=Σvalue`、durable offset 连续、`commit.log` 无重复标记。
- “崩溃现场”测试逐个检查磁盘形态：commit 半行无结尾换行、prepare 残段不可解析、
  处理中崩溃无新段无新快照但输入已持久化、落盘后提交前快照 `pendingCommit` 正确。

### 端到端演示（真实 HTTP + 真实杀进程）

```bash
./scripts/demo.sh
```

实跑摘要（2026-09-23）：摄入 offset 0..2 → 在 offset 3 注入
`AFTER_STATE_PERSISTED/HALT`，进程死亡时现场为：

```
snapshot-4.json: {"epoch":4,"offset":3,"pendingCommit":"txn-3","state":46,...}
commit.log:      txn-0 txn-1 txn-2          （txn-3 已 prepare 未 commit）
segments:        txn-0.rec … txn-3.rec
```

重启服务后自动补提交 `txn-3` 并重放，继续摄入 4、5，最终：

```
state:   {"offset":5,"state":75,...,"committedOutputs":6}
outputs: txn-0..txn-5 各一次，result=100,121,144,169,196,225（10²..15²）
verifier: RESULT: PASS — no missing, no duplicate visible outputs
```

---

## 5. 开发过程中实际发现并修复的问题（如实记录）

实现中途用测试抓到两个真实缺陷，均已修复并被回归测试覆盖：

1. **只追加通道在“另一文件描述符截尾”后 position 错位**：崩溃留下的半行
   commit 标记在启动时被截断修复，但已以 APPEND 打开的 `FileChannel` 仍保留旧 position，
   恢复后第一次写会在旧位置写、产生文件空洞，使可见输出多出一条空标记。
   修复：每次追加前显式 `channel.position(channel.size())` 对齐文件尾
   （`DurableFiles.appendForce` 与故障注入提交路径）。
2. **校验器把正常日志尾部当成空标记**：`"a\n".split("\n",-1)` 产生尾部空串，
   校验器误判为“空提交标记”。修复后正确区分“完整最后一行”和“中间空行损坏”。

---

## 6. 边界、假设与未完成项

**假设与边界**
- 单机、单数据目录；一个目录同一时刻只允许一个服务实例（没有分布式锁/租约）。
- 本地接收器保证依赖本地文件系统语义：原子 rename、目录 fsync、`O_APPEND`；
  在真正的本地磁盘/容器卷上成立，网络文件系统（NFS 等）上的 fsync/rename 语义不保证。
- commit.log 无垃圾回收，长期运行会持续增长；快照保留最新两份，暂存段随提交永久保留为输出本体。
- 输入 WAL 不做压缩/截断；偏移必须从 0 连续，否则启动报损坏。
- HTTP 服务仅监听 127.0.0.1，无鉴权/TLS；`--debug` 故障注入端口不应暴露到不可信网络。
- 一次处理一条输入（批大小 1）。微批/多条原子提交、并发算子不在范围内。
- “输出去重”靠偏移确定性事务 ID；接收器本身不去重外部世界的等价输出。

**未完成 / 未做**
- 未做多节点、选主、共享存储或外部系统 XA/两阶段提交——题目明确限定为本地接收器。
- 未做崩溃后的自动拉起（supervisor/systemd）；恢复在服务重启时自动进行，重启动作本身由外部负责。
- 未做性能基准与压测。
- 仅在 Linux x86_64 + Temurin 17 实跑；其他平台/更高 JDK 未专门回归（只用标准 API，预期可用）。
