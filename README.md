# taskq — 持久任务取消竞态（Persistent Task Cancel/Complete Race）

用 Go 标准库 `net/http` 实现的纯后端任务队列，核心解决一个问题：
**取消、完成、租约超时并发发生时，终态必须唯一且线性化；只有当前尝试（attempt）
的持有 worker 能提交结果，旧 worker 的结果必须被拒绝；重启后状态不得倒退。**

- 无界面，只有 JSON HTTP 接口
- **零第三方依赖**：仅使用 Go 标准库（`net/http`、`encoding/json`、`os`…）
- 持久化：每条状态转换先 **fsync 写 WAL** 再应答；周期性生成原子快照压缩
- 租约（lease）+ 尝试号（attempt）：心跳续租，超时自动重派，attempt 单调递增
- 每次成功的状态转换带全局单调 `version`（事件序列号），便于去重与排查

---

## 1. 依赖

| 依赖 | 版本 | 说明 |
| --- | --- | --- |
| Go | 1.23（开发用 1.23.4 验证） | 唯一的构建/运行依赖 |
| 标准库 | 随 Go 发行 | **没有任何外部 module** |

`go.mod` 中没有 `require` 块，`go.sum` 为空——不引入第三方代码，因此无需下载、
也不存在供应链版本漂移；这本身即“锁定依赖”。

构建不联网：

```bash
go build ./...
GOFLAGS=-mod=mod GOPROXY=off go test ./...   # 验证无需网络
```

## 2. 启动命令

```bash
# 直接运行
go run ./cmd/taskq -addr :8080 -data ./data -lease 30s -sweep 1s

# 或构建后运行
go build -o taskq ./cmd/taskq
./taskq -addr :8080 -data ./data -lease 30s -sweep 1s
```

| 参数 | 默认值 | 环境变量 | 含义 |
| --- | --- | --- | --- |
| `-addr` | `:8080` | `TASKQ_ADDR` | 监听地址 |
| `-data` | `./data` | `TASKQ_DATA` | 持久化目录（`wal.log` + `snapshot.json`） |
| `-lease` | `30s` | `TASKQ_LEASE` | worker 领取任务后的租约时长 |
| `-sweep` | `1s` | `TASKQ_SWEEP` | 后台扫描过期租约的间隔 |

健康检查：`GET /healthz` → `{"status":"ok"}`
停止：`Ctrl-C` / `SIGTERM`，会优雅关闭 HTTP 并落盘。

**想要空状态：删除数据目录即可**（`rm -rf ./data`）。重启进程会自动重放
`snapshot.json` + `wal.log`，不需要任何迁移命令。

## 3. HTTP 接口

所有请求/响应均为 JSON。错误形态统一为：

```json
{"error":{"code":"STALE_LEASE","message":"worker \"w1\" does not hold the current lease ..."}}
```

| 方法 & 路径 | 作用 | 请求体 | 成功状态码 |
| --- | --- | --- | --- |
| `POST /tasks` | 提交任务 | `{"payload": <任意 JSON>}` | 201 |
| `GET /tasks` | 列出全部任务 | – | 200 |
| `GET /tasks/{id}` | 查询单个任务 | – | 200 |
| `POST /tasks/claim` | 领取最早的 PENDING 任务 | `{"worker_id":"w1"}` | 200 |
| `POST /tasks/{id}/heartbeat` | 续租 | `{"worker_id","lease_token"}` | 200 |
| `POST /tasks/{id}/complete` | 提交结果（仅当前租约） | `{"worker_id","lease_token","result":<任意 JSON>}` | 200 |
| `POST /tasks/{id}/cancel` | 取消（PENDING/RUNNING 均可，幂等） | 可选，忽略 | 200 |
| `POST /tasks/{id}/retry` | 主动交还重派 / 已排队则幂等 | `{"worker_id","lease_token"}`（PENDING 时可省） | 200 |

`claim` 响应在顶层返回一次性 `lease_token`（不会出现在任务状态或 GET 响应中）：

```json
{"lease_token":"lease_6556...","task":{"id":"task_...","state":"RUNNING","attempt":1,...}}
```

错误码：`BAD_REQUEST`(400)、`NOT_FOUND`(404)、`NO_TASK_AVAILABLE`(404)、
`INVALID_STATE`(409)、`STALE_LEASE`(409)。

## 4. 请求样例（curl）

```bash
# 启动一个短租约的演示服务
go run ./cmd/taskq -addr :8080 -data /tmp/taskq-demo -lease 4s -sweep 1s

# 或直接跑编排好的完整脚本（正常流程/竞态/超时/重派）
./examples/demo.sh http://localhost:8080
```

