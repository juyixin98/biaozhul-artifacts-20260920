# Resumable Behavior-Tree Backend

纯后端、可恢复执行的行为树服务。**Go + net/http + PostgreSQL**，动作只调用进程内本地桩（local stubs），不依赖任何外部服务。

实现并测试了以下能力：

- **节点类型**：`sequence`、`fallback`、`parallel`（成功/失败配额阈值）、`timeout`、`action`。
- **状态传播**：`running` / `success` / `failure` 三态，按 BT 标准规则在父子节点间传播。
- **可恢复 tick**：每次 tick 有持久化、单调连续的序号；整次 tick 在单个 PostgreSQL 事务中提交。tick 上下文在提交前被取消时整体回滚，序号不被消耗（记一条 `interrupted` 审计行）。
- **动作去重**：每个动作用稳定 ID（`stable_key`，由执行 ID + 节点 ID 确定性派生）去重。**进程重启后，已成功的非幂等动作绝不重复执行**——直接复用持久化结果；只有 `pending`/`running` 的孤儿调用会以新的 `attempt` 纪元重新领取。
- **取消传播**：父节点（parallel 达到配额、timeout 到期、整树取消）会在事务内把仍在运行的子调用置为 `canceled` 并中断其 worker 上下文。
- **迟到结果屏障**：worker 回报只在 `status='running' AND attempt=<当前纪元>` 时才被接受；被取消或旧纪元的迟到结果影响 0 行，**永远不能把树“复活”**。
- **不可变版本**：树定义发布后不可修改；相同内容哈希返回同一版本，内容变更产生新版本；执行在创建时绑定具体版本，之后发布新版本不影响在跑的执行。
- **真实计算**：桩动作里的等待、失败都是真实发生的；`hash.sha256` 动作真实计算 SHA-256；内容哈希、稳定 ID 也用真实 SHA-256。

---

## 1. 目录结构

```
.
├── cmd/btserver/main.go          HTTP 服务入口（迁移、恢复、优雅退出）
├── internal/
│   ├── tree/                     定义、JSON 解析、结构校验、规范化内容哈希
│   ├── stub/                     本地桩动作注册表（stub / stub.nonidempotent / hash.sha256）
│   ├── store/                    PostgreSQL schema、连接池、事务与全部查询
│   ├── engine/                   节点语义、事务化 tick、worker、取消、恢复
│   └── httpapi/                  HTTP 路由与 JSON 协议
├── examples/                     示例树定义 + 一键验收脚本
├── go.mod / go.sum               锁定依赖（pgx v5.6.0 / uuid v1.6.0，兼容 Go 1.22）
└── README.md
```

## 2. 本地前置条件

- Go 1.22+
- PostgreSQL 14+（本机已有 16 即可）

创建数据库账号与库（用超级用户执行一次）：

```bash
sudo -u postgres psql -c "CREATE ROLE btapp LOGIN PASSWORD 'btapp_dev_pw';"
sudo -u postgres createdb -O btapp btdb
```

> schema 由服务启动时自动迁移（`internal/store/schema.sql`，全部 `CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS`），无需手工建表。

## 3. 启动

```bash
go build -o btserver ./cmd/btserver
# 默认 DSN: postgres://btapp:btapp_dev_pw@localhost:5432/btdb?sslmode=disable
# 默认监听: :8080  （可用 BT_HTTP_ADDR / BT_DATABASE_DSN 覆盖）
./btserver
```

看到如下日志即就绪：

```
schema migrated
startup recovery complete
listening on :8080
```

服务启动时会对崩溃前处于 `pending`/`running` 的调用做恢复（重新领取并以新 attempt 纪元执行），并重建 timeout 定时器。

## 4. HTTP 协议

| 方法 | 路径 | 说明 |
|---|---|---|
| GET  | `/healthz` | 健康检查 |
| POST | `/trees/{name}/versions` | 发布版本（body 为树 JSON）。相同内容返回 `200 created:false` 且复用版本；新内容返回 `201` |
| GET  | `/trees/{name}/versions/latest` | 最新版本号 |
| GET  | `/trees/{name}/versions/{version}` | 读取某不可变版本的定义 |
| POST | `/executions` | 创建执行，body `{"tree":"name","version":1}`（version 可省略，取最新） |
| POST | `/executions/{id}/tick` | 手动推进一个 tick；body 可带 `{"note":"..."}` |
| POST | `/executions/{id}/cancel` | 取消整树并取消所有在跑动作 |
| GET  | `/executions/{id}` | 执行头信息 + tick 历史 |
| GET  | `/executions/{id}/snapshot` | 完整可恢复快照：执行、各节点状态、各调用（含 dispatches/attempt/result）、tick 历史 |

动作完成后引擎会自动连续 tick，直到下一个 running 或终态；通常只需发第一个 tick（或等 timeout 定时器触发）。对终态执行再 tick 返回 `409`。

### 树定义 JSON

```json
{
  "root": "root_seq",
  "nodes": {
    "root_seq": {"id":"root_seq","kind":"sequence","children":["a","b"]},
    "a": {"id":"a","kind":"action","action":"stub","idempotent":false,
          "params": {"delay_ms": 100, "message": "hi", "result": "success"}},
    "b": {"id":"b","kind":"timeout","timeout_ms":500,"children":["c"]},
    "c": {"id":"c","kind":"parallel","success_threshold":1,"failure_threshold":2,
          "children":["d","e"]},
    "d": {"id":"d","kind":"action","action":"hash.sha256","params":{"input":"abc"}},
    "e": {"id":"e","kind":"action","action":"stub","params":{"result":"failure"}}
  }
}
```

字段规则：

