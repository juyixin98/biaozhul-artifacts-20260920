# 运行记录（RUNLOG）

本文件如实记录本项目从空目录到交付过程中**实际执行的命令、得到的结果，以及中途
失败并修复的问题**。时间：2026-09-25，平台 Linux x86_64。

## 1. 环境

- 系统自带工具链：`go version go1.22.2 linux/amd64`（`/usr/bin/go`）。
  - 开发最初一个非登录 shell 中 `go` 不在 PATH，曾下载过一份 Go 1.23.4 到
    `~/sdk/go`；随后确认系统已有 `/usr/bin/go`，最终构建与测试均使用系统的
    **go1.22.2**，`go.mod` 声明 `go 1.22`。
- 纯标准库，**零第三方依赖**（`go.sum` 不存在）；无 CGO、无外部数据库。
- 本机 18081 端口被无关进程 `breaker-server` 长期占用，因此人工演示改用
  19090/19091、20010/20011；自动化验收客户端（idemcheck）每次随机选端口，不受影响。

## 2. 最终命令与结果

### 2.1 编译

```
$ make build
mkdir -p bin
go build -o bin/idempotency-server ./cmd/server
go build -o bin/auditserver         ./cmd/auditserver
go build -o bin/idemcheck           ./cmd/client
```

结果：成功，生成三个二进制；`go vet ./...` 无输出（干净）；`gofmt -l .` 无输出。

### 2.2 单元 / 集成测试（含竞态检测与覆盖率）

```
$ go test -race -coverprofile=coverage.out ./internal/...
ok  idempotentsave/internal/api          ~7.7s   coverage: 86.0%
ok  idempotentsave/internal/auditclient  ~0.0s   coverage: 86.7%
ok  idempotentsave/internal/clock        ~1.0s   coverage: 83.3%
ok  idempotentsave/internal/fakeaudit    ~0.0s   coverage: 83.3%
ok  idempotentsave/internal/idem         ~1.0s   coverage: 90.5%
ok  idempotentsave/internal/service      ~0.5s   coverage: 95.2%
ok  idempotentsave/internal/store        ~1.1s   coverage: 82.1%

$ go tool cover -func=coverage.out | tail -1
total: (statements) 82.7%
```

结果：全部 `ok`，`-race` 下无数据竞争；内部包聚合语句覆盖率 **82.7% ≥ 80%**。
（`cmd/` 为进程装配/测试运行器代码，其端到端正确性由 idemcheck 覆盖，未计入该门槛。）

### 2.3 端到端故障注入验收

```
$ make build
$ ./bin/idemcheck -work ./run/acceptance -out ./run/report.json -keep
report written to ./run/report.json
idemcheck exit=0  elapsed=4s
```

报告 `run/report.json` 顶层汇总：

```
summary: {total: 9, passed: 9, failed: 0, assertions: 55, assertions_passed: 55}
```

9 个场景全部通过（重放、冲突、处理中并发、24 路并发、提交前/后断线、
崩溃恢复、重启后重放、外部不恰好一次）。`run/acceptance/` 下保留了子进程日志
（`server.log`、`auditserver.log`）、WAL（`data/wal.log`）与持久化时钟（`data/clock.txt`）。

### 2.4 人工请求样例

```
$ # 审计 / 业务两个进程分别监听 127.0.0.1:20011 / 20010（全新数据目录）
$ BASE=http://127.0.0.1:20010 AUDIT=http://127.0.0.1:20011 \
    bash examples/curl-examples.sh
curl rc=0
created=4 replayed=4
"balance_after" 取值：50×1, 100×3, 300×1, 500×1, 900×1   # 无 0，无串台
```

结果：首次/重放计数正确，各账户 `balance_after` 与金额一致；409/202/502 等异常
路径均按预期返回。

## 3. 开发过程中真实出现、并已修复的问题

以下问题都在自动化或人工验证中**实际暴露**过，按出现顺序记录（均已修复并有测试守护）：

