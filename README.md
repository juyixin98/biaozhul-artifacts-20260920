# stream-match：流式模式匹配（A 后 B 且期间无 C）

纯 Java 后端项目（**零外部依赖、无前端、无外部消息系统**），实现：

1. **事件流计算库**：在按键（key）事件流上增量识别模式 **A 之后出现 B，且 A、B 之间没有 C**；
2. **JSON 输入输出服务**：基于 JDK 内置 HTTP Server 的有状态 REST 服务；
3. **小数据精确参考实现**：对完整事件集做暴力穷举，用于与流式引擎差分核对；
4. **可注入的时间与调度**：同时支持事件时间（watermark）与处理时间（注入时钟 + 定时器）；
5. **确定性重放**：把事件集排序后重算，并与参考实现逐对比较。

---

## 1. 环境与构建

- 需要 **JDK 21+**（仅用到 JDK 自带 API；编译运行不需要 Maven/Gradle/任何 jar）。

```bash
./build.sh          # 编译 src/ 与 tests/ -> build/classes
./run-tests.sh      # 运行全部自动化测试
./run-server.sh     # 启动 HTTP 服务（默认 127.0.0.1:8080）
```

等价的手工命令：

```bash
mkdir -p build/classes
find src tests -name '*.java' > build/sources.txt
javac -d build/classes @build/sources.txt
java -cp build/classes tests.RunTests
java -cp build/classes streammatch.service.ApiServer
```

服务启动配置（环境变量）：

| 变量 | 默认 | 说明 |
|------|------|------|
| `PORT` | `8080` | 监听端口（传 0 表示随机端口） |
| `BIND` | `127.0.0.1` | 绑定地址 |
| `MODE` | `EVENT_TIME` | `EVENT_TIME` 或 `PROCESSING_TIME` |
| `WINDOW_MILLIS` | `1000` | 时间窗口 W（毫秒，>0） |
| `ALLOWED_LATENESS_MILLIS` | `0` | 允许迟到 L（毫秒，≥0，仅事件时间） |
| `MATCH_POLICY` | `ALL_CANDIDATES` | `ALL_CANDIDATES` 或 `SKIP_PAST_LAST` |
| `LATE_POLICY` | `DROP` | `DROP` 或 `REJECT`（仅事件时间） |

---

## 2. 语义定义（重点：顺序、重叠、迟到）

### 2.1 全序：同时间事件的次序如何确定

每个事件在到达时获得单调递增的到达序号 `seq`。引擎内所有先后判断都基于全序：

- **事件时间模式**：按 `(timestamp, seq)` 排序——时间戳相同，**先到达者在前**；
- **处理时间模式**：按 `(到达时钟读数, seq)` 排序；同一毫秒内先到达者在前。
  事件 JSON 自带的 `timestamp` 字段在该模式下被忽略。

这意味着“同一毫秒/同一时刻”不会产生歧义：例如同刻的 `A, C, B` 按此到达序
喂入时，C 在全序上位于 A 之后、B 之前，故 A 被 C 打断、不匹配（见
[手算样例 例 5](docs/hand-computed.md)）。同刻 `A, B`（A 先到）合法匹配；
同刻 `B, A`（B 先到）不匹配。

### 2.2 模式判定

对同 key、全序上 `A` 先于 `B` 的一对事件，当且仅当：

- `0 ≤ tB − tA ≤ windowMillis`（**窗口两端闭区间**，tA==tB 也允许），并且
- 在全序的开区间 `(A, B)` 内不存在同 key 的 `C`。

输出包含**参与匹配的事件 ID**：`{key, aId, bId, aTimestamp, bTimestamp, ...}`。

### 2.3 C 打断

`C` 到达时，清理同 key 中**所有在全序上位于 C 之前**的等待中 A，并记录
`INTERRUPTED_BY_C`（含 `cId`）。位于 C 之后（更新的“早到”事件）不受影响。
已经超时的 A 记 `TIMEOUT`，不会被晚到的 C 改记为打断（watermark 先推进、先清理）。

### 2.4 重叠匹配策略（多个 A 候选怎么处理）

- `ALL_CANDIDATES`（默认）：输出**全部**合格 `(A,B)` 对。一个 B 可同时匹配多个 A；
  一个 A 在窗口内也可被多个 B 重复匹配。匹配后 A 继续等待直到窗口结束
  （以 `EXPIRED_AFTER_MATCH` 退场，仅统计用）。
- `SKIP_PAST_LAST`：非重叠（skip-till-next-match 风格）。B 只与等待中**最早**的合格 A
  产出一次，随后清空所有等待 A（其余记 `SKIPPED_AFTER_MATCH`）；每个 A 至多参与一次。

### 2.5 超时

- **事件时间**：`watermark = 已见最大事件时间 − allowedLateness`。
  当 `watermark > tA + W`（**严格**大于）时 A 超时。这保证 tB 恰好等于 `tA+W` 仍匹配。
- **处理时间**：注入的调度器在逻辑时刻 `tA + W + 1` 触发超时，与上面的严格判定一致。
  另外，每次事件到达也会按当前时钟做一次防御性清理，所以“定时器未触发但时钟已跳过”
  不会造成错误匹配。

### 2.6 迟到策略（仅事件时间）

事件 `timestamp < watermark` 即为迟到：

- `DROP`（默认）：静默丢弃，不改变任何状态、不推进 watermark，ID 记入响应 `lateDropped`；
  已发出的匹配不撤回（无撤回/retraction 语义）。
- `REJECT`：整批请求被拒绝（HTTP 422），且为**原子**拒绝——批内任一事件迟到，
  则同批所有事件都不生效。

被 DROP 的事件不进入正式状态，也不进入服务默认的重放集合（要补算请在 `/replay`
请求里显式提供完整事件集）。

