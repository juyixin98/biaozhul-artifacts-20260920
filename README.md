# 可抢占检查点调度器（Preemptible Checkpoint Scheduler）

离线集群调度的离散时间（tick）模拟器，纯后端 HTTP 服务。任务带检查点成本、剩余工作量和优先级；高优任务可抢占低优任务，但**被抢占任务只能从已完成提交（committed）的检查点恢复**——保存尚未完成的检查点会被中止丢弃。保存检查点期间同样占用集群资源，保存时间计入总开销。

同一场景分别用「可抢占 / 不可抢占」两种策略各跑一遍，输出每个任务的完成时间对比。

## 依赖与启动

- **Go 1.23+**（开发机实测 go1.23.4 linux/amd64）
- **零第三方依赖**，仅用标准库 `net/http`、`encoding/json`；`go.mod` 已锁定 module 与 Go 版本，无需 `go mod download`
- 无配置文件、无数据库、无界面

```bash
# 编译
go build ./...

# 启动（默认监听 :8080，可用 -addr 改端口）
go run ./cmd/server -addr :8080
```

健康检查：

```bash
curl -s http://localhost:8080/healthz
# {"status":"ok"}
```

## HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 服务与字段说明 |
| GET | `/healthz` | 健康检查 |
| POST | `/simulate` | 提交场景，返回两种策略的完整模拟结果与完成时间对比 |

### 请求字段

| 字段 | 含义 |
|---|---|
| `capacity` | 集群总资源量 |
| `tasks[].id` | 唯一非空 ID |
| `tasks[].priority` | 优先级，**数值越大优先级越高**，仅可抢占严格更低优先级 |
| `tasks[].arrival_tick` | 到达时刻（tick，≥0） |
| `tasks[].total_work` | 总工作量（执行 tick 数，不含保存开销） |
| `tasks[].checkpoint_every` | 距上次已提交检查点后，每做多少新工作触发一次保存 |
| `tasks[].checkpoint_cost` | 保存一个检查点需要的 tick 数（保存期间持续占用资源） |
| `tasks[].resource_demand` | 运行和保存时占用的资源量 |

### 模拟规则

1. 每个 tick，运行中的任务执行 1 单位工作，保存中的任务花 1 tick 写检查点；两者都占资源。
2. 每做满 `checkpoint_every` 个新工作单位，进入 `checkpoint_cost` tick 的保存阶段。**保存完整结束的那一刻检查点才提交**（成为可用恢复点）。
3. **可抢占策略**：容量不足时，到达的高优任务可驱逐严格更低优先级的在运行/保存任务（同优先级不互斥；凑不齐所需容量就不抢）。
4. **不可抢占策略**：在运行/保存的任务绝不被驱逐，就绪任务按优先级排队等待。
5. 被驱逐任务：
   - 若正在保存，**该保存整体作废**，已花的保存 tick 计入 `wasted_save_ticks`；
   - 无论被打断时是在跑还是在存，上次已提交检查点之后的工作都要重做（`redundant_work`），从 `committed` 位置恢复。
6. `checkpoint_cost=0` 时检查点当 tick 立即提交。

## 验收示例（高优任务在保存中到达）

请求体见 [`examples/acceptance.json`](examples/acceptance.json)。场景：容量 2；低优任务 L（优先级 1，t=0 到达，12 工作量，每 4 单位存一次、保存成本 3 tick，占 2 资源）；高优任务 H（优先级 10，**t=6 到达**，8 工作量，保存成本 1 tick，占 2 资源）。

L 在 t=3 完成前 4 单位工作、开始保存（原计划 t=7 提交到 work=4 的检查点）。**H 在 t=6 到达——保存还差 1 tick 才完成**。

```bash
go run ./cmd/server -addr :8080 &
curl -s -X POST http://localhost:8080/simulate \
  -H 'Content-Type: application/json' \
  -d @examples/acceptance.json | python3 -m json.tool
```

实测可抢占策略时间线（2026-09-24，go1.23.4）：

```
t= 0 start                  L                          # L 开始
t= 3 save_begin             L  target=4                # 检查点保存开始（占用资源）
t= 6 arrive                 H                          # 高优任务在【保存中】到达
t= 6 preempt                L  in_flight_checkpoint_discarded: wasted_save_ticks=2 resume_from=0
t= 6 start                  H                          # 保存未完成 → 作废，不从 work=4 恢复
t=11 checkpoint_committed   H  from_cp=4
t=15 complete               H
t=15 resume                 L  from_cp=0               # 只能从已提交检查点（=0）恢复
t=22 checkpoint_committed   L  from_cp=4
t=29 checkpoint_committed   L  from_cp=8
t=33 complete               L
```

