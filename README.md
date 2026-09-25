# 流式模式匹配（Streaming CEP）纯后端服务

在按键事件流上检测模式 **“出现 A，之后出现 B，且 A、B 之间没有 C”**（A→then→B, no C in between），
带可配置时间窗口，并输出**匹配所用的事件 ID**。

纯 Java（JDK 21 验证）实现，**零外部依赖**：事件流计算库 + JSON 输入输出的 HTTP 服务；
时间与调度可注入；另提供一个**小数据精确参考实现**用于交叉验证。无前端、无外部消息系统。

---

## 1. 快速开始

需要 JDK（在 **OpenJDK 21** 上开发验证；语法用到 21 的 switch/record，建议 21+）。
无需 Maven/Gradle，无需联网拉依赖。

```bash
./build.sh            # 编译主代码 + 测试到 build/classes
./build.sh test       # 编译并运行全部 58 个自动化测试
./run.sh 8080         # 启动 HTTP 服务（默认端口 8080）

# 另开一个终端，运行 9 个请求样例 + 会话/重放流程
./samples/curl-demo.sh http://127.0.0.1:8080
```

或手动：

```bash
javac -d build/classes @<(find src/main/java -name '*.java')
java -cp build/classes cep.service.Main 8080
curl -s http://127.0.0.1:8080/healthz
```

---

## 2. 模式语义（明确约定，验收以此为准）

### 2.1 事件与全局全序

每个事件 = `{id, key, timestamp, seq?}`。

- **事件时间** `timestamp`（毫秒，long）：所有匹配与超时只依据它，与墙钟无关。
- **同刻次序** `seq`：同一 `timestamp` 下的先后。未给 `seq` 时，引擎按**到达顺序**补 `0..n-1`。
- 全局全序为 **`(timestamp 升序, seq 升序)`**。记 `x ≺ y` 表示 x 在该全序上严格早于 y。
- “A 在 B 之前”“C 位于 A、B 之间”全部按此全序判定，因此同一毫秒的事件也有确定先后。
- 事件 `id` 必须唯一；重复 id 被忽略并计入 `stats.duplicates`。`(timestamp,seq)` 冲突会被拒绝。

### 2.2 匹配条件

对一个活跃候选 A 和一个到达的 B：

1. `A ≺ B`（严格在全序之前；同刻按 seq，**B 不会匹配“未来”的 A**）；
2. `0 ≤ B.timestamp − A.timestamp ≤ windowMs`（**窗口边界包含**，dt 恰好等于窗口也算）；
3. A 仍活跃：未被先前的 B 匹配/消费、未被 C 杀掉、未超时；
4. “期间无 C”：A 之后到达过的任何 C，只要 `A ≺ C`（全序严格在 A 之后），都会**立即杀掉 A**。
   - C 只杀它**之前**的 A；`C ≺ A'` 的 A′ 不受影响（见手算场景 3）。
   - 被杀的 A **不产生匹配也不产生超时**，计入 `stats.cKilled`。
   - 等价表述：最终匹配 `(A,B)` 的开区间 `(A,B)` 内不存在全序位于其间的 C。

### 2.3 多 A 候选 / 重叠匹配策略 `policy`

一个 B 到达时窗口内可能有多个仍有效的 A。四种可配置策略：

| 策略 | 行为 |
|---|---|
| `ALL_PAIRS`（默认） | B 与窗口内**每一个**有效 A 各产出一个匹配（重叠匹配全部保留），按 A 全序输出 |
| `EARLIEST_A` | B 只与全序**最早**的有效 A 匹配 |
| `LATEST_A` | B 只与全序**最晚**的有效 A 匹配 |
| `NON_OVERLAPPING` | 贪婪地只与最早 A 匹配，并**消费**区间 `[A..B]` 内所有 A（它们不再匹配、也不超时），从而匹配互不重叠 |

通用规则：**任何策略下，一个 A 最多被匹配一次**；被匹配/消费后其窗口定时器取消，不再发超时。

### 2.4 时间、定时器与超时

