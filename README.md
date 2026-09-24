# taskrace — 持久任务取消竞态

纯后端 Go 服务（仅标准库 `net/http`，无第三方依赖），实现带**尝试号**的任务状态机，
核心问题是**取消与完成的竞态**：两者必有且仅有一个获胜，旧尝试（attempt）的工作者
永远无法提交结果，重启后状态不倒退。

## 依赖与启动

- 依赖：Go ≥ 1.22（开发用的是 go1.23.4）；无第三方模块，`go.mod` 即锁定全部依赖。
- 运行测试：`go test -race ./...`
- 启动服务：`go run . -addr :8080 -wal taskrace.wal`（或 `go build -o taskrace-server . && ./taskrace-server`）
- 请求样例：服务启动后执行 `./examples.sh`（可用 `./examples.sh http://host:port` 指定地址）

## 状态机

```
PENDING --claim--> RUNNING --complete--> SUCCEEDED (终态)
                   RUNNING --fail(次数用尽)--> FAILED (终态) --retry--> PENDING
                   RUNNING --fail(未用尽)/租约超时--> PENDING
任意非终态 --cancel--> CANCELLED (终态)
```

- `attempt` 单调递增、终身不重置（`retry` 也不重置）。只有**当前 attempt** 且持有
  租约的 worker 能心跳/完成/失败；其余一律 409。
- 租约默认 30s，后台 sweeper 每秒把过期 RUNNING 任务打回 PENDING（懒超时：
  Get/List/Claim 时也会结算）。

## 线性化点

所有变更操作（Claim/Heartbeat/Complete/Fail/Cancel/Sweep/Retry）都在同一把
`Store.mu` 临界区内完成：**校验前置条件 → 修改内存状态 → 追加 WAL 快照 → fsync →
释放锁**，成功响应只在 fsync 之后返回。因此：

- **Cancel vs Complete**：谁先拿到锁谁赢，输家看到终态并收到 409，不存在双赢窗口。
- **Complete vs 超时**：sweep 先行则旧 attempt 作废，后来的 Complete 得
  `stale_attempt`；Complete 先行则任务已终态，sweep 跳过。
- **持久性**：WAL 只追加完整快照，`version` 单调递增；重启重放时按 version 取最新，
  状态只会前进不会倒退。

## HTTP 接口

| 方法 | 路径 | 请求体 | 说明 |
|---|---|---|---|
| POST | `/tasks` | `{"id","payload","max_attempts"}` | 提交任务（201） |
| GET | `/tasks` / `/tasks/{id}` | — | 查询 |
| POST | `/tasks/{id}/claim` | `{"worker"}` | 领取，attempt+1 |
| POST | `/tasks/{id}/heartbeat` | `{"worker","attempt"}` | 续租约 |
| POST | `/tasks/{id}/complete` | `{"worker","attempt","result"}` | 提交结果（仅当前 attempt） |
| POST | `/tasks/{id}/fail` | `{"worker","attempt"}` | 失败重试/次数用尽转 FAILED |
| POST | `/tasks/{id}/cancel` | `{}` | 取消（对已 CANCELLED 幂等） |
| POST | `/tasks/{id}/retry` | `{}` | FAILED 手动重开 |
| POST | `/admin/sweep` | `{"now"}` 可省 | 触发超时结算（测试用） |

冲突均返回 409，body 中 `error` 区分原因：`terminal` / `stale_attempt` /
`not_running` / `not_claimable` / `worker_mismatch` / `not_retryable`。

## 测试（验收）

`store_test.go`，全部在 `go test -race` 下通过：

- `TestExhaustiveInterleavings`：**穷举** claim/heartbeat/complete/stale-complete/
  cancel/timeout/fail 七种操作所有长度 1..4 的序列（共 **2800** 条），逐前缀检查
  不变量：attempt 与 version 单调、终态唯一且吸收（结果不被改写）、旧 attempt 的
  complete 永不成功；每条序列结束后**重启**（WAL 重放）校验状态完全一致，且重启后
  终态仍吸收一切操作。
- `TestCancelCompleteRace`：200 次并发 cancel/complete 对射，恰好一方获胜，
  终态与胜者一致（`-race` 无数据竞争）。
- `TestStaleWorkerRejectedAfterTimeout`：租约超时→他人重领→旧 worker 结果被拒。
- `TestRestartNoRegression`：多任务不同终态下重启，状态逐项相等且终态拒绝一切变更。
- `TestFailRetryCycle`、`TestHTTP`：重试预算与 HTTP 端到端。

## 实测记录（2026-09-24，go1.23.4 linux/amd64）

- `go vet ./...` 通过；`gofmt` 干净。
- `go test -race ./...`：**PASS**（6 个测试，含 2800 条穷举交错，约 7.6s）。
- 实际启动服务并用 curl 验证：取消先于完成时完成方得 `409 terminal`；租约过期后
  workerB 领到 attempt 2，workerA 的旧结果得 `409 stale_attempt`，workerB 完成成功；
  杀掉进程用同一 WAL 重启后 `CANCELLED`/`SUCCEEDED` 状态与结果原样恢复，终态仍拒绝
  写入。原始输出见上文会话/可运行 `./examples.sh` 复现。

## 未完成项 / 限制

- 单进程单文件存储：无多副本、无分布式一致性；WAL 无压缩/截断（长期运行需自行轮转）。
- 无认证鉴权；`/admin/sweep` 接受客户端时钟，仅限测试用途。
- 取消已 CANCELLED 任务为幂等 200，取消 SUCCEEDED/FAILED 为 409——语义固定但属设计取舍。
