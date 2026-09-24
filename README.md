# drf-scheduler — 多资源主导公平分配（DRF）调度服务

纯后端 Go 服务（仅标准库 `net/http`），对 **CPU + 内存** 两种资源按
**主导资源公平（Dominant Resource Fairness, DRF）** 分配**不可拆分**任务，
支持租户**权重**与**配额**，放不下的任务保留在队列中，**绝不超卖**资源。

## 特性

- **DRF 调度**：每轮选择"加权主导份额"最小且队头任务可放置的租户，放置其队头任务，循环直到无可放置任务。
- **租户权重**：正整数权重 `w`，加权主导份额 = `主导资源占比 / w`。权重 2 的租户在同等占用下优先获得资源。
- **租户配额**：每个资源维度的绝对上限（`quota`），某维度为 0 表示该维度不限（上限为集群容量）。配额独立于集群空闲量强制生效。
- **不可拆分任务**：任务要么整体放置，要么整体排队；不抢占、不切片。
- **资源守恒**：任何时刻 `Σ(运行中任务需求) ≤ 集群容量`，且 `Σ(运行中任务需求) == 调度器记账值`（测试中以随机化场景断言）。
- **确定性平局规则**：加权主导份额完全相等时，按**租户名字典序**取小者；份额比较用 `big.Int` 精确交叉相乘，不受浮点误差影响。
- **释放后重调度**：`DELETE /tasks/{id}` 释放资源后立即重新调度，排队任务按 DRF 规则补位。
- **租户内 FIFO 队列**：每租户一条先进先出队列，队头放不下则阻塞该租户后续任务（避免插队语义歧义）；其他租户不受影响。

## 依赖与锁定

- **Go ≥ 1.21**（开发验证环境：Go 1.21+，linux/amd64）。
- **零第三方依赖**：只用标准库（`net/http`、`encoding/json`、`math/big`、`sync` 等）。
  `go.mod` 中没有任何 `require`，因此无需 `go.sum`——依赖即被"锁定"为标准库本身。
  可用 `go build -mod=readonly ./...` 验证（本仓库实测通过）。

## 目录结构

```
cmd/server/main.go              # 入口：HTTP 服务（-addr / 环境变量 ADDR，默认 :8080）
internal/scheduler/scheduler.go # DRF 调度核心（并发安全，互斥锁保护）
internal/api/server.go          # HTTP JSON API（路由、校验、错误码映射）
internal/scheduler/scheduler_test.go  # 调度语义测试
internal/api/server_test.go           # HTTP 端到端与错误路径测试
examples/demo.sh                # 验收场景演示脚本
examples/demo-output.txt        # 演示脚本的实测输出
```

## 快速开始

```bash
# 构建
go build -o bin/drf-server ./cmd/server

# 启动（默认 :8080；可用 -addr 或 ADDR 环境变量覆盖）
./bin/drf-server -addr :8080

# 运行测试（含竞态检测）
go test -race ./...

# 运行验收演示（服务需已启动）
./examples/demo.sh http://127.0.0.1:8080
```

## HTTP API

所有请求/响应均为 JSON；错误响应为 `{"error": "..."}`。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 |
| POST | `/config` | 设置集群容量 `{"cpu":10,"mem":10}`；存在运行中任务时拒绝（409） |
| POST | `/reset` | 清空全部状态（测试/演示用） |
| POST | `/tenants` | 创建租户 `{"name":"A","weight":1,"quota":{"cpu":3,"mem":0}}`；`weight` 缺省为 1，`quota` 某维为 0 表示不限 |
| GET | `/tenants/{name}` | 查看租户状态（用量、主导份额、队列） |
| POST | `/tasks` | 提交任务 `{"id":"a1","tenant":"A","demand":{"cpu":4,"mem":0}}`；返回 `state: running/queued` |
| GET | `/tasks/{id}` | 查看任务状态 |
| DELETE | `/tasks/{id}` | 释放任务并立即重调度 |
| POST | `/schedule` | 手动触发一次调度（提交/释放时已自动触发） |
| GET | `/state` | 全量状态快照（容量、用量、运行/排队任务、各租户份额） |

错误码：`400` 非法请求（需求超容量/超配额、权重非正、JSON 非法）、
`404` 租户或任务不存在、`405` 方法不允许、`409` 冲突（重复 ID、未配置容量、有运行任务时改容量）。

### 请求样例（验收场景）

```bash
B=http://127.0.0.1:8080
curl -X POST $B/reset
curl -X POST $B/config -d '{"cpu":10,"mem":10}'
curl -X POST $B/tenants -d '{"name":"A","weight":1}'          # CPU 密集租户
curl -X POST $B/tenants -d '{"name":"B","weight":1}'          # 内存密集租户
curl -X POST $B/tasks -d '{"id":"a1","tenant":"A","demand":{"cpu":4,"mem":0}}'
curl -X POST $B/tasks -d '{"id":"b1","tenant":"B","demand":{"cpu":0,"mem":4}}'
curl -X POST $B/tasks -d '{"id":"a2","tenant":"A","demand":{"cpu":4,"mem":0}}'
curl -X POST $B/tasks -d '{"id":"b2","tenant":"B","demand":{"cpu":0,"mem":4}}'
curl -X POST $B/tasks -d '{"id":"a3","tenant":"A","demand":{"cpu":4,"mem":0}}'   # -> queued
curl -X POST $B/tasks -d '{"id":"b3","tenant":"B","demand":{"cpu":0,"mem":4}}'   # -> queued
curl $B/state                                                  # 守恒检查：used 8/8 ≤ 10/10
curl -X DELETE $B/tasks/a1                                     # a3 补位运行
curl -X DELETE $B/tasks/b1                                     # b3 补位运行
curl $B/state                                                  # 全部 6 个任务中 4 个在运行，无超卖
```

