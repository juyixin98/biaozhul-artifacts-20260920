# 拓扑感知 GPU 任务分配服务（Topology-aware GPU Placement）

纯后端 HTTP 服务：给定集群拓扑（每台设备的**显存**、所属 **NUMA 节点**、设备间**互联代价**）和一个 GPU 任务（副本数、每副本显存需求），先满足硬约束，再在所有可行方案中**最小化任务组内通信成本**。不调用任何真实 GPU，纯内存计算。

仅使用 Go 标准库（`net/http`、`encoding/json`），**零第三方依赖**。

---

## 1. 依赖

| 依赖 | 版本 | 说明 |
| --- | --- | --- |
| Go | ≥ 1.23（实测 1.23.4 / linux-amd64） | 唯一依赖 |
| 第三方库 | 无 | `go.mod` 中无 require 项；无 `go.sum` |

依赖锁定方式：`go.mod` 中 `go 1.23` 钉选工具链版本；由于不引用任何外部模块，不存在可漂移的传递依赖。

## 2. 构建与启动

```bash
# 直接运行（会先编译到 bin/）
make run ADDR=:8080

# 或手动
go build -o bin/placementd ./cmd/placementd
./bin/placementd -addr :8080
```

监听地址也可用环境变量指定（`PORT` 为裸端口号）：

```bash
PORT=9090 ./bin/placementd        # 等价于 -addr :9090
```

收到 SIGINT/SIGTERM 时优雅关闭（10s 超时）。

## 3. HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/` | 服务信息与路由列表 |
| `GET` | `/healthz` | 健康检查，返回 `{"status":"ok"}` |
| `POST` | `/api/v1/placement` | 计算一次分配 |

- 请求/响应均为 JSON；请求体上限 1 MiB。
- 未知字段会被拒绝（`DisallowUnknownFields`）。
- **HTTP 状态码语义**：`200` 表示“调度器成功回答了问题”——无论分配可行还是被拒绝，结论放在响应体 `status` 中；`400` 表示请求本身非法（JSON 错误、字段校验失败）；`405` 表示方法不对。

### 请求格式

```jsonc
{
  "cluster": {
    "devices": [
      // id：设备唯一 ID
      // memory_mb：总显存（MB）；used_memory_mb：已占用显存
      // numa_node：所属 NUMA 节点编号（>=0）
      { "id": "gpu0", "memory_mb": 80000, "used_memory_mb": 0, "numa_node": 0 }
    ],
    "links": [
      // 显式互联代价，无向；缺省的设备对按 NUMA 默认代价补全
      { "a": "gpu0", "b": "gpu1", "cost": 1 }
    ],
    "default_link_cost": {
      "same_numa_cost": 1,    // 缺省值 1
      "cross_numa_cost": 10   // 缺省值 10
    }
  },
  "task": {
    "name": "llm-training",
    "replicas": 4,                    // 必填，>=1
    "memory_per_replica_mb": 20000,   // 必填，>=0；副本不可拆分到多卡
    "max_search_states": 0            // 可选，>0 时覆盖分支限界节点预算（默认 2,000,000）
  }
}
```

### 响应：成功（`status: "optimal"`）

```jsonc
{
  "status": "optimal",
  "task_name": "llm-training",
  "replicas": 4,
  "memory_per_replica_mb": 20000,
  "placement": [
    { "replica_index": 0, "device_id": "gpu0", "numa_node": 0 }
    // ... 每副本一台设备
  ],
  "total_communication_cost": 8,   // 组内所有设备对代价之和
  "cross_numa_pair_count": 0,
  "pairs": [
    { "from": "gpu0", "to": "gpu1", "cost": 1, "cross_numa": false }
    // ... 每对设备的代价，便于人工核验
  ],
  "diagnostics": {
    "strategy": "exhaustive-enumeration", // 或 branch-and-bound / trivial-single-device
    "states_explored": 35,
    "combinations_total": 35,
    "free_memory": [ { "device_id": "gpu0", "numa_node": 0, "free_mb": 80000, "eligible": true } ]
  }
}
```

`status` 取值：

