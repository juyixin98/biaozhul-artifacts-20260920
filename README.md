# 行为树可恢复执行（Resumable Behavior Tree Backend）

纯后端实现：Go + 标准库 `net/http` + PostgreSQL。动作全部是**本地桩动作**（不访问外部系统），
但执行、持久化、时序、取消、fence、哈希与随机 ID 都是真实计算，无模拟数据库/时钟（测试
可注入假时钟以确定性验证超时）。

- 节点类型：`sequence`、`fallback`、`parallel`、`timeout`（另含 `action`、`root`、`inverter`、`succeeder`）
- 状态：`running` / `success` / `failure`（内部还有 `canceled`，对父节点按 failure 等价处理）
- 每次 tick 有持久递增序号；中断的 tick 也占用一个序号（不重用、不留空号）
- 动作以稳定节点 ID 去重；**成功的非幂等桩动作重启后绝不重复执行**
- 父节点决策时取消仍在运行的子节点（并行阈值竞争、超时、abort）
- **迟到结果不得恢复树**：结果只更新动作 latch 行，且按 attempt 号 fence；树只在显式下一次 tick 推进
- 树定义发布后不可变，按内容 SHA-256 寻址；执行绑定具体 `(tree_id, version)`

## 目录结构

```
cmd/bt/                 HTTP 服务入口
internal/model/         树定义、校验、规范化 JSON + SHA-256 内容哈希
internal/store/         PostgreSQL schema(embed) 与持久化查询
internal/stub/          本地桩动作注册表（succeed/fail/block/wait/gate/echo/crash）
internal/engine/        tick 算法、saga 去重、取消、fence、重启恢复
internal/api/           HTTP 路由与处理器
examples/               示例树定义与 curl 验收脚本
scripts/setup-db.sh     本地数据库/角色初始化
```

## 运行环境

- Go 1.22+
- PostgreSQL 13+（在 16 上验证）
- 唯一直接依赖：`github.com/jackc/pgx/v5`（版本锁定在 `go.mod` / `go.sum`）

## 本地启动

```bash
# 1) 建库建角色（幂等；需要 sudo 到 postgres 系统用户）
./scripts/setup-db.sh

# 2) 启动服务（schema 自动迁移；启动时自动回收上次进程遗留的 running 调用/未闭合 tick）
BT_DATABASE_URL='postgres://bt065b:bt065b@localhost:5432/btree065b?sslmode=disable' \
  go run ./cmd/bt
# 默认监听 :8080，可用 BT_HTTP_ADDR 覆盖
```

## 验收命令

```bash
# 自动化测试（真实 PostgreSQL，每个测试使用独立 schema，结束自动清理；含 -race）
BT_TEST_DATABASE_URL='postgres://bt065b:bt065b@localhost:5432/btree065b?sslmode=disable' \
  go test ./... -count=1 -race

# HTTP 端到端走查（另开一个终端保持服务运行）
./examples/acceptance.sh
```

## 传播规则

| 节点 | 规则 |
|---|---|
| `sequence` | 从左到右；子成功→下一个；首个 `failure`/`canceled`→节点 failure；全成功→success；遇 running→running |
| `fallback` | 从左到右；子 `failure`/`canceled`→下一个；首个成功→success；遇 running→running；全失败→failure |
| `parallel` | 每 tick 求值全部子节点；成功数达到 `success` 阈值→success；失败数达到 `failure` 阈值→failure；否则 running。**阈值率先达到者决策，随后仍在运行的兄弟节点被取消**（成功竞争也取消落后者） |
| `timeout` | 首次 tick 设定并持久化 wall-clock 截止时间；期内子成功/失败按子结果；到点子节点仍 running→取消子树并 failure。截止时间跨重启保留 |
| `action` | 同步桩在 tick 内执行；异步桩启动后立即 running，结果经 watcher 落库。成功即 latch，后续 tick/重启直接返回成功，不再调用桩 |

终态节点状态写入 `node_states` 后被 latch：求值时命中终态直接返回，这是“成功非幂等动作不重复”的核心机制。

## 可恢复语义（关键设计）

1. **动作 saga**：`action_calls` 行先以 `running` 独立事务提交（含 attempt 号），桩执行结束后结果再独立事务提交。
   即使随后树状态事务回滚（tick 中断）或进程崩溃，成功记录依然在；running 但未成功的调用在启动时被回收为 `interrupted`，允许重跑。
   **只有 success 抑制重放**；failure/canceled/interrupted 可在后续 tick 以 attempt+1 重试。
2. **树状态单事务 latch**：每个 tick 的节点状态变化在最后一个事务写入；中断即整体回滚，不留半成品树状态。
3. **fence**：异步结果仅当数据库中 `attempt` 仍匹配且状态为 `running` 才写入。取消/超时/abort/重放后到达的结果 attempt 已过期，被丢弃；结果本身**不遍历树**，只改 latch。
4. **取消**：abort 先取消执行级 context（让正卡在同步桩里的 tick 立即中断并释放 advisory lock），再持同一把执行级 advisory lock 做状态迁移；父节点决策通过 `cancelSubtree` 物理取消仍在运行的动作并把它们标记为 canceled。
5. **tick 串行化**：同一执行的 tick 由 PostgreSQL session 级 advisory lock（按执行 ID 哈希）串行化，跨 HTTP handler / 进程安全。
6. **tick 中断**：HTTP 请求 context 取消即中断 tick；同步动作随 tick context 取消，异步动作跨 tick 存活（仅本 tick 启动的异步尝试会随中断取消）。

