# 动态合并会话窗口（Dynamic Session-Window Merge）— 纯 Java 后端

按键（`key`）聚合事件为**会话窗口**：同一 key 下，按事件时间排序，相邻事件
时间差 `<= gap` 即属于同一会话。支持**迟到事件桥接**已输出的两个会话，并对
已输出结果生成 **RETRACT（撤回）+ UPSERT（新版本）**；用**水位（watermark）**
定义迟到事件的拒收规则。

仅使用 JDK 自带能力（`com.sun.net.httpserver.HttpServer`），**零第三方依赖**，
无界面，只提供 HTTP JSON 接口。

---

## 1. 依赖与启动

### 依赖

- **JDK 17+**（开发与验收使用 Eclipse Temurin `17.0.20.1+1`）。
- 不需要 Maven/Gradle，不需要任何外部 jar。完整依赖说明见
  [`DEPENDENCIES.md`](DEPENDENCIES.md)。

```bash
# 指定 JDK（若 java/javac 已在 PATH 上可省略）
export JAVA_HOME=/path/to/jdk-17
```

### 构建 / 测试 / 启动

```bash
./scripts/build.sh          # 仅编译主程序 -> build/classes
./scripts/test.sh           # 编译并运行全部自动化测试（35 个用例）
./scripts/run.sh            # 启动 HTTP 服务（默认 :8080）
```

或直接用 javac/java：

```bash
javac -d build/classes src/sessionwindow/*.java
java -cp build/classes sessionwindow.ServerMain
```

### 配置（环境变量，或同名 `-D` 系统属性）

| 变量                     | 默认值 | 含义                                   |
|--------------------------|--------|----------------------------------------|
| `PORT`                   | `8080` | HTTP 端口（`0` = 随机端口，测试用）    |
| `GAP_MS`                 | `10`   | 会话合并间隔，相邻差 `<= gap` 即合并   |
| `ALLOWED_LATENESS_MS`    | `10`   | 允许迟到时长，决定水位                 |
| `DATA_DIR`               | `data` | 事件持久化目录（append-only 日志）     |

启动示例（验收参数 gap=10、lateness=10）：

```bash
GAP_MS=10 ALLOWED_LATENESS_MS=10 PORT=8080 ./scripts/run.sh
```

---

## 2. 语义定义（与验收口径一致）

1. **会话切分**：同一 key 内按事件时间升序排列；设相邻两事件时间为
   `t_prev, t_cur`，当 `t_cur - t_prev <= gapMillis` 时二者同属一个会话，
   否则切分。时间戳相同按到达顺序排序（确定性）。
2. **迟到桥接**：新事件被插入已排序序列后**重新计算该 key**。若它把原本两个
   会话接到一起，则后一个会话整体消失、前一个会话扩展。
3. **撤回与新版本（changelog）**：每次摄入对比新旧物化结果，按差异输出
   - 消失的会话：`RETRACT`（撤回其最后版本）；
   - 内容变化的存活会话：先 `RETRACT` 旧版本，再 `UPSERT` 新版本
     （`version` 单调 +1）；
   - 全新会话：`UPSERT`（version=1）；
   - 未变化：不输出任何消息。
4. **不重复计数**：物化视图中每个 sessionId 只保留最新版本。
   `materializedEventRows`（物化事件行数）恒等于去重后的已接收事件数；
   changelog 的 “UPSERT 行数 − RETRACT 行数” 净值也恒等于该数。
5. **水位拒收**：`watermark = maxObservedEventTime - allowedLateness`。
   - 事件时间 **`< watermark`** → **拒收**（HTTP 422），永不进入聚合、不计数、
     不落盘；
   - 事件时间 **`== watermark`** → 保留（迟到但仍在容忍预算内，含边界）；
   - 去重先于水位判断：已接收事件的幂等重投即使水位已推进也返回成功。
6. **会话 ID 稳定**：`sessionId = "<key>@<startTs>"`。桥接时起始更早的会话
   “存活”（ID 保留、version +1），被并入的较晚会话被撤回。
7. **恢复**：已接收事件以 JSONL 追加写入 `DATA_DIR/events.log`。重启后按到达
   顺序重放——水位、会话 ID、版本号、changelog 均确定性重建，与首次运行一致。

---

## 3. HTTP 接口

| 方法 & 路径        | 说明                                                        |
|--------------------|-------------------------------------------------------------|
| `POST /events`     | 摄入单个事件对象，或事件对象数组（批量）                    |
| `GET  /sessions`   | 当前物化会话；可选 `?key=` 过滤                             |
| `GET  /changelog`  | 全部 RETRACT/UPSERT 消息；可选 `?since=<seq>` 增量拉取      |
| `GET  /rejected`   | 被水位拒收的事件列表                                        |
| `GET  /watermark`  | 水位、最大观测时间、计数器快照                              |
| `GET  /accounting` | 计数与“不重复计数”不变量 `noDoubleCount`                    |
| `GET  /state`      | 完整状态（配置 + 水位 + 当前会话）                          |
| `GET  /health`     | 存活检查                                                    |

事件对象：

```json
{ "eventId": "e10", "key": "user-1", "timestamp": 10 }
```

状态码：`200` 已接收（含幂等重复，返回 `duplicate:true`）；`400` 请求非法；
`409` 同一 eventId 携带不同时间戳（冲突）；`422` 低于水位被拒收。

---

## 4. 请求样例（验收场景：时间 0、20、10，gap=10 的桥接）

> 完整可复制脚本见 [`scripts/examples.sh`](scripts/examples.sh)。
> 以下为 `curl` 手工执行的关键步骤。

