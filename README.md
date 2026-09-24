# 两阶段提交（2PC）崩溃恢复模拟器

纯后端的本地两阶段提交模拟器：**1 个协调者 + N 个参与者**，每个节点独立进程、
各自维护 fsync 的持久化 WAL。支持 `prepare / commit / abort`、重启恢复，并在
每个"日志已落盘"边界注入硬崩溃（`os.Exit(42)`，不执行 defer）。

仅使用 Go 标准库（`net/http`），**无任何第三方依赖**。

## 1. 要解决的问题与验收点

经典两阶段提交协议：

1. 协调者先落盘 `BEGIN`（含完整写集）；
2. phase 1：向所有参与者发 `prepare`；参与者把 `PREPARE` 记录 fsync 后才回 `yes`；
3. 协调者**仅在全部 yes 时**落盘 `COMMIT`，否则落盘 `ABORT` —— 决定必须先于
   任何 phase-2 消息持久化，这是原子提交边界；
4. phase 2：下发决定，全部 ack 后记 `DONE`。

本模拟器逐条验证：

- **逐个落盘边界崩溃**（共 6 个点，见下），恢复后绝不出现"部分参与者提交、
  部分回滚"；
- **已 prepare 的参与者绝不凭超时自行中止**：协调者不可达时，参与者无限期
  保持 `PREPARED`、持锁、拒绝冲突事务；只能由协调者的显式 `commit/abort`
  解除不确定状态；
- 协调者重启：`BEGIN` 未决定 → 安全中止；已决定 → 按原决定重投直到完成。

## 2. 目录结构

```
cmd/tpc/                 命令行入口（coordinator | participant 两种角色）
internal/wal/            fsync 的 JSON-line WAL，重放时截断损坏的半行
internal/crashpoint/     TPC_CRASH 环境变量触发的单点硬崩溃
internal/participant/    参与者：prepare/commit/abort、持锁、WAL 恢复
internal/coordinator/    协调者：两阶段、决定落盘、后台恢复重投
internal/apprun/         进程装配（HTTP server + 信号优雅退出）
internal/integration/    进程级测试（真实子进程、SIGKILL、WAL 恢复）
scripts/demo.sh          端到端演示脚本
go.mod                   无 require：零第三方依赖
```

## 3. 依赖与启动

- Go ≥ 1.23（开发环境实测 go1.23.4 linux/amd64），无外部依赖、无 CGO。
- 构建：`go build -o bin/tpc ./cmd/tpc`（或 `make build`）。

启动两个参与者和一个协调者（三个终端，或后台运行）：

```bash
./bin/tpc participant -name p1 -listen 127.0.0.1:9101 -datadir ./data/p1
./bin/tpc participant -name p2 -listen 127.0.0.1:9102 -datadir ./data/p2

./bin/tpc coordinator -name c1 -listen 127.0.0.1:9000 -datadir ./data/c1 \
  -participants 'p1=http://127.0.0.1:9101,p2=http://127.0.0.1:9102'
```

协调者启动时会做一次短恢复扫描，并有一个每 500ms 的后台恢复循环，
持续把"已决定但未完成"的事务重投下去。

## 4. HTTP 接口

### 协调者（:9000）

| 方法/路径 | 说明 |
|---|---|
| `POST /txn` | 提交一个事务，执行完整两阶段 |
| `POST /recover` | 立即执行一次恢复扫描 |
| `GET  /txn/{id}` | 状态：`UNKNOWN / UNDECIDED / COMMIT_PENDING / ABORT_PENDING / COMMIT_COMPLETE / ABORT_COMPLETE` |
| `GET  /health` | 健康检查 |

提交事务：

```bash
curl -sS -X POST localhost:9000/txn -H 'Content-Type: application/json' -d '{
  "txid": "T1",
  "writes": [
    {"participant": "p1", "key": "acc",  "value": "100"},
    {"participant": "p2", "key": "note", "value": "hello"}
  ]
}'
# 成功: {"decision":"COMMIT","status":"COMMITTED","txid":"T1","votes":{...}}
```

响应语义：

- `200 COMMITTED / ABORTED`：两阶段全部完成；
- `502 COMMIT_UNCERTAIN`：**COMMIT 已持久化**，但 phase-2 没在 3s 内全部
  ack。结果只能是提交，后台恢复会继续重投，可轮询 `GET /txn/{id}`；
- `202 ABORT_PENDING`：已决定回滚，部分 abort 仍在投递。

### 参与者（:9101 / :9102）

| 方法/路径 | 说明 |
|---|---|
| `POST /prepare` | body `{txid,writes:[{key,value}]}`；成功投 yes 并 fsync PREPARE |
| `POST /commit` | body `{txid}`；幂等 |
| `POST /abort` | body `{txid}`；幂等 |
| `POST /recover` | **只报告** blocked 的 prepared 事务，绝不自行决定 |
| `GET  /txn/{id}` | `PREPARED / COMMITTED / ABORTED / UNKNOWN`，含 `blocked`、`keys` |
| `GET  /kv/{key}` | 当前值、是否被锁及持锁事务 |
| `GET  /state` | 全部 kv、锁、事务状态（调试用） |
| `GET  /health` | 健康检查 |

`prepare` 返回非 200 即视为投 no（参数错误 400、锁冲突 409）。锁冲突时
参与者不写任何日志、不持锁。

## 5. 崩溃点（逐个落盘边界）

用环境变量 `TPC_CRASH=<点>` 启动节点，该节点在对应日志 fsync **返回之后**
立即 `os.Exit(42)`（defer 不执行，模拟掉电）：

