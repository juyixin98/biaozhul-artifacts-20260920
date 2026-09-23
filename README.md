# 复杂事件序列匹配引擎（A → B → C）

纯 Java 后端实现，使用 **JDK 自带的 `com.sun.net.httpserver.HttpServer`** 提供 HTTP 接口，
**零第三方运行/测试依赖**（不依赖 Maven/Gradle，不引入 JSON 库或 JUnit），
自带预写日志（WAL）保证进程硬崩溃后部分匹配状态可完整恢复。

---

## 1. 匹配语义（需求逐条对照）

| 需求 | 实现行为 |
|---|---|
| 模式 A 后 B 后 C | 同实体下按时间先后出现 A、B、C 三个事件即产生一个匹配 |
| 按实体分组 | 每个 `entityId` 独立维护等待中的 A 与 A→B，**跨实体绝不串配** |
| 十秒内完成 | 窗口为 **10000 毫秒**，要求 `C.timestamp − A.timestamp <= 10000`。边界合法：恰好 10.000 s 命中，10.001 s 不命中 |
| 跳过无关事件 | 类型不是 A/B/C 的事件正常接收，只推进实体时间水位，不参与、也不破坏部分匹配 |
| 支持重叠匹配 | 每个 A 都保留为候选；一个 B 与窗口内**所有**存活 A 组合；一个 C 与窗口内**所有**存活 A→B 组合。事件不会因参与过一次匹配而被“消费” |
| 相同时间按输入序号排序 | 同毫秒事件按接收顺序分配全局单调递增序号 `seq`，序号小的视为先发生；乱序输入（时间戳回退）直接返回 **409**，不静默重排 |
| 禁止静默截断组合 | 单个 C 的候选组合数超过上限（默认 100000，`--max-combines` 可调）时，整批返回 **422** 且状态完全不变，绝不少给结果 |

### 两个验收场景的答案

- **A, A, B, C**（同实体、窗口内）→ **2 个匹配**（两个 A 分别与同一个 B、C 组合）。
- **A, B, 超时, C**（C 距 A > 10s）→ **0 个匹配**，超时后等待中的 A→B 被清除，后续事件不受影响。

### 第三个验收场景（故障恢复）

- 写入事件后调用测试端点 `POST /test/crash`，服务执行 `Runtime.getRuntime().halt(1)`
  （不执行 shutdown hook，等价于 `kill -9` / 断电）。
- 用**同一数据目录**重启后：已确认（崩溃前收到 200）的匹配一个不丢，
  崩溃时未完成的部分匹配（等待中的 A、A→B、水位、已分配序号）逐条一致，
  可继续写入并接续匹配。真实的“启动→崩溃→重启→比对”由
  `RecoveryProcessTest` 以独立 JVM 子进程方式自动化验证。

---

## 2. 运行环境与依赖

- **JDK 17+**（仅用标准库；在 Eclipse Temurin 17.0.20 上实际编译、测试通过）。
- 不需要 Maven、Gradle 或任何 jar 包。
- `demo.sh` 额外需要系统自带的 `curl`（以及用于挑选空闲端口的 `python3`，
  缺失时回退到固定端口 18080）。

> 本仓库交付环境中没有预装 JDK，实际使用的是免安装的
> **Eclipse Temurin JDK 17.0.20.1+1 (x64 linux)**：
> 下载地址 `https://api.adoptium.net/v3/binary/latest/17/ga/linux/x64/jdk/hotspot/normal/eclipse`
> SHA-256（tar.gz）：`3808d1d15e3ec6bd5b84057fb5d84c33d8a1536a258146bcea2e603fc726e08e`
>
> 若系统已装 JDK 17+，直接使用即可；否则可解压上述 JDK 后设置 `JAVA_HOME`。
> 因为没有任何外部构件，这就是“锁定依赖”的全部内容（见 `deps.lock`）。

### 启动命令

```bash
# 1) 编译（输出到 build/classes 与 build/test-classes）
./build.sh

# 2) 启动服务（默认 127.0.0.1:8080，数据目录 ./data）
./run.sh
# 或带参数：
JAVA_HOME=/path/to/jdk-17 ./run.sh --port=9090 --data-dir=/var/lib/cep \
    --max-combines=100000 [--test-endpoints]
```

