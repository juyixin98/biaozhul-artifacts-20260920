# 任务资源死锁检查服务（Task Resource Deadlock Check）

为机器人离线任务实现的**纯后端**资源分配与死锁防护服务。任务在执行前声明要**同时持有**的一组工具（tool）与工位（station），服务负责：

- **原子整组获取（all-or-nothing）**：要么一次性拿到全部资源，要么一个都不占，绝不留部分持有。
- **动态等待图（wait-for graph）拒绝环**：运行中任务可动态追加资源请求；若授权会让依赖图成环，立即返回 `409 cycle_detected` 并给出环路径，图始终保持无环。
- **超时先进入不确定（uncertain）状态**：任务超时**不立即释放**资源——它可能只是心跳迟到；在确认停止（complete/fail/revoke）或心跳恢复之前，资源绝不交给下一任务。
- **确认停止才撤销**：撤销（revoke）只释放调用方显式声明“已确认停止”的、且确属本任务持有的资源；任何一项不满足，整次撤销回滚。
- **优先级老化（aging）**：有效优先级 = 基础优先级 + 老化速率 × 已等待秒数，等待越久越靠前，防止低优先级任务饥饿。
- **重启恢复**：全部状态持久化在 PostgreSQL；重启后自动重建等待图，uncertain 任务的资源栅栏跨重启依然有效。
- **防篡改证据账本**：每次授权 / 释放 / 超时 / 撤销都写入一条审计记录，对**规范化（canonical）载荷做真实的 HMAC-SHA256 签名**（Go `crypto/hmac`+`crypto/sha256`，密钥 256-bit 随机、持久化在库中），读取时逐条验签，任何字段被篡改都会导致 `valid=false`。

技术栈：**Go 1.22 + chi v5 + PostgreSQL 16（pgx/v5）**，无前端页面。

---

## 目录结构

```
.
├── cmd/deadlockd/         # 服务入口：迁移 → 密钥引导 → 恢复 → 超时扫描器 → HTTP
├── internal/
│   ├── config/            # 环境变量配置
│   ├── store/             # 连接池、schema 迁移(go:embed)、HMAC 密钥引导
│   ├── core/              # 分配事务、等待图/环检测、老化、授权波、超时栅栏、撤销、恢复
│   ├── api/               # chi 路由、JSON 编解码、错误→HTTP 状态码
│   ├── evidence/          # canonical 规范化 + HMAC-SHA256 签名/验签（真实密码学）
│   └── testutil/          # 每测试独立数据库，支持跨包并行
├── examples/              # 示例输入 JSON + 端到端演示脚本 demo.sh
├── docker-compose.yml     # PostgreSQL 16（tmpfs，端口 55432）
├── Makefile
├── go.mod / go.sum        # 锁定依赖
└── README.md
```

## 并发与正确性模型

所有会读写分配状态的事务都先获取**同一个事务级咨询锁**（`pg_advisory_xact_lock(0x444541444C4F434B)`，即 ASCII "DEADLOCK"）。这把全局锁把分配决策串行化，临界区内只做少量单轮 SQL，既杜绝并发交叉授权，又不影响普通只读查询。

每次分配走一个“**授权波（grant wave）**”：

1. 载入一致快照：活跃任务、全部持有（holds）、全部等待请求及其声明资源；
2. 依据 holds + pending requests **重建** `wait_edges`；
3. 等待者按 `(老化后有效优先级 DESC, 到达时间 ASC, 请求 ID ASC)` 排序；
4. 依次尝试：**仅当整组资源此刻全部空闲才授权**（在同一事务内插入 holds），否则跳过——等待者拿不到任何部分；
5. 新授权后删除其出边，同波次后续请求看到更新后的占用。

动态请求在入队后、授权前先做一次**假设成环检测**（DFS，三级着色）；会成环则拒绝该请求、回滚，已有持有原封不动。

---

## 本地启动

前置：Docker（用于 Postgres）与 Go 1.22+。

```bash
# 1) 启动 PostgreSQL（用户名/密码/库名均为 deadlock，宿主端口 55432，tmpfs 不落盘）
docker compose up -d

# 2) 启动服务（默认 :8080）
go run ./cmd/deadlockd
# 或指定地址 / 库
DATABASE_URL='postgres://deadlock:deadlock@localhost:55432/deadlock?sslmode=disable' \
HTTP_ADDR=:8080 go run ./cmd/deadlockd
```

