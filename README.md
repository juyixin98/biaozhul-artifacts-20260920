# 追踪关键路径分析（Trace Critical Path Analyzer）

纯后端可观测性数据处理样例：接收分布式追踪（trace）的 span 数据，构建 span DAG，
**区分同步依赖与并行子任务**计算关键路径，给出每个 span 的自耗时，并对时钟矛盾与
结构性坏数据输出诊断。仅用 Go 标准库 + 合成数据 + 本地 JSON 文件持久化，无前端、
不依赖任何真实监控平台。

## 1. 构建与运行

要求 Go 1.22+（标准库之外零依赖）。

```bash
make build           # 或 go build -o cpathtrace ./cmd/cpathtrace
make test            # 全部自动化测试
make run             # HTTP 服务，默认 :8080，数据目录 ./data
```

三种运行方式：

```bash
# 1) HTTP 服务
./cpathtrace serve --addr :8080 --data ./data

# 2) 把内置合成数据写入本地存储
./cpathtrace seed --name all          # 或 serial_parallel / sync_overlap /
                                       # missing_span / cycle / clock_skew

# 3) 离线分析一个 trace JSON 文件（结果打到 stdout）
./cpathtrace analyze --file examples/requests/01-ingest-serial-parallel.json
```

端到端 curl 演示（先启动服务）：

```bash
./examples/curl-demo.sh
```

## 2. 数据模型

时间为整数纳秒，相对任意起点均可（只比较先后与差值）。

```json
{
  "trace_id": "trace-serial-parallel",
  "spans": [
    {"span_id": "root", "name": "gateway", "start_time": 0, "end_time": 120, "relation": "sync"},
    {"span_id": "a1", "name": "fanout-A", "start_time": 20, "end_time": 120,
     "relation": "async", "parent_span_id": "root"}
  ]
}
```

| 字段 | 说明 |
| --- | --- |
| `span_id` | trace 内唯一，必填 |
| `parent_span_id` | 父 span；根节点留空 |
| `relation` | `sync`（默认）或 `async` |
| `start_time` / `end_time` | 整数纳秒时间戳 |

- **`sync`（同步依赖）**：父 span 阻塞等待该子 span 完成。它对父关键时间的贡献
  **全额计入**；同一父节点下的 sync 子 span 在墙钟时间上不应重叠。
- **`async`（并行子任务）**：父节点发起后不等待的并行分支。并行分支之间、以及并行
  分支与同步工作在墙钟上**允许重叠**；关键路径上只有**最长的那一条 async 子树**
  可能贡献时间（`max`，不是求和）。

## 3. 计算规则（重点）

### 3.1 自耗时 self time —— 区间并集，绝不把子耗时相加

```
self(s) = duration(s) − 所有子 span 区间在 [s.start, s.end] 内覆盖部分的并集长度
```

规则细节：

1. 每个子 span（无论 sync/async）的区间先**裁剪**到父区间 `[start,end]` 内；
   区间完全/部分落在父区间之外是时钟矛盾，诊断为 `CHILD_OUTSIDE_PARENT`，但仍按
   裁剪后的区间参与计算。
2. 裁剪后的所有子区间做**排序 + 合并**，重叠/相邻区间只算一次覆盖。
3. `self = 父时长 − 覆盖并集长度`。因此并行/重叠子任务不会被重复扣减，`self`
   不会因为两个并行子各覆盖了一段时间就变成负数。

> 反例：父 [0,100) 有两个重叠 async 子 [0,80)、[20,100)。
> 错误做法 `100 − 80 − 80 = −60`；正确做法：覆盖并集 [0,100) 长度 100，self=0。

自耗时代表“父 span 自身消耗、且没有任何子 span 在执行的时间”（真正在本地干活的
时间、以及没有被建模子任务覆盖的空窗）。

### 3.2 关键路径 —— 同步求和、并行取最大

DAG 自底向上（后序）递归：

