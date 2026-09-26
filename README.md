# Hybrid Logical Clock (HLC) — 纯后端时间/版本规则服务

用 Java 实现的 **Hybrid Logical Clock（HLC，混合逻辑时钟）** 计算后端。它在不依赖
中心化时间服务器、不假设物理时钟完全可靠的前提下，为分布式事件产生**因果一致**的
时间戳 `(l, c)`，并提供 JSON 输入输出、HTTP API、命令行、本地持久化恢复以及自动化测试。

- **零外部依赖**：仅使用 JDK 21 内置能力（`com.sun.net.httpserver`、`java.time`、
  `java.net.http`），不引入 Maven/Gradle 或第三方库。
- **本地固定测试数据**：所有演示与验收场景都用固定的虚拟物理时间，输出可逐字节复现。
- **记录时区数据库（tzdata/IANA）版本**：写入健康接口、仿真响应与持久化文件。
- **明确的非目标**：本项目**不是**预约系统、**不是**考勤/打卡系统，也**没有前端**；
  不读取真实业务数据库，不做用户认证。

---

## 1. 它解决什么问题

HLC（Kulkarni, Demirbas, Gorda, Madappa, *Logical Physical Clocks*, 2014）为每个事件
产生时间戳 `(l, c)`：

- `l`：逻辑化的物理时间（本实现单位为**微秒**），单调不减；
- `c`：当物理时间不前进时使用的逻辑计数，用于在同一 `l` 内打破并列。

它满足关键性质：

> **若事件 a happens-before 事件 b（同一节点的先后，或消息的发送→接收，传递闭包），
> 则一定有 `ts(a) < ts(b)`（按 `(l,c)` 字典序）。**