- `sequence` / `fallback`：需要 ≥1 个 `children`，按顺序 tick。
- `parallel`：需要 `success_threshold`、`failure_threshold`（均 ≥1 且 ≤ 子节点数，且二者之和 ≤ 子节点数 + 1）。达到成功配额 → 节点成功并取消其余 running 子节点；达到失败配额 → 节点失败并取消其余子节点；同 tick 两者皆满足时成功优先（确定性）。
- `timeout`：恰好 1 个子节点，`timeout_ms > 0`。截止时刻在**第一次 tick 时确定并持久化**，重启不重置计时。到期时节点 `failure`，子树被取消。
- `action`：无子节点；`action` 必须是已注册桩；`idempotent` 为声明值（成功后无论是否幂等都不会重跑）。
- 定义必须是一棵真正的树：从 root 可达、无共享节点/环、无孤儿节点。

### 内置桩动作

| action | 参数 | 行为 |
|---|---|---|
| `stub` | `delay_ms`、`result`（success/failure）、`message`、`echo` | 可声明为幂等；上下文取消时立即返回取消错误 |
| `stub.nonidempotent` | 同上 | 语义相同，注册为非幂等（用于验证重启不重放） |
| `hash.sha256` | `input`、可选 `delay_ms` | **真实计算** SHA-256，返回 hex 摘要 |

## 5. 快速手动验证（curl）

```bash
# 发布
curl -s -X POST localhost:8080/trees/demo/versions -d @examples/01_sequence.json
# 建执行
E=$(curl -s -X POST localhost:8080/executions -d '{"tree":"demo"}' \
    | python3 -c "import json,sys;print(json.load(sys.stdin)['execution_id'])")
# 第一个 tick（之后自动驱动）
curl -s -X POST localhost:8080/executions/$E/tick -d '{}'
# 看终态与各动作的 dispatches/attempt/result
curl -s localhost:8080/executions/$E/snapshot | python3 -m json.tool
```

## 6. 一键验收

先启动服务，然后：

```bash
./examples/acceptance.sh
```

脚本覆盖：不可变发布/幂等重发、sequence + 真实 SHA-256、fallback 逐分支回退、
parallel 阈值与慢兄弟取消、timeout 与成功竞争（双向）、迟到结果不复活、
取消后 tick 409、非法定义 400、未知执行 404。全部通过时退出码为 0。

## 7. 自动化测试

```bash
# 需要可连的 PostgreSQL；DSN 可用 BT_TEST_DSN 覆盖
go test -race -p 1 ./...
```

> 说明：各测试包会 TRUNCATE 同一个库，因此用 `-p 1` 串行执行测试包。
> 连不上数据库时 DB 测试会 skip（`tree` 包的纯逻辑测试始终运行）。

关键测试：

- `internal/tree`：解析、校验（环/共享/孤儿/阈值非法等）、内容哈希稳定且对语义敏感。
- `internal/engine`：
  - `TestParallelThresholdCancelsSiblings` / `TestParallelFailureThreshold`：阈值与取消传播；
  - `TestTimeoutWins` / `TestTimeoutSuccessRace`：超时与成功竞争，迟到结果不复活；
  - `TestNonIdempotentNoReexecuteAcrossRestart`：**重建引擎（模拟重启）后非幂等成功动作 dispatches/attempt 保持 1，新 registry 计数为 0**；
  - `TestTimeoutDeadlineSurvivesRestart`：timeout 截止时刻跨重启仍然生效（剩余预算而非重新计时）；
  - `TestTickInterruption`：提交前取消 → 事务回滚、序号不前进、无泄漏调用、留下 interrupted 行、随后同序号重试成功；
  - `TestConcurrentTicksSerialized`：并发 tick 被串行化，动作只 dispatch 一次、已提交序号无空洞；
  - `TestLateResultRejected`：worker 迟到回报被 attempt 屏障拒绝；
  - `TestFallbackBranches` / `TestFallbackResumePosition`：回退分支与跨 tick 续跑位置。
- `internal/httpapi`：端到端 HTTP、取消冲突 409、404、执行绑定版本不被后续发布影响。

## 8. 设计要点（为什么能保证恢复与不重放）

1. **一次 tick 一个事务**。读执行行时 `SELECT … FOR UPDATE` 取行锁，配合进程内每执行互斥锁，串行化同一执行的所有 tick（含多实例部署时跨进程串行）。
2. **序号只在提交时推进**。`ticks` 表记录每次尝试；提交前取消的 tick 随事务回滚，另写一条 `interrupted` 行审计，下一次 tick 仍是同一个序号。
3. **调用表用稳定主键** `(execution_id, stable_key)`。节点重入时先读已有调用：
   - `success`/`failure` → 直接返回，绝不重新执行；
   - `canceled` → 永远失败，迟到结果无效；
   - `pending`/`running` → 提交后重新 dispatch（首次 tick 提交后崩溃、或 worker 随进程死亡的恢复路径）。
4. **领取（claim）即纪元 +1**。`ClaimInvocation` 把 `attempt`/`dispatches` 原子 +1，只有领取者运行桩体；完成回报带乐观条件 `status='running' AND attempt=?`。取消把行改为 `canceled`，于是旧 worker 的迟到回报影响 0 行。
5. **取消在事务内完成**，worker 上下文在事务提交后才中断，保证“DB 已取消”与“worker 已停”顺序不会颠倒；worker 即使在取消前一刻算出结果，也会被第 4 步的屏障挡下。
6. **版本内容寻址**。发布时对规范化 JSON 计算 SHA-256：相同定义（无论字段/键序）哈希相同，语义变化哈希必变；执行行冗余保存版本号与哈希，定义表不再提供更新/删除接口，保证不可变。