### 2.7 重放

`/replay` 将事件按 `(timestamp, 接受顺序)` 排序后重算“无迟到假设”下的理想结果，
同时跑流式引擎和暴力参考实现，并比对两者匹配三元组集合 `(key,aId,bId)` 是否一致
（`consistent: true/false`）。在线（受 watermark 限制）与重放（理想序）结果可以不同，
服务如实返回，不做静默修正。处理时间模式不支持重放（其语义依赖墙钟到达，无法从历史重建）。

---

## 3. HTTP API

| 方法 路径 | 作用 |
|-----------|------|
| `GET /health` | 健康检查 |
| `GET /config` | 查看配置 |
| `POST /config` | 修改配置（`mode` 运行期不可变，违反返回 422） |
| `POST /events` | 提交一批事件，返回增量匹配/移除/迟到 |
| `GET /matches` | 累计匹配列表 |
| `GET /state` | 完整状态（配置、累计匹配、等待中的 A、watermark） |
| `POST /watermark` | 事件时间模式手动推进 watermark（`{"watermarkMillis":101}`） |
| `POST /replay` | 确定性重放 + 参考实现对照（可选 `events`、`config`） |
| `POST /reset` | 清空状态（可携带新配置字段） |

事件对象：`{"id":"A1","key":"k","type":"A","timestamp":0}`，`type ∈ {A,B,C}`。
错误统一为 `{"error": "CODE", "message": "..."}`：请求体/字段问题为 400，
语义问题（重复 ID、迟到 REJECT、模式不可变等）为 422。

### curl 示例

```bash
# 1) 多 A 候选：一个 B 同时匹配 A1、A2
curl -s localhost:8080/events -H 'Content-Type: application/json' \
  -d @samples/01-multiple-a.json

# 2) C 打断
curl -s localhost:8080/reset -d '{}'
curl -s localhost:8080/events -H 'Content-Type: application/json' \
  -d @samples/02-c-interrupts.json

# 3) 超时 / 同刻次序 / 边界 / 多 key
curl -s localhost:8080/events -d @samples/03-timeout.json
curl -s localhost:8080/state

# 4) 乱序迟到 + 用完整事件集重放补算
curl -s localhost:8080/reset -d '{}'
curl -s localhost:8080/events -d '{"events":[
  {"id":"A1","key":"k","type":"A","timestamp":0},
  {"id":"B2","key":"k","type":"B","timestamp":80}]}'
curl -s localhost:8080/events -d @samples/07-late-arrival.json   # A3@10 迟到被 DROP
curl -s localhost:8080/replay -d @samples/08-replay-full-set.json # 理想序补回 A3
```

更多现成请求体见 [`samples/`](samples/)。逐步手算推演见
[`docs/hand-computed.md`](docs/hand-computed.md)。

---

## 4. 代码结构

```
src/streammatch/
  model/        领域模型：Event / Match / EngineConfig / EngineMode /
                MatchPolicy / LatePolicy / RemovedA / EngineResult
  time/         可注入时间：Clock（System/Manual）、TaskScheduler（Wall/Manual）
  engine/       PatternMatcher 接口 + StreamMatcher 双模式流式引擎
  reference/    NaiveReferenceMatcher：小数据暴力穷举参考实现
  json/         Json：零依赖 JSON 解析/序列化
  service/      MatchingService（校验、状态、重放）+ ApiServer（HTTP）
tests/          裸 JDK 自动化测试 + RunTests 运行器
docs/           手算样例（验收依据）
samples/        请求样例 JSON
```

### 作为库直接使用

```java
EngineConfig cfg = new EngineConfig(
        EngineMode.EVENT_TIME, 100,
        MatchPolicy.ALL_CANDIDATES, 0L, LatePolicy.DROP);
PatternMatcher m = StreamMatcher.eventTime(cfg);

EngineResult r = m.process(List.of(
        new Event("A1", "k", Event.A, 0, 0),
        new Event("B2", "k", Event.B, 50, 1)));
r.matches().forEach(x -> System.out.println(x.aId() + " -> " + x.bId()));
```

处理时间模式注入手动时钟/调度器（测试中确定地控制“超时 vs 事件”竞速）：

```java
ManualClock clock = new ManualClock(0);
ManualScheduler scheduler = new ManualScheduler();
PatternMatcher m = StreamMatcher.processingTime(cfg, clock, scheduler);
m.processOne(new Event("A1", "k", Event.A, 0, 0));
scheduler.advanceTime(101);   // 触发窗口超时
```

---

## 5. 测试

| 测试类 | 覆盖内容 |
|--------|----------|
| `HandComputedTest` | 15 个手算短序列：多 A 候选、C 打断、超时、同刻次序、闭区间边界、乱序迟到、重放、REJECT、多 key |
| `DifferentialTest` | 800 个随机序列 ×2 策略：流式引擎与暴力参考的匹配对集合 + 移除结局差分 |
| `PolicyAndLatenessTest` | SKIP_PAST_LAST、allowedLateness 临界值 |
| `ProcessingTimeTest` | 注入时钟/调度器：窗口、定时器超时、边界、C 打断、同刻批内次序 |
| `JsonTest` | JSON 解析/序列化往返、转义、畸形输入 |
| `ServiceTest` | 校验、错误码、重复 ID、重放一致性、reset |
| `HttpServerTest` | 真实启动 HTTP 服务走全路由的端到端测试 |

```bash
$ ./run-tests.sh
[PASS] HandComputed          (33 项断言)
[PASS] Differential          (随机差分场景 800 个)
...
全部测试通过：7 个测试类
```

> 说明：本项目仅为纯后端库 + JSON 服务，**不含任何前端/页面**。
