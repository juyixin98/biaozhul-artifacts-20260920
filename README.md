# 乱序去重与墓碑（Out-of-Order Dedup & Tombstones）

一个**纯后端**的事件流计算库 + JSON 输入输出服务，用 Java 实现，演示在**有乱序、重复、时钟回退、重启**的环境下如何做：

- 事件 **ID 与事件时间分离**：`id` 是去重身份，`eventTime` 是独立的事件时间（逻辑时间）；
- **有界去重状态**：既按时间（水位线）释放，也按容量（条目数）释放；
- **水位线（watermark）之后释放状态**，并**明确不保证**窗口外旧重复被去重，通过可观察标志 `dedupUnverified` 如实上报；
- **墓碑（DELETE tombstone）**：按业务 key 抑制被删除的数据，同样有界、可过期，过期后返回 `tombstoneUncertain`；
- 时间与调度**可注入**（`Clock` / `Scheduler`），事件时间推进不依赖墙上时钟，因此**墙上时钟回退绝不会导致状态提前释放或重复输出**；
- 状态可 **snapshot / restore**，支持**重启恢复**；
- 附**小数据精确参考实现**（无界 `HashSet` / 全量窗口），与有界流水线做**差异测试**。

**无任何外部消息系统、无第三方依赖**：HTTP 用 JDK 内置 `com.sun.net.httpserver.HttpServer`，JSON 与测试框架均为本仓库自带的最小实现。仅需 JDK（在 JDK 21 上开发与验证）。

---

## 1. 目录结构

```
src/main/java/com/example/dedup/
  json/Json.java                 零依赖 JSON 解析/序列化/规范化（canonical）
  time/                          可注入时间与调度
    Clock.java  WallClock.java  ManualClock.java  MonotonicClock.java
    Scheduler.java  WallScheduler.java
  model/                         Event / IngestResult / WindowResult / LateSideOutput / Metrics
  watermark/                     WatermarkGenerator + BoundedOutOfOrdernessWatermarks
  dedup/                         DedupState（有界去重，时间+容量释放）、PayloadHasher
  tombstone/                     TombstoneStore（有界 keyed 墓碑）
  window/                        TumblingWindowOperator（事件时间滚动窗口 + 迟到宽限）
  pipeline/                      PipelineConfig + StreamPipeline（装配全部组件）
  service/                       HttpService（JSON HTTP 服务）
  Main.java                      CLI 入口
src/test/java/com/example/dedup/
  tests/                         54 个自动化测试（单元 / 验收 / 差异 / JSON / HTTP）
  tests/reference/               小数据精确参考实现（RefDedup / RefTumblingWindow）
examples/                        请求样例 JSON
build.sh  run_tests.sh  demo.sh  构建 / 测试 / 端到端演示脚本
RUNLOG.md                        实际运行命令与结果的如实记录
```

---

## 2. 快速开始

需要 JDK 17+（已在 **JDK 21** 验证）。无需 Maven/Gradle，也不联网下载依赖。

```bash
# 编译
./build.sh

# 运行全部自动化测试（54 个）
./run_tests.sh

# 端到端演示：启动 JSON 服务，用 curl 走查四个验收场景
PORT=18099 ./demo.sh
```

手动启动服务（手动确定性模式）：

```bash
java -cp build/classes com.example.dedup.Main \
  --port 8080 --mode manual \
  --window-ms 10000 --lateness-ms 1000 \
  --retention-ms 12000 --tombstone-ttl-ms 12000 \
  --snapshot-file build/state.json
```

`--mode wall` 使用墙上时钟；`--auto-watermark-ms <n>` 大于 0 时由定时器按**观察到的事件时间**周期性推进水位线（不是按墙上时钟）。

---

## 3. HTTP 接口（全部 JSON）

| 方法 | 路径 | 请求体 | 说明 |
|---|---|---|---|
| GET | `/health` | — | 健康检查、当前模式、处理时钟 |
| POST | `/events` | `{"events":[ Event, ... ]}` | 摄入事件，逐条返回可观察结果 |
| POST | `/watermark` | `{"watermark": 毫秒}` | 显式推进水位线；回退会被拒绝并计数 |
| POST | `/tick` | `{"advanceMillis": 毫秒}` | 仅手动模式：推进处理时钟（可为负，模拟回退） |
| POST | `/drain` | `{}` | 取走已触发的窗口结果与迟到侧输出（取后即清空） |
| GET | `/metrics` | — | 计数器/仪表盘 |
| POST | `/snapshot` | `{}` | 返回状态快照；若配了 `--snapshot-file` 同时落盘 |
| POST | `/restore` | — | 通过"带同一快照文件重启进程"完成恢复 |

### 事件格式