**完成时间对比（实测）：**

| 任务 | 可抢占完成 tick | 不可抢占完成 tick | Δ（不可抢占 − 可抢占） |
|---|---:|---:|---:|
| H（高优） | **15** | 27 | **+12（抢占提前 12 tick）** |
| L（低优） | 33 | 18 | −15（被抢占代价） |
| 总工期 makespan | 33 | 27 | −6 |

可抢占策略下：H 早 12 tick 完成；L 被抢占 1 次，`wasted_save_ticks=2`（作废的保存）、`redundant_work=4`（重做工作），总工期反而多 6 tick——抢占把延迟从高优任务转移给了低优任务，并因丢失未提交进度产生净开销。这与「抢占并非免费」的预期一致，结果如实列出。

完整 JSON 响应见 [`examples/acceptance-response.json`](examples/acceptance-response.json)。

### 通用 curl 样例

```bash
curl -s -X POST http://localhost:8080/simulate \
  -H 'Content-Type: application/json' \
  -d '{
    "capacity": 4,
    "tasks": [
      {"id":"A","priority":1,"arrival_tick":0,"total_work":10,"checkpoint_every":3,"checkpoint_cost":2,"resource_demand":2},
      {"id":"B","priority":8,"arrival_tick":2,"total_work":5, "checkpoint_every":5,"checkpoint_cost":1,"resource_demand":3}
    ]
  }'
```

非法输入（capacity<1、重复 id、demand>capacity、未知字段、body >1MiB 等）返回 `400 {"error": "..."}`。

## 项目结构

```
go.mod                          module 声明（无第三方依赖，锁定 go 1.23）
sim/sim.go                      离散时间模拟引擎（两种策略、检查点状态机、统计）
sim/sim_test.go                 引擎测试（含验收场景）
api/api.go                      net/http handler（GET /、GET /healthz、POST /simulate）
api/api_test.go                 HTTP 层测试
cmd/server/main.go              服务入口（-addr）
examples/acceptance.json        验收场景请求
examples/acceptance-response.json  实测响应存档
```

## 测试

```bash
go test ./...            # 全部
go test -v ./sim         # 引擎用例详情
go vet ./...             # 静态检查
gofmt -l .               # 格式检查（无输出即通过）
```

实测结果（2026-09-24，go1.23.4 linux/amd64，`go test ./...`）：

```
?   checkpoint-scheduler/cmd/server [no test files]
ok  checkpoint-scheduler/api        0.012s
ok  checkpoint-scheduler/sim        0.002s
```

共 11 个测试全部通过；`go vet` 无告警；`gofmt` 无差异。覆盖：

- **`TestAcceptancePreemptDuringSave`**（验收）：高优任务保存中到达 → t=6 抢占；t≤6 无 L 的 checkpoint 提交事件；恢复点 from_cp=0 而非 in-flight 的 4；浪费保存 2 tick、重做 4 工作；两策略完成时间 H=15 vs 27；
- 容量约束：从事件流逐 tick 重建占用，两种策略均不超过 capacity；
- 不可抢占策略 0 次抢占、严格 FIFO（按优先级排队）；
- 同优先级永不互斥；
- `checkpoint_cost=0` 当 tick 立即提交；
- 全部参数校验分支；
- 同输入结果确定性一致；
- HTTP 层：200 用例、各类 400、404、health、超大 body。

## 已建模与未完成项

已建模：

- 离散 tick 调度、优先级抢占（只抢严格更低优先级、容量凑不齐不抢）；
- 检查点保存中/已提交两阶段语义，保存占用资源、保存时间计开销；
- 抢占丢弃未完成保存、未提交进度重做、从已提交检查点恢复；
- 双策略对比（逐任务完成 tick、makespan、等待/重做/浪费保存/抢占次数统计、完整事件流）。

未做（范围之外或可后续扩展）：

- 仅纯后端，无前端界面；
- 无持久化，每次请求独立计算（确定性结果，重启无状态）；
- 未建模检查点存储容量上限、检查点恢复本身的读取时间、任务依赖/DAG、多资源维度（CPU/内存/GPU）；
- 抢占选择策略为固定启发式（最低优先级优先、同优先级最早分配优先），未实现可配置策略或最优调度搜索；
- 未做鉴权/限流/TLS（本机模拟用途，前置代理负责）。