```
C(s) = self(s)
     + Σ C(c)          对所有 sync 子 c（串行工作，相加）
     + max C(c)        对所有 async 子 c（并行只取最长分支；无 async 则省略）
```

- 这正是“不能直接求所有子耗时之和”的体现：sync 链串行相加；async 并行分支只取
  **最大值**。
- trace 关键路径长度为 `C(root)`。`critical_to_wall_ratio = C(root)/root 墙钟时长`，
  比值 >1 是异常信号（见下）。
- 关键路径的 span 序列以 **DFS 树**形式输出：根 → 按启动时间排列的 sync 子树与
  胜出的最长 async 子树；被淘汰的较短 async 分支不出现在路径中，每个条目标注
  `reason`（`sync-blocking` / `async-parallel-longest`）。

### 3.3 手算样例（串并行 DAG）

见 `examples/requests/01-ingest-serial-parallel.json`，`serial_parallel` 合成场景：

```
root [0,120)
├─ s1 (sync)  [0,20)            时长20, self20, C=20
├─ a1 (async) [20,120)          子区间全覆盖 → self0
│   ├─ x (sync) [20,70)         时长50, self50, C=50
│   └─ y (sync) [70,120)        时长50, self50, C=50
│                                C(a1)=0+50+50=100
├─ a2 (async) [20,50) 叶子      时长30, self30, C=30   ← 并行输家
└─ s2 (sync)  [50,70)           时长20, self20, C=20

root 被所有子区间并集完全覆盖 → self(root)=0
C(root) = 0 + (20 + 20)        ← 两个 sync 串行求和
              + max(100, 30)   ← 并行只取最长 a1
        = 140
```

路径：`root → s1 → a1 → x → y → s2`（`a2` 是较短并行分支，被排除）。

这里 140 > root 墙钟时长 120（比值 1.1667）：建模的“同步串行工作 + 最长并行分支”
超出了实际墙钟，说明关系标注（本应 async 的被标成了 sync）或时钟存在矛盾，系统
诊断 `CRITICAL_EXCEEDS_DURATION`。这是**刻意保留**的有意义信号：并行被误标成同步
时，关键路径会“超出现实”。

### 3.4 诊断码（时钟矛盾与坏数据）

| Code | 级别 | 触发条件 |
| --- | --- | --- |
| `NEGATIVE_DURATION` | warning | `end < start`，时钟不可能 |
| `ZERO_DURATION` | warning | 零时长 span |
| `CHILD_OUTSIDE_PARENT` | warning | 子区间起点早于父起点，或终点晚于父终点（时钟偏移/埋点错误） |
| `SYNC_CHILD_OVERLAP` | warning | 同一父下两个 sync 子区间在墙钟上重叠（边界相接 `end==start` 不算） |
| `MISSING_PARENT` | warning | `parent_span_id` 指向 trace 中不存在的 span；该 span 当孤儿根处理，不进入被选根的路径 |
| `MULTIPLE_ROOTS` | warning | 多个真正根；选择启动最早（再按 span_id）的一个 |
| `CRITICAL_EXCEEDS_DURATION` | warning | 某 span 的建模关键时间 > 其墙钟时长（sync/async 标错或时钟偏移） |
| `CYCLE` | **error** | parent 边存在环（三色 DFS 检测，报告环路径） |
| `NO_ROOT` | **error** | 所有 span 都有父（纯环），没有入口 |

error 时 HTTP 返回 `422 Unprocessable Entity`，`critical_path_duration` 为 `null`、
`critical_path` 为空；warning 不影响结果产出。所有诊断按 code、span_id 稳定排序。