- 每个进入的 A 注册一个窗口到期定时器，`deadline = A.timestamp + windowMs`（饱和加法）。
- **超时为半开判定**：仅当水位推进到 `wm > deadline` 且 A 仍未匹配/未被杀时，发一个超时。
  `wm == deadline` **不**超时——这样 B 恰在 `deadline` 时刻到达仍能匹配（边界包含）。
- 引擎把“缓冲事件”和“到期定时器”放在**同一条事件时间轴**上归并；二者同一时刻时
  **先处理事件、再触发定时器**（这是上面边界包含的实现保证）。
- 多个超时按 `(deadline 升序, A 全序升序)` 发射。可用 `emitTimeouts:false` 关闭超时输出。
- `flush()` 把水位推到 `+∞`（`Long.MAX_VALUE`），排空所有剩余匹配与超时。

### 2.5 Watermark（水位）、乱序与迟到

- **自动水位**：`wm = maxObservedEventTime − outOfOrderBound`（默认 bound=0，即输入按事件时间有序）。
  设置 `outOfOrderBound` 后，引擎会缓冲“晚到但未越过水位”的事件，按事件时间排出，从而容忍有限乱序。
- **手动水位**：可随时注入（只增不减；取手动值与自动值的较大者）。
- **迟到判定**：事件满足 `timestamp + allowedLateness < wm`（严格小于，边界不算迟到）。
  - `latePolicy = DROP`（默认）：丢弃，计入 `stats.droppedLate`。
  - `latePolicy = ACCEPT`：进入**迟到尽力车道（best-effort）**，按到达顺序立即处理：
    可与现存有效 A/B 产生**新**结果并在输出中标记 `"late": true`；
    **已发出的匹配/超时一律不撤回、不修改**；迟到 C 仍会杀掉全序在它之前的现存活跃 A；
    迟到 A 若窗口已完全落在水位之前则立即发一个 late 超时。

> 结果列表 `matches` / `timeouts` 只追加、不更改，契合流式语义与审计需求。

---

## 3. 手算短序列对照（验收核心）

下列推演均可在自动化测试 `HandComputedScenarioTest` 与 `samples/` 中复现，输出含事件 ID。

**场景 1 —— 多个 A 候选 + C 打断 + 超时**，窗口 1000，ALL_PAIRS：
```
A1@0  A2@100  C1@400  A3@600  B1@1000  A4@5000(flush)
```
- t=400 的 C1 杀掉全序在它之前的 A1、A2（cKilled=2，不超时）；
- B1@1000 时窗口内有效 A 只有 A3@600（dt=400）→ 匹配 **A3→B1**；
- A4 等不到 B，flush 超时。
- 结果：`matches=[A3->B1]`，`timeouts=[A4]`，`cKilled=2`。样例 `samples/01-*.json`。

**场景 2 —— 重叠匹配与策略对比**，事件 `A1@0 A2@100 A3@200 B1@500`，窗口 1000：
- ALL_PAIRS：`[A1->B1, A2->B1, A3->B1]`（一个 B 对三个 A，按 A 全序）— 样例 `02`；
- EARLIEST_A：仅 `A1->B1`；LATEST_A：仅 `A3->B1`；
- NON_OVERLAPPING：`A1->B1`，且 A2、A3 被消费（不超时）— 样例 `09`。

**场景 3 —— C 的位置**：`A1@0 C@50 A2@100 B@800`
- C 只杀它之前的 A1；A2 在 C 之后，照常 `A2->B1`，cKilled=1。

**场景 4 —— 窗口边界**：
- `A1@0 B@1000`：dt=1000 **包含** → 匹配；
- `A2@5000 B@6001`：dt=1001 → 不匹配，A2 超时。样例 `05`。

**场景 5 —— 同一毫秒的次序**（全序 `(ts,seq)`）：
- 到达序 `A,B,C`（同 ts=100）：A≺B≺C → 匹配 **a→b**，C 在 B 后无可杀之 A。样例 `03`；
- 到达序 `A,C,B`：C 杀掉 A → 无匹配（cKilled=1）。样例 `04`；
- 到达序 `B,A`：B 早于 A，B 不匹配未来的 A，A 最终超时；
- 乱序到达时可用显式 `seq` 指定全序（测试“乱序到达但显式seq决定全序”）。

