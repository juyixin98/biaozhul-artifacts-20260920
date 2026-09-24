# 双资源主导份额公平调度服务（DRF Scheduler）

纯后端 HTTP 服务，对 **CPU 与内存**两种资源实现**主导份额公平（Dominant Resource
Fairness, DRF）**调度。任务**不可拆分**（要么整体放置，要么继续排队）；支持
**租户权重**与**配额（hard cap）**；**绝不超卖**，放不下的任务保留在队列中，
任何释放都会立即触发重调度。

仅使用 Go 标准库（`net/http`、`math/big`、`encoding/json`），无任何第三方依赖。

---

## 1. 依赖与启动

### 环境要求

- Go **1.23+**（开发验证版本：`go1.23.4 linux/amd64`）
- 无第三方依赖；演示脚本另外需要 `bash`、`curl`、`python3`（仅用于格式化输出与守恒断言）

### 依赖锁定方式

本项目零第三方模块，因此没有 `go.sum`。为满足可复现构建，做了三重锁定：

1. `go.mod` 声明 `go 1.23` 与 `toolchain go1.23.4`；
2. 根目录 `go.env` 与 `Makefile` 设置 `GOTOOLCHAIN=local`，构建时**不联网下载/切换**工具链；
3. `vendor/modules.txt` 是依赖集的锁定清单（空依赖集的显式记录），构建使用
   `-mod=vendor`，完全不查询模块网络。

### 构建与运行

```bash
make build                       # 产物 bin/server
./bin/server -addr :8080 \
  -capacity-cpu 10000 \
  -capacity-mem 10000

# 或直接运行
make run                         # 等价 go run ./cmd/server -addr :8080
```

| 标志 | 默认 | 含义 |
|---|---|---|
| `-addr` | `:8080` | 监听地址 |
| `-capacity-cpu` | `10000` | 集群 CPU 总量，单位 **millicore**（1000 = 1 核） |
| `-capacity-mem` | `10000` | 集群内存总量，单位 **MiB** |

### 测试与演示

```bash
make test          # 全部单元测试/HTTP 测试
make test-race     # 加 -race
make vet
make demo          # 自动构建、起服务、跑端到端场景并校验资源守恒，结束自动清理
```

---

## 2. 调度模型

### 2.1 主导份额（Dominant Share）

租户 *i* 在两种资源上的份额：

```
share_cpu(i) = alloc_cpu(i) / capacity_cpu
share_mem(i) = alloc_mem(i) / capacity_mem
dominant(i)  = max(share_cpu(i), share_mem(i))
```

调度选择依据**加权主导份额**：

```
key(i) = dominant(i) / weight(i)
```

每轮放置循环在“队列非空”的租户中选择 `key` **最小**者，尝试放置其**队首**任务；
放置成功后份额变化，重新选择。份额计算与比较全部使用 `math/big.Rat` **精确分数**，
不使用浮点，因此 `weight = 1/3` 这类无限循环小数的平局判定是确定且精确的。

### 2.2 确定性平局规则（两层）

1. **租户间**：加权主导份额相同 → 租户 ID **字典序**升序；
2. **租户内**：严格 **FIFO**（按全局提交序号），后面的任务不能越过放不进的队首
   （head-of-line blocking，见 API 中 `blocked_reason=waiting_behind_head_of_queue`）。

因此对同一串操作，无论运行多少次、Go map 以何种顺序遍历，结果**逐字节一致**
（测试 `TestDRF_DeterministicReplay` 连续回放 20 次比对快照）。

### 2.3 不超卖与配额

放置一个需求为 `(d_cpu, d_mem)` 的任务，必须同时满足：

```
used_cpu + d_cpu <= capacity_cpu
used_mem + d_mem <= capacity_mem
alloc_cpu(t) + d_cpu <= quota_cpu(t)
alloc_mem(t) + d_mem <= quota_mem(t)
```

