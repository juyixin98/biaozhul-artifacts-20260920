# 资源预约冲突求解（resourcebooking）

纯后端的时间区间资源预约调度库 + 本地 HTTP 接口（Go 1.22，无第三方依赖）。

- 时间区间一律 **半开区间 `[Start, End)`**：相邻区间 `[1,3)` 与 `[3,5)` 不冲突。
- 任务必须占用一段 **连续时段**（不可拆分），占用 **多维容量**（需求向量与资源容量向量等长）。
- 提供 **最早可行位置查询**（只查询不落地）与 **原子批量预约**（批次内任意一条冲突，整批拒绝、零落地）。
- **调度时钟与执行器均可替换**：`clock.Clock`（墙钟 / 假时钟）、`executor.Executor`（Noop / Func / 自定义）。
- 所有状态变更产出 **结构化事件**（带序号、时间戳、类型化负载），支持订阅与 JSON Lines 落盘。
- 核心算法通过 **与离散小时间轴穷举参考实现的随机对比测试**（共数百个随机场景）交叉验证。

## 目录结构

```
cmd/reservations/          HTTP 服务入口（支持 --seed、--fake-clock）
internal/
  clock/                   可替换时钟：Wall / Fake
  scheduler/               核心调度库（区间、容量、扫描线、批次、事件、生命周期）
  executor/                可替换执行器：Noop / Func
  dispatcher/              时钟 -> tick 推进 -> 执行器编排
  server/                  本地 HTTP 接口（net/http，Go 1.22 方法路由）
examples/
  seed.json                启动种子数据
  requests.sh              10 个端到端请求样例
  sample_output.txt        样例脚本的一次真实运行输出
RUN_LOG.md                 实际构建/运行/测试记录（含命令与结果）
```

## 快速开始

要求 Go 1.22+。

```bash
# 运行全部测试（普通 + 竞态检测）
go test ./...
go test -race ./...

# 启动服务（假时钟固定在 2030-01-01，便于演示时间推进）
go run ./cmd/reservations \
  -addr 127.0.0.1:8080 \
  -fake-clock 2030-01-01T00:00:00Z \
  -seed examples/seed.json

# 另开终端：执行 10 个请求样例
bash examples/requests.sh
```

生产模式去掉 `-fake-clock` 即可使用系统墙钟（事件时间戳、tick 换算都跟随同一时钟）。

## 核心概念

### 半开区间与整数 tick

调度库内部不感知墙钟，只在整数刻度 `Ticks`（`int64`）上运算。
HTTP 层把对齐到分钟的 RFC3339 时间映射为“自 Unix 纪元起的分钟数”。

- `[Start, End)` 在 `Start` 时刻占用容量，在 `End` 时刻**立即释放**。
- `[1,3)` 与 `[3,5)` 相邻，在 t=3 处容量先释放后占用，互不冲突。
- 扫描线在同一时刻按“先结束事件、后开始事件”排序，正是该语义的算法实现。

### 多维容量

资源声明容量向量（各分量非负，**允许为 0**），预约给出等长需求向量：

```json
{"id": "venue", "capacity": [2, 6]}      // 2 间房、6 个席位
{"resource": "venue", "demand": [1, 4]}  // 占 1 间房、4 个席位
```

- 单条需求超过容量（含在 **容量 0** 维度上要正数）→ `400 invalid_request`。
- 多条预约在任一时刻、任一维度上的需求之和超过容量 → `409 conflict`，
  错误体逐段给出 `segment / dimension / load / capacity` 违规明细与参与冲突的预约 ID。

### 最早可行位置

`POST /v1/reservations:earliest-feasible` 在给定窗口内寻找能容纳指定时长与需求的
**最早连续时段**，只返回结果不落地。找不到时返回 `200 {"feasible": false}`（这不是错误）。

### 原子批量

`POST /v1/reservations:batch` 接受一批条目，每条可以是：

- `fixed`：固定区间；
- `place`：只给窗口 + 时长，由系统放到（相对批次内已选段的）最早可行位置。