环境变量：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DATABASE_URL` | `postgres://deadlock:deadlock@localhost:55432/deadlock?sslmode=disable` | PostgreSQL DSN |
| `HTTP_ADDR` | `:8080` | HTTP 监听地址 |
| `AGING_PER_SEC` | `1.0` | 未显式指定时任务的默认老化速率（每秒增加的优先级） |
| `SWEEP_INTERVAL` | `500ms` | 超时扫描器周期 |
| `EVIDENCE_SECRET` | 空（首次启动随机生成并持久化） | HMAC 密钥；**仅在库中尚无密钥时**生效，便于从首启起多副本共享固定密钥；库中已存在的密钥不会被覆盖，以免历史证据失验 |

健康检查：`curl -sS localhost:8080/healthz` → `{"status":"ok"}`

---

## HTTP API（v1，JSON）

资源：
- `POST /api/v1/resources` 注册工具/工位
- `GET  /api/v1/resources` 资源清单及当前持有者

任务：
- `POST /api/v1/tasks` 建任务并**原子尝试**初始请求（成功 `201`，等待 `202`）
- `GET  /api/v1/tasks` / `GET /api/v1/tasks/{id}` 列表 / 详情（含持有、等待原因、队列位置、有效优先级）
- `POST /api/v1/tasks/{id}/requests` 运行中任务**动态追加**资源（会成环 → `409`）
- `POST /api/v1/tasks/{id}/complete` 确认完成 → 释放全部资源（uncertain 迟到完成也走这里）
- `POST /api/v1/tasks/{id}/fail` 确认失败 → 释放全部资源
- `POST /api/v1/tasks/{id}/revoke` 撤销：body 中逐项声明已确认停止的资源
- `POST /api/v1/tasks/{id}/heartbeat` 心跳续租；uncertain 任务借此恢复 running
- `GET  /api/v1/tasks/{id}/events` 生命周期事件流

可观测性 / 证据：
- `GET  /api/v1/holds` 资源持有证据（谁、哪个任务、哪次请求、何时获取）
- `GET  /api/v1/evidence?task_id=&limit=` 签名审计账本，每条带 `valid`
- `GET  /api/v1/debug/waits` 实时等待图（edges + cycles）
- `POST /api/v1/admin/sweep` 立即执行一次超时扫描
- `POST /api/v1/admin/grant-wave` 立即执行一次授权波

错误形态：`{"error":"cycle_detected","message":"...","details":{...}}`，状态码：400 校验、404 资源/任务不存在、409 冲突/成环、422 状态非法/资源非本任务持有。

### 最小调用示例

```bash
# 注册资源
curl -sS -XPOST localhost:8080/api/v1/resources -H 'Content-Type: application/json' -d '{
  "resources":[{"kind":"tool","name":"drill"},{"kind":"station","name":"bay-1"}]}'

# 原子获取 drill + bay-1（30s 租约）
curl -sS -XPOST localhost:8080/api/v1/tasks -H 'Content-Type: application/json' -d '{
  "label":"robot-A","priority":100,"timeout_ms":30000,
  "resources":[{"kind":"tool","name":"drill"},{"kind":"station","name":"bay-1"}]}'

# 等待者会拿到明确的等待原因：
# {"resource":{"kind":"tool","name":"drill"},
#  "holder_task_id":1,"holder_label":"robot-A","holder_state":"running","since":"..."}

# 确认停止（仅此时资源可被授权波交给等待者）
curl -sS -XPOST localhost:8080/api/v1/tasks/1/complete -d '{}'
```

更多示例输入见 [`examples/`](examples)：`resources.json`、`task-a.json`、`extra-request.json`。

---

## 自动化测试

测试用例真实连接 PostgreSQL（不是 mock），并为每个用例**自动创建/销毁独立数据库**，因此可跨包并行运行。

```bash
# 一条命令：起库 + 跑全部测试
make test

# 竞态检测
make test-race

# 或手动
docker compose up -d
go test -count=1 ./...
TEST_DATABASE_URL='postgres://...' go test -count=1 ./...   # 自定义库
```

覆盖场景（均真实执行并断言）：

