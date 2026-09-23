# 多源水位线协调（Multi-Source Watermark Coordination）

纯 Java 后端：多分区事件流的**水位线聚合、空闲检测、恢复不倒退、迟到分流**，
附带一个**小数据精确参考实现**（事件时间滚动窗口）和 **JSON 输入输出服务**。

- 无任何外部依赖（不需要 Maven/Gradle/JUnit/Kafka），仅需 **JDK 17+**（本仓库在 JDK 21 上验证）。
- 时间与调度全部可注入：测试用确定性的虚拟时钟 `VirtualClock` + `ManualScheduler`，
  生产可用系统时钟 `Clock.system()` + `ExecutorScheduler`。
- 无外部消息系统：输入为 JSON 请求脚本（HTTP 或本地文件），输出为 JSON 状态/观测序列。

## 1. 语义说明

### 1.1 全局水位线
```
globalWatermark = min( 当前处于 ACTIVE 的各分区 localWatermark )
```
- `WAITING`（已注册但从未来过事件）与 `IDLE`（空闲超时）分区**不参与**最小值，避免静默分区卡死全局进度。
- 当没有任何分区能贡献时，全局水位线**保持上一次的值**（不重置）。

### 1.2 单调性（核心保证）
- 每个分区 local watermark 只增不减（`BoundedOutOfOrdernessGenerator` 跟踪 `maxTimestamp - maxOutOfOrderness`）。
- 全局水位线在聚合时再次 clamp：`global = max(global, min(active locals))`。
- 因此**暂停分区恢复接入时，其陈旧 local watermark 绝不会把全局水位线拉低**。

### 1.3 恢复与迟到事件
- 事件满足 `eventTimestamp <= globalWatermark` 即判定**迟到**，进入迟到通道：
  - 不喂给生成器、不推进任何水位线；
  - 记录在 `lateEvents[]`，含 `globalWatermark`、到达处理时间、`fromResumedPartition` 标记。
- 恢复的分区若先重放一批**旧事件**，这些事件全部明确进入迟到通道；
  只有比当前全局水位线新的事件才重新参与推进。

### 1.4 空闲检测（处理时间，边界明确）
- 每个 tick 检查：`now - lastEventTime >= idleTimeoutMillis` 则 `ACTIVE -> IDLE`。
- **边界**：恰好等于超时即判定空闲；判定粒度为一个发射间隔；`idleTimeoutMillis <= 0` 表示关闭检测。
- 空闲分区的下一个事件立即 `IDLE -> ACTIVE`（若该事件迟到，状态仍恢复但事件进迟到通道）。

### 1.5 精确参考窗口
`TumblingWindowProcessor` 实现事件时间滚动窗口 `[start, end)`：
- 当 `watermark >= end` 时窗口关闭并输出（边界精确，`wm=999` 不关 `[0,1000)`，`wm=1000` 才关）；
- 空窗口不触发；迟到事件永不进入窗口。
- 数据全在内存 List 中，输出可作为小规模数据的“标准答案”。

## 2. 目录结构

```
src/main/java/com/example/watermark/
  time/        Clock, VirtualClock, Scheduler, ManualScheduler, ExecutorScheduler ...
  watermarks/  WatermarkManager（核心聚合）、分区状态、BoundedOutOfOrdernessGenerator、迟到事件 ...
  windowing/   TumblingWindowProcessor（小数据精确参考实现）
  json/        Json（零依赖 JSON 解析/序列化）
  service/     WatermarkSession（JSON 脚本执行）、WatermarkHttpServer、main 入口、本地脚本运行器
src/test/java/com/example/watermark/test/   自带迷你测试框架 + 33 个测试
examples/    请求样例 *.json 与对应 *.response.json
scripts/     build.sh / test.sh / run.sh / run-file.sh / example.sh
```

## 3. 构建与测试

```bash
scripts/build.sh          # 仅 javac 编译到 out/classes
scripts/test.sh           # 编译并运行全部自动化测试（失败时退出码非 0）
```

## 4. 运行服务