处理流程：字段校验/ID 去重 → 逐条相对“既有占用 + 批次内已选段”求解 →
**全部可行才统一提交**。任一条失败则整批 `409`，响应里逐条目给出原因，
且此前没有任何一条写入（测试见 `TestBatchAtomicOnPartialConflict`、
`TestConcurrentBatchAtomic`，并有穷举神谕对比）。

## HTTP 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/healthz` | 健康检查（含当前墙钟与 tick） |
| POST | `/v1/resources` | 创建资源（多维容量） |
| GET | `/v1/resources` / `/v1/resources/{id}` | 列出 / 查询资源 |
| POST | `/v1/reservations` | 固定区间预约 |
| POST | `/v1/reservations:earliest-feasible` | 最早可行位置查询（不落地） |
| POST | `/v1/reservations:batch` | 原子批量预约（fixed/place 混合） |
| GET | `/v1/reservations` | 列出预约（`?resource=&status=` 过滤） |
| GET | `/v1/reservations/{id}` | 查询单条 |
| DELETE | `/v1/reservations/{id}` | 取消（立即释放容量） |
| GET | `/v1/events` | 结构化事件流（`?after_seq=` 增量拉取） |
| POST | `/test/clock/advance` | 仅假时钟模式：推进时钟并同步调度生命周期 |

错误响应统一为 `{"code": ..., "message": ..., "details": [...]}`，
`code` 取值：`invalid_request`(400) / `not_found`(404) /
`conflict`(409) / `no_feasible_slot`(409) / `terminal`(409)。

完整请求/响应样例见 `examples/requests.sh` 与 `examples/sample_output.txt`。

## 事件

事件只追加、不改写，字段为 `seq`（进程内单调递增）、`at`（来自可替换时钟）、
`type`、`detail`（类型化负载）。事件负载是**快照**：预约后续的状态迁移
（running/completed/failed/cancelled）不会回改历史事件。

事件类型：`resource.added`、`reservation.created`、`reservation.rejected`、
`reservation.batch_created`、`reservation.batch_rejected`、
`reservation.started`、`reservation.completed`、`reservation.failed`、
`reservation.cancelled`。

库层面可用 `EventLog.AddSink(NewJSONLSink(w))` 把事件追加为 JSON Lines 文件。

## 生命周期与可替换执行器

`dispatcher.Runner` 按固定间隔读取时钟、推进调度：

1. `Start <= now` 的 pending 预约 → running，发出 `started`；
2. 对刚进入 running 的预约调用注入的 `executor.Executor.Run`；
   返回错误则预约 → failed、立即释放容量并发出 `failed`；
3. `End <= now` 的预约 → completed、释放容量，发出 `completed`。
   同一拍内开始即结束的预约直接完成，不调用执行器。

替换执行器只需实现：

```go
type Executor interface {
    Run(ctx context.Context, r *scheduler.Reservation) error
}
```

## 算法与正确性验证

- 生产实现：端点扫描线（同一时刻先减后加）做多维容量检查；
  最早可行位置只在“窗口起点 / 重叠占用的结束时刻”之间跳跃搜索
  （占用开始不会释放容量，故不可能产生新的可行分界）。
- 参考实现（仅测试代码 `bruteforce_test.go`）：在 24 格离散轴上
  逐时刻维护负载、逐格穷举可行性与最早起点，与生产代码完全独立。
- 三组属性测试（共约 700 个随机场景）逐拍对比两者：
  固定预约接受/拒绝、最早可行起点、混合批次接受性与落点；
  每个操作后还校验逐时刻负载一致且不超容量。
- 边界专项测试：相邻区间、容量为 0、零需求、批次部分冲突零落地、
  批次内互相冲突、50 goroutine 并发（`-race`）。

## 设计边界（明确不做的事）

- 无持久化（除可选 JSONL 事件落点）；进程状态在内存中，重启用 `--seed` 重建。
- 不做认证、鉴权、配额、分页与前端 UI。
- HTTP 时间粒度固定为分钟；调度库本身支持任意整数 tick 粒度。
