# GPU 拓扑感知任务放置服务（gpu-placement）

纯后端 HTTP 服务：给定集群的**设备显存、NUMA 归属与设备间互联代价**，
为 GPU 多卡任务做放置决策——**先满足硬约束，再最小化组内通信成本**。
不调用任何真实 GPU，全部状态在内存中维护。

- 仅使用 Go 标准库（`net/http`、`encoding/json` 等），**零第三方依赖**
- 小规模输入**穷举所有可行放置**，保证全局最优；规模超阈值自动切换启发式并显式标注
- 每次分配返回可核验的结果（设备、代价、同/跨 NUMA 对数、评估的候选数）；
  每次拒绝返回结构化原因与容量快照

## 依赖与启动

依赖：Go ≥ 1.23（开发验证版本 go1.23.4 linux/amd64）。无第三方模块，
`go.mod` 即完整依赖清单，无需 `go.sum`，构建不需要网络。

```bash
go build -o gpu-placement .   # 构建
./gpu-placement               # 启动，默认监听 :8080
GPU_PLACEMENT_ADDR=127.0.0.1:9090 ./gpu-placement   # 自定义监听地址
```

运行测试：

```bash
go test ./...          # 单元 + 端到端测试
go test -race ./...    # 含竞态检测
```

一键演示（构建、启动、跑完全部场景后自动关闭）：

```bash
./examples/run_demo.sh
```

## 调度模型

**硬约束**（全部满足才进入优化阶段）：

1. 每个副本独占一张卡（同一任务的两个副本不共卡）；
2. 该卡**剩余显存 ≥ `memoryPerReplicaMB`**。设备可被多个不同任务共享显存，
   已用显存按任务累加、释放时回滚。

**目标函数**：在所有满足硬约束的 `replicas` 元设备子集中，最小化
组内通信代价 `cost = Σ cost(u,v)`（被分配设备两两之间的链路代价之和，
即全归约通信的简化模型，每对设备权重相等）。

**代价矩阵**：未显式声明的设备对按 NUMA 归属取默认值
（同 NUMA `defaultSameNUMACost`，跨 NUMA `defaultCrossNUMACost`，
内置默认 1 / 10）；`links` 可逐对覆盖（如 NVLink 直连设为 0），链路双向对称。

**求解器**：

- 候选数 `C(可放置设备数, replicas) ≤ 200000` 时**穷举**，结果 `exhaustive: true`，全局最优；
- 超过阈值时用 medoid 贪心构造 + 1-swap 局部搜索，`exhaustive: false`；
- 并列裁决完全确定性：通信代价升序 → 跨 NUMA 对数升序 → 设备 id 字典序升序，
  同一输入序列在任何一次运行中产出完全一致的结果。

**拒绝原因**（按此优先级判定，附容量快照便于复核）：

| reason | 含义 |
|---|---|
| `NO_CLUSTER` | 尚未配置集群（HTTP 409） |
| `INVALID_TASK` | 参数非法：空 taskId、replicas/显存 ≤ 0（HTTP 400） |
| `DUPLICATE_TASK` | 同名任务已存在（HTTP 422） |
| `NOT_ENOUGH_DEVICES` | 集群设备总数 < replicas（HTTP 422） |
| `INSUFFICIENT_MEMORY` | 全集群剩余显存总和 < 需求总量（HTTP 422） |
| `FRAGMENTED_MEMORY` | 总量够、卡数够，但单卡剩余显存够装一个副本的设备不足（HTTP 422） |

## HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | 健康检查 |
| PUT | `/api/cluster` | 配置/重配集群拓扑（有在运行任务时拒绝） |
| GET | `/api/cluster` | 集群状态：每卡显存账目 + 全量两两代价矩阵 |
| POST | `/api/tasks` | 申请放置；201 返回分配，4xx/422 返回拒绝原因 |
| GET | `/api/tasks` | 全部已放置任务 |
| GET | `/api/tasks/{taskID}` | 单个任务详情 |
| DELETE | `/api/tasks/{taskID}` | 释放任务，回收显存 |

### 请求样例

配置集群（完整样例见 `examples/cluster.json`）：

```bash
curl -X PUT localhost:8080/api/cluster -d '{
  "devices": [
    {"id":"gpu0","memoryMB":24576,"numaNode":0},
    {"id":"gpu1","memoryMB":24576,"numaNode":0},
    {"id":"gpu2","memoryMB":24576,"numaNode":1},
    {"id":"gpu3","memoryMB":24576,"numaNode":1}
  ],
  "links": [{"a":"gpu0","b":"gpu1","cost":0}],
  "defaultSameNUMACost": 1,
  "defaultCrossNUMACost": 10
}'
```

申请 2 卡任务：

```bash
curl -X POST localhost:8080/api/tasks \
  -d '{"taskId":"train-1","replicas":2,"memoryPerReplicaMB":8192}'
```

成功响应（201，可直接核验）：

