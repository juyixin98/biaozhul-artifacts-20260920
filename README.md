# Gang Scheduler（成组任务原子调度器）

纯后端内存调度器，用 **Go 标准库 `net/http`** 实现，无任何第三方依赖。

同一个 gang（任务组）的所有任务要么**一次性全部**拿到节点（HELD→RUNNING），
要么**全部等待**（WAITING），绝不会部分启动。支持：

- **两阶段预留/提交**：reserve 先在节点上持有（hold）槽位并设预留超时（TTL），commit 时用版本检查确认计划未过期后原子转运行；
- **节点标签选择器**：任务可要求 `match_labels`（等值）与 `in_labels`（IN 集合）；
- **反亲和**：`distinct_nodes=true` 时同组任务必须落在不同节点；
- **预留超时**：后台 reaper 回收未提交的预留与久等无着的排队组，槽位不泄漏；
- **提交计划版本检查**：节点 ON/OFF 等结构性变化会推进 `version`，基于旧版本的提交被拒绝并整体回滚。

## 目录结构

```
cmd/gangd/main.go              HTTP 服务入口（信号优雅关停、访问日志）
internal/scheduler/scheduler.go   调度核心（单锁状态机 + FIFO 队列 + reaper）
internal/scheduler/scheduler_test.go  调度器单元/并发测试（11 个）
internal/api/server.go         HTTP 路由与 JSON DTO
internal/api/server_test.go    HTTP 端到端验收测试（4 个）
scripts/demo.sh                真实起服务跑验收剧本
docs/API-EXAMPLES.md           全部接口的 curl 请求样例
docs/demo-output.log           最近一次 demo 的完整请求/响应记录
go.mod                         模块定义（仅标准库，无第三方依赖）
```

## 依赖与环境

- Go **1.23+**（本机实测 go1.23.4 linux/amd64）
- 运行/构建零第三方依赖，仅用标准库（`net/http`、`encoding/json`、`sync`、`time` 等）
- `go.mod` 锁定语言版本；因无外部模块，无需也不会生成 `go.sum`
- demo 脚本需要 `curl`、`jq`、`bash`（端口自动选取，需要 `python3`，没有则用随机端口兜底）

## 启动命令

```bash
# 构建
go build ./...

# 启动（默认 :8080，预留 TTL 5s，reaper 50ms）
go run ./cmd/gangd
# 或编译后运行
go build -o gangd ./cmd/gangd && ./gangd -addr :8080 -ttl 5s -reap 50ms
# 也可用环境变量 GANGD_ADDR 指定监听地址
```

健康检查：`curl -s http://127.0.0.1:8080/healthz` → `{"status":"ok"}`

## 快速体验（真实起服务跑验收剧本）

```bash
./scripts/demo.sh        # 自动选空闲端口，输出存 docs/demo-output.log
```

手工请求样例见 **[`docs/API-EXAMPLES.md`](docs/API-EXAMPLES.md)**。

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET  | `/healthz` | 健康检查 |
| POST | `/v1/nodes` | 注册节点 `{name,capacity,labels}` |
| GET  | `/v1/nodes` / `/v1/nodes/{name}` | 节点列表/详情（含 reserved/running/version） |
| POST | `/v1/nodes/{name}/status` | `{"status":"ONLINE"|"OFFLINE"}`，版本号 +1 |
| POST | `/v1/gangs` | 提交组：`{id,ttl_ms,distinct_nodes,tasks[]}`，立即尝试整组预留 |
| GET  | `/v1/gangs` / `/v1/gangs/{id}` | 组列表/详情 |
| POST | `/v1/gangs/{id}/commit` | 携带 `expected_node_versions` 原子提交 |
| POST | `/v1/gangs/{id}/release` | 结束运行/取消等待或预留，归还槽位 |
| POST | `/v1/gangs/{id}/replan` | FAILED/WAITING 重新排队尝试（不插队，走 FIFO） |
| DELETE | `/v1/gangs/{id}` | 删除（RUNNING/HELD 需先 release） |
| GET  | `/v1/state` | 全量快照（核对账目用） |

任务选择器字段：

```json
{
  "id": "t1",
  "slots": 2,
  "match_labels": {"tier": "gpu"},
  "in_labels": {"zone": ["a", "b"]}
}
```

## 设计与正确性说明

**状态机**：`WAITING → HELD → RUNNING → COMPLETE`；任何一步失败进入
`FAILED`（可 `replan` 重新排队）。所有状态迁移在同一把互斥锁下完成，
reserve / commit / 节点上下线 / 释放彼此不可交错。

