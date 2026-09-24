# 任务资源死锁检查服务 (Task Resource Deadlock Check)

机器人离线任务的**资源分配 / 死锁防护**纯后端服务。任务在执行前声明需要**同时**持有
的一组工具（tool）与工位（station）；调度器保证整组资源**原子**授予，失败绝不部分占用。
系统维护一张**动态等待图（wait-for graph）**，任何会形成环的新增依赖立即拒绝；
超时任务进入 **uncertain（不确定）** 隔离状态，其占用的资源在确认物理停止前**绝不**
转交给下一任务。

技术栈：**Go 1.22 + chi + PostgreSQL**（pgx 原生驱动）。所有状态、持锁关系与凭证
均持久化在 PostgreSQL 中，重启后可完整恢复。

---

## 1. 核心语义（设计约束）

| 约束 | 实现方式 |
|---|---|
| **整组资源原子取得，失败不占部分** | 单个数据库事务 + `pg_advisory_xact_lock` 串行化分配决策；逐资源检查"是否全部空闲"，任一被占则整任务只留 `wanted` 行，一行 `held` 都不产生。资源表上还有部分唯一索引 `WHERE status='held'` 做最终防线。 |
| **动态依赖图拒绝环** | `task_resources` 中 `wanted`→`held` 的边构成有向等待图。新建任务和运行中任务**动态追加资源**时，用递归 CTE 从该任务出发搜索是否闭合环；成环则整体拒绝（HTTP 409 并返回环路径），任务保持原状。 |
| **运行中撤销只释放已确认停止的资源** | `revoke` 只把 `running/uncertain` 置为 `revoking`，**资源继续持有**；此时 worker 自己上报 `complete` 也被拒绝。只有运维/机器人安全层调用 `confirm-stop`（确认物理停机）后才释放并提升等待者。等待中的任务从未持锁，撤销立即生效。 |
| **超时先进入不确定状态** | 租约（lease）由 heartbeat 续租。后台 sweeper 把过期任务置为 `uncertain`，**不释放资源**：迟到的 `complete` 仍会被受理（真实机器人可能只是网络慢）；若最终确认宕机，才经 `confirm-stop` 释放。等待原因中会明确标注 `holderState=uncertain`。 |
| **优先级老化（aging）** | 有效优先级 = `priority − floor(等待时长 / AGING_STEP) × bonus`（有封顶）。等待越久越靠前，避免低优先级任务饥饿。释放时按有效优先级、等待时长、ID 确定性排序提升。 |
| **重启恢复 + 栅栏（fencing）** | 启动时 bump 全局 `fence_epoch`，所有 `running`→`uncertain`（资源继续 fenced），`waiting` 重建图。worker 完成/心跳必须携带授予时的 epoch；重启后旧进程的请求因 epoch 过旧被拒（fencing token 模式）。 |
| **资源持有证据** | 每次授予生成 **Ed25519 签名令牌**（载荷含任务、资源、授予时刻、epoch、服务器 ID），签名私钥种子持久化在数据库 `meta` 表，重启后签名不变。可通过 API 验签；`hold_ledger` 是 `wanted→granted→released` 的只追加审计链。 |

任务状态机：

```
            全部资源空闲                租约过期(sweeper)
 waiting ───────────────────▶ running ──────────────────▶ uncertain
   │  ▲                         │  │                          │
   │  │ 资源释放后按老化优先级提升  │  │ revoke                   │ late complete
   │  └─────────────────────────┘  ▼                          ▼ (epoch正确时受理)
   │                          revoking ◀────────────  confirm-stop / revoke
   │  revoke(从未持锁)            │ confirm-stop
   ▼                              ▼
 revoked ◀────────────────── 释放资源 → 提升等待者
```

---

## 2. 目录结构

```
.
├── cmd/deadlock-server/     # 服务入口：迁移、恢复、后台 sweeper、HTTP
├── internal/
│   ├── store/               # pgx 连接池、schema 迁移、签名密钥持久化
│   ├── service/             # 分配核心：事务、等待图、环检测、老化、fencing、恢复
│   └── httpapi/             # chi 路由与 JSON API
├── examples/                # 示例输入（资源、任务 JSON）
├── scripts/
│   ├── db-setup.sh          # 创建数据库角色/库（幂等）
│   ├── run-demo.sh          # 构建并用演示时序启动，再跑验收
│   └── acceptance.sh        # 纯 HTTP 端到端验收脚本（34 项断言）
├── docker-compose.yml       # 可选：本地 PostgreSQL
├── go.mod / go.sum          # 锁定依赖
└── README.md
```