但**反过来不成立**：`ts(a) < ts(b)` **不能**推出 a 因果先于 b——并发事件之间没有
因果关系，时间戳仍然会给它们一个任意的先后。本项目用专门的测试和 API 字段同时证明
这两点（见 [第 8 节](#8-验收因果性如何被验证)）。

---

## 2. 环境要求

| 组件 | 版本 | 说明 |
|------|------|------|
| JDK | 21+ | 已在 `OpenJDK 21.0.12` 上实际编译运行 |
| 操作系统 | Linux/macOS | 脚本为 bash；Windows 可直接用 `javac/java` 命令 |
| 第三方依赖 | 无 | 不需要联网、不需要包管理器 |

查看版本与 tzdb：

```bash
java -version
java -cp build/classes hlc.cli.Main info
```

---

## 3. 目录结构

```
.
├── src/main/java/hlc
│   ├── PhysicalClock.java                 # 物理时钟接口 + 固定/虚拟时钟
│   ├── HLCTimestamp.java                  # 不可变时间戳 (l,c)，比较与解析
│   ├── HLCClock.java                      # HLC 核心：local/send/receive/restore
│   ├── LogicalCounterOverflowException.java
│   ├── HLCException.java
│   ├── HLCFileStore.java                  # 原子持久化（properties + fsync + rename）
│   ├── CausalityEngine.java               # 虚拟时钟 + 消息交错 + 因果闭包校验
│   ├── TimeZoneInfo.java                  # tzdata 版本与默认时区
│   ├── Json.java                          # 零依赖 JSON 解析/序列化
│   ├── cli/{Main,DemoScenarios}.java      # 命令行与内置固定数据演示
│   └── server/{ApiService,ScenarioService,HLCHttpServer}.java
├── src/test/java/hlc                      # 35 个自动化测试（内置迷你 runner）
├── samples/*.json                         # 固定请求样例
├── scripts/{build,run-tests,demo-http}.sh
├── data/                                  # 运行期持久化文件（git 忽略）
├── README.md
└── RUNLOG.md                              # 如实记录的命令、结果、踩坑与修复
```

---

## 4. 构建与测试

```bash
# 编译主代码 + 测试代码到 build/classes
scripts/build.sh

# 运行全部自动化测试（编译 + 运行，失败时退出码非 0）
scripts/run-tests.sh
```

内置测试 runner 是一个约 100 行的反射小框架（`src/test/java/hlc/TestRunner.java`），
因此在完全离线环境也能跑测试。当前结果：**35/35 全部通过**，详见 `RUNLOG.md`。

---

## 5. 三种使用方式

### 5.1 内置固定数据演示（最快看到全部特性）

```bash
scripts/build.sh
java -cp build/classes hlc.cli.Main demo
```

输出 5 个确定性场景：三节点带时钟偏差的消息交错、物理时钟回退、并发事件的时间戳次序、
逻辑计数溢出处理、持久化与跨重启恢复。

### 5.2 HTTP JSON 服务

```bash
# 自己指定端口（若为 0 则由操作系统分配空闲端口，适合端口被占用的共享机器）
java -Dport=8080 -DstateFile=data/hlc-state.properties \
     -cp build/classes hlc.server.HLCHttpServer
# 也可用环境变量 HLC_PORT / HLC_STATE
```

一键可复现端到端演示（自动构建、起服务、用 `samples/` 发请求、落盘、停服务）：

```bash
scripts/demo-http.sh
```

### 5.3 命令行（每次调用自动 load → 操作 → 原子 save）

```bash
java -cp build/classes hlc.cli.Main nodes create --node alice --initial-pt 1000000 \
     --state data/cli-state.properties
java -cp build/classes hlc.cli.Main tick --node alice --pt 1000000 --state data/cli-state.properties
java -cp build/classes hlc.cli.Main send --node alice --pt 1000000 --state data/cli-state.properties
java -cp build/classes hlc.cli.Main receive --node bob --pt 950000 --message 1000000:1 \
     --state data/cli-state.properties
java -cp build/classes hlc.cli.Main snapshot --node bob --state data/cli-state.properties
java -cp build/classes hlc.cli.Main simulate samples/07-simulate-concurrent.json
```

---

## 6. HTTP API 参考

所有请求/响应均为 `application/json; charset=utf-8`。除 `/health`、`/info` 和
`GET /nodes`、`GET /snapshot` 外均为 POST。时间戳线上格式为字符串 `"<l>:<c>"`。

| 方法 & 路径 | 请求体（关键字段） | 说明 |
|---|---|---|
| `GET /health` | — | 存活检查 + `tzdb` 版本 |
| `GET /info` | — | 算法说明、时间戳格式、tzdb、默认时区 |
| `POST /nodes` | `node`, `initialPhysicalMicros?` | 创建虚拟节点 |
| `GET /nodes` | — | 列出节点及当前时间戳 |
| `POST /tick` | `node`, `physicalMicros` | 本地事件 |
| `POST /send` | `node`, `physicalMicros` | 发送事件，响应内含可传递的 `message` |
| `POST /receive` | `node`, `physicalMicros`, `message` | 接收/合并消息时间戳 |
| `GET /snapshot?node=` | — | 读取当前时间戳（不推进） |
| `POST /restore` | `node`, `state` | 从持久化状态恢复（节点可尚不存在） |
| `POST /persist/save` | 可空 | 原子写入全部节点时钟 |
| `POST /persist/load` | 可空 | 从状态文件重载 |
| `POST /simulate` | `name?`, `events[]` | 跑固定交错场景并校验因果性 |

`message`/`state` 既接受时间戳字符串 `"1000000:1"`，也接受对象
`{"hlc":"1000000:1"}` 或 `{"l":1000000,"c":1}`。

`events[]` 中每个事件：

```json
{ "label": "b1", "node": "bob", "type": "RECV",
  "physicalMicros": 1000100, "from": "a1" }
```

- `type`：`LOCAL` / `SEND` / `RECV`；
- `physicalMicros`：该事件发生时此节点认定的物理时间（**显式给定，可回退**）；
- `from`：仅 `RECV` 需要，指向一个此前已 `SEND` 的事件 `label`。

### 错误响应

| HTTP | code | 触发 |
|---|---|---|
| 400 | `INVALID_REQUEST` | JSON 非法、字段缺失、未知节点、引用不存在的消息等 |
| 507 | `LOGICAL_COUNTER_OVERFLOW` | 逻辑计数到达上限，无法安全产生下一个时间戳 |
| 500 | `INTERNAL_ERROR` | 其它未预期错误 |

### 典型响应片段

发送：

```json
{ "node": "alice", "hlc": "1000000:1", "l": 1000000, "c": 1,
  "message": { "from": "alice", "hlc": "1000000:1", "l": 1000000, "c": 1 } }
```

仿真结论：

```json
{ "soundnessHolds": true, "violations": [],
  "concurrentButTimestampOrdered": [ { "earlier": "y1", "later": "x1",
    "happensBefore": false, "...": "..." } ] }
```

---

## 7. 算法规则（本实现采用的约定）

设本地当前为 `(l, c)`，物理时钟读数 `pt`，收到消息时间戳 `(lm, cm)`。

**本地 / 发送事件**

```
l' = max(l, pt)
若 l' == l ：c' = c + 1     // 物理时间没走到 l 前面
否则       ：c' = 0         // 物理时间严格推进，计数器归零（HLC 论文约定）
```

**接收事件**

```
l' = max(l, lm, pt)
若 l' == l 或 l' == lm ：
    c' = 1 + max( c  （当 l' == l ）,
                  cm （当 l' == lm）)
否则 ：c' = 0              // pt 严格领先于两个逻辑时间
```

由此保证：合并后的时间戳严格大于"本地旧状态"和"消息时间戳"中需要被排序的两者。

### 7.1 物理时钟回退

`pt` 仅作为 `max` 的一个输入，被视为**不可信建议值**。当时钟跳变（NTP 回拨、虚拟时钟
重置、热迁移）时，`l` 保持、`c` 自增，已发放时间戳严格单调。例如物理时间
`10000 → 5000 → 1000`：

```
10000:0  →  10000:1  →  10000:2
```

### 7.2 逻辑计数溢出（显式处理，绝不回绕/饱和）

`c` 是非负 `long`，上限 `2^63-1`。在物理时间停滞且计数到达上限时，下一次操作抛出
`LogicalCounterOverflowException`（HTTP 507），**不产生**任何会破坏次序的时间戳，
时钟状态保持不变。文档化的恢复方式是让物理时间前进到超过 `l`，此时计数归零、操作恢复：

```
5000:9223372036854775807  --(再 tick)--> 抛异常，状态不变
5000:9223372036854775807  --(pt 前进到 5001)-->  5001:0
```

### 7.3 持久化与恢复

- 状态文件为 UTF-8 properties：`version=1`、`tzdb=<IANA 版本>`、`node.<名字>=<l>:<c>`。
- 写入走"临时文件 → `fsync` → 原子 `rename`"，崩溃不会留下半截状态。
- 恢复后即便墙上时钟已回退，后续事件仍按上面的 `max` 规则与已持久化状态保持单调
  （跨重启单调性有专门测试）。

### 7.4 时区数据库版本

HLC 用的是绝对物理时间，**与时区无关**；记录 tzdb 版本（如 `2026b`）纯粹是出处
（provenance）信息，便于将来把时间戳渲染成人类可读本地时间时保持可复现。该版本来自
JDK 内置 `java.time` 时区库，出现在 `/health`、`/info`、`/simulate` 响应和状态文件中。

---

## 8. 验收：因果性如何被验证

`CausalityEngine` 接收一个**全局事件交错序列**，每个事件显式钉住该节点当时的虚拟
物理时间，因此时钟偏差与回退都是输入的一部分，而非碰运气。它：

1. 按序驱动每节点的 `HLCClock`（`LOCAL`/`SEND`/`RECV`）；
2. 维护 happens-before 图：同节点上一事件 + `RECV` 指向的 `SEND`，并求传递闭包；
3. 对所有事件两两检查：
   - **可靠性（因果 ⇒ 时间戳）**：凡 `a → b`，必须 `ts(a) < ts(b)`，否则记入
     `violations`（验收要求该数组为空）；
   - **反例（时间戳 ⇏ 因果）**：凡两者互不 happens-before（并发）却存在
     `ts(a) < ts(b)`，记入 `concurrentButTimestampOrdered`。

`samples/07-simulate-concurrent.json` 中 nodeX 与 nodeY **从不通信**，却得到
`y1(1000:0) < x1(2000:0)` 这样的时间戳次序——该对被明确标记 `happensBefore:false`，
具体证明"时间戳在先不代表因果在先"。对应自动化断言见
`CausalityEngineTest.timestampOrderDoesNotImplyCausality()`。

---

## 9. 自动化测试覆盖（35 项）

| 测试类 | 覆盖内容 |
|---|---|
| `HLCTimestampTest` | 解析/格式化、字典序、非法输入 |
| `HLCClockTest` | 首事件、同刻计数、物理前进归零、**时钟回退单调**、receive 合并、滞后时钟、**溢出显式异常与恢复**、跨重启恢复、跨节点相等时间戳语义 |
| `CausalityEngineTest` | **带偏差因果链**、happens-before 传递闭包、**反推不成立的具体见证**、交错顺序、回退场景、非法输入 |
| `HLCFileStoreTest` | 存取往返、缺失文件、版本不兼容拒绝、tzdb 落盘 |
| `JsonTest` | 嵌套/数组/Unicode/转义往返、非法 JSON、字段校验 |
| `ApiServiceTest` | 收发、回退、持久化、溢出错、校验、仿真结论 |
| `HLCHttpServerIT` | 真实起 HTTP 服务的端到端流程、仿真、400/507 错误码 |

手工/端到端验证记录在 `RUNLOG.md`。

---

## 10. 设计取舍

- **微秒单位**：`System#currentTimeMillis` 乘以 1000 即可得到，跨平台稳定；生产可换
  `Clock` 实现接入更高分辨率源，接口 `PhysicalClock` 已为此预留。
- **虚拟时钟显式传参**：服务端每个操作都要求 `physicalMicros`，让偏差/回退成为可控、
  可复现的输入，而不是依赖睡眠或墙上时钟。
- **不可变值类型 + 同步时钟**：`HLCTimestamp` 不可变；`HLCClock` 的所有变更在实例上
  串行化，线程安全。
- **持久化只保证单机原子性**：它不是复制日志或事务系统，刻意保持简单。
- **不做的事**：无预约/考勤语义、无前端、无外部数据库、无认证授权、无网络节点自动
  通信（消息时间戳由调用方在请求里显式传递）。