**场景 6 —— 乱序、迟到与重放**：
- bound=500：`B@600` 先到、`A@500` 晚到但未越过水位（500 ≥ wm=100），缓冲后按事件时间排出 `A->B`；
  `A@99` 越过水位（99<100）→ DROP。样例 `06`；
- ACCEPT：`A@0 A@2000(使wm=2000) A_late@1500 B@2200`，A_late 迟到但窗口未过，
  B@2200 补出 `A_late->B1`（`late=true`）与 `A2->B1`，A1 超时。样例 `07`；
- **重放**：会话 `reset()` 后按原始喂入日志重新喂入，匹配/超时结果逐字节一致（见会话测试）。

**场景 7 —— 重复 ID**：重复 id 被忽略，计 `duplicates`，不参与匹配。

此外 `samples/08-*.json` 带 `"useReference": true`，服务同时跑独立的小数据精确参考实现，
返回 `agreesWithReference: true`。自动化测试里还有 **3000 轮固定种子随机差分测试**
（2000 轮引擎 vs 参考实现 × 4 策略；1000 轮引擎 vs 独立 O(n²) 谓词定义）。

---

## 4. HTTP/JSON API

| 方法 & 路径 | 说明 |
|---|---|
| `POST /evaluate` | 一次性、确定性评估（无状态，最常用） |
| `POST /api/sessions` | 创建有状态会话，返回 `sessionId` |
| `POST /api/sessions/{id}/events` | 追加单个事件对象或 `{events:[...]}` |
| `POST /api/sessions/{id}/watermark?watermark=N` | 手动注入水位（也可用 body `{"watermark":N}`） |
| `GET  /api/sessions/{id}` | 当前快照（不推进时间） |
| `POST /api/sessions/{id}/flush` | 终局推进，返回全部结果；之后拒绝追加（409） |
| `POST /api/sessions/{id}/replay` | `reset` + 按原始喂入日志重放 + flush |
| `GET  /healthz` | 健康检查 |

错误统一为 `{"error":true,"status":4xx,"message":...}`（400 参数/JSON 错误，404 会话不存在，409 已 flush）。

### 4.1 `POST /evaluate` 请求

```json
{
  "pattern": {
    "a": "A", "b": "B", "c": "C",
    "windowMs": 1000,
    "policy": "ALL_PAIRS",
    "latePolicy": "DROP",
    "outOfOrderBound": 0,
    "allowedLateness": 0,
    "emitTimeouts": true
  },
  "events": [
    {"id": "A1", "key": "A", "timestamp": 0},
    {"id": "B1", "key": "B", "timestamp": 1000}
  ],
  "useReference": false
}
```

`pattern` 字段也可平铺到顶层。配置项：

| 字段 | 默认 | 含义 |
|---|---|---|
| `a`/`b`/`c` | 必填（互不相同） | 三个按键名 |
| `windowMs` | 必填，>0 | 时间窗口，边界包含 |
| `policy` | `ALL_PAIRS` | `ALL_PAIRS`/`EARLIEST_A`/`LATEST_A`/`NON_OVERLAPPING` |
| `latePolicy` | `DROP` | `DROP`/`ACCEPT` |
| `outOfOrderBound` | 0 | 乱序容忍度，wm=maxTs−bound |
| `allowedLateness` | 0 | 迟到宽限；ts+allowedLeness<wm 判迟到 |
| `emitTimeouts` | true | 是否输出超时 |

### 4.2 响应

```json
{
  "matches": [
    {"aId":"A1","bId":"B1","aTimestamp":0,"bTimestamp":1000,
     "durationMs":1000,"matchedAtWatermark":1000,"late":false}
  ],
  "timeouts": [
    {"aId":"A9","aTimestamp":5000,"deadline":6000,"late":false}
  ],
  "watermark": 9223372036854775807,
  "flushed": true,
  "stats": {"received":2,"processed":2,"duplicates":0,"droppedLate":0,
            "acceptedLate":0,"matches":1,"timeouts":1,"cKilled":0,"watermarks":1},
  "mode": "streaming"
}
```