| status | 含义 |
| --- | --- |
| `optimal` | 找到可行分配，并**证明**通信成本最小 |
| `best_effort` | 找到可行分配（硬约束全部满足），但搜索预算耗尽、未证明最优；`reason = SEARCH_BUDGET_EXCEEDED` |
| `infeasible` | 无可行解，附机器可读 `reason` 和逐设备空闲显存明细 |

### 响应：拒绝（`status: "infeasible"`）

```jsonc
{
  "status": "infeasible",
  "reason": "MEMORY_TOO_LARGE",   // 或 INSUFFICIENT_ELIGIBLE_DEVICES
  "message": "no device has 25000 MB free memory; largest contiguous free block is 20000 MB ...",
  "rejected": {
    "required_replicas": 2,
    "eligible_devices": 0,        // 满足单副本显存的设备数
    "total_devices": 4,
    "required_free_mb": 25000,
    "largest_free_mb": 20000,
    "free_memory": [ /* 每台设备空闲多少、是否合格 */ ]
  },
  "diagnostics": { "free_memory": [ /* 同上 */ ] }
}
```

拒绝原因码：

| reason | 触发条件 |
| --- | --- |
| `MEMORY_TOO_LARGE` | 没有任何一台设备的空闲显存放得下**一个**副本（显存碎片 / 超大请求，即使所有卡空闲总量够也会拒绝——副本不可拆分） |
| `INSUFFICIENT_ELIGIBLE_DEVICES` | 至少有一台设备放得下，但合格设备数 < 副本数 |

## 4. 请求样例（curl）

```bash
# 成功：4 副本收敛到同一 NUMA 节点，总成本 8
curl -s -X POST http://localhost:8080/api/v1/placement \
  -H 'Content-Type: application/json' \
  -d @examples/optimal_same_numa.json

# 拒绝：显存碎片（单卡最大空闲 20GB < 需求 25GB）
curl -s -X POST http://localhost:8080/api/v1/placement \
  -H 'Content-Type: application/json' \
  -d @examples/reject_fragmentation.json

# 拒绝：合格设备不足（4 副本，只有 2 台设备有 20GB 空闲）
curl -s -X POST http://localhost:8080/api/v1/placement \
  -H 'Content-Type: application/json' \
  -d @examples/reject_insufficient.json
```

样例文件：

- [`examples/optimal_same_numa.json`](examples/optimal_same_numa.json)
- [`examples/reject_fragmentation.json`](examples/reject_fragmentation.json)
- [`examples/reject_insufficient.json`](examples/reject_insufficient.json)

## 5. 优化模型

**硬约束（必须全部满足）**

1. 每个副本恰好放置在一台设备上，一台设备至多承载本任务一个副本；
2. `memory_per_replica_mb ≤ memory_mb − used_memory_mb`，不允许超分，不允许把一个副本拆到多卡；
3. 合格设备数 ≥ 副本数。

**目标函数（硬约束之上最小化）**

$$
\text{cost}(S)=\sum_{0\le i<j<k} w(S_i,S_j)
$$

即分配组内所有设备对的互联代价之和（all-reduce / all-to-all 组通信量与设备对代价的自然模型）。设备对代价优先取显式 `links`，未声明的设备对按“同/跨 NUMA”取默认代价，因此跨 NUMA 会天然受惩罚。等成本时按设备 ID 字典序打破平局，保证结果确定、可复现。

**求解算法**

- `k=1`：平凡解（无组内通信）。
- 小集群：当组合数 C(n,k) ≤ 500,000 时**穷举全部组合**，枚举顺序与代价累加保证字典序平局规则，结果即全局最优，作为验收基准。
- 大集群：深度优先**分支限界**。贪心构造初始上界；每个部分解用“剩余待选行中 r 个最小入边代价之和”作为可接受下界（测试中专门验证了该下界绝不高估，见 `TestLBNeverOverestimates`）。预算（默认 200 万节点，可用 `max_search_states` 覆盖）耗尽时返回可行但未证明最优的 `best_effort`。

## 6. 测试与验收

```bash
make test        # go test ./...
make test-race   # 带竞态检测
make cover       # 覆盖率报告
make vet         # 静态检查
```

### 实测结果（本仓库实际运行）

环境：Go 1.23.4，linux/amd64。

```
$ go test -race -cover ./...
ok  topology-aware-gpu-scheduler/internal/api        coverage: 65.0% of statements
ok  topology-aware-gpu-scheduler/internal/solver     coverage: 96.9% of statements
ok  topology-aware-gpu-scheduler/internal/topology   coverage: 98.5% of statements
```