```json
{
  "id": "evt-1001",
  "eventTime": 1000,
  "type": "UPSERT",
  "key": "user:42",
  "payload": { "name": "ada", "score": 10 }
}
```

`type` 为 `UPSERT`（默认）或 `DELETE`（`DELETE` 可省略 `payload`）。`id` 必填，`eventTime` 必填，`key` 默认空串。

### 每条摄入结果的可观察标志

```jsonc
{
  "accepted": true,               // 首次存活出现（通过去重）
  "emitted": true,                // 是否真正产生可见输出（accepted 且未被墓碑压制、未因窗口迟到被丢弃）
  "duplicate": false,             // 同 id 在状态仍保留期间再次出现
  "payloadMismatch": false,       // 重复出现但载荷不同
  "eventTimeSkew": false,         // 同 id 的事件时间与首次不同
  "late": false,                  // 事件时间落后当前水位线
  "dedupUnverified": false,       // 关键：去重状态已释放，无法保证去重（窗口外旧重复）
  "suppressedByTombstone": false, // 被存活墓碑（DELETE）压制
  "tombstoneUncertain": false,    // 墓碑已过期，无法判断是否应压制
  "windowLateDropped": false,     // 所属窗口已触发，作为迟到事件进入侧输出
  "capacityEvicted": false        // 本次插入触发了容量淘汰（承诺范围因此收窄）
}
```

---

## 4. 核心语义与设计决策

### 4.1 水位线与"承诺范围"

启发式水位线（有界乱序）：

```
watermark = 已观察到的最大事件时间 - maxOutOfOrdernessMillis
```

也可由 `/watermark` 显式推进；水位线**单调不减**，任何回退请求都被拒绝并计入 `watermarkRegressions`。

- `eventTime < watermark` 的事件标记 `late`。
- 定时器的周期推进目标来自**观察到的事件时间**，与墙上时钟无关。

### 4.2 有界去重与状态释放

`DedupState` 对每个事件 id 保留一条记录（首次事件时间、观察到的最大事件时间、载荷哈希）。状态在两个维度上有界：

1. **时间释放**：当 `watermark - retentionMillis > 该 id 观察到的最大事件时间` 时释放；
2. **容量释放**：超过 `maxEntries` 时淘汰最大事件时间最小（平局取最早插入）的条目，并返回 `capacityEvicted`/被淘汰 id。

**承诺（promise）**：在状态仍保留期间（水位线尚未越过 `事件时间 + retentionMillis`，且容量未被打满），同 id 的重复**保证**被识别并抑制，绝不产生第二次输出。

**窗口外旧重复不保证**：一旦状态释放，同 id 旧数据再到达时，系统无法区分它到底是"真重复"还是"恰好复用了旧时间戳的新 id"。此时**不假装去重成功**，而是置 `dedupUnverified=true`，由调用方决定如何处理。

> 重要不变式：`retentionMillis >= windowSizeMillis + windowLatenessMillis`（墓碑 TTL 同理）。流水线构造时会强制校验。这保证**任何无法验证的重复，其所属窗口必然已经触发**——它只会进入迟到侧输出（`windowLateDropped`），**绝不会被再次折入某个仍打开的窗口**。因此**窗口聚合输出在承诺范围内不可能重复计数**。

### 4.3 事件 ID 与事件时间分离

- 去重身份只看 `id`。
- 同 id 重复但事件时间不同：仍判为重复，同时置 `eventTimeSkew=true`；若重复携带更晚的事件时间，会保守地延长该条目的存活期（按观察到的最大事件时间做 GC）。
- 载荷以 **canonical JSON（key 排序后）的 SHA-256** 比较：`{"x":1,"y":2}` 与 `{"y":2,"x":1}` 视为相同载荷。

### 4.4 墓碑（DELETE）语义

`TombstoneStore` 按业务 key 保留一组 DELETE 事件时间，按 TTL 与容量释放。压制规则按**事件时间**判定：

- 对 key 的 UPSERT，其事件时间为 `t`：若存在**存活的、删除时间 `d >= t`** 的墓碑，则该 UPSERT 被视为"删除前的旧数据乱序到达"，置 `suppressedByTombstone=true` 并抑制；
- UPSERT 事件时间 **晚于**删除时间（`t > d`）表示"删除后重新创建"，**正常通过**；
- 墓碑过期后到达的很旧 UPSERT，无法确认是否曾被删除，置 `tombstoneUncertain=true`（既不盲目压制也不盲目放行声明）。

> 选择事件时间而非到达顺序，是为了与水位线/窗口的事件时间语义一致（乱序到达不应改变最终事实）。若业务采用"到达顺序即最终顺序"的 KStream 风格，只需把 `ceiling(t)` 改为"是否存在任意存活墓碑"即可，存储结构不变。

### 4.5 时钟回退为什么安全