参数说明：

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--port` | `8080` | 监听端口；`0` 表示由系统分配（测试用，端口打印在启动日志） |
| `--data-dir` | `data` | 预写日志 `event.log` 所在目录，自动创建 |
| `--max-combines` | `100000` | 单个 C 事件允许产生的匹配上限，超出即 422，禁止静默截断 |
| `--test-endpoints` | 关闭 | 开启 `POST /test/crash` 硬崩溃模拟端点（仅演示/测试用） |

启动时会自动重放数据目录中的日志并打印恢复摘要，例如：

```
[cep] 故障恢复: 重放 4 个批次 / 9 个事件，重建匹配 2 个，耗时 9 ms
[cep] 监听 http://127.0.0.1:8080  (数据目录 data, 测试端点 关闭, 组合上限 100000)
```

---

## 3. HTTP 接口

请求/响应均为 UTF-8 JSON。事件时间戳 `timestamp` 为 epoch 毫秒；
`seq`（输入序号）由服务端分配，**客户端传入 seq 会被 400 拒绝**。

### `POST /events` — 写入事件（支持单对象或数组）

请求体（数组按元素顺序视为同一批次的输入顺序，序号连续分配）：

```json
[
  {"type": "A", "entityId": "e1", "timestamp": 1000},
  {"type": "A", "entityId": "e1", "timestamp": 1000},
  {"type": "B", "entityId": "e1", "timestamp": 2000},
  {"type": "C", "entityId": "e1", "timestamp": 3000}
]
```

成功响应 `200`：

```json
{
  "accepted": 4,
  "created": 2,
  "totalMatches": 2,
  "events": [ ...按输入顺序回显，含分配的 seq... ],
  "newMatches": [ ...本批新产生的匹配明细... ]
}
```

错误（均为 JSON：`{"error": 代码, "message": 中文说明}`）：

| 状态码 | error | 触发条件 |
|---|---|---|
| 400 | `invalid_json` / `invalid_request` / `invalid_event` | JSON 非法、空数组、字段缺失/类型错误、客户端自带 seq |
| 409 | `late_event` | 同实体时间戳早于已见水位（乱序），消息中带当前水位 |
| 422 | `combination_limit` | 候选组合数超上限；整批不生效，响应写明候选数与上限 |
| 405 | `method_not_allowed` | 方法不对 |
| 404 | `not_found` | 路径不存在（或未开启 `--test-endpoints` 访问崩溃端点） |
| 500 | `wal_write_failed` 等 | 预写日志写入失败（批次未生效，不会出现“客户端以为成功但没落盘”） |

### `GET /matches?entityId=e1` — 查询匹配

省略 `entityId` 返回全部。匹配按
`实体 → C 时间/序号 → B → A` 确定性排序：

```json
{"count": 2, "matches": [
  {"entityId":"e1",
   "a":{"type":"A","entityId":"e1","timestamp":1000,"seq":0},
   "b":{"type":"B","entityId":"e1","timestamp":2000,"seq":2},
   "c":{"type":"C","entityId":"e1","timestamp":3000,"seq":3},
   "spanMs":2000}
]}
```

### `GET /state?entityId=e3` — 查询部分匹配状态

省略 `entityId` 返回全部实体。字段：`watermark`（该实体已见最大时间戳）、
`waitingA`（窗口内等待 B 的 A）、`waitingAB`（等待 C 的 A→B）。

### `POST /reset` — 清空内存状态与预写日志（序号归零）

### `GET /health` — 存活检查，返回 `totalMatches` 与 `nextSeq`

### `POST /test/crash` — 仅 `--test-endpoints` 时可用，先回 200 再 `halt(1)` 硬退出

---

## 4. 请求样例（curl）

```bash
# 验收 1：A,A,B,C -> 2 个匹配
curl -sS -X POST http://127.0.0.1:8080/events \
  -H 'Content-Type: application/json' \
  -d '[{"type":"A","entityId":"e1","timestamp":1000},
       {"type":"A","entityId":"e1","timestamp":1000},
       {"type":"B","entityId":"e1","timestamp":2000},
       {"type":"C","entityId":"e1","timestamp":3000}]'
curl -sS 'http://127.0.0.1:8080/matches?entityId=e1'

# 验收 2：A,B,超时,C -> 0 个匹配
curl -sS -X POST http://127.0.0.1:8080/events -H 'Content-Type: application/json' \
  -d '[{"type":"A","entityId":"e2","timestamp":0},{"type":"B","entityId":"e2","timestamp":1000}]'
curl -sS -X POST http://127.0.0.1:8080/events -H 'Content-Type: application/json' \
  -d '[{"type":"C","entityId":"e2","timestamp":11000}]'
curl -sS 'http://127.0.0.1:8080/matches?entityId=e2'

# 验收 3：硬崩溃后重启（需 --test-endpoints 启动）
curl -sS -X POST http://127.0.0.1:8080/events -H 'Content-Type: application/json' \
  -d '[{"type":"A","entityId":"e3","timestamp":50000},{"type":"B","entityId":"e3","timestamp":50500}]'