验收点与对应测试：

- **小集群穷举校验最优解**：`TestOptimalPrefersSingleNuma` 等用例除调用生产穷举器外，还用一份**独立编写的暴力枚举**（`referenceSolve`，不共享生产代码）交叉比对成本与所选设备；`TestBranchAndBoundMatchesExhaustive` 在 200 个随机拓扑（6–14 设备、随机碎片显存、随机全连接代价矩阵）上三方对齐：生产穷举 = 分支限界 = 独立参考。
- **显存碎片**：`TestMemoryFragmentation`——4 张卡空闲分别 10/20/15/12GB，总量足够 2×25GB 但没有单卡放得下，返回 `MEMORY_TOO_LARGE`，并报告最大连续空闲块 20GB。
- **跨 NUMA 惩罚**：`TestCrossNumaPenaltyBreaksTie`（同 NUMA 1 vs 跨 NUMA 10，选同节点对）、`TestExplicitLinksOverrideNumaDefaults`（显式链路可覆盖 NUMA 默认值，且跨节点标志仍正确）。
- **无解任务**：`TestMemoryFragmentation` 与 `TestInsufficientEligibleDevices`，返回结构化拒绝原因和逐设备空闲显存。
- **预算保护**：`TestBudgetExhaustionReturnsBestEffort` 验证大实例低预算下返回可行 `best_effort`，且成本不差于高预算求得的证明最优。

端到端实测（真实起服务 + curl，8 卡拓扑见 `examples/optimal_same_numa.json`）：

| 场景 | 结果 |
| --- | --- |
| 4 副本，7 台合格设备 | `optimal`，穷举 35 = C(7,4) 组合，成本 8，全部落在 NUMA 0 |
| 碎片拒绝 | HTTP 200 + `infeasible/MEMORY_TOO_LARGE`，最大空闲 20000 MB |
| 合格设备不足 | HTTP 200 + `infeasible/INSUFFICIENT_ELIGIBLE_DEVICES`，合格 2/5 |
| 非法 JSON / 缺字段 / 未知字段 / 错方法 | HTTP 400/405，错误信息聚合所有字段问题 |
| 40 设备、8 副本（C(40,8)=76,904,685） | 默认预算：0.40s 返回 `best_effort`，成本 662；提高预算：1.35s、7.21M 节点证明 `optimal`，成本同为 662 |

## 7. 项目结构

```
.
├── cmd/placementd/main.go          # HTTP 服务入口（net/http，优雅关闭）
├── internal/
│   ├── topology/topology.go        # 设备/链路模型、代价矩阵构建与校验
│   ├── solver/solver.go            # 请求/结果模型、硬约束、结果装配
│   ├── solver/algorithms.go        # 穷举、分支限界、贪心初始界、组合数
│   └── api/handler.go              # HTTP handler、DTO、请求校验
├── examples/                       # 三个可直接 curl 的请求样例
├── Makefile
└── go.mod                          # 无任何外部依赖
```

## 8. 简化假设与边界（未做项）

- **单次单任务、无状态**：每次请求独立计算；服务不持久化分配结果，也不做多任务之间的显存预留/装箱（bin-packing）。`used_memory_mb` 由调用方在请求中提供，代表其它工作负载的占用。
- **对称代价、单卡一副本**：互联矩阵无向；不支持同一任务多副本共享一卡、副本拆分或多副本协作（MIG/MPS 未建模）。
- **目标仅限组内通信**：不考虑跨任务干扰、带宽争用、PCIe 交换层级（只有“设备对代价 + NUMA 默认值”两层拓扑），也不考虑 CPU/网卡亲和。
- **规模上限是尽力而为**：分支限界在随机代价矩阵（最坏情形，剪枝效果差）上对 n=40、k=8 约需 720 万节点证明最优；再大的实例可能只能拿 `best_effort`。这是有意的预算保护，避免请求长时间占用服务；需要更强可扩展性时可引入更紧的下界（如 SDP/谱松弛）或 MILP 求解器，本次未实现。
- 无鉴权、TLS、限流、指标导出与 OpenAPI 文档；本项目范围为纯后端算法服务。