**全组原子性**：规划器用 DFS 最佳适配为整组找一套放置方案；放不下整组就
**一个槽位都不持有**并入 FIFO 队尾。队列按顺序泵取（pump），队头放不下即
阻塞（head-of-line），后面的组不允许插队，保证公平且无饿死式重排。

**两阶段与版本检查**：

1. `reserve`：方案成立时把槽位记为 `reserved`（其他组可见其不可用，但
   本组尚未运行），返回每节点 `version` 作为计划基线；
2. `commit`：在锁内依次校验 ① 预留未过期 ② 计划涉及节点的版本与客户端
   基线一致 ③ 节点仍在线、提交后 `running ≤ capacity` ④ 标签/反亲和仍
   满足；全部通过才一次性把 `reserved` 转为 `running`。任一不通过 →
   释放**整组**预留、置 FAILED，已运行任务数恒为 0（无部分启动）。

**版本号语义**：`version` 仅在节点**结构性变化**（ONLINE↔OFFLINE）时
推进；其他组在同一节点上的预留/提交/释放属于容量记账范畴，不会让本组的
计划基线过期（各持有者的槽位在 reserve 时即已互斥记账，提交只是
reserved→running 的搬运）。

**超时不泄漏**：reaper 定时扫描，HELD 组超过 `ttl_ms` 未提交则整体释放；
WAITING 组超过自身 TTL 未获预留也置 FAILED 出队，避免不可调度队头长期
堵队列。组在 WAITING→HELD 提升时预留窗口重新起算。

**账目不变量**（测试中逐场景断言）：对任意节点恒有
`0 ≤ reserved`、`0 ≤ running`、`reserved + running ≤ capacity`，
且节点 `reserved` 之和恒等于所有 HELD 预留占用之和。

## 验收场景与实测结果

验收要求：两个组竞争交叠节点；在预留与提交之间下线节点；验证不会部分
启动、资源泄漏或重复占用。

**剧本**（`scripts/demo.sh`，2 节点 × 6 槽）：

1. 组 A 预留 4+4（占 8/12）→ `HELD`，返回 n1/n2 版本 `{1,1}`；
2. 组 B 也要 4+4 → 容量不足，`WAITING`，无任何预留；
3. A 提交前把 **n1 置 OFFLINE**（版本变为 2）；
4. A 用旧版本提交 → **409 `VERSION_MISMATCH`**，A=`FAILED`，
   `running=[]`（无部分启动），两节点 `reserved=0, running=0`（无泄漏）；
5. 再次提交同样被 409 拒绝（不可重复占用）；
6. n1 恢复 ONLINE：FIFO 队头 B 先被整组提升为 HELD，提交后两个任务同时
   RUNNING（原子）；
7. B release 后槽位归还，A replan 整组提升并提交成功；
8. 另起组 C，`ttl_ms=300` 不提交 → reaper 将其置
   `FAILED: RESERVATION_EXPIRED`，槽位自动归还。

完整请求/响应留档：[`docs/demo-output.log`](docs/demo-output.log)。

**自动化测试实测**（2026-09-24，go1.23.4，`go test -race -count=1 ./...`）：

```
ok  github.com/example/gangscheduler/internal/api       (4 tests, 含完整 HTTP 验收剧本)
ok  github.com/example/gangscheduler/internal/scheduler (11 tests, 含 -race 并发压测)
```

覆盖：整组全有或全无、标签等值/IN 选择器、distinct 反亲和、两组竞争交叠
节点、预留后下线节点导致版本冲突、TTL 过期回收与后继提升、FIFO 顺序、
release 归还、20 goroutine 并发 reserve/commit/release 的竞态与账目检查。

## 已知边界 / 未完成项（如实记录）

- **纯内存、单实例**：重启状态丢失；未做持久化与多副本高可用。
- **标签不可变**：节点注册后不支持修改标签/容量（结构性变更目前只有
  ON/OFF）；如需改标签，语义上应同样推进版本并拒绝旧计划。
- **放置策略为启发式**：DFS + 最佳适配能找到可行解但不是最优装箱；
  极大 gang（任务数成百上千、候选节点巨大）的回溯未做性能优化，当前规模
  面向演示/验收。
- **WAITING 过期语义**：排队 TTL 从最近一次入队/重新排队起算；被 TTL
  置 FAILED 的组需要客户端显式 `replan`，不自动重试。
- **无认证/TLS/限流**：仅适合本地或可信网络演示。
- demo 脚本依赖 `jq` 做输出美化；服务本身不依赖它。
