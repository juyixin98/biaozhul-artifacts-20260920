# 追踪关键路径分析（Critical Path Analyzer）

纯后端可观测性数据处理样例：接收 span（追踪片段）数据，构建 span DAG，计算**关键路径**，
区分**同步依赖**与**并行子任务**，对时钟矛盾与结构错误输出诊断。全部使用合成数据，
不依赖任何真实监控平台；提供 HTTP 摄入/查询接口与本地 JSONL 持久化。无前端。

- 语言：Go 1.22（仅标准库，零第三方依赖）
- 持久化：本地追加式 JSONL（`data/spans.jsonl`），重启自动重放
- 时间单位：API 字段名带 `_us`，但数值只要求同一 trace 内可比（纳秒/微秒均可）

## 目录结构

```
cmd/server/             HTTP 服务入口
internal/analyzer/      span 模型、关键路径引擎、合成数据
internal/store/         内存索引 + JSONL 追加持久化
internal/api/           HTTP 路由与请求校验
examples/               请求样例 JSON 与端到端 curl 脚本
```

## 快速开始

```bash
go run ./cmd/server -addr :8080 -data ./data
# 或：CP_ADDR=:8080 CP_DATA=./data CP_SEED=1 go run ./cmd/server
```

生成内置样例并查询：

```bash
# 串并行 DAG
curl -s -X POST http://127.0.0.1:8080/v1/sample/demo?trace_id=t1
curl -s http://127.0.0.1:8080/v1/traces/t1/critical-path

# 一把梭端到端脚本（覆盖重叠/缺 span/循环等全部诊断场景）
chmod +x examples/requests.sh
./examples/requests.sh
```

## HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET  | `/healthz` | 健康检查 |
| POST | `/v1/traces/{tid}/spans` | 摄入一批 span（body：`{"spans":[...]}`），返回即时分析 |
| GET  | `/v1/traces/{tid}` | 查看 trace 全部原始 span |
| GET  | `/v1/traces/{tid}/critical-path` | 关键路径完整报告 |
| GET  | `/v1/traces` | 列出全部 trace |
| POST | `/v1/sample/{kind}` | 生成内置合成 trace：`demo` `synth`(带 `?n=`) `overlap` `cycle` `missing` `selfoverlap` |

span 字段：`trace_id, span_id, parent_id, name, start_us, end_us, async`。
`async=true` 表示**并行子任务**（父不等待）；缺省为**同步依赖**（父阻塞等待它）。
结构性错误（循环、重复 ID、负时长、空 trace）返回 **422**，数据仍落库；参数错误返回 **400**。

## 算法说明

### 1. 关键路径：墙上时间扫描，不是子耗时求和

在 trace 的时间轴上逐段扫描，每个时刻把时间归属给“决定该时刻存在”的那个 span：

1. 先在**当前活动的同步 span** 中选**层级最深**者（同层取结束更晚、平局取 span_id 小）；
2. 只有当不存在任何活动同步 span 时（即已越过根结束点的异步尾部），
   才归属给活动的 async span——表示泄漏的后台任务真实地把 trace 顶长了。

由此保证：

- 并行兄弟即使耗时长，只要在父结束前完成，**就不在关键路径上**（父没在等它）；
- 并行重叠的时间在墙上只计一次。例如三个完全重叠的 80 单位并行任务，
  `sum_span_durations=340`，但关键路径仍是根的墙上长度 100。

### 2. 手算串并行 DAG（`demo`）

```
root     [0,300)
├─write  [10,200)   同步
│  ├─shard-a [50,130)  同步
│  │  └─wal   [60,90)   同步
│  └─shard-b [100,180) async（并行写分片）
└─gc      [210,260)  async（后台 GC）
```

| 墙上区间 | 归属 span | 说明 |
|---|---|---|
| [0,10) | root | 根自身工作 |
| [10,50) | write | write 发起 shard 前的工作 |
| [50,60) | shard-a | |
| [60,90) | wal | 最深的同步调用 |
| [90,130) | shard-a | wal 返回后 |
| [130,200) | write | shard-b 在 [130,180) 虽在运行，但是并行任务，write 没等它 |
| [200,300) | root | gc 在 [210,260) 并行运行，root 未被阻塞，时间归 root |

关键路径 = `root(110) → write(110) → shard-a(50) → wal(30)`，合计 **300 = 根墙上时长**；
`shard-b`、`gc` 不在路径上，`parallel_slack_us = 130`
（shard-b 的 80 + gc 的 50，即被并行吸收的墙上时间）。
所有 span 原始耗时之和是 **730**，正好演示“不能直接求所有子耗时之和”。

### 3. 自耗时（self time）规则

```
self(span) = span.duration − union(每个孩子与 span 区间的交集)
```

- 用**孩子区间的并集**扣除：两个孩子时间重叠时，重叠段只扣一次。
  例：root[0,100) 下 x[10,60)、y[40,90) 重叠 [40,60)，
  naive 求和会扣 50+50=100（自耗时错算成 0），并集只有 80，**自耗时正确为 20**。
- 结果夹到非负（时钟矛盾时也不产生负自耗时）。
- 自耗时对同步/异步孩子一视同仁（都算“花在孩子上的时间”）；
  它与关键路径是**两个口径**：自耗时回答“父自己干了多少活”，
  关键路径回答“谁的耗时决定了 trace 总长”。
- 递归恒等式：`duration(root) = Σ self(all spans in tree) + 越过根结束的异步尾部`。

### 4. 诊断

| 级别 | kind | 触发条件 | 处理 |
|---|---|---|---|
| error | `CYCLE_DETECTED` | parent 关系成环（迭代三色 DFS，附具体环路径） | 不计算路径，422 |
| error | `DUPLICATE_SPAN_ID` | 同 trace 内 span_id 重复 | 422 |
| error | `NEGATIVE_DURATION` | end < start | 422 |
| error | `EMPTY_TRACE` | 无 span | 422 |
| warning | `OVERLAPPING_SYNC_SIBLINGS` | 两个同步兄弟时间重叠（串行依赖下不应并发，典型时钟偏差） | 重叠段归结束更晚者 |
| warning | `CHILD_OUTSIDE_PARENT` | 子区间越过父区间（跨机时钟偏差/埋点错误） | 同步子越界部分顶长观察窗口；async 同 |
| warning | `ASYNC_TAIL_EXTENDS_TRACE` | 并行任务越过根结束仍在跑 | 尾部归属 async 链，上关键路径 |
| warning | `ORPHAN_SPAN` | parent_id 指向不存在的 span（缺 span） | 提升为根 |
| warning | `MULTIPLE_ROOTS` | 多个根（含孤儿提升） | 取最早开始者为主窗口 |

## 测试

```bash
go test -v -count=1 ./...
```

覆盖：手算串并行 DAG 的逐段归属、同步兄弟重叠、孩子越界、缺 span、循环、
重复 ID、负时长、空 trace、异步尾部顶长、并行重叠不计和、区间并集算法，
以及存储重放和 HTTP 层（400/404/422）。无任何外部依赖，可离线 `go test`。

## 持久化说明

摄入的每个 span 以一行 JSON 追加到 `data/spans.jsonl`，写盘 `fsync` 后更新内存索引；
同一 span 重复写入产生多行，重放时后者覆盖（upsert 幂等）。
这是“本地持久化样例”级别实现，未做压缩/分片/并发写入批处理。