1. **验收客户端把正文序列化了两次。** `deposit()` 先 `json.Marshal` 得到字节，
   又调用会再次 `Marshal` 的 `postJSON`，字节数组被编码成 base64 字符串，
   服务端全部回 422（首轮 9/9 场景因此失败）。改为直接发送原始字节后修复。

2. **误用轮询导致整体超时。** TTL 前的"应当立即得到 202"检查错误地用了
   会循环重试到非 202 的 `waitForFinal`，每次 202 等满等待窗口，客户端运行超过
   200s 被杀。改为 TTL 前只发一次请求断言 202；并把验收默认等待窗口从 3s 降到 1s。
   端到端耗时随后从约 131s 降到约 4s。

3. **假时钟在进程重启后"倒流"。** 假时钟初值是固定常量，业务进程崩溃重启后时钟
   回到起点，而遗留 pending 的过期时间是崩溃前（已被多个场景拨快后）写入的，
   导致该占位永远不过期、客户端无限收到 202。修复方式：把假时钟当前时间持久化到
   数据目录 `clock.txt`，重启时恢复，模拟"真实时间不会因进程重启而倒流"。
   （见 `internal/clock/clock.go` 的 `LoadOrCreateFake`。）

4. **访问日志中间件挡住了连接劫持。** 包装 `ResponseWriter` 的 `statusWriter`
   没有转发 `http.Hijacker`，导致"强制断线"故障退化成返回 503 而不是真正断开。
   为其补上 `Hijack()` 透传后修复。

5. **Go HTTP 客户端自动重放掩盖了断线。** 请求体最初是可重绕的 `*bytes.Reader`，
   当对端在等待响应头时关闭连接，`http.Client` 会自动重发该 POST；第二次请求撞上
   刚建立的 pending 返回 202，使验收看不到"第一次的原始断线"。修复：验收/相关测试
   用不可重绕的 `oneShotReader` 且不提供 `GetBody`，显式禁用自动重放
   （真正的重试逻辑由场景代码控制）。

6. **崩溃场景空转 40 次。** 与第 2 条同源：crash 场景在拨钟之前调用了
   `waitForFinal`，单协程下时钟永远等不到推进。改为单次 202 断言后再拨钟。

7. **【真实正确性缺陷】重放响应里 `balance_after` 为 0。** 最初响应体在提交前就序列化，
   而"提交后余额"在存储提交完成后才得到，导致保存下来的响应余额字段为 0。
   人工 curl 演示首先暴露了这一点。修复：把 `store.Commit` 改为在**持锁状态、
   算出提交后新余额之后**回调生成响应体（`buildResponse(balanceAfter)`），
   保证响应体与副作用来自同一状态点、并一起写入同一条 WAL 记录。
   新增回归测试 `TestReplayFreezesBalanceSnapshotAtCommit`（快照冻结于提交当时，
   不被后续入账改写）。修复后 idemcheck 全部响应的 `balance_after` 均正确。

8. **排查环境问题（非代码缺陷）。** 人工演示一度打到机器上残留的旧 server 进程，
   表现为"明明修了余额却仍看到 0"。用全新高端口 + 全新数据目录隔离复现，确认新二进制
   行为正确；随后清理了残留进程。

## 4. 明确的边界与未做项（非失败，是设计取舍）

- **不保证任意外部系统调用恰好一次。** 外部假审计通过真实 HTTP、独立进程访问，
  不在本地 WAL 事务内。`external_not_exactly_once` 场景刻意制造
  "外部已落事件却返回 503"，证据为：**外部 events=2，而本地 effects=1、余额只加一次**。
  要做到跨系统恰好一次需要外部系统以幂等键做服务端去重 / 分布式事务 / 对账，超出范围。
- 持久化用 fsync WAL + 内存快照，无 checkpoint/压缩，WAL 会增长（可用
  `POST /admin/reset` 截断）；目标是清晰表达本地事务原子性与崩溃恢复，不是生产级数据库。
- 无前端、无鉴权、无 TLS；故障注入头与 `/admin/*` 仅适合本地可信环境。
- 截止本次记录，**没有已知未通过项**：单元/集成测试、`-race`、端到端 9 场景 55 断言
  全部通过。