任一不满足则不放置（任务入队）。配额是**绝对硬上限**：哪怕集群空闲，租户也不能
超过自己的配额。需求超过集群容量或租户配额的任务在提交时直接以 `400` 拒绝
（它在配置不变的前提下永远无法运行，只会永久阻塞同租户后续任务）。

### 2.4 工作守恒与重调度

提交时与每次释放后都会立即跑一遍放置循环：只要还存在“放得下”的队首任务，就会
继续放置；当所有非空队列的队首都放不下时停止。注意循环会**跳过本轮放不进的租户**
继续尝试份额更大的租户——例如内存租户队首因内存不足被卡住时，CPU 租户仍可被放置。

---

## 3. HTTP 接口

所有请求/响应均为 JSON。错误响应：`{"error": "..."}`。

| 方法 | 路径 | 说明 | 成功状态码 |
|---|---|---|---|
| `GET` | `/healthz` | 健康检查 | 200 |
| `GET` | `/state` | 全局快照（容量/用量/空闲、每租户份额与任务） | 200 |
| `POST` | `/admin/reset` | 清空全部租户与任务（演示/测试用） | 200 |
| `POST` | `/tenants` | 创建租户 | 201 |
| `GET` | `/tenants/{id}` | 查询租户 | 200 |
| `DELETE` | `/tenants/{id}` | 删除租户（须无任何任务） | 200 |
| `POST` | `/tasks` | 提交任务（放不下也会接受并入队） | 201 |
| `GET` | `/tasks/{id}` | 查询任务（含排队阻塞原因） | 200 |
| `DELETE` | `/tasks/{id}` | 释放/完成任务并立即重调度 | 200 |

错误码映射：参数非法 `400`；不存在（租户/任务）`404`；ID 重复 / 租户仍有任务
`409`；方法不允许 `405`；请求体上限 1 MiB，拒绝未知字段与尾随数据。

### 请求样例（curl）

```bash
# 创建租户：weight 可省略（默认 1）；接受数字 2.5 或精确分数字符串 "1/3"
curl -sS -X POST localhost:8080/tenants \
  -H 'Content-Type: application/json' \
  -d '{"id":"alpha","weight":1}'
curl -sS -X POST localhost:8080/tenants \
  -H 'Content-Type: application/json' \
  -d '{"id":"beta","weight":"1/3"}'

# 带硬配额的租户
curl -sS -X POST localhost:8080/tenants \
  -H 'Content-Type: application/json' \
  -d '{"id":"gamma","weight":2,"quota":{"cpu":4000,"mem":6000}}'

# 提交任务：cpu 单位 millicore，mem 单位 MiB
curl -sS -X POST localhost:8080/tasks \
  -H 'Content-Type: application/json' \
  -d '{"id":"a1","tenant":"alpha","cpu":6000,"mem":1000}'
# -> 201 {"status":"running", "task":{...}}

# 放不下时任务仍被接受，status=queued
curl -sS -X POST localhost:8080/tasks \
  -H 'Content-Type: application/json' \
  -d '{"id":"a2","tenant":"alpha","cpu":6000,"mem":1000}'
# -> 201 {"status":"queued", "task":{...,"blocked_reason":"insufficient_cluster_cpu"}}

curl -sS localhost:8080/state
curl -sS localhost:8080/tasks/a2

# 释放任务：响应附带释放后重调度的最新全局状态
curl -sS -X DELETE localhost:8080/tasks/a1
```

`GET /state` 响应（节选）：

```json
{
  "capacity": {"cpu": 10000, "mem": 10000},
  "used":     {"cpu": 7000, "mem": 7000},
  "free":     {"cpu": 3000, "mem": 3000},
  "num_running": 2,
  "num_queued": 2,
  "tenants": [
    {
      "id": "alpha",
      "weight": "1", "weight_decimal": "1.000000",
      "quota": {"cpu": 10000, "mem": 10000},
      "allocated": {"cpu": 6000, "mem": 1000},
      "num_running": 1, "num_queued": 1,
      "dominant_resource": "cpu",
      "dominant_share": "3/5",
      "weighted_share": "3/5", "weighted_share_decimal": "0.600000",
      "running": [ ... ],
      "queued":  [ { "id": "a2", "status": "queued",
                     "blocked_reason": "insufficient_cluster_cpu", ... } ]
    }
  ]
}
```

