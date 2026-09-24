# dagexec — 可恢复 DAG 执行器（纯 Go 标准库）

本地运行的 DAG（有向无环图）任务执行 HTTP 服务。任务只能调用**内置白名单纯函数**；
支持依赖调度、有限重试（指数退避）、取消、成功结果缓存，以及**调度状态持久化与崩溃恢复**。
仅后端，无界面。

零第三方依赖：仅使用 Go 标准库（`net/http`、`encoding/json`、`crypto/rand` 等），
因此没有 `go.sum`，`go build` 不需要联网。

---

## 1. 依赖与启动

### 依赖

- Go 1.23+（仅标准库；在 `go1.23.4 linux/amd64` 上实测）
- 无外部数据库 / 无第三方 Go module / 无前端

### 构建

```bash
go build -o bin/dagexec ./cmd/dagexec
```

### 启动

```bash
go run ./cmd/dagexec \
  -addr :8080 \
  -state ./data/state.json \
  -max-attempts 3 \
  -max-parallel 4 \
  -retry-base-delay 200ms \
  -retry-max-delay 30s
```

| 参数 | 默认值 | 说明 |
|---|---|---|
| `-addr` | `:8080` | HTTP 监听地址 |
| `-state` | `./data/state.json` | 持久化状态文件（原子写：临时文件 + `rename`） |
| `-max-attempts` | `3` | 每个节点单次激活的默认尝试次数上限 |
| `-max-parallel` | `4` | 每个 DAG 内节点并发上限（信号量） |
| `-retry-base-delay` | `200ms` | 首次重试退避，之后倍增 |
| `-retry-max-delay` | `30s` | 退避上限 |

启动时会读取状态文件并**恢复所有未终态的 DAG**；进程重启（含 SIGKILL 崩溃）后，
已成功节点不会重复执行（见“持久化与恢复语义”）。

健康检查：

```bash
curl http://localhost:8080/health
# {"status":"ok"}
```

---

## 2. HTTP 接口

所有请求/响应均为 JSON。

| 方法与路径 | 说明 | 成功状态码 |
|---|---|---|
| `GET  /health` | 健康检查 | 200 |
| `GET  /tasks` | 列出白名单任务与状态枚举 | 200 |
| `POST /dags` | 提交一个 DAG（校验：环、缺失依赖、白名单、重复 id/依赖） | 201 |
| `GET  /dags` | 列出所有 DAG 及状态 | 200 |
| `GET  /dags/{id}` | 查询单个 DAG 的完整状态 | 200 |
| `GET  /dags/{id}/wait?timeout=5s` | 阻塞至终态或超时（超时返回 202 + 当前状态） | 200 |
| `POST /dags/{id}/cancel` | 请求取消（先持久化 `cancelling`，再中断在跑任务） | 202 |
| `POST /dags/{id}/retry` | 重新激活 `failed`/`cancelled` 的 DAG | 202 |

错误：校验失败 `400`（`details` 中一次返回全部错误）；未知 id `404`；
状态不允许该操作（如对运行中的 DAG retry）`409`。

### 提交体格式

```json
{
  "name": "可选名称",
  "max_attempts": 3,
  "max_parallel": 4,
  "nodes": [
    {"id": "A", "task": "identity", "params": {"value": 7}},
    {"id": "B", "task": "flaky",
     "params": {"fail_times": 2, "succeed_with": 32},
     "deps": ["A"], "max_attempts": 3}
  ]
}
```

- 节点级 `max_attempts` 覆盖 DAG 级，DAG 级覆盖服务默认值；解析后的默认值在提交时
  固化进 spec，使持久化状态自包含。
- `deps` 是依赖节点 id 列表；任务函数通过 `upstream[depId]` 拿到所有依赖的**缓存结果**。

### 节点 / DAG 状态

节点：`pending running success failed blocked cancelled`
DAG：`pending running succeeded failed cancelling cancelled`

每个节点带：