```bash
# 1) t=0
curl -s -X POST localhost:8080/events -H 'Content-Type: application/json' \
  -d '{"eventId":"e0","key":"k","timestamp":0}'

# 2) t=20（与 t=0 相差 20 > gap 10 -> 第二个会话 k@20）
curl -s -X POST localhost:8080/events -H 'Content-Type: application/json' \
  -d '{"eventId":"e20","key":"k","timestamp":20}'

curl -s localhost:8080/sessions
# -> 两个会话: k@0 [e0], k@20 [e20]

# 3) 迟到事件 t=10：10-0=10<=10 且 20-10=10<=10 -> 桥接两个会话
curl -s -X POST localhost:8080/events -H 'Content-Type: application/json' \
  -d '{"eventId":"e10","key":"k","timestamp":10}'
```

第 3 步响应（含撤回与新版本）：

```json
{
  "eventId" : "e10",
  "key" : "k",
  "timestamp" : 10,
  "watermark" : 10,
  "accepted" : true,
  "durable" : true,
  "changes" : [ {
    "seq" : 3, "type" : "RETRACT", "sessionId" : "k@20", "version" : 1,
    "startTs" : 20, "endTs" : 20, "eventIds" : [ "e20" ],
    "eventTimestamp" : 10, "reason" : "bridged into a later session"
  }, {
    "seq" : 4, "type" : "RETRACT", "sessionId" : "k@0", "version" : 1,
    "startTs" : 0, "endTs" : 0, "eventIds" : [ "e0" ],
    "eventTimestamp" : 10, "reason" : "superseded by version 2"
  }, {
    "seq" : 5, "type" : "UPSERT", "sessionId" : "k@0", "version" : 2,
    "startTs" : 0, "endTs" : 20, "eventIds" : [ "e0", "e10", "e20" ],
    "eventTimestamp" : 10, "reason" : "late event extended/merged the session"
  } ]
}
```

```bash
curl -s localhost:8080/sessions
# 只剩一个会话 k@0（version=2，[e0,e10,e20]，start=0,end=20）

curl -s localhost:8080/accounting
# materializedEventRows=3, upsert/retract 净值=3, noDoubleCount=true
```

水位拒收样例：

```bash
curl -s -X POST localhost:8080/events -H 'Content-Type: application/json' \
  -d '{"eventId":"hi","key":"k","timestamp":100}'   # watermark -> 90
curl -i -X POST localhost:8080/events -H 'Content-Type: application/json' \
  -d '{"eventId":"tooLate","key":"k","timestamp":5}' # 5 < 90 -> HTTP 422 拒收
curl -s localhost:8080/rejected
```

恢复：`Ctrl-C` 停服后再次 `./scripts/run.sh`（同一 `DATA_DIR`），
`GET /sessions` 输出的会话 ID、版本、事件成员与停服前完全一致。

---

## 5. 自动化测试

```bash
./scripts/test.sh
```

- `AggregatorTest`（18 例）：纯切分、**0/20/10 桥接全链路**、撤回不重复计数、
  changelog 净值、**恢复后 ID/版本稳定**、水位边界（`==` 保留 / `<` 拒收 /
  lateness=0）、gap 边界、多 key、幂等、冲突、三路桥接等。
- `JsonTest`（7 例）：自研 JSON 工具的往返、转义、坏输入、键序。
- `HttpIntegrationTest`（10 例）：启动真实 JDK HTTP 服务（随机端口）经
  HTTP 调用，覆盖验收桥接、422 拒收、幂等、409 冲突、批量、坏 JSON、
  changelog 分页，以及**停服重启后的恢复一致性**。

### 实测结果（2026-09-23，本机真实执行）

```
SessionAggregator unit tests: 18/18 passed
Json unit tests:               7/7 passed
HTTP integration tests:       10/10 passed
ALL SUITES GREEN
```

---

## 6. 代码结构

```
session-window/
├── README.md
├── DEPENDENCIES.md            # 依赖/工具链锁定说明（零三方依赖）
├── scripts/
│   ├── build.sh               # javac 构建
│   ├── test.sh                # 构建并运行全部测试
│   ├── run.sh                 # 启动服务
│   └── examples.sh            # 验收场景 curl 脚本（含水位/幂等/重启）
├── src/sessionwindow/
│   ├── ServerMain.java        # JDK HttpServer + 路由 + JSON 接口
│   ├── SessionAggregator.java # 核心：会话切分/桥接/撤回版本/水位/恢复
│   ├── EventLogStore.java     # append-only 事件日志（持久化恢复）
│   └── Json.java              # 最小 JSON 解析/序列化（无第三方库）
└── test/sessionwindow/
    ├── TestRunner.java        # 零依赖迷你测试框架
    ├── AggregatorTest.java
    ├── JsonTest.java
    ├── HttpIntegrationTest.java
    └── AllTests.java
```

---

## 7. 已知限制 / 未完成项（如实记录）

- **单机、内存态**：所有会话保存在单进程内存，无分布式、无横向扩展；
  多线程摄入虽在聚合器上加了同步，但 HTTP 层固定单线程执行器以保证
  摄入顺序确定。
- **持久化只做了事件日志**：每次重启全量重放，数据量大时启动会变慢；
  没有快照/checkpoint、没有日志压缩。写入仅 `flush()` 到操作系统，
  **未做磁盘级 fsync**（掉电可能丢失最后未落盘数据）。
- **事件时间为整数毫秒的 long**；不支持事件时间乱序下的 key 级独立水位
  （当前是全局单水位）。
- **无鉴权/TLS/限流**，仅适合本地演示与验收，不应直接暴露公网。
- 时间戳相同的事件按“到达顺序”归并，没有再按 eventId 做二级稳定排序
  （同刻多事件的会话归属仍是确定的，但 ID 稳定性对同刻起始事件依赖到达序）。