## 定义不可变与版本绑定

- 发布时做结构校验（节点 ID 稳定且树内唯一、并行阈值可决策、timeout 需要正数 ms 等）。
- 规范化 JSON（args 反序列化再编码，消除键序/空白差异）后计算 SHA-256；相同内容重复发布返回同一版本（幂等）。
- 同名修改后的定义复用同一 lineage `tree_id` 并产生下一 `version`；执行表以复合外键 `(tree_id, tree_version)` 绑定，旧执行永远跑旧版本。

## HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | 健康检查 |
| POST | `/v1/trees` | 发布树（body：`{"tree": {...}}`），返回 `tree_id/version/hash` |
| GET | `/v1/trees/{id}` | 查询树 lineage 元数据（默认最新版本） |
| POST | `/v1/executions` | 创建执行：`{"tree_id":"...","version":0}`（0=最新） |
| GET | `/v1/executions/{id}` | 完整快照（执行状态、各节点状态、各动作 latch、末次 tick） |
| POST | `/v1/executions/{id}/ticks` | 执行一次 tick；请求被取消则返回 `status:"interrupted"` 且占用序号 |
| POST | `/v1/executions/{id}/abort` | 终止执行并取消所有运行中动作 |
| GET | `/v1/executions/{id}/invocations/{node}` | 某动作物理执行次数（去重证据） |
| GET | `/internal/gates` | 当前被 gate 桩持有的令牌 |
| POST | `/internal/gates/{token}/resolve` | 异步放行 gate：`{"status":"success|failure",...}`；无人持有时返回 404（迟到放行被拒绝） |

### 树定义 JSON 示例（节选）

```json
{
  "id": "p", "kind": "parallel", "success": 2, "failure": 2,
  "children": [
    {"id": "a", "kind": "action", "stub": "gate", "args": {"token": "task-a"}},
    {"id": "b", "kind": "action", "stub": "wait", "args": {"ms": 250}},
    {"id": "c", "kind": "action", "stub": "succeed", "non_idempotent": true}
  ]
}
```

## 内置桩动作

| stub | 类型 | 参数 / 行为 |
|---|---|---|
| `succeed` | 同步 | 立即成功（可带 `output`） |
| `fail` | 同步 | 立即失败（`reason`） |
| `crash` | 同步 | 失败，用于 fallback 演示 |
| `echo` | 同步 | 成功并把 args 作为 output |
| `block` | 同步 | 阻塞直到被取消，或 `release_ms` 后返回（确定性测试 tick 中断） |
| `wait` | 异步 | `ms` 后返回 `outcome`（默认成功） |
| `gate` | 异步 | 等待 `/internal/gates/{token}/resolve` 放行；取消时令牌自动释放，迟到放行 404 |

## 测试覆盖

`internal/engine/engine_integration_test.go`（真实 PostgreSQL，独立 schema）：

1. Sequence 全成功 / 短路失败
2. 非幂等成功跨多 tick + 模拟完整重启**只物理执行一次**
3. Parallel 成功阈值竞争：领先者成功，落后兄弟 canceled，迟到放行被拒绝
4. Parallel 失败阈值（3 子、s=2/f=2）及决策后取消剩余子节点
5. Timeout 到期取消子节点 + 迟到结果 fence
6. Timeout 成功竞争（期限内成功）
7. Timeout 截止时间跨重启持久化
8. Fallback 回退分支（同步/异步）
9. tick 中断占用持久序号、同步动作中断重跑（连续两次中断 seq=1,2）
10. 序号跨重启单调无空洞
11. 无视取消的 rogue 桩迟到成功被 SQL fence 丢弃，执行状态不变
12. abort 取消子节点、终态拒绝 tick、迟到结果拒绝
13. 定义不可变/版本绑定：相同内容幂等、改动产生 v2、旧执行绑定 v1
14. 重复节点 ID 等非法定义发布时拒绝

`internal/api/api_test.go`：真实 TCP 服务器上的完整生命周期、**客户端断连中断 tick**、
非法输入 400、abort 后迟到 gate 放行 404。`internal/model/tree_test.go`：校验与内容哈希稳定性。

## 失败如实报告

- 数据库不可用、迁移失败、启动回收失败：进程 `log.Fatal`，不伪装成健康。
- 未知桩：动作记 failure（可触发 fallback），不静默成功。
- 终态执行 tick/abort：HTTP 409；不存在资源：404；非法定义：400。
- 迟到/过期结果：SQL fence 丢弃，gate 放行返回 404，并在审计表 `stub_invocations` 留痕。