| 测试 | 验证内容 |
|---|---|
| `TestAtomicAcquireAndPartialFailure` | 交叉请求：只满足部分资源时**零持有**，等待原因精确指向持有者 |
| `TestCrossRequestsAndCycleRejection` | AB-BA 动态交叉请求，闭环边返回 `409` 且带环路径；拒绝后图仍无环、已有持有不变；释放后正确转交 |
| `TestThreeNodeCycle` | 三任务环 T1→T2→T3→T1 被拒 |
| `TestTimeoutUncertainKeepsResources` | 超时 → uncertain，**资源不释放、授权波不转交**；迟到完成后才交给下一任务 |
| `TestLateHeartbeatRecoversUncertain` | uncertain 任务心跳恢复 running，保留资源并刷新截止期 |
| `TestRevokeOnlyConfirmedStopped` | 撤销的原子性：含未知资源整单回滚；非本任务持有返回 `resource_not_held`；单资源撤销后等待者被授权 |
| `TestPriorityAging` | 低优先级等待约 1s 老化反超新到的高优先级任务，并真正先获得资源 |
| `TestRestartRecovery` | 关闭连接池后用全新实例恢复：状态/持有保留、等待图重建、uncertain 栅栏跨重启有效 |
| `TestEvidenceSignatureAndTamper` | 授权/完成均有 HMAC 签名且验签通过；**直接篡改库中 canonical 载荷后验签失败** |
| `TestEvidenceCanonicalDeterminism` | canonical 规范化确定性、字段排序、篡改必被 HMAC 拒绝 |
| `TestHTTP*`（api 包） | 端到端 HTTP：409 成环、404/400 状态码、超时栅栏、证据端点 |

---

## 验收命令（建议照此执行）

```bash
# 0) 起依赖
docker compose up -d

# 1) 全部自动化测试（真实 Postgres，含 -race）
make test test-race

# 2) 启动服务
make run            # 或：go run ./cmd/deadlockd

# 3) 另开终端，跑端到端演示脚本（原子获取/成环/超时栅栏/迟到完成/撤销/老化/证据）
BASE=http://localhost:8080 bash examples/demo.sh
```

演示脚本会真实打印：B 想拿 drill+welder 时只等待且**不持有** welder；C 的闭环请求收到
`HTTP 409 cycle_detected` 与环 `["3:C","1:A","3:C"]`；D 超时后 E 仍 `waiting`
且 `holder_state="uncertain"`，D 迟到 `complete` 后 E 才 `running`；老化让
`L-old-lowprio` 在 ~2.2s 内从队尾升到队首；证据账本每条 `valid=True`。

### 手工快速核对

```bash
curl -sS localhost:8080/api/v1/holds            # 资源持有证据
curl -sS localhost:8080/api/v1/debug/waits      # 等待图（cycles 恒为空）
curl -sS localhost:8080/api/v1/evidence         # HMAC 证据（valid=true）
```

### 重启恢复核对

```bash
# 任务运行/等待中直接杀掉进程后重新 go run ./cmd/deadlockd
# 启动日志会打印：restart recovery complete: active tasks map[running:.. waiting:.. uncertain:..]
# 随后 GET /tasks/{id} 与 /debug/waits 显示状态、持有与等待边均已重建；
# /evidence 因 HMAC 密钥持久化在 service_meta，仍全部 valid=true。
```

---

## 设计取舍说明

- **不确定状态是一等公民**：`running → uncertain` 只清截止期，不清 holds、不触发授权波。这正对应“超时任务先进入不确定状态，不能立即把仍占用资源交给下一任务”。要真正释放，只有四条路径：`complete`（含迟到完成）、`fail`、逐条 `revoke`、或 `heartbeat` 恢复。
- **撤销语义**：服务不替调用方判断机器人是否真的停了——它只接受“**调用方断言已停 + 该资源确属本任务持有**”两个条件同时成立，整批成功或整批失败，保证证据链可信。
- **成环即拒，而非事后检测/抢占**：动态依赖图采用“保守拒绝闭环边”，因此系统中永不出现真实死锁，也无需死锁牺牲品回滚；已持有的资源绝不因等待而被回收。
- **证据链**：审计表存原始事件字段、规范化字符串与 HMAC；密钥不落盘到文件、只在库中（可由 `EVIDENCE_SECRET` 覆盖以便多实例）。验签覆盖事件类型、任务/请求 ID、时间戳与全部资源，任何一项改动都会失配。

## 关闭环境

```bash
docker compose down -v      # 停止并删除 tmpfs 数据库
```
