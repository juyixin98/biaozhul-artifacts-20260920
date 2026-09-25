# 事件时间会话窗口（Event-Time Session Windows）— 纯后端

零外部依赖的 Java 事件流计算库 + JSON 服务：按 key 维护**会话窗口**（session
window），支持乱序事件、**迟到事件桥接已封存窗口**、水位线 + 允许迟到共同决定
封窗与状态清理；结果以明确的 **RETRACT / ADD** 变更记录表达。

- 仅需 JDK（开发环境：OpenJDK 21），不使用 Maven/Gradle，不引入任何第三方库；
- 时间与调度可注入（`TimerService`），测试完全确定，无线程睡眠；
- 提供小数据量的离线"完整分组"参考实现，自动化对照流式结果；
- JSON 输入输出：命令行与内置 HTTP 服务两种入口，无外部消息系统。

## 语义定义（所有边界均为闭区间）

- 会话间隔 `gap`：同一 key 相邻事件间隔 `<= gap` 归入同一会话；**间隔恰好等于
  gap 也合并**，间隔 `gap+1` 才拆分。
- 水位线 `W`：事件时间 `t >= W` 为准时，`t < W` 为迟到。
- 允许迟到 `L`：仅 `t >= W - L` 的迟到事件被接受；更早的事件丢弃并计入
  `droppedLateEvents`。门控也是闭区间：`t == W-L` 仍接受。
- 封窗：窗口 `[s,e]` 在 `W >= e + gap` 时封存，发出一条 `ADD`。
- 迟到桥接：被接受的迟到事件若同时触及两个窗口（至少一个已封存），将其及传递链
  上的窗口合并为一个新窗口——对每个被吞掉的已封存窗口发 `RETRACT`，再对合并窗口
  发 `ADD`（若已过封窗点则立即封存，否则作为活动窗口等待）。
- 状态清理：已封存窗口保留到 `W > e + gap + L`（严格大于，保证允许迟到的最后一个
  时刻状态仍在），随后清除且不产生结果。

## 目录结构

```
src/sessions/
  model/      Event, Window, ResultKind, WindowUpdate
  agg/        AggregateFunction, CountAggregate, SumAggregate
  time/       TimerService(接口), SimTimerService(确定性实现)
  op/         SessionWindowOperator（核心算子）
  reference/  ReferenceSessions(离线完整分组), ChangelogFold(变更折叠为结果表)
  json/       Json(解析), JsonWriter(生成)
  service/    SessionRequestRunner, SessionHttpServer, BadRequestException
  Main.java   CLI 入口
test/sessions/
  testing/    零依赖迷你测试框架
  *Test.java  37 个测试用例
samples/      请求样例 4 个（及 out/ 下实际响应）
build.sh / test.sh / run-samples.sh
```

## 构建与测试（实际验证过的命令）

```bash
./build.sh        # javac 编译主代码与测试代码到 build/
./test.sh         # 运行全部 37 个测试
./run-samples.sh  # 离线运行 samples/*.json，响应写入 samples/out/
```

## 命令行使用

```bash
# 离线运行请求文件
java -cp build sessions.Main run samples/01-bridge-unordered.json
# 从标准输入
cat samples/02-gap-equality.json | java -cp build sessions.Main run -
```

## HTTP 服务

```bash
java -cp build sessions.Main serve 8080     # 端口可改
curl -s http://localhost:8080/health
curl -s -X POST http://localhost:8080/sessions/run \
     -H 'Content-Type: application/json' \
     -d @samples/01-bridge-unordered.json
```

`GET /health` 返回 `{"status":"ok"}`；`POST /sessions/run` 运行请求。
非法 JSON/参数返回 400，错误方法返回 405。服务本身无状态，每个请求独立计算。

## 请求格式

```json
{
  "gap": 10,
  "allowedLateness": 20,
  "aggregate": "SUM",
  "watermarkStrategy": {"type": "NONE"},
  "input": [
    {"key": "a", "timestamp": 1, "value": 10},
    {"watermark": 15}
  ],
  "finish": false
}
```

- `aggregate`：`COUNT`（默认，忽略 value 计数）或 `SUM`。
- `watermarkStrategy`：
  - `{"type":"NONE"}`：只由 input 中的 `{"watermark": N}` 项手动推进；
  - `{"type":"BOUNDED","maxOutOfOrderness":K}`：每个事件后自动把水位线推进到
    `事件时间 - K`（可与手动 watermark 项混用，单调不减）。
- `finish`（默认 `true`）：结尾把水位线推到正无穷，封存全部活动窗口、清空全部
  状态。设为 `false` 可在响应的 stats 中观察尚未清理的窗口。

## 响应格式

- `changelog`：有序变更记录，字段 `seq/kind(ADD|RETRACT)/key/start/end/aggregate/
  watermark`。撤回与新增成对出现，从不对旧结果做就地修改。
- `finalResults`：把 changelog 折叠（RETRACT 撤销对应 ADD）后的最终结果表。
- `stats`：接收/丢弃事件数、当前仍保留的活动/已封存窗口数（**状态清理检查**）、
  最终水位线。

## 验收场景与样例对应

| 验收点 | 样例 | 测试 |
|---|---|---|
| 乱序 + 迟到桥接两个窗口（RETRACT×2 + ADD） | `01-bridge-unordered.json` | `lateEventBridgesTwoSealedWindows`、`fixedScenarioMatchesReference` |
| 边界间隔相等（==gap 合并，gap+1 拆分） | `02-gap-equality.json` | `gapEqualityBoundaries`、`gapPlusOneSeparates` |
| 封窗后迟到（门控接受/丢弃、状态清理） | `03-late-after-seal.json` | `lateWithinAllowedLatenessAcceptedAtBoundary`、`lateAfterPurgeDropped` |
| 多 key 隔离、BOUNDED 水位线 | `04-multi-key-bounded.json` | `perKeyIsolation`、`boundedStrategy` |
| 流式结果 == 离线完整分组（600 组随机乱序） | — | `ReferenceEquivalenceTest` |
| 状态清理（finish 后 retained=0） | 全部样例 stats | 各测试 `totalRetainedWindowCount` 断言 |

实际运行命令与结果见 [RUNS.md](RUNS.md)。