- `attempt`：**当前激活**内已尝试次数（retry 后归零）
- `total_runs`：**生命周期内**真实执行总次数（不随重试/retry 归零，重启也保留）。
  这是“成功节点不重复执行”的客观证据。
- `result`：成功后缓存的结果；`retry_after`：下次退避到期时间。

---

## 3. 内置白名单任务

| task | 行为 |
|---|---|
| `noop` | 无输入，返回 `null` |
| `identity` | 返回 `params.value` |
| `add` | `params.values` 与所有上游数值结果之和（整数结果返回整数） |
| `mul` | 同上，求积 |
| `concat` | 把 `params.values` 与上游结果按字符串拼接，`params.sep` 为分隔符 |
| `collect` | 返回 `{"params": ..., "upstream": {依赖id: 值}}` |
| `fail` | 恒失败，错误信息取 `params.message` |
| `flaky` | **生命周期内**前 `params.fail_times` 次执行失败，之后返回 `params.succeed_with`（默认 `"ok"`）。计数依据持久化的 `total_runs`，故失败计划跨重启有效 |
| `sleep` | 睡眠 `params.ms` 毫秒，响应取消；用于取消演示 |

任务函数都是 `(params, upstream结果, attempt信息)` 的纯函数（`sleep` 仅为取消演示，
不产出可观察值）。不在白名单内的任务名在提交时被拒绝，**不会**执行任何外部命令。

---

## 4. 请求样例（curl）

### 4.1 菱形依赖 + 中间任务永久失败（核心验收场景）

```bash
curl -s -X POST localhost:8080/dags -H 'Content-Type: application/json' \
  -d @examples/diamond_fail.json
# 结构：A -> B(恒失败,2 次) -> E
#       A -> C(add)       -> E
curl -s "localhost:8080/dags/<id>/wait?timeout=5s"
# A=success(runs=1) B=failed(runs=2) C=success(runs=1, 复用 A 的 7) E=blocked(runs=0)
```

### 4.2 取消（取消后不得启动新的下游节点）

```bash
curl -s -X POST localhost:8080/dags -H 'Content-Type: application/json' \
  -d @examples/cancel_demo.json
# A(sleep 30s) -> B -> C
curl -s -X POST localhost:8080/dags/<id>/cancel
# A 被中断（runs=1），B/C 从未启动（runs=0），整体 cancelled
```

### 4.3 有限重试 + 指数退避 + 结果缓存

```bash
curl -s -X POST localhost:8080/dags -H 'Content-Type: application/json' \
  -d @examples/diamond_flaky.json
# B 前 2 次失败、第 3 次成功（退避 200ms+400ms），E=32+10=42
```

### 4.4 环 / 缺失依赖 / 非白名单（均 400）

```bash
curl -i -X POST localhost:8080/dags -H 'Content-Type: application/json' -d '{
  "nodes": [
    {"id":"a","task":"noop","deps":["b"]},
    {"id":"b","task":"noop","deps":["a"]}
  ]}'
# 400, details: ["dependency cycle detected involving nodes: ..."]

curl -i -X POST localhost:8080/dags -H 'Content-Type: application/json' -d '{
  "nodes":[{"id":"a","task":"shell.exec","deps":["ghost"]}]}'
# 400, details 同时包含 "not in the whitelist" 与 "depends on missing node"
```

### 4.5 失败后人工 Retry（成功节点保持缓存不重跑）

```bash
curl -s -X POST localhost:8080/dags/<failed-id>/retry
# success 节点 total_runs 不变；failed/blocked/cancelled 节点重置后重跑
```

---

## 5. 持久化与恢复语义（重启验收点）

- 每次状态迁移都在**任务协程启动之前**同步落盘（JSON，临时文件 `fsync` 后原子 `rename`）。
- **成功节点**：重启后保持 `success` + 缓存 `result` + 原 `total_runs`，**绝不重跑**。
- **崩溃时处于 `running` 的节点**：该次尝试随进程死亡，重启后重置为 `pending` 重新执行
  （`total_runs` +1）；其已成功的上游不重跑。