手工逐步：

```bash
# 提交
curl -s -X POST localhost:8080/tasks -H 'Content-Type: application/json' \
  -d '{"payload":{"url":"https://example.com/job"}}'
# -> 201, 记下 task.id

# 领取（attempt 变成 1，返回 lease_token）
curl -s -X POST localhost:8080/tasks/claim -H 'Content-Type: application/json' \
  -d '{"worker_id":"w1"}'

# 心跳续租
curl -s -X POST localhost:8080/tasks/<id>/heartbeat -H 'Content-Type: application/json' \
  -d '{"worker_id":"w1","lease_token":"<token>"}'

# 提交结果
curl -s -X POST localhost:8080/tasks/<id>/complete -H 'Content-Type: application/json' \
  -d '{"worker_id":"w1","lease_token":"<token>","result":{"answer":42}}'

# 取消（与 complete 同时发，恰好演示竞态）
curl -s -X POST localhost:8080/tasks/<id>/cancel
```

## 5. 状态机与竞态线性化点（设计说明）

### 5.1 状态与尝试号

```
                 claim(worker, new lease_token)
   PENDING ───────────────────────────────────► RUNNING
      ▲                                           │  │
      │                              heartbeat    │  │
      │  timeout(lease_deadline≤now) / retry(owner)  complete(owner)
      │◄──────────────────────────────────┘       │  │
      │                                           ▼  ▼
      │                                        COMPLETED (终态)
      └─ cancel (PENDING 或 RUNNING 都可) ──► CANCELLED (终态)
```

- `attempt` 是**派发号**：任务创建时为 0，每次 **claim 成功 +1**。
  超时 / 主动 retry 只是把任务放回 PENDING，**不增加** attempt；下一次领取才 +1。
- 每次 claim 生成一个全新的随机 `lease_token`（32 个十六进制字符）。
  worker 之后的 heartbeat/complete/retry 必须同时匹配 `worker_id` 与该 token。
- 超时发生的瞬间旧 token 立即失效，因此**结果只可能来自当前尝试**。

### 5.2 线性化点在哪

所有写操作在 store 层经过**同一把互斥锁**，临界区内依次完成：

1. 取逻辑时间 `now`；
2. **先**让所有 `lease_deadline ≤ now` 的 RUNNING 任务过期（惰性超时事件）；
3. 应用本次命令（cancel/complete/…）；
4. 把产生的全部事件**逐条 fsync 追加进 WAL**。

> 请求拿到这把锁的先后顺序，就是 cancel 与 complete 竞态的全序。
>
> - complete 先入临界区且持有效租约 → 状态翻成 COMPLETED、结果落盘；之后的
>   cancel 收到 `409 INVALID_STATE`。
> - cancel 先入临界区 → 状态翻成 CANCELLED、租约作废；之后的 complete 收到
>   `409 INVALID_STATE`（若租约恰好也到期，则是 `STALE_LEASE`）。
>
> 两者**不可能同时成功**；WAL 中也只可能有其中一个事件，因此重启后结论一致。

超时是“惰性 + 后台 sweep”双保险，但两条路径进入的是**同一个临界区、同一个状态
转换**：sweep 只是把过期事件主动生成并 fsync；即使没有 sweep，任何请求进入
`Step` 时都会先补做过期。测试 `TestExhaustiveCancelTimeoutComplete` 同时用
“显式定位超时事件”与“Step 惰性超时”两种模型跑完全部排列，终态逐一比对一致。

### 5.3 持久化与“不倒退”

- `wal.log`：每行一条事件（`seq` + 转换后的完整任务快照），**每条都 fsync**。
  已应答的转换一定在盘上；未 fsync 的半行（崩溃撕裂尾）在下次启动时被截断丢弃。
- `snapshot.json`：WAL 累计超过阈值（默认 256 条）时，原子写快照
  （`tmp → rename → fsync 目录`）**之后**才清空 WAL。崩溃在任何一步，
  要么是旧快照+旧 WAL，要么是新快照（重放时按 `seq` 去重），状态不会倒退。
- 启动时先装快照，再重放所有 `seq > snapshot.seq` 的 WAL 记录。

## 6. 自动化测试

```bash
go test ./...                 # 全部测试
go test -race ./...           # 带竞态检测器
go test -race -count=20 ./internal/machine ./internal/store
go test -v -run TestExhaustive ./internal/machine
```

测试分层：

