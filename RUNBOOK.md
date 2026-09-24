# RUNBOOK — 实际运行记录

本文件如实记录本项目在交付环境中的实际构建、测试与端到端运行结果，
以及过程中发现并修复的问题和明确的未完成项。

- 环境：Linux 6.8.0-90-generic (x86_64)，Go `go1.23.4 linux/amd64`
- 依赖：**0 个第三方 module**（仅标准库），无 `go.sum`，构建不联网
- 记录时间：2026-09-24

---

## 1. 构建 / 静态检查 / 单元测试（实测）

命令：

```bash
go build ./...
gofmt -l .        # 最终为空（全部已格式化）
go vet ./...      # 通过
go test ./... -race -count=1 -cover
```

结果（最终一次运行）：

```
?   dagexec/cmd/dagexec   [no test files]
ok  dagexec/internal/api        coverage: 71.6%
ok  dagexec/internal/dag        coverage: 80.0%
ok  dagexec/internal/scheduler  coverage: 84.4%
ok  dagexec/internal/store      coverage: 69.6%
```

- `go vet ./...`：clean。
- 竞态：全部测试在 `-race` 下通过；调度器/API 测试还以 `-count=5`、`-count=10`
  重复运行以排查时序 flaky，均通过。
- `cmd/dagexec` 覆盖率为 0：该包是 flag/HTTP 装配薄层，未单测；其行为由
  端到端黑盒脚本覆盖。

测试用例数（按 `go test -v` 计，约 30 个测试函数），关键覆盖：

- 菱形 + 中间永久失败 + **重启后成功节点 total_runs 不变**；
- 取消后下游节点 `total_runs=0`；取消态跨重启；
- **硬崩溃恢复**：任务执行中 SIGKILL，重启后崩溃节点恰好重跑一次、成功上游不重跑；
- 有限重试耗尽 / 退避后成功 / 结果缓存；
- 环、自环、缺失依赖、非白名单、重复 id/依赖、空图；
- 每 DAG 并发上限；Cancel 与 Retry 20 轮并发竞争（`-race`）；
- API 状态码 201/202/400/404/405 与校验 `details`。

---

## 2. 端到端黑盒验收（实测，44 项断言）

使用 `examples/accept_e2e.py`，对真实 HTTP 服务（非 mock）执行。

### 重启前（`pre`）

```
SUMMARY: 28 passed, 0 failed
```

要点（真实观测值）：

- 菱形 `A->{B(fail,2次),C(add)}->E(collect)`：
  `A=success runs=1 result=7`；`B=failed runs=2`；
  `C=success runs=1 result=7`（复用 A 的缓存结果）；`E=blocked runs=0`；`fail_node=B`。
- 取消链 `A(sleep)->B->C`：取消后 `A=cancelled runs=1`，
  `B=cancelled runs=0`，`C=cancelled runs=0`。
- 环 → 400（`dependency cycle detected involving nodes: ...`）；
  缺失依赖 + 非白名单 → 400，`details` 同时报告两类问题；未知 DAG → 404。
- flaky 菱形（B 前 2 次失败、第 3 次成功）：`B runs=3 result=32`，
  `E result=42`，端到端耗时 **0.62s**，与配置退避 200ms+400ms 吻合。

### 重启（同一 `-state` 文件，进程退出后重新启动）

### 重启后（`post`）

```
SUMMARY: 16 passed, 0 failed
```

要点：

- 菱形 DAG 仍为 `failed`；`A/C` 仍为 `success` 且 `total_runs` 仍为 **1**（未重跑），
  缓存结果 `7` 保留；`B` 仍 `failed runs=2`；`E` 仍 `blocked runs=0`。
- 已 `cancelled` 的 DAG 重启后仍为 `cancelled`，B/C 仍未执行。
- 对失败菱形 `POST /retry`：`A/C` 的 `total_runs` 仍为 **1**（成功节点不重跑），
  `B` 在新激活期再执行 2 次，生命周期 `total_runs=4`。
- 重启后新提交的 flaky DAG 正常成功，证明调度功能在恢复后仍然可用。

### 额外硬崩溃场景（SIGKILL，实测）

构造 `A(identity=100) -> S(sleep 60s) -> B(add)`，在 S 执行中对进程发 `SIGKILL`：

- 崩溃瞬间磁盘状态：dag=`running`，`A=success runs=1`，`S=running runs=1`，`B=pending`。
- 重启后立即查询：`A=success runs=1`（不重跑），`S=running runs=2`（恰好重跑一次），
  `B=pending runs=0`。随后取消该 DAG 释放长睡眠，终态符合预期。

---

## 3. 开发过程中发现并修复的问题（如实记录）

这些问题在最初实现中存在，均在交付前通过测试/压测发现并修复，并有回归测试：

1. **Retry 与 runner 退出的竞态**：最初 runner 到终态即退出并从注册表删除，
   恰在该窗口到达的 Retry 可能丢失唤醒导致 DAG 卡死。改为 runner **常驻挂起**、
   终态时等待 wake/cancel，并为每个 DAG 增加操作串行锁。回归：
   `TestConcurrentCancelAndRetry`（20 轮 + `-race`）。
2. **失败传播的可见性窗口**：节点耗尽重试（写 failed）与下游标记 blocked
   原本是两个持久化事务，外部可能在中间观察到“failed DAG 但下游仍 pending”。
   合并为**同一事务**完成失败扇出。
3. **已取消 DAG 被恢复逻辑复活**：进程在含遗留 `running` 节点时重启，
   `recover()`/`tick()` 的兜底分支会把 `cancelled` DAG 错误改回 `running`。
   修复为终态 DAG 恢复时保持终态。回归：
   `TestRecoverDoesNotResurrectCancelledDag`。该问题正是在端到端“重启后”
   阶段被真实抓到（1 个断言先失败、修复后通过），故此处如实保留记录。
4. 取消通道在 Retry 重新激活后的复用问题：改为可替换 channel + 加锁；
   在跑任务的 cancel 注册表改为带 token 的包装，避免跨激活误删。

---

## 4. 复现命令（最小步骤）

```bash
# 构建
go build -o bin/dagexec ./cmd/dagexec

# 单元测试
go test ./... -race -count=1

# 端到端（两个终端）
./bin/dagexec -addr :18093 -state /tmp/dagfinal/state.json -retry-base-delay 200ms
python3 examples/accept_e2e.py pre  http://localhost:18093 /tmp/dagfinal
# 停掉服务，再用同一 -state 启动
python3 examples/accept_e2e.py post http://localhost:18093 /tmp/dagfinal
```

样例请求体见 `examples/`：`diamond_fail.json`、`diamond_flaky.json`、`cancel_demo.json`。

---

## 5. 未完成项 / 限制（不夸大）

- 仅支持**单进程**写同一状态文件；没有多实例锁或选主。
- 崩溃在跑的任务按“至少一次”重放（成功结果恰好生效一次）。因任务仅限纯函数
  白名单，重放安全；未提供面向非幂等外部任务的幂等键机制。
- 无鉴权、无限流、无多租户；默认监听全网卡。
- 无死信队列 / 调度优先级 / cron 触发 / 退避抖动。
- 无界面（按需求只做后端）。
- `cmd/dagexec` 装配层未做独立单元测试（由黑盒端到端覆盖）。
