# 可抢占检查点调度模拟器（checkpoint-scheduler）

纯后端离线集群调度模拟，Go + `net/http`，仅标准库。任务带**检查点成本、剩余工作量、优先级**；
可被高优先级任务**抢占**，但恢复时**只能从已提交（保存完成）的检查点**继续——保存到一半的检查点
不存在、不可恢复；保存期间任务**仍占用资源槽位**且保存耗时计入完成时间。

## 依赖与锁定

- Go ≥ 1.23（开发验证用 go1.23.4）
- 无第三方依赖：`go.mod` 即完整依赖锁定（仅模块声明 + go 指令，无需 `go.sum`）

## 启动

```bash
go run . -addr :8080        # 直接运行
# 或
go build -o cksched . && ./cksched -addr :8080
```

## 测试

```bash
go test ./...               # 12 个测试：引擎 + HTTP 处理器
go vet ./... && gofmt -l .  # 静态检查 / 格式
```

## 调度模型

- 集群有 `slots` 个资源槽，每个运行中任务占 1 槽（**保存检查点期间照占**）。
- 任务每完成 `checkpoint_interval` 单位工作，花 `checkpoint_cost` 时间保存一次检查点
  （保存期间无工作进展，但槽位不释放）；最后一段不足一个间隔时直接跑到完成，不再保存。
- 空闲槽分配给优先级最高的等待任务（平级按到达时间、再按 id）。
- 开启抢占时：等待任务优先级高于某运行任务且槽已满 → 抢占优先级最低的运行任务。
- 被抢占任务**回滚到最近一次已提交的检查点**，未提交的工作全部丢失；
  若抢占发生在保存中途，该次保存作废（`checkpoint_aborted`），此检查点永远不可用。

## HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | 健康检查 |
| POST | `/api/simulate` | 单次模拟，Body 见下；`preempt` 缺省 `true` |
| POST | `/api/compare` | 同一场景分别跑有/无抢占，输出完成时间对比 |

请求体：

```json
{
  "slots": 1,
  "preempt": true,
  "tasks": [
    {"id": "low",  "priority": 1, "arrival": 0,  "work": 100, "checkpoint_interval": 20, "checkpoint_cost": 5},
    {"id": "high", "priority": 9, "arrival": 47, "work": 30,  "checkpoint_interval": 10, "checkpoint_cost": 2}
  ]
}
```

字段：`priority` 越大越优先；`arrival` 到达时刻；`work` 总工作量；
`checkpoint_interval` 两次检查点间的工作量；`checkpoint_cost` 每次保存耗时。时间均为抽象单位（浮点）。

响应含：`events`（事件轨迹：arrival/start/checkpoint_begin/checkpoint_commit/
checkpoint_aborted/preempted/resume/complete）、`tasks`（每任务完成时间、提交/作废检查点数、
被抢占次数、丢失工作量）、`makespan`；`/api/compare` 额外给出 `completion_delta`
（无抢占 − 有抢占，正值表示抢占让该任务更早完成）。

## 验收场景与请求样例

`examples/scenario.json`：1 个槽位；低优任务 `low`（t=0 到达，100 单位工作，每 20 单位花 5 时间保存），
高优任务 `high`（t=47 到达）——**正好落在 low 第二次保存（t=45..50）进行中**。

```bash
curl -s -X POST localhost:8080/api/compare \
  -H 'Content-Type: application/json' \
  --data @examples/scenario.json | jq '.completion_delta, .with_preemption.tasks'
```

只跑单次（可加 `"preempt": false` 关闭抢占）：

```bash
curl -s -X POST localhost:8080/api/simulate --data @examples/scenario.json | jq '.events'
```

### 实际运行结果（2026-09-24，go1.23.4，本机实测）

关键事件（有抢占）：

```
t=47  high arrival
t=47  low  checkpoint_aborted  "checkpoint save interrupted by preemption; partial checkpoint discarded and unusable"
t=47  low  preempted          "lost 20 uncommitted work units, rolled back to 20 secured units"
t=47  high start
t=81  high complete
t=81  low  resume             "resuming from 20/100 secured work units"   ← 从已提交的 20 恢复，而非保存中断处的 40
t=176 low  complete
```

完成时间对比（实测输出，与手算一致）：

| 任务 | 有抢占 | 无抢占 | delta（无−有） |
|---|---|---|---|
| high | **81** | 154 | +73（抢占受益） |
| low  | 176 | **120** | −56（被抢占代价：丢失 20 单位工作 + 重跑） |
| makespan | 176 | 154 | |

手算验证：无抢占时 low = 100 工作 + 4 次保存 × 5 = 120；有抢占时 low 在 t=47 被抢占，
第二次保存（45..50）作废，回滚到 t=25 提交的 20/100，待 high 于 t=81 完成后从 20/100 重跑，
80 工作 + 3 次保存 × 5 = 95，完成于 81+95 = 176。**保存中的检查点未被使用**——
`checkpoints_aborted=1`，恢复点为 20/100 而非 40/100。

### 自动化测试记录

`go test ./...` 实测全部通过（12 个用例，含）：

- `TestPreemptionDuringCheckpointSave` — 验收核心：保存中被抢占，断言 `checkpoint_aborted` 事件、
  作废不计入已提交、丢失 20 单位、恢复点为 20/100、完成时间 81/176
- `TestNoPreemptionBaseline` / `TestCompareCompletionTimes` — 无抢占基线 120/154 与 delta 73/−56
- `TestCheckpointOverheadCounted` — 保存耗时计入完成时间
- `TestSlotOccupiedDuringSave` — 保存期间槽位仍被占用，同优先级不抢占
- `TestMultiSlotScheduling`、`TestValidation`、HTTP 处理器测试（200/400/405）

## 未完成项 / 限制

- 单维度资源（只有槽位数），未建模 CPU/内存/GPU 异构资源与网络带宽。
- 抢占本身假设零开销（保存作废的耗时已自然计入）；未建模迁移/杀进程成本。
- 非抢占模式下高优任务只能等整任务跑完（不会在检查点边界让位），这是有意的简化。
- 无持久化与认证，进程内单次模拟，重启即清空；未做并发请求下的性能测试。