份额字段同时给出**精确分数**（`"3/5"`，调度实际使用的值）和 6 位小数展示值。
排队任务的 `blocked_reason` 取值：

- `insufficient_cluster_cpu` / `insufficient_cluster_mem`：集群该维度不足；
- `tenant_quota_cpu_exceeded` / `tenant_quota_mem_exceeded`：受租户配额限制；
- `waiting_behind_head_of_queue`：本租户前面还有任务（队头阻塞）。

---

## 4. 验收场景与实测结果

默认容量 `(10000m CPU, 10000 MiB)`，建两个等权租户：

- **alpha（CPU 密集）**：任务 `(6000 cpu, 1000 mem)`
- **beta（内存密集）**：任务 `(1000 cpu, 6000 mem)`

执行序列：`b1 入 → b2 入 → a1 入 → a2 入 → 释放 b1 → 释放 b2 → 释放 a1`。

### 实测结果（`make demo` 真实输出摘要）

| 步骤 | 放置结果 | 用量 (cpu,mem) | 空闲 (cpu,mem) |
|---|---|---|---|
| b1 | running | (1000,6000) | (9000,4000) |
| b2 | **queued**（内存不足） | (1000,6000) | (9000,4000) |
| a1 | **running**（跳过卡住的 beta 队首，份额最小的 alpha 放得下） | (7000,7000) | (3000,3000) |
| a2 | **queued**（平局 ID 偏向 alpha 先选，但只剩 3000 CPU） | (7000,7000) | (3000,3000) |
| 释放 b1 | **b2 立即重调度**，a2 仍排队（a1 占 6000 CPU） | (7000,7000) | (3000,3000) |
| 释放 b2 | a2 仍排队（空闲仅 4000 CPU < 6000） | (6000,1000) | (4000,9000) |
| 释放 a1 | **a2 立即运行** | (6000,1000) | (4000,9000) |

每一步 `scripts/demo.sh` 都用 python 自动断言：

1. **资源守恒**：`used + free == capacity` 两个维度恒成立；
2. **不超卖**：`used <= capacity`，且每租户 `allocated <= quota`；
3. **账实相符**：每租户 `allocated` 严格等于其 running 任务需求之和。

实测 4 个检查点全部输出 `conservation OK`，脚本退出码 0。

### 自动化测试（`make test-race` 实测）

- `TestDRF_CPUAndMemoryIntensiveTenants`：上述验收主线；
- `TestDRF_ReleaseReschedulesChain`：释放后链式重调度；
- `TestDRF_DeterministicTieBreak`：filler 预占 + 逐块释放，验证
  份额相同按 ID 字典序（`alpha < mid < zeta`）、租户内 FIFO，以及离散任务
  造成的资源孤岛（剩 1000/1000 无人能用但**不超卖**）；
- `TestDRF_WeightedShareIsExact`：权重 `1/3` 与 `1`，饱和时精确得到
  **alpha=75、beta=25**（份额比 3:1），加权主导份额**精确相等**（`3/4`）；
- `TestDRF_QuotaHardCap`：配额硬上限，释放配额后排队任务立即运行；
- `TestDRF_HeadOfLineBlocking`：队头阻塞语义；
- `TestDRF_TaskLargerThanCapacityRejected`：超大任务 400 拒收；
- `TestDRF_FairnessDeviationBoundedByOneTask`：公平偏差上界（见下节）；
- `TestDRF_RandomizedConservation`：2000 步随机提交/释放模糊测试，
  全程守恒、配额不被突破；