- 去重释放、墓碑过期、窗口触发全部只由**事件时间水位线**驱动；
- 处理时钟（`WallClock`/`ManualClock`）只用于周期性调度。`ManualClock` 可显式回退用于测试；`MonotonicClock` 包装器在底层 OS 时钟回退时保持不回退并计数 `rollbackCount`；
- 因此处理时钟即使向后跳，也不会让水位线倒退、不会让状态提前过期、不会造成重复输出。测试 AC-2 与 HTTP `/tick` 用例验证了这一点。

### 4.6 重启恢复

`POST /snapshot` 导出包含 `dedup / tombstones / windows / watermarkState` 的 JSON；用同一 `--snapshot-file` 重启进程会自动 `restore`。恢复后：去重判断、墓碑压制、未触发窗口的累加器、水位线全部延续。AC-3 与 HTTP 重启用例覆盖。

---

## 5. 四个验收场景

| 场景 | 位置 | 期望 |
|---|---|---|
| 同 id 不同载荷 | AC-1 / `examples/02-*.json` | 首次保留并输出；重复 `duplicate=true, payloadMismatch=true`，不覆盖首次载荷，窗口只计一次 |
| 时钟回退 | AC-2 / `/tick` 负值 | 处理时钟回退后去重仍生效；回退水位线被拒绝并计数 |
| 重启恢复 | AC-3 / HTTP restart | 快照后新进程恢复去重、墓碑、未触发窗口与水位线 |
| 极迟重复 | AC-4 / `examples/04~06` | 水位线越过保留期后状态释放；旧重复返回 `dedupUnverified=true`、`late=true`、`windowLateDropped=true`，不再输出 |

**承诺范围内无重复输出**由 AC-0（50 个唯一 id 各重投 4 次，逐条与窗口聚合均无重复）以及 DIFF-1/DIFF-2 差异测试保证。

---

## 6. 测试策略（54 个）

- **单元**：去重（释放边界、unverified、容量、skew、快照）、墓碑（事件时间压制、TTL、不确定、容量、快照）、窗口（分桶、触发时刻、迟到、墓碑窗口、快照）、水位线、时间/调度、JSON。
- **验收**：上表四个场景 + 承诺无重复 + 墓碑 + 载荷哈希 key 顺序无关。
- **HTTP**：黑盒启动服务，覆盖健康检查、摄入、去重/载荷不一致标志、400 校验、水位线、排水、指标、**落盘快照并新进程重启恢复**、时钟回退。
- **差异测试**：确定性伪随机流（重投携带**相同事件时间**），同时喂给有界流水线与无界精确参考实现，在承诺范围内逐事件比对去重结论，并对已触发窗口逐窗口比对 `(start,key,upsertCount,deleteCount,lastUpsertId)`。

精确参考实现位于 `src/test/java/com/example/dedup/tests/reference/`：
- `RefDedup`：无界 `HashSet`，记住所有 id，是"是否重复"的基准真值；
- `RefTumblingWindow`：永久保留全部窗口，按需产出精确聚合。

---

## 7. 配置项（CLI 标志）

| 标志 | 默认 | 含义 |
|---|---|---|
| `--port` | 8080 | 监听端口（测试用 0 = 随机端口） |
| `--mode` | manual | `manual`（时钟仅由 `/tick` 推进）或 `wall` |
| `--window-ms` | 10000 | 滚动窗口大小 |
| `--lateness-ms` | 5000 | 窗口允许迟到 |
| `--retention-ms` | 60000 | 去重保留期（须 ≥ window+lateness） |
| `--tombstone-ttl-ms` | 60000 | 墓碑 TTL（须 ≥ window+lateness） |
| `--max-entries` | 100000 | 去重条目容量上界 |
| `--max-tombstone-keys` | 100000 | 墓碑 key 容量上界 |
| `--out-of-orderness-ms` | 5000 | 水位线乱序宽限 |
| `--auto-watermark-ms` | 0（关闭） | wall 模式下定时推进水位线周期 |
| `--snapshot-file` | 无 | 快照落盘路径，启动时若存在则恢复 |

---

## 8. 范围与取舍

- 本项目**不做前端**，只提供 JSON HTTP 服务、库、样例与测试。
- 不接入 Kafka/Pulsar 等外部消息系统；数据通过 HTTP 请求进入。
- 容量淘汰是安全上界而非主回收路径：一旦发生容量淘汰，`capacityEvicted` 指标会暴露，语义上对应承诺范围收窄。
- 窗口为按键滚动窗口，聚合为"最后一次 UPSERT + upsert/delete 计数"的示例算子；去重/墓碑算子可独立于窗口使用。
- 实际运行的命令、结果与任何未通过项见 **`RUNLOG.md`**（如实记录）。