完整可运行脚本见 `examples/demo.sh`，实测输出见 `examples/demo-output.txt`。

## 调度语义细节

- **主导份额**：租户的主导份额 = `max(已用CPU/总CPU, 已用内存/总内存)`；
  加权主导份额 = `主导份额 / 权重`。每轮调度在"队头任务可放置"的租户中选加权主导份额最小者。
- **平局**：份额比较用 `big.Int` 精确交叉相乘（`na·db·wb < nb·da·wa`），完全相等时按租户名字典序。
  例如两租户各占 4/12 且权重相同、只剩一个槽位时，名字较小的租户获得该槽位（`TestDeterministicTieBreak` 重复 5 次验证）。
- **放置条件**（三者同时满足）：集群剩余资源足够、租户配额余量足够、任务需求本身不超过容量与配额
  （后两者在提交时校验，永远放不下的任务直接 400 拒绝，不占队列）。
- **守恒**：调度器只在"放得下"时才扣减资源；释放即归还并触发重调度。
  `TestConservationInvariant` 用确定性伪随机序列提交 60 个任务，断言
  `used ≤ capacity`、`Σ运行任务需求 == used`、`Σ各租户用量 == used`，且调度达到不动点（无任何队头可放置）。

## 离散任务的公平偏差（为什么 DRF 在不可拆分任务下会"不公平"）

DRF 的经典公平性结论（无嫉妒、份额保证）建立在**资源可无限细分**的假设上。
本系统中任务不可拆分，会出现系统性偏差：

1. **量化误差**：资源只能以任务大小为粒度分配。验收场景中 A、B 各得 8/10（主导份额 0.8），
   理论公平点是各 1.0，但下一个 4 单位任务放不进剩余的 2/2——**每个租户最多偏差一个任务**，
   这是离散 DRF 的已知上界（Ghodsi et al., NSDI'11 对 indivisible tasks 的分析）。
2. **碎片**：`TestIndivisibilityFragmentation` 展示 6/6 集群跑一个 4×4 任务后，
   剩余 2/2 对任何 4 单位任务都不可用——资源空闲却无法分配。
3. **顺序敏感**：先到任务先占位，同样的任务集合不同提交顺序可能得到不同的最终分配；
   本实现通过"每轮重算份额 + 确定性平局"保证**同一事件序列必然产生同一结果**（可重放），
   但不承诺与"理想连续 DRF"的分配一致。
4. **队头阻塞**：租户内 FIFO 意味着大队头会挡住后面的小任务（即使小任务放得下），
   这是用可预测性换取的语义简化。

## 实测结果（本仓库交付时实际运行）

- `go vet ./...`：通过。
- `go test -race -count=20 ./...`：全部通过（确定性验证：20 次重复无差异）。
- 覆盖率：`internal/scheduler` 91.3%，`internal/api` 79.0%。
- `go build -mod=readonly ./...`：通过（零外部依赖）。
- `examples/demo.sh` 对运行中的服务实测通过，输出存档于 `examples/demo-output.txt`：
  - 资源守恒：任意时刻 `used ≤ capacity`（峰值 8/8，未触及 10/10 上限，无超卖）；
  - 平局规则：A/B 主导份额均为 0.8 时按字典序交替放置（a1,b1,a2,b2）；
  - 释放重调度：释放 a1 后 a3 立即补位，释放 b1 后 b3 补位；
  - 权重场景（12/12 集群，hi 权重 2 / lo 权重 1）：释放一个槽位后 hi3 优先于 lo2 补位，
    最终两者加权主导份额均为 0.333；
  - 配额场景（q 配额 cpu=3）：q2 被配额拦在队列（集群有空闲也不放行），
    超配额需求 q3 直接 400 拒绝。

## 已知限制 / 未完成项

- **单进程内存态**：无持久化，重启即丢失（提供 `/reset` 便于测试）。
- **无认证鉴权**：任何能访问端口的客户端都可操作，仅适合内网/测试环境。
- **不支持任务取消排队任务**：`DELETE /tasks/{id}` 只接受运行中任务的释放；
  排队任务无法单独取消（可 `/reset` 全清）。
- **容量不可在线变更**：有运行中任务时 `POST /config` 返回 409，需先释放或 `/reset`。
- **无抢占与回填优化**：严格租户内 FIFO，不做 backfill（小任务不能越过大队头）。
- **权重仅接受正整数**：不支持小数权重（保证 `big.Int` 精确比较）。