- **`cancelling` 时崩溃**：重启后收敛为 `cancelled`，所有非成功节点标记 cancelled。
- **已 `cancelled`/`succeeded` 的 DAG**：重启后保持终态，不会被复活。
- 失败扇出：某节点耗尽重试后，其所有传递下游在**同一持久化事务**内标记 `blocked`，
  与失败无关的独立分支继续执行。

### 一键端到端验收脚本

`examples/accept_e2e.py` 对一个运行中的服务做黑盒断言（44 项）。分两阶段，中间手动重启服务：

```bash
# 终端 1：起服务（全新状态目录）
go run ./cmd/dagexec -addr :18093 -state /tmp/dagfinal/state.json -retry-base-delay 200ms

# 终端 2：重启前阶段（菱形失败 / 取消 / 校验 / 重试退避）
python3 examples/accept_e2e.py pre  http://localhost:18093 /tmp/dagfinal

# 终端 1：Ctrl-C 停掉，再用同一 -state 重新启动
go run ./cmd/dagexec -addr :18093 -state /tmp/dagfinal/state.json -retry-base-delay 200ms

# 终端 2：重启后阶段（成功节点不重跑 / cancelled 不复活 / retry 缓存保持）
python3 examples/accept_e2e.py post http://localhost:18093 /tmp/dagfinal
# SUMMARY: 16 passed, 0 failed
```

---

## 6. 自动化测试

```bash
go test ./... -race -count=1
```

覆盖（含 `-race`）：

- `internal/dag`：环检测（含自环）、缺失依赖、重复 id/依赖、非白名单、空图；
  各白名单函数；`flaky` 跨激活计数；`sleep` 的取消响应。
- `internal/store`：状态往返、缺失文件视为空库、深拷贝隔离、原子文件格式。
- `internal/scheduler`（核心验收）：
  - 菱形 + 中间永久失败 + **重启后成功节点 total_runs 不变**；
  - 取消后下游 `total_runs=0`；取消态持久化跨重启；
  - 硬崩溃（任务运行中 SIGKILL）后崩溃节点恰好重跑一次、成功上游不重跑；
  - 有限重试耗尽 / 退避后成功 / 结果缓存传递；
  - 独立分支在失败扇出外继续运行；并发上限；Cancel 与 Retry 并发不损坏状态；
  - 已 cancelled DAG 恢复时不被复活（回归测试）。
- `internal/api`：通过 `httptest` 验证路由、状态码（201/202/400/404/405）、
  校验 details、cancel→retry 全流程、`/wait`。

实测结果见仓库根目录 `RUNBOOK.md`（如实记录通过项与限制）。

---

## 7. 目录结构

```
cmd/dagexec/main.go          入口：flag、HTTP server、优雅停机
internal/dag/                领域模型：Spec/State、校验（环/缺失依赖）、白名单函数
internal/store/              JSON 文件持久化（原子写、深拷贝）
internal/scheduler/          调度器：依赖调度、重试退避、取消、崩溃恢复
internal/api/                net/http handler 与错误映射
examples/                    提交样例 JSON 与端到端验收脚本
```

---

## 8. 明确的限制（未做 / 不做）

- **单进程**：持久化假设同一时刻只有一个 server 写状态文件，无多实例分布式锁。
- 崩溃窗口内正在执行的那次尝试可能有**外部副作用**——本服务只允许纯函数白名单，
  因此重跑安全；若将来接入非幂等任务，需要任务自身幂等键。
- `at-most-once` 的持久化 + `running` 重放提供的是“成功结果恰好生效一次、崩溃在跑
  的任务至少执行一次”的语义，不是分布式 exactly-once。
- 退避为固定指数（base 倍增至 cap），无抖动、无死信队列、无优先级、无定时触发。
- 无鉴权 / 限流 / 多租户；监听地址默认全网卡，生产暴露需自行加反代与访问控制。
- 无界面（按要求仅后端）。