`matches[].aId/bId` 即**匹配所用事件 ID**；`watermark=9223372036854775807`（=Long.MAX_VALUE）表示 flush 后的终局“+∞”水位。

---

## 5. 代码结构

```
src/main/java/cep/
  model/      KeyEvent, Match, Timeout, EngineResult, EngineStats
  config/     PatternConfig, MatchPolicy, LatePolicy
  time/       Clock(可注入时钟), VirtualClock,
              Scheduler / HeapScheduler（可注入事件时间定时器，最小堆）
  pattern/    PatternEngine（流式增量引擎，watermark/迟到/四策略）
              BruteForceMatcher + ReferenceResult（小数据精确参考实现，独立算法）
  json/       JsonValue, JsonParser, JsonWriter（零依赖迷你 JSON）
  service/    ApiService（JSON<->引擎，纯函数）, SessionStore（会话+重放）,
              HttpApiServer（JDK com.sun.net.httpserver）, Main
src/test/java/cep/
  test/       迷你测试框架 + AllTests 入口
  json/       JSON 往返测试
  pattern/    手算场景 / 策略 / 全序超时 / 乱序迟到 / 随机差分与参考等价
  service/    HTTP 端到端测试（真实起服务发请求）
samples/      9 个请求样例、curl-demo.sh、responses/（实际响应留档）
build.sh run.sh   构建/运行脚本
RUNLOG.md     实际运行命令与结果的如实记录
```

### 可注入的时间与调度

- `Clock`：`Clock.system()`（生产）/ `VirtualClock`（测试手动推进）。
- `Scheduler`：`HeapScheduler` 用最小堆管理事件时间定时器，时间只能前进、同 deadline 按注册序触发、
  支持取消；引擎据此与缓冲事件归并。测试在不 sleep、不依赖墙钟的情况下确定性驱动时间。
- 流式引擎本身的所有判定只依赖**事件时间**，因此给定相同输入，结果完全确定、可重放。

### 参考实现

`BruteForceMatcher` 用最直白的排序 + 扫描 + O(n²) 枚举独立实现同一语义（无缓冲、无迟到概念，
对应有序无迟到基线），与流式引擎算法互不共用。`/evaluate` 的 `useReference` 与随机差分测试都用它交叉核对。

---

## 6. 测试

`./build.sh test`（或 `java -cp build/classes cep.test.AllTests`）运行 **58 个测试**：

- JSON 解析/写出（含中文/转义/非法输入）；
- 7 类手算短序列（多 A、C 打断、窗口边界、同刻次序、乱序、迟到、重放、重复 ID）；
- 四种匹配策略横向对比与边界；
- 全序、事件/定时器同刻次序、手动水位、虚拟时钟与调度器；
- 乱序 bound、迟到宽限、DROP/ACCEPT 各分支；
- **3000 轮固定种子随机差分**（引擎 vs 参考实现 / 独立谓词定义）；
- 真实 HTTP 端到端（健康检查、/evaluate、会话生命周期、重放一致、4xx 错误）。

测试框架是自研的迷你断言/运行器（`cep.test.TestFramework`），同样零依赖。

---

## 7. 设计取舍与边界

- 选 **JDK 内置 HttpServer + 手写 JSON**，以满足“无外部依赖/离线可复现”；适合作为库与小服务，
  未做高并发调优、鉴权与持久化（会话存内存，重启即失；重放日志只在进程内）。
- “期间无 C”实现为 **C 到达即杀掉其之前所有活跃 A**（negated class 的标准 CEP 处理），
  因此 C 不需要“回看”已匹配的历史。
- **A 匹配一次即消耗**：ALL_PAIRS 的“重叠”是多个 A 共享同一个 B，而非一个 A 被多个 B 重复使用。
- 时间单位为毫秒 long；对 `ts+window` 等做了饱和处理；终局水位用 `Long.MAX_VALUE` 表示 +∞。
- 未实现前端（按要求）。所有语义都可在库层直接调用 `PatternEngine` 验证，无需经过 HTTP。
