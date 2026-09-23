# 实际运行记录（RUNLOG）

以下命令均在交付环境中**真实执行**。环境：

```
OS:    Linux 6.8.0-90-generic (Ubuntu 24.04)
JDK:   OpenJDK 21.0.12.1 (javac 21.0.12.1)
构建:  纯 javac，无 Maven/Gradle，无任何第三方依赖
日期:  2026-09-23
```

> 说明：无外部消息系统、无前端；HTTP 服务仅用 JDK 内置 HttpServer。

## 1. 干净构建 + 全部自动化测试

命令：

```bash
rm -rf out && mkdir -p out
scripts/test.sh
```

结果（退出码 `0`，33 个测试全部通过）：

```
Tests run: 33, passed: 33, failed: 0
```

测试清单（按类）：

- `VirtualClockTest`（3）：时钟只进不退；周期任务在虚拟时钟跳变时按计划点补触发；取消后静默；nextFireTime 跟踪。
- `ExecutorSchedulerTest`（1）：真实时间调度器冒烟（系统时钟 + 真实线程，触发 ≥3 次）。
- `WatermarkManagerTest`（13）：
  - 全局 = 活跃分区最小值；
  - **极端快分区不能拖跑全局**（fast 到 10¹²，slow=10 → 全局=10）；
  - **暂停→空闲→恢复全程**：300ms 边界判 IDLE；恢复重放 ts=500 旧事件进迟到通道且 `fromResumedPartition=true`；全局水位线序列 1000→3000→3500 严格单调；恢复后陈旧 local=1000 不拉低全局；
  - **空闲边界**：299ms 仍 ACTIVE、300ms 恰好 IDLE、新事件立即恢复；
  - WAITING 不阻塞聚合、不被误判为 IDLE；
  - 迟到边界 `ts == globalWatermark` 即迟到（闭区间）；
  - 首事件前为 NO_WATERMARK；`idleTimeout=0` 关闭检测；未知分区动态注册；
  - 乱序余量 `maxOutOfOrderness`；分区内 local 不倒退；手动 tick；系统时钟。
- `WindowTest`（4）：`wm >= end` 才关闭（999 不关/1000 关）；按分区开窗；空窗口不触发；迟到事件不被接纳并上报。
- `JsonTest`（4）：全类型解析、转义、科学计数法、嵌套往返、美化输出、畸形输入报错。
- `ScenarioTest`（5）：通过 JSON 服务跑 examples 三个场景；畸形脚本拒绝；逐步观测全局单调性。
- `RandomMonotonicityTest`（1，内含 **200 个确定性随机工作负载**，种子起 `20260923`）：
  随机 1–5 分区、随机 tick 间隔/超时/乱序余量，混合正常推进、旧事件重放、百万级快跳；
  断言：① 每次发射全局严格增大；② 发射值恰为当时活跃分区 local 的最小值（有分区恰好等于它）；
  ③ 迟到事件不推进任何水位线；④ 空闲不会早于超时发生。
- `HttpServiceTest`（2）：真实 HTTP 上 health / `/api/run` / 会话创建-脚本-查询-删除-404 / 400 错误。

## 2. 本地（非 HTTP）执行四个请求样例

命令：

```bash
scripts/run-file.sh examples/pause-resume.json    # 其余三个同理
```

结果摘要（完整 JSON 已存为 `examples/*.response.json`）：

### pause-resume（暂停 / 空闲 / 恢复 / 迟到 / 不倒退）
```
globalWatermark = 3500
lateEvents      = [p2 ts=500, fromResumedPartition=true]（拒绝它的全局水位线=3000）
逐步全局水位线： null → 1000（t=100）→ 1000（t=200，p2 仍活跃拖住）
              → 3000（t=300，p2 判 IDLE 被剔除）→ 3000（旧事件迟到）
              → 3500（t=500，p2 追上）
```

### fast-partition（极端快分区）
```
globalWatermark = 30      # fast 达 2×10^12，全局仍由 slow=30 决定，无迟到
```

### windows（精确滚动窗口）
```
windows = [p1 [0,1000) 2 个事件(v1,v2), p2 [0,1000) 1 个事件(v3)]
late    = [(p1, ts=50)]  # 窗口关闭后到达，被拒
最终 globalWatermark = 1100
```

### idle-boundary（空闲边界 + 乱序余量 100）
```
处理时间 100/200ms：b=ACTIVE，global=900（1000-100）
处理时间 300ms  ：b 恰好 IDLE（300-0>=300），global 跳到 1500
b 以新事件恢复后：global 保持 1800，不回退
```

## 3. 实际启动 HTTP 服务并用 curl 验证

命令与真实输出：

```
$ scripts/run.sh 19092
Watermark service listening on port 19092
Endpoints: GET /health | POST /api/run | POST /api/sessions

$ curl -s http://127.0.0.1:19092/health
{
  "status": "UP"
}

$ curl -X POST http://127.0.0.1:19092/api/run \
       -H 'Content-Type: application/json' --data-binary @examples/pause-resume.json
globalWatermark = 3500
lateEvents = [('p2', 500, 'fromResumed=', True)]

$ curl -X POST .../api/run --data-binary @examples/fast-partition.json
globalWatermark = 30

$ curl -X POST .../api/run --data-binary @examples/windows.json
windows = [('p1', 0, 1000, 2), ('p2', 0, 1000, 1)]
late = [('p1', 50)]
```

会话生命周期（来自自动化测试 `HttpServiceTest`，真实 HTTP）：
`POST /api/sessions → 201 + sessionId` → `POST .../script → 200（global=77）`
→ `GET ... → 200` → `DELETE → 200` → 再次 `GET → 404`；非法 body → `400 {"error":...}`。

> 注：环境中 8080/18080 端口被其他进程（`vccsim`）占用，故记录中使用 19091/19092；
> 端口可由命令行参数指定，不影响功能。

## 4. 未通过项 / 已知限制

- **最终结果：0 项未通过**。`scripts/test.sh` 退出码 0，33/33 通过，四个样例本地与 HTTP 均符合预期。
- 开发过程中曾出现并已修复的问题（如实记录）：
  1. `ManualScheduler` 初版在时钟大跨度跳变时一个 tick 只触发一次周期任务——已改为按计划周期点补触发（语义对齐 `ScheduledExecutorService`：追赶触发读到的是跳后时钟）。
  2. JSON 序列化最初不支持 record，导致快照输出退化为字符串——已为快照提供 `toMap()` Map 视图。
  3. HttpServer 固定 4 线程在连接保活时可能耗尽——已改为守护线程缓存池。
- 设计内的**有意行为**（非缺陷）：
  - 空闲分区被恢复的首个迟到事件重新激活后、下一个 tick 之前，该分区 local 可能暂时低于 global；
    global 已做 clamp 不受影响，下一次 on-time 事件/tick 后即一致。属性测试在“纯 tick 后状态”上校验聚合契约。
  - 窗口处理对“迟到但理论窗口尚未关闭”的事件同样不接纳（迟到事件一律不进入算子），仅区分是否标记 dropped。
- 已知范围限制（题目明确不需要）：无持久化、无多实例分布式聚合、无外部消息系统接入、无前端。