```bash
scripts/run.sh [port]     # 默认 8080；0 表示由系统分配临时端口
```

HTTP 接口（仅用 JDK 内置 `com.sun.net.httpserver`）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET  | `/health` | 健康检查 |
| POST | `/api/run` | 无状态：`config` + `steps` 一次跑完 |
| POST | `/api/sessions` | 建会话，返回 `sessionId` |
| POST | `/api/sessions/{id}/script` | 在会话上执行 `steps` |
| GET  | `/api/sessions/{id}` | 当前状态快照 |
| DELETE | `/api/sessions/{id}` | 关闭并删除会话 |

也可以完全不走 HTTP，本地直接跑请求文件：

```bash
scripts/run-file.sh examples/pause-resume.json
scripts/example.sh examples/fast-partition.json     # 需要先起服务（默认 127.0.0.1:8080）
```

## 5. 请求格式

```json
{
  "config": {
    "maxOutOfOrdernessMillis": 0,
    "autoWatermarkIntervalMillis": 100,
    "idleTimeoutMillis": 300,
    "allowedLatenessMillis": 0,
    "windowSizeMillis": 1000,
    "partitions": ["p1", "p2"]
  },
  "steps": [
    {"op": "event",   "key": "p1", "timestamp": 1000, "value": "任意JSON值"},
    {"op": "advance", "time": 300},
    {"op": "advance", "durationMillis": 100},
    {"op": "register","key": "p3"},
    {"op": "tick"},
    {"op": "snapshot"}
  ]
}
```

- 所有时间单位均为毫秒；`timestamp` 是**事件时间**，`advance.time` 是**处理时间（虚拟时钟）**。
- `advance` 会触发虚拟时钟上已到期的周期 tick（大跨度会按计划周期补触发）。
- 响应含：`snapshot`（全局/各分区水位线、状态、最后事件处理时间、迟到计数）、
  `lateEvents[]`、`windows[]`（配置了 `windowSizeMillis` 时）、以及每个 step 的逐步观测。

## 6. 验收场景（均用虚拟时钟）

| 样例 | 验证点 |
| --- | --- |
| `examples/pause-resume.json` | p2 暂停→在 300ms 边界判 IDLE→全局摆脱 p2 前进→p2 恢复时重放 ts=500 旧事件进迟到通道且 `fromResumedPartition=true`→全局水位线 1000→3000→3500 **不倒退** |
| `examples/fast-partition.json` | fast 分区跳到 10¹²，slow 只有 10/20/30；全局水位线始终=slow，极端快分区不能拖跑 |
| `examples/idle-boundary.json` | 299/300ms 空闲边界、`IDLE` 后恢复、乱序余量 `maxOutOfOrderness=100` |
| `examples/windows.json` | 精确滚动窗口：`wm>=end` 才关闭、空窗口不触发、ts=50 的迟到事件被拒 |

另有：
- `WatermarkManagerTest`：暂停/恢复、极端快分区、全局单调性、空闲 299/300 边界、
  `ts == wm` 迟到边界、WAITING 不阻塞、关闭空闲检测、乱序余量等；
- `RandomMonotonicityTest`：**200 个随机工作负载**（暂停、恢复、百万级快跳、旧事件重放），
  校验全局严格单调、发射值恰为活跃分区最小值、迟到事件不推进、空闲不早于超时。

## 7. 实际运行记录

完整命令与输出见 [`RUNLOG.md`](RUNLOG.md)（在本环境真实执行并记录）。

## 8. 设计取舍

- 选择 **min(active)** + 处理时间空闲超时，而不是 Flink 的“idle 分区不产生 watermark”
  的传递式空闲模型之外的启发式：语义简单、可判定、边界可测。
- 迟到判定采用 `timestamp <= globalWatermark`（闭区间），与“水位线表示 t≤wm 的事件应已到齐”一致。
- 服务层所有仿真时间都是虚拟的；HTTP 只是外壳，便于用 curl/任何语言驱动，
  不引入任何消息中间件。