curl -sS -X POST http://127.0.0.1:8080/test/crash -H 'Content-Type: application/json' -d '{}'
# 用同一 --data-dir 重新启动后：
curl -sS 'http://127.0.0.1:8080/state?entityId=e3'   # waitingAB 仍在
```

更多样例（含相同时间戳排序、409、422 等）可直接运行 `./demo.sh`，
完整真实输出见 `results/demo-output.txt`。

---

## 5. 自动化测试

```bash
./test.sh          # 编译并运行全部测试，退出码 0 表示全绿
```

测试同样**零第三方依赖**：`test/cep/TestRunner.java` 是约 90 行的反射式迷你框架，
直接断言并打印结果。共 **36 个用例**：

- `EngineTest`（14 个）：两个验收场景、窗口边界（10000 命中 / 10001 不命中）、
  重叠匹配（A,B,B,C,C → 4）、事件不被消费、无关事件跳过、实体分组隔离、
  同毫秒按序号排序、跨批次序号、晚到事件原子拒绝（409 对应异常）、
  组合上限显式失败且状态不变、上限内组合一个不少、部分状态视图、
  无关事件触发过期清理。
- `EventLogTest`（6 个）：追加/重放往返、崩溃半行截断恢复、
  中间行损坏拒绝启动、空日志、reset、落盘字节校验。
- `RecoveryTest`（3 个）：引擎层重放后**已完成匹配 + 部分匹配 + 水位 + 序号**逐字段一致、
  冷启动、重放决定论。
- `HttpTest`（11 个）：在同 JVM 随机端口启动真实 HTTP 服务，覆盖两个验收场景、
  单对象/数组、400/404/405/409/422、实体过滤、reset、崩溃端点开关。
- `RecoveryProcessTest`（2 个）：**真实独立 JVM 子进程**完成“写入 → `halt(1)` 硬崩溃
  → 同目录重启 → 比对 → 续配 → 再次重启幂等”，以及手工构造“写入途中掉电”半行记录后
  不得产生幽灵匹配/重复序号。

---

## 6. 故障恢复机制（WAL）

- 写入是两阶段：① 引擎在**状态副本**上做晚到校验与组合上限预检（失败则现状不变）；
  ② 整个批次序列化为一行 NDJSON 追加到 `data/event.log` 并 **fsync**；③ fsync 成功后
  才提交内存状态并返回 200。因此“已确认 ⇒ 已落盘”。
- 启动时按行顺序重放，事件上的 `seq` 是崩溃前分配的原值，重放结果与崩溃前严格一致
  （事件处理是确定性的状态机），`nextSeq` 接续历史最大值。
- 崩溃只可能让最后一行写到一半：**末尾不含换行的片段视为未确认批次并物理截断**
  （该批次从未向客户端返回成功）；任何中间行损坏都直接拒绝启动，绝不跳过。
- 当前未做定期快照/日志压缩：长时间运行日志会持续增长，重放是 O(事件总数)。
  这是已知的可扩展项（见第 8 节），对功能正确性没有影响。

## 7. 目录结构

```
src/cep/
  Event.java       事件模型（type/entityId/timestamp/seq）
  Match.java       完整匹配（不可变）
  Engine.java      匹配引擎：分组、窗口、重叠组合、序号、晚到拒绝、组合上限、恢复接口
  EventLog.java    预写日志：批次追加 + fsync、重放、半行截断、reset
  Json.java        零依赖 JSON 解析/序列化
  Main.java        JDK HttpServer、路由、请求校验、错误码、崩溃端点
test/cep/          36 个自动化测试（自带迷你 TestRunner，AllTests 为入口）
build.sh           仅用 javac 编译
run.sh             启动服务（透传参数）
test.sh            编译 + 跑全部测试
demo.sh            三个验收场景的端到端 curl 演示
deps.lock          依赖与 JDK 版本/校验和锁定说明
results/           实际运行结果记录
```

---

## 8. 实际运行结果与已知限制

### 实际运行（环境：无预装 JDK 的 Linux，Temurin JDK 17.0.20.1+1）

- `./build.sh`：编译通过，无告警（`-Xlint:all,-serial -Werror`）。
- `./test.sh`：**36/36 全部通过**（含真实子进程硬崩溃恢复用例）。
- `./demo.sh`：三个验收场景全部符合预期，输出原样保存在
  [`results/demo-output.txt`](results/demo-output.txt)，其中可看到：
  - A,A,B,C 返回 `"created":2`；
  - 超时 C 返回 `"created":0`，且 state 中 `waitingAB` 已清空；
  - 硬崩溃退出码 1，重启日志 `重放 4 个批次 / 9 个事件，重建匹配 2 个`，
    e3 的部分匹配仍在，补 C 后命中，序号从 9 继续；
  - 超组合上限返回 422 且 `cap` 实体匹配数为 0（整批未生效）。

### 明确的已知限制 / 未完成项（如实记录）

1. **无快照与日志压缩**：WAL 无限增长、重放为 O(全量事件)。高吞吐长期运行场景需要
   增加“状态快照 + 截断旧日志”。接口与文件格式已为该扩展留好空间，但本次未实现。
2. **单实例、单机内存状态**：没有分布式多副本；水平扩展需要按 entityId 分片。
3. **事件时间模型要求同实体非递减时间戳**：不支持“允许迟到 N 分钟并重新计算匹配”的
   watermark 回流语义；乱序按需求采取显式拒绝（409）。
4. **HTTP 为 127.0.0.1 明文绑定**：无 TLS、鉴权与限流；生产部署应放在反向代理之后。
5. 匹配查询返回全量并在每次请求时排序，数据量很大时需要分页/持久化索引。
6. `POST /test/crash` 是测试设施；未显式开启时返回 404，生产请勿开启。