| 点 | 位置 | 磁盘上已有 | 恢复后全局结果 |
|---|---|---|---|
| `C1` | 协调者 BEGIN 落盘后、发 prepare 前 | 仅 BEGIN | ABORT（已 prepare 的参与者随后收到 abort） |
| `C2` | 协调者 COMMIT/ABORT 决定落盘后、发 phase-2 前 | BEGIN+COMMIT/ABORT | **保持同一决定**（commit 必提交） |
| `C3` | 协调者 DONE 落盘后、应答客户端前 | 全部记录 | 已完成，对客户端幂等 |
| `P1` | 参与者 PREPARE 落盘后、回 yes 前 | 参与者 PREPARE | ABORT（协调者收不到票按 no 处理） |
| `P2` | 参与者 COMMIT 落盘后、应用+ack 前 | 参与者 PREPARE+COMMIT | COMMIT（重启重放即已提交） |
| `P3` | 参与者 ABORT 落盘后、释放锁+ack 前 | 参与者 PREPARE+ABORT | ABORT（重启重放即已回滚） |

例：

```bash
# 武装协调者 C2
TPC_CRASH=C2 ./bin/tpc coordinator -name c1 -listen 127.0.0.1:9000 \
  -datadir ./data/c1 -participants 'p1=...,p2=...'
# 提交事务后连接被切断（进程退出 42）；去掉 TPC_CRASH 重启即恢复
```

## 6. 协调者不可达时的阻塞行为（重点）

这是 2PC 的本质代价，本模拟器**故意不提供任何超时自动中止**：

- 参与者 fsync PREPARE 后进入**不确定（blocked）**状态，它不知道协调者
  最终会 commit 还是 abort；
- 协调者一直不可达时，参与者**永远保持 PREPARED**：继续持有写锁、拒绝
  冲突事务的 prepare、对外仍可见旧值；
- 参与者自己重启后，从 WAL 重放出 PREPARED 和锁，**继续阻塞**；
- `/recover` 只汇报 blocked 列表，不做任何决定；
- 只有协调者恢复后按其持久化的决定重投 `commit`/`abort`，参与者才解锁。
  协调者 WAL 若只有 BEGIN（从无决定），恢复时安全中止并补发 abort。

对应自动化测试：
`TestPreparedParticipantBlocksWhenCoordinatorGone`（协调者被杀 6 秒，
超过所有 RPC 超时，参与者状态不变；参与者自身重启后仍阻塞；决定到达后
才提交）。

## 7. 自动化测试

```bash
make test        # go test ./...
make test-race   # 带竞态检测
make demo        # 端到端演示脚本
```

- `internal/wal`：追加/重放、**半行损坏截断**（模拟写一半掉电）；
- `internal/participant`：prepare 阻塞与持锁、commit/abort 幂等、投 no；
- `internal/integration`：**真实子进程**测试，崩溃点真的 `os.Exit`、
  用 SIGKILL 杀进程、同一 datadir 重启后只靠 WAL 恢复：
  - `TestHappyPathCommit`、`TestVoteNoAbortsEntireTxn`；
  - `TestCrashAtEveryDurableBoundary`：C1/C2/C3/P1/P2/P3 **六个边界逐一
    崩溃**，断言所有参与者同生共死、锁全部释放；
  - `TestPreparedParticipantBlocksWhenCoordinatorGone`：阻塞保证；
  - `TestCoordinatorCrashC1AbortsPreparedParticipants`：C1 后已 prepare
    参与者先阻塞、协调者恢复后被 abort。

### 实测结果（go1.23.4, linux/amd64）

`go test -race -count=1 ./...`（完整输出存档于 `docs/test-run.log`）：

```
ok  tpc/internal/wal          1.026s
ok  tpc/internal/participant  1.026s
ok  tpc/internal/integration  10.934s
```

`scripts/demo.sh` 实跑（正常提交 / 一票 no 全回滚 / C2 崩溃）：

- T1 两节点提交，`acc=100`、`note=T1-ok`；
- T2 因 p2 锁冲突投 no，p1 虽投 yes，整体 ABORT，`acc` 保持 100；
- T3 协调者在 C2 退出（curl 收到空响应）；协调者死亡期间两个参与者
  都是 `PREPARED/blocked`；干净重启后 `GET /txn/T3 = COMMIT_COMPLETE`，
  两节点 `acc=300`、`log=T3-crash`，协调者 WAL 含
  `BEGIN T3 / COMMIT T3 / DONE T3`。

## 8. 已简化的范围（有意为之，非缺陷）

- 单机、HTTP/JSON、key-value 写集，不做真正的资源管理器/SQL；
- 不实现 Presumed-Abort/Prepare 的垃圾回收优化，DONE 记录永久保留
  （模拟器需要完整审计轨迹）；
- 参与者地址在协调者命令行静态配置，不做成员变更；
- phase-2 投递为有界重试 + 500ms 后台循环（指数小退避），不做乱序
  防护之外的复杂传输；
- 崩溃模型是"日志 fsync 返回后立即进程死亡"，不模拟磁盘损坏。

## 9. 未完成项 / 已知限制

- 无鉴权与 TLS（本地模拟器，只监听 127.0.0.1）；
- 提交请求为同步阻塞式，phase-1 使用 30s 上限、phase-2 同步窗口 3s；
  极端慢节点下客户端会拿到 `*_PENDING/COMMIT_UNCERTAIN`，需轮询；
- 单一协调者，无协调者选举（标准 2PC 本就依赖协调者恢复，参与者因此
  可能长时间阻塞——这正是第 6 节展示的行为，3PC/Paxos 类协议才能消除）；
- 集成测试依赖 Linux `/proc/<pid>/stat` 判断僵尸进程状态。