## 4. HTTP API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/traces` | 摄入一个 trace（严格 JSON：未知字段、重复 span_id、非法 relation 返回 400） |
| `GET` | `/api/traces` | 列出已存 trace_id |
| `GET` | `/api/traces/{id}` | 读取原始 trace（404 如不存在） |
| `DELETE` | `/api/traces/{id}` | 删除 |
| `GET` | `/api/traces/{id}/critical` | 分析：自耗时、关键路径、诊断（有 error 时 422） |
| `POST` | `/api/synthetic/seed?name=...` | 写入内置合成 trace，响应直接附带分析结果 |
| `GET` | `/healthz` | 存活检查 |

摄入期只做结构性校验（trace_id/span_id/name 非空、id 唯一、relation 合法）。
**重叠、缺 span、时钟矛盾、循环都允许写入**，由分析接口暴露诊断——采集端不应
丢数据，问题数据也要可查。

响应统一包一层：成功 `{"data": ...}`（直接返回对象的接口除外），失败
`{"error":{"code":...,"message":...}}`。分析结果字段：

```jsonc
{
  "trace_id": "...",
  "root_span_id": "root",
  "trace_duration": 120,
  "critical_path_duration": 140,
  "critical_to_wall_ratio": 1.1667,
  "critical_path": [ {"span_id":"root", "self_time":0, "critical_time":140,
                      "reason":"root", "relation":"sync", "name":"gateway"}, ... ],
  "span_times": [ {"span_id":"...", "duration":120, "self_time":0, "critical_time":140}, ... ],
  "diagnostics": [ {"code":"CRITICAL_EXCEEDS_DURATION", "severity":"warning",
                    "span_id":"root", "message":"..."} ]
}
```

## 5. 持久化

`internal/store`：每个 trace 一个 `data/<trace_id>.json`；写入走
**临时文件 + fsync + rename** 原子替换，互斥锁保护并发；trace_id 做路径穿越校验
（拒绝 `..`、`/`、`\`）。这是样例级实现，足以演示本地持久化，不追求高吞吐。

## 6. 内置合成场景（均可手算）

| name | 验收点 |
| --- | --- | --- |
| `serial_parallel` | sync 求和 + async 取 max 的正常串并行 DAG，140 |
| `sync_overlap` | 重叠 sync 区间触发告警；区间并集 self=0；关键时间 200 > 100 |
| `missing_span` | dangling parent 诊断；孤儿不进入选定根路径 |
| `cycle` | 根可达范围外的环同样使整图报错，路径为 null |
| `clock_skew` | 子区间早于/晚于父区间两处告警；裁剪后 self=30，关键时间 160 |

## 7. 测试

```bash
make test     # go test ./...
make cover    # 覆盖率
```

- `internal/analyzer/analyzer_test.go`：手算串并行 DAG（self/关键时间/路径逐条断言）、
  重叠区间并集、并行 max 非求和、区间裁剪、缺 span、可达环与纯环、负/零时长、
  多根选择、边界相接不算重叠。
- `internal/store/store_test.go`：存取/覆盖/列表/删除、原子文件、路径穿越拒绝。
- `internal/api/server_test.go`：httptest 端到端（摄入/查询/分析/404/422/400/seed）。

实际执行的命令、输出与任何未通过项记录在 **[RUNLOG.md](./RUNLOG.md)**。

## 8. 目录结构

```
cmd/cpathtrace/        # CLI: serve / seed / analyze
internal/model/        # Span / Trace 模型
internal/analyzer/     # 区间并集 self time + DAG 关键路径 + 诊断
internal/store/        # 本地 JSON 文件持久化
internal/synthetic/    # 合成 trace 构造
internal/api/          # HTTP 路由与处理
examples/requests/     # 可直接 POST 的请求样例
examples/curl-demo.sh  # 端到端演示脚本
```

## 9. 已知限制（样例范围）

- 关系模型只有 sync/async 两类；没有跨进程时钟偏移自动校正，只做诊断。
- 关键路径按“同步串行 + 最长并行分支”的结构模型计算；若业务语义更复杂
  （线程调度、锁等待、网络排队），需要额外的 cause/link 数据。
- 文件存储适合演示与单机样例，非并发写入密集场景设计。