```json
{
  "taskId": "train-1",
  "replicas": 2,
  "assignments": [
    {"deviceId":"gpu0","numaNode":0,"allocatedMB":8192,"usedMemoryMB":8192,"freeMemoryMB":16384,"totalMemoryMB":24576},
    {"deviceId":"gpu1","numaNode":0,"allocatedMB":8192,"usedMemoryMB":8192,"freeMemoryMB":16384,"totalMemoryMB":24576}
  ],
  "cost": 0,
  "sameNUMAPairs": 1,
  "crossNUMAPairs": 0,
  "exhaustive": true,
  "candidatesEvaluated": 6
}
```

拒绝响应（422，含可复核的容量快照）：

```json
{
  "taskId": "train-frag",
  "reason": "FRAGMENTED_MEMORY",
  "detail": "总量充足但显存碎片化：仅 1 台设备单卡剩余 >= 16384MB，任务需要 2 台",
  "context": {
    "requestedReplicas": 2,
    "memoryPerReplicaMB": 16384,
    "requiredTotalMemoryMB": 32768,
    "totalDevices": 2,
    "eligibleDevices": 1,
    "totalFreeMemoryMB": 36864,
    "eligibleFreeMemoryMB": 24576
  }
}
```

## 测试与验收

测试覆盖（`go test ./...`，共 3 个包）：

- **穷举最优性交叉验证**：测试内用独立的位掩码枚举器（与生产代码不同实现）
  逐一比对——固定 6 卡集群的全部 k=1..6，以及 60 个固定种子随机拓扑
  （2–7 卡、随机 NUMA/显存/显式链路）的全部 k，断言代价、跨 NUMA 对数、
  设备集合三者完全一致；
- **跨 NUMA 惩罚**：2 NUMA × 2 卡集群，2 卡任务必落同 NUMA；
  显式配置跨 NUMA 的 0 代价 NVLink 后，最优解必须翻转到该链路；
- **显存碎片**：2×24GB 集群，先占 12GB，再申请 2×16GB
  （总剩余 36GB ≥ 32GB）必须返回 `FRAGMENTED_MEMORY` 且快照数字正确，
  释放后同一请求必须成功；
- **无解任务**：卡数超限 → `NOT_ENOUGH_DEVICES`，总量不足 → `INSUFFICIENT_MEMORY`，
  非法参数 → `INVALID_TASK`，重名 → `DUPLICATE_TASK`；
- **启发式 sanity**：强制走启发式路径时，其代价从不低于穷举最优
  （实测 60 个随机拓扑共 296 个子问题全部达到最优）；
- **HTTP 端到端**：`httptest` 覆盖全部接口、状态码与错误路径
  （非法 JSON、未知字段、拓扑校验失败、404 等）；
- **并发安全**：`go test -race ./...` 通过。

### 实测结果（2026-09-24，go1.23.4 linux/amd64）

```
$ go test ./...
ok  github.com/example/gpu-placement                  0.009s
ok  github.com/example/gpu-placement/internal/scheduler  0.006s
ok  github.com/example/gpu-placement/internal/topology   0.002s

$ go test -race -count=1 ./...
ok  github.com/example/gpu-placement                  1.034s
ok  github.com/example/gpu-placement/internal/scheduler  1.029s
ok  github.com/example/gpu-placement/internal/topology   1.015s
```

`./examples/run_demo.sh` 实际运行通过，关键输出：

- 2 卡任务 → `gpu0+gpu1`（NVLink 直连，`cost=0`，`exhaustive=true`）
- 连续三个 4 卡任务 → 前两个整体落 NUMA0（显存共享），第三个因
  NUMA0 占满整体落 NUMA1，均 `crossNUMAPairs=0`
- 9 卡申请 → `NOT_ENOUGH_DEVICES`；8×24GB → `INSUFFICIENT_MEMORY`
- 碎片场景 → `FRAGMENTED_MEMORY`，释放后同请求成功

## 项目结构

```
main.go                        入口：监听地址、优雅退出
server.go                      HTTP 路由与编解码（仅标准库）
server_test.go                 HTTP 端到端测试
internal/topology/             拓扑模型、输入校验、代价矩阵
internal/scheduler/            硬约束过滤、穷举/启发式求解器、有状态分配
examples/cluster.json          8 卡 / 2 NUMA / 4 对 NVLink 示例集群
examples/task-*.json           任务请求样例
examples/run_demo.sh           一键端到端演示
```

## 已知限制 / 未完成项

- 状态只在内存中，重启即丢失（未做持久化）；
- 目标函数是等权重的两两代价之和，未建模通信量差异、链路带宽共享/
  拥塞，也未考虑任务间干扰；
- 启发式路径（候选数 > 20 万）不保证全局最优，仅在响应中如实标注
  `exhaustive: false`；
- 单副本独占一张卡，不支持一个副本跨多卡切分（模型并行）；
- 无认证/鉴权与限流，仅面向可信内网环境；
- 重配集群要求先释放全部任务，不支持在线拓扑变更。
