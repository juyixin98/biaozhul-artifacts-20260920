# tpc — 两阶段提交（2PC）崩溃恢复模拟器

纯后端、仅依赖 Go 标准库（`net/http`）的两阶段提交模拟器。协调者与参与者各自维护
**落盘即 fsync 的预写日志（WAL）**，支持 prepare / commit / abort 与重启恢复，并通过
崩溃注入点逐边界验证原子性。

## 依赖与构建

- Go ≥ 1.22（开发环境为 go1.23.4，见 `go.mod`）
- **零第三方依赖**，`go.mod` 即锁定文件，无需 `go.sum`、无需联网拉取

```sh
go build ./...          # 构建
go test -race ./...     # 全部自动化测试（含子进程崩溃测试）
go build -o bin/tpc-coordinator ./cmd/tpc-coordinator
go build -o bin/tpc-participant ./cmd/tpc-participant
```

## 启动

```sh
./bin/tpc-coordinator -addr :8080 -log coord.wal
./bin/tpc-participant -addr :8081 -log pa.wal -coordinator http://localhost:8080
./bin/tpc-participant -addr :8082 -log pb.wal -coordinator http://localhost:8080
```

`-log` 是各节点唯一的持久状态（append-only JSON 行，每次写入后 fsync）。删掉它等于
换掉节点身份；保留它重启即为"崩溃恢复"。

## HTTP 接口与请求样例

协调者：

| 请求 | 说明 |
|---|---|
| `POST /tx` `{"id","participants":[url,...],"payload":{...}}` | 创建事务，落盘 `PREPARING` |
| `POST /tx/{id}/commit` | 跑完整两阶段：prepare 全员 → 落盘决定 → 投递决定 |
| `POST /tx/{id}/abort` | 在做出提交决定前中止 |
| `GET /tx/{id}` | 查询状态（参与者恢复时用它取决定） |

参与者：

| 请求 | 说明 |
|---|---|
| `POST /prepare` `{"txId","payload":{...}}` | 投票；`200`=yes（PREPARED 已落盘），`409`=no |
| `POST /commit` / `POST /abort` `{"txId"}` | 应用决定，幂等 |
| `GET /status` | 所有事务状态；`blocked:true` 表示已 prepare、正等待协调者决定 |

`payload` 约定字段：`"resource":"名字"`（prepare 后持锁，其他事务 prepare 同名资源
被拒 409，用于演示阻塞）；`"vote":"no"`（模拟投反对票）。

```sh
curl -X POST localhost:8080/tx -d '{"id":"tx1","participants":["http://localhost:8081","http://localhost:8082"],"payload":{"resource":"account-42"}}'
curl -X POST localhost:8080/tx/tx1/commit
curl localhost:8080/tx/tx1
curl localhost:8081/status
```

## 协议与恢复规则

协调者 WAL：`PREPARING → COMMIT|ABORT（决定）→ DONE`。
参与者 WAL：`PREPARED → COMMITTED|ABORTED`。
不变式：**任何状态先落盘（fsync）成功，才允许对外行动**（回复 RPC / 发送下一条消息）。

- 协调者重启：
  - 只有 `PREPARING`（决定未落盘）→ 补写 `ABORT` 并投递给所有参与者；
  - 已有 `COMMIT`/`ABORT` 决定 → 无限重试投递，直到全部 ack，再写 `DONE`；
  - `DONE` → 无事可做。
- 参与者重启：对每个 `PREPARED` 事务重新拿锁，并轮询协调者 `GET /tx/{id}` 取决定。

**协调者不可达时的阻塞行为**：已 `PREPARED` 的参与者**没有任何超时中止逻辑**——
协调者可能已决定 COMMIT，自行中止会破坏原子性。该事务保持 `PREPARED`
（`/status` 中 `blocked:true`，资源锁不释放），恢复循环一直轮询，直到协调者回来
给出决定为止。这是 2PC 固有的阻塞特性，本模拟器如实呈现而非掩盖。