## 3. 本地启动

### 3.1 准备 PostgreSQL

任选其一：

**A. 使用本机已有的 PostgreSQL（Ubuntu/Debian peer 认证）**

```bash
scripts/db-setup.sh
# 创建角色 deadlock / 密码 deadlock_pw_068，库 deadlock_db 与 deadlock_db_http
```

**B. 使用 Docker Compose**

```bash
docker compose up -d        # 监听 5432，库 deadlock_db，账号 deadlock/deadlock_pw_068
export DATABASE_DSN='postgres://deadlock:deadlock_pw_068@localhost:5432/deadlock_db?sslmode=disable'
```
> 用 compose 时，HTTP 集成测试库需额外创建一次：
> `docker compose exec postgres psql -U deadlock -d postgres -c 'CREATE DATABASE deadlock_db_http;'`

### 3.2 构建并运行

```bash
go build -o bin/deadlock-server ./cmd/deadlock-server
./bin/deadlock-server
# 可调环境变量：
#   DATABASE_DSN     默认 postgres://deadlock:deadlock_pw_068@localhost:5432/deadlock_db?sslmode=disable
#   HTTP_ADDR        默认 :8068
#   SWEEP_INTERVAL   默认 1s（租约扫描周期）
#   AGING_STEP_MS    默认 1000（每等待 1 秒有效优先级 -1）
#   AGING_CAP        默认 1000（老化加分上限）
```

启动时会自动建表迁移并执行重启恢复，日志形如：

```
connected and migrated
recovery: epoch=3 running->uncertain=[task-7] waiting=[task-9]
listening on :8068
```

### 3.3 一键演示（推荐）

```bash
scripts/run-demo.sh
```

它会用 `AGING_STEP_MS=100 SWEEP_INTERVAL=500ms`（便于快速观察老化与超时）启动服务，
随后运行 34 项 HTTP 端到端断言，覆盖题目要求的全部场景。

---

## 4. HTTP API（JSON）

| 方法 路径 | 说明 |
|---|---|
| `GET  /healthz` | 健康检查 |
| `GET  /v1/verify-key` | Ed25519 验签公钥（base64） |
| `POST /v1/verify-token` | 校验持有证据令牌，返回其明文载荷 |
| `GET/POST /v1/resources` | 列出 / 注册资源（kind=`tool`|`station`） |
| `GET  /v1/tasks` | 所有任务（含 `effectivePriority` 老化值） |
| `POST /v1/tasks` | **原子申请整组资源**，返回 `granted`/`waiting`/`rejected` |
| `GET  /v1/tasks/{id}` | 状态 + 当前持有（带签名证据）+ 等待原因 |
| `GET  /v1/tasks/{id}/waits` | 仅等待原因（被谁阻塞、持有者状态） |
| `GET  /v1/tasks/{id}/events` | 审计事件流 |
| `POST /v1/tasks/{id}/resources` | 运行中动态追加资源（成环则 409） |
| `POST /v1/tasks/{id}/heartbeat` | 续租，body `{"fenceEpoch":N}` |
| `POST /v1/tasks/{id}/complete` | 完成（允许 uncertain 下的迟到完成），body 带 epoch |
| `POST /v1/tasks/{id}/revoke` | 请求停机：running→revoking，**不释放** |
| `POST /v1/tasks/{id}/confirm-stop` | 确认物理停机：释放资源并提升等待者 |
| `POST /v1/sweep-timeouts` | 手动触发一次租约扫描 |
| `GET  /v1/graph` | 实时等待图：节点、边、当前检测到的所有环 |

错误一律 `{"error":"..."}`，业务拒绝返回 409。

### curl 速览

```bash
curl -s localhost:8068/v1/resources -X POST -d '{"id":"arm-1","kind":"tool"}'
curl -s localhost:8068/v1/tasks -X POST \
  -d '{"id":"t1","priority":100,"resources":["arm-1","station-a"],"deadlineMs":30000}'
# 响应里 holds[].evidenceToken 即持有证据；fenceEpoch 用于后续 heartbeat/complete
curl -s localhost:8068/v1/verify-token -X POST \
  -d '{"token":"<evidenceToken>"}'
curl -s localhost:8068/v1/tasks/t1
curl -s localhost:8068/v1/tasks/t1/waits
curl -s localhost:8068/v1/graph
curl -s localhost:8068/v1/tasks/t1/revoke        -X POST
curl -s localhost:8068/v1/tasks/t1/confirm-stop  -X POST
```