- `TestDRF_DeterministicReplay`：同一操作序列回放 20 次快照逐字节一致；
- `internal/server`：HTTP 全生命周期、分数权重、配额、各类 400/404/409/405。

实测结果：`go test -race ./...` 全部 **PASS**（含竞态检测），`go vet` 无告警。

---

## 5. 离散任务的公平偏差（indivisibility gap）

经典 DRF 的公平性证明建立在**资源可无限细分**的假设上：理想情况下各租户主导
份额会被拉到完全相等。真实任务不可拆分，每一次放置都是“一整个任务份额”的
**跳变**，份额差不可能任意小，由此产生两类可观测偏差：

1. **份额粒度偏差**。设任务的主导份额为 `g = max(d_cpu/C, d_mem/M)`，
   则任意两个**始终有排队任务**（持续有需求）的租户，其主导份额之差满足

   ```
   |dominant(i) - dominant(j)| <= g_max
   ```

   即偏差不超过“最大一个任务的主导份额”。任务越大、容量越小，`g` 越大，偏差
   越明显。`TestDRF_FairnessDeviationBoundedByOneTask` 用 `(3000,3000)` 的任务
   （`g = 3/10`）实测：平局租户各放 2 个与 1 个时，份额差**恰好为 3/10**，
   且随后逐任务释放、全程不超过该上界。

2. **多维互补需求造成的资源孤岛**。CPU 密集与内存密集任务并存时，可能出现
   “每维都有空闲、但没有任何队首任务的二维需求能同时装下”的碎片状态
   （例如各剩 1000 CPU 与 1000 内存，而队首都要 3000）。这部分资源被搁置，
   属于离散、多维装箱的固有代价；调度器选择**保留队列而非超卖**。

其他偏差来源：租户**没有排队需求**时份额自然归零（这不是不公平，偏差上界定理
只约束“仍在竞争”的租户之间）；**队头阻塞**让同租户后续小任务无法利用碎片；
**平局规则**本身（ID 字典序）在份额完全相等时把“一整个任务”的优势确定性地
分给字典序靠前的租户——这是用可解释、可重放的确定性换取的取舍，而非随机抖动。

任务越细（`g` 越小）、租户吞吐越连续，结果越接近连续 DRF 的理想公平。

---

## 6. 代码结构

```
cmd/server/main.go            入口：flag 解析、HTTP server 超时配置
internal/scheduler/
  scheduler.go                DRF 核心：租户/任务模型、放置循环、配额、精确分数
  state.go                    对外快照（份额、阻塞原因），锁内深拷贝
  scheduler_test.go           调度核心测试（守恒/平局/权重/配额/偏差/模糊/回放）
internal/server/
  server.go                   net/http 路由、DTO、权重解析、错误码映射
  server_test.go              HTTP 端到端测试
scripts/demo.sh               验收场景一键演示（含守恒自动断言）
go.mod / go.env / vendor/     依赖与工具链锁定（零第三方依赖）
```

并发模型：调度器内部一把 `sync.Mutex` 保护全部状态，所有变更（提交/释放/重置）
在锁内完成放置循环后再释放；快照在锁内深拷贝。状态为内存态，进程重启即清空。

## 7. 未完成项 / 已知边界

- **状态不持久化**：无磁盘存储，重启丢失（题目要求纯后端调度服务，未要求持久化）。
- **无认证鉴权 / 多租户隔离**：`/admin/reset` 等接口裸露，仅适合本地/内网演示。
- **单集群单节点模型**：资源是一个二维总量池，没有多主机的放置约束（如 NUMA、
  实例分布）；这是把问题建模为经典 DRF 的刻意简化。
- **任务无时长/无自动完成**：任务只能通过 `DELETE` 显式释放，没有 TTL 或异步执行器。
- **队头阻塞**：同租户严格 FIFO，不支持按任务可放置性跳过队首（可作为后续策略选项）。
- **份额上界定理未做形式化证明**，仅以单元测试在固定场景与 2000 步随机序列上验证。