## 崩溃注入（验收用）

`TPC_CRASH_AFTER` 环境变量（或 `-crash-after` 标志）在指定**落盘边界**之后立即
`os.Exit(2)` 模拟断电：

| 崩溃点 | 位置 |
|---|---|
| `c:after-preparing` | 协调者：PREPARING 落盘后、响应前 |
| `c:after-all-prepared` | 协调者：收齐 yes 后、写决定前 |
| `c:after-decision` | 协调者：COMMIT/ABORT 决定落盘后、通知前 |
| `c:after-done` | 协调者：DONE 落盘后 |
| `p:after-prepared` | 参与者：PREPARED 落盘后、回复 yes 前 |
| `p:after-committed` | 参与者：COMMITTED 落盘后、ack 前 |
| `p:after-aborted` | 参与者：ABORTED 落盘后、ack 前 |

例：`TPC_CRASH_AFTER=c:after-decision ./bin/tpc-coordinator -addr :8080 -log coord.wal`

## 自动化测试

`go test -race ./...` 共 14 个用例：

- 进程内（`integration_test.go`）：正常提交、反对票→中止、资源锁阻塞、
  协调者两类恢复（有/无决定）、**参与者永不超时自行中止**（协调者下线 500ms 仍
  PREPARED，协调者回归后按决定 COMMIT）、决定投递幂等。
- 子进程（`crash_test.go`）：真实编译出的二进制，**逐个落盘边界崩溃后重启**，
  每个用例断言原子性不变式——所有参与者最终状态一致（全 COMMITTED 或全
  ABORTED），绝无部分提交部分回滚；其中 `TestCrashAfterAllPrepared` 还断言协调者
  宕机 1.2s 内已 prepare 的参与者保持 PREPARED 不自行中止。

## 实际运行记录（本仓库交付前真实执行）

`go test -race -count=1 ./...`：14/14 通过（`ok tpc 4.756s`），含 race 检测。
开发过程中曾通过 `-count=10` 复现并修复一个协调者读状态未持锁的 data race，
修复后 `-race -count=3` 全绿。

手动演示（真实输出，端口 19080-19082）：

```
$ curl -X POST $C/tx -d '{"id":"tx2","participants":[...],"payload":{"resource":"account-7"}}'
{"id":"tx2","state":"PREPARING"}
$ curl -X POST $C/tx/tx2/commit          # 协调者武装了 c:after-decision
(connection lost: coordinator crashed)   # 决定已 fsync 后进程死亡
$ cat coord.wal
{"txId":"tx2","state":"PREPARING",...}
{"txId":"tx2","state":"COMMIT"}          # 决定已持久化
$ curl $P1/status                        # 协调者宕机中
{"txs":{"tx2":{"blocked":true,"resource":"account-7","state":"PREPARED"}}}
# 等待 2s 后依然 PREPARED —— 不超时自行中止
$ ./bin/tpc-coordinator -addr :19080 -log coord.wal   # 重启
$ curl $C/tx/tx2
{"decision":"COMMIT","state":"DONE",...}
$ curl $P1/status
{"txs":{"tx2":{"blocked":false,"resource":"account-7","state":"COMMITTED"}}}
```

## 已知限制 / 未完成项

- 单协调者，无高可用；协调者永久丢失（日志损毁）时已 prepare 的参与者将永远阻塞
  （2PC 固有缺陷，需 Paxos/Raft 提交类协议才能根治）。
- 无日志压缩/GC：WAL 只增不减，DONE 事务记录永久保留（恢复与查询依赖完整历史）。
- 资源锁是演示用的单资源排他锁，非真实存储引擎；payload 不真正"应用"到任何数据库。
- 协调者 `POST /tx/{id}/commit` 同步阻塞至决定投递完毕；参与者长期宕机时该请求会
  一直挂着（恢复重试在后台亦会进行）。
- 无 TLS/认证，仅面向本地实验。
- 崩溃注入点是手动枚举的代表性边界，非对每行代码做故障注入的形式化验证。