示例输入文件见 `examples/`（`resource-*.json`、`task-*.json`）。

---

## 5. 验收命令

```bash
# 0) 数据库（一次性）
scripts/db-setup.sh          # 或 docker compose up -d（见 3.1B 补测试库）

# 1) 自动化测试（真实 PostgreSQL，非 mock；含竞态检测与 HTTP 端到端）
go test ./...                # 使用 deadlock_db / deadlock_db_http
go test -race ./...          # 含 -race

# 2) 黑盒验收（服务真实运行，34 项断言，覆盖四个指定场景）
scripts/run-demo.sh
# 或分两步：
go build -o bin/deadlock-server ./cmd/deadlock-server
AGING_STEP_MS=100 SWEEP_INTERVAL=500ms ./bin/deadlock-server
scripts/acceptance.sh
```

### 测试与场景对应关系

| 题目要求 | Go 测试 | 验收脚本段落 |
|---|---|---|
| 交叉资源请求 | `TestCrossResourceRequests` / `TestAtomicAllOrNothing` | "cross over the arm" |
| 原子取得、不占部分 | `TestAtomicAllOrNothing`（断言 waiting 方持有数=0）+ DB 部分唯一索引 | "holds ZERO resources" |
| 动态图拒绝环 | `TestDynamicCycleRejected` / `TestHTTPCycleRejectedOverAPI` | "requests that would close a cycle" |
| 超时→不确定，不转交资源 | `TestTimeoutUncertainAndLateCompletion` / `TestHTTPEndToEnd` | "becomes UNCERTAIN… NOT receive" |
| 超时后迟到完成 | 同上（过期后带原 epoch complete 被受理） | "late completion … honoured" |
| 撤销仅在确认停止后释放 | `TestRevokeHandshake` / `TestRevokeRejectsWorkerComplete` | "releases only after confirmed stop" |
| 优先级老化 | `TestPriorityAgingHeadStart`（受控时钟） | "priority aging overtakes…" |
| 重启恢复 | `TestRestartRecovery`（bump epoch、running→uncertain、旧 epoch 被拒） | 脚本末段说明 + 可手动 kill -9 复现 |
| 持有证据/等待原因 | `TestCrossResourceRequests`（验签+篡改拒绝）、`TestLedgerEvidenceChain` | "Ed25519 token"、"audit trail" |
| 并发安全 | `TestConcurrentAcquires`（20 并发仅 1 获胜）+ `-race` | — |

重启恢复的手动复现（已实测）：

```bash
# 起服务、授予一个长租约任务 H、再排队 W
kill -9 $(pgrep -x deadlock-server)      # 模拟崩溃
./bin/deadlock-server                    # 重启日志：epoch+1，H running->uncertain
curl -sX POST localhost:8068/v1/tasks/H/complete -d '{"fenceEpoch":<旧epoch>}'
# => 409 stale or missing fence epoch ... (old worker rejected)
curl -sX POST localhost:8068/v1/tasks/H/confirm-stop   # 确认停机后 W 才 running
```

---

## 6. 依赖（已锁定）

`go.mod` / `go.sum` 已提交，构建可离线复现：

- `github.com/go-chi/chi/v5 v5.2.1`（Go 1.22 兼容线）
- `github.com/jackc/pgx/v5 v5.7.4`（原生 PostgreSQL 协议）

密码学使用 Go 标准库 **crypto/ed25519** 真实签名/验签（非占位）；时间与租约
由数据库事务与后台 sweeper 真实驱动，测试全部连接真实 PostgreSQL 执行。

## 7. 备注与边界

- 本服务是**分配与安全状态机**，不实现具体机器人通信；`confirm-stop` 代表外部安全
  层/运维对"物理已停止"的断言，这是释放 fenced 资源的唯一途径。
- 分配决策用单一事务级 advisory lock 串行化，正确性优先；吞吐受限于单点锁，
  对离线机器人任务规模足够，水平分片可作为后续演进。