| 测试 | 位置 | 覆盖的验收点 |
| --- | --- | --- |
| 状态机单元（生命周期/心跳/超时/取消/重试/校验/重放） | `internal/machine/machine_test.go` | 基本转移、旧租约拒绝 |
| **穷举 cancel×complete×timeout 全排列（3!=6）**，每个排列还跑“惰性超时”模型对比 | `internal/machine/race_test.go` | **终态唯一、当前尝试结果才被接受、两种超时模型等价** |
| 加入 heartbeat 的 4!=24 短交错 | `internal/machine/race_test.go` | 续租改变超时结果、取消/完成仍唯一 |
| 超时→重派，旧 worker complete/heartbeat、伪造 token 全被拒 | 同上 | 旧 worker 结果被拒 |
| 200×并发 cancel vs complete 同任务 | `internal/store/store_test.go` | 恰好一方提交一个版本；幂等取消不产生新版本 |
| 旧/伪造 worker 并发刷结果 + 当前 worker 完成 + 重启 | 同上 | 提交的结果永远属于当前尝试，重启保持 |
| 重启恢复（提交/领取/心跳/超时/重派/完成各阶段后重启） | 同上 | **状态不倒退**、版本号/截止时间/结果保持 |
| 快照压缩 25 个任务后重启 | 同上 | 压缩路径正确 |
| WAL 回环、压缩截断、撕裂尾容忍 | `internal/wal/wal_test.go` | 崩溃安全 |
| HTTP 全流程、真实并发 cancel/complete、真实超时重派、服务器重启、400/404/405/409 | `internal/api/api_test.go` | 接口契约与端到端语义 |

## 7. 实际运行记录（本仓库交付时）

在 Go 1.23.4 / linux-amd64 上实际执行：

- `go vet ./...`：无告警；`go build ./...`：通过。
- `go test -race -count=1 ./...`：4 个包全部 `ok`；
  竞态相关测试又以 `-count=20`（机器/存储层）与 `-count=5`（全量）重复运行，均通过。
- 用短租约（4s）真实启动二进制，执行 `examples/demo.sh`：
  - 伪造 token 心跳 → `409 STALE_LEASE`；
  - 并发 cancel/complete → 本次 cancel 先线性化：`cancel 200 / complete 409
    INVALID_STATE`，终态 CANCELLED、无结果；
  - 等待 5s 后旧任务自动变 PENDING，旧 worker 结果 → `409`；新 worker
    attempt=2 领取并 COMPLETED，结果 `"fresh"`；
  - 杀掉进程、用**同一数据目录**重启后 `GET`：CANCELLED(attempt 1, version 4)
    与 COMPLETED(attempt 2, result `"fresh"`, version 9) 均保持，`/tasks` 仍为 2 条。

> 并发竞态每次运行谁先拿到锁不固定：本环境该次是 cancel 赢；测试对两种赢家都做
> 断言（穷举测试固定排列、HTTP/存储并发测试接受任一赢家但验证终态与版本数）。

## 8. 目录结构

```
.
├── cmd/taskq/main.go        # 入口：参数、启动 store/sweeper/HTTP、优雅退出
├── internal/
│   ├── machine/machine.go   # 纯状态机：状态、命令、事件、attempt/lease 规则
│   ├── wal/wal.go           # fsync WAL + 原子快照 + 撕裂尾修复
│   ├── store/store.go       # 互斥锁线性化、持久化、sweep、压缩、重放
│   └── api/api.go           # net/http handler 与 JSON 契约
├── examples/demo.sh         # 可直接运行的端到端演示
├── go.mod                   # 仅 stdlib，无 require；go.sum 为空
└── README.md
```

## 9. 范围与未完成项（如实说明）

已完成：状态机、线性化、WAL+快照持久化、重启恢复、全部 HTTP 接口、
穷举/并发/真实超时测试与可运行演示。

**有意未做 / 已知边界**（本任务范围外，未声称支持）：

1. **单进程单实例**：锁是进程内互斥锁，不是分布式锁。多实例共享同一数据目录
   会损坏文件；要多节点需引入租约存储（etcd/Postgres 等），那会引入外部依赖。
2. **后台 sweep 失败只记录不告警**：WAL fsync 失败会把 store 置为 fail-fast
   （之后所有写返回 500），但没有指标/告警通道。
3. **WAL 阈值压缩按事件条数**，没有按字节或时间；快照包含全部任务（无任务级
   TTL/清理），任务无限增长时快照会变大。没有实现任务删除/归档接口。
4. **没有鉴权/TLS/限流**，监听裸 HTTP；生产部署应放在网关之后。
5. **payload/result 上限 1MiB**（请求体统一限制），没有大对象外置存储。
6. 时钟依赖服务器本机时间；测试通过注入时钟消除不确定性，生产上没有做
   时钟漂移处理（单实例下无影响）。
