# ordered-events — 异步结果有序提交服务（纯 JDK 后端）

一个纯后端事件处理服务：事件异步执行（可注入任意延迟、失败、超时），
**每个分区内严格按输入序号提交结果**，分区间互相独立；限制每分区在途
（缓冲）数量与并发尝试数；定义了失败占位、重试和取消语义。

- 只用 JDK（`com.sun.net.httpserver.HttpServer` + 线程池 + JUC），
  **零第三方依赖、零构建工具依赖**。
- HTTP/JSON 接口，长轮询拉取有序结果。
- 自带 64 项断言的端到端自动化测试（真实 HTTP 收发，非 mock）。

## 1. 依赖与环境

| 项 | 要求 |
|---|---|
| JDK | **17 或更新**（仅用到正式特性；HTTP Client、sealed 类、record） |
| 第三方库 | **无**（不依赖 Maven/Gradle/JUnit，也不下载任何构件） |
| OS | 任意带 JDK 17 的系统；在 Linux x86_64 实测 |
| 演示工具 | `curl`、`jq`（仅 `scripts/demo.sh` 需要；测试本身不需要） |

“锁定依赖”的方式：没有外部依赖坐标需要锁定；所需运行时唯一版本要求是
JDK 17+。开发/实测使用的精确版本为 **Temurin OpenJDK 17.0.20.1**。

> 若机器上没有 JDK，可下载解压 Temurin 17 到任意目录后用
> `JAVA=/path/to/jdk/bin/java JAVAC=/path/to/jdk/bin/javac scripts/test.sh`
> 指定，无需 root。

## 2. 构建与启动

```bash
# 编译（输出 target/classes）
scripts/build.sh

# 启动（默认 :8080）
scripts/run.sh

# 自定义参数
scripts/run.sh -- \
  --port 9090 \
  --max-in-flight 16 \
  --max-concurrent 4 \
  --default-max-attempts 3 \
  --default-timeout-ms 1000 \
  --retry-backoff-ms 50
```

启动后：

```
ordered-events service listening on http://localhost:8080
  per-partition maxInFlight=8, maxConcurrent=2, ...
```

停止：`Ctrl-C`（注册了 shutdown hook，会停掉 HTTP 线程池与工作线程池）。

### 命令行参数

| 参数 | 默认 | 含义 |
|---|---|---|
| `--port` | 8080 | HTTP 端口（0 = 随机端口，测试使用） |
| `--max-in-flight` | 8 | **每分区**在途（缓冲中 + 运行中）事件上限，超出提交返回 503 |
| `--max-concurrent` | 2 | **每分区**同时执行的尝试数，其余事件在分区内排队 |
| `--default-max-attempts` | 3 | 每事件默认总尝试次数（含首次，1 = 不重试） |
| `--default-timeout-ms` | 1000 | 单次尝试默认超时（毫秒） |
| `--retry-backoff-ms` | 50 | 两次尝试之间的固定退避 |

单事件可用请求体里的 `timeoutMillis` / `maxAttempts` 覆盖默认值
（上限分别为 60000ms 与 10 次）。

## 3. 核心语义

- **序号**：事件进入分区时分配分区内单调递增序号（从 0 开始）。
- **有序提交（commit barrier）**：结果只在“队头事件有定论”时按序号提交。
  后到先完成的事件在队列里等待，不会越过尚未落定的队头。
- **失败也是结果（占位）**：
  - 成功 → `SUCCEEDED`，带回显值；
  - 尝试次数耗尽仍失败 → `FAILED`；
  - 每次尝试在截止时间前未完成（框架中断）→ 最后一次记 `TIMED_OUT`。
  `FAILED/TIMED_OUT` 都作为**占位条目**在该事件自己的序号位置提交，
  因此失败不会破坏或跳过顺序。
- **重试**：按 `maxAttempts` 依次执行尝试计划（见下），每次尝试独立超时；
  最后一次失败/超时才落定占位，否则固定退避后重试。客户端给的计划比
  预算短时，重复最后一个计划项。
- **取消**：
  - 已提交（结果已输出）→ `409 Conflict`，结果不可撤回；
  - 已取消再取消 → 幂等 `200`；
  - **缓冲中**（等并发许可）→ 立即密封为 `CANCELLED`、移出在途槽位、
    释放一个在途名额；
  - **运行中** → 中断 worker 等待、打断尝试；密封后绝不会提交结果。
  - 取消的事件在输出中表现为**序号空缺**（不产生任何条目），并打开队头栅栏，
    让后面的事件继续提交。
- **在途上限**：`缓冲中 + 运行中 ≤ maxInFlight`，超出立即 `503`；
  事件提交（成功/占位）或被取消后名额归还。
- **分区独立**：每个分区有自己的锁、序号、队列、信号量和在途计数，
  A 分区的队头不阻塞 B 分区。

## 4. HTTP 接口

所有请求/响应均为 JSON（`Content-Type: application/json`）。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 |
| GET | `/partitions` | 分区列表 |
| POST | `/partitions/{p}/events` | 提交事件，`202` 返回事件快照（含 id、seq） |
| GET | `/partitions/{p}/events/{id}` | 查询单个事件状态 |
| POST | `/partitions/{p}/events/{id}/cancel` | 取消事件 |
| GET | `/partitions/{p}/results?afterSeq=&waitForSeq=&waitMillis=` | 有序输出（支持长轮询） |
| GET | `/partitions/{p}/status` | 分区在途/并发计数 |

分区名规则：字母数字、`.`、`_`、`-`，不含 `..`，长度 ≤128。
分区在首个事件提交时自动创建。

### 提交事件请求体

```json
{
  "payload": "任意 JSON 值，成功时原样回显",
  "timeoutMillis": 1000,
  "maxAttempts": 3,
  "attempts": [
    {"delayMillis": 100, "behavior": "fail", "error": "transient boom"},
    {"delayMillis": 100, "behavior": "succeed"}
  ]
}
```

- `attempts[].behavior`：`succeed` | `fail` | `timeout`
  - `succeed`：延迟后成功；
  - `fail`：延迟后失败（用 `error` 描述原因）；
  - `timeout`：模拟“下游不响应”——一直工作，直到框架在
    `timeoutMillis` 截止时将其切断。
- 缺省 `attempts` = 一次 0ms 立即成功；计划短于预算时重复最后一项。
- 未覆盖时用服务默认超时/尝试次数。

### 有序输出

- `afterSeq`：只返回序号严格大于该值的条目（游标，初始传 `-1`）。
- `waitForSeq`：长轮询直到提交序号到达该水位（或超时）。
- `waitMillis`：最长等待毫秒数（封顶 30000）；默认 0 立即返回。

结果条目：

```json
{
  "id": "uuid", "seq": 0, "status": "SUCCEEDED|FAILED|TIMED_OUT",
  "value": "成功回显，失败时缺省",
  "error": "失败/超时原因，成功时缺省",
  "attempts": 2,
  "durationMillis": 405,
  "committedAtMillis": 1790169026824
}
```

事件状态机：`PENDING → RUNNING → SUCCEEDED | FAILED | TIMED_OUT → COMMITTED`；
提交前任一非终态可转为 `CANCELLED`（不进入输出）。

## 5. 请求样例（curl）

```bash
# 提交三个事件：队头会超时，后两个很快成功
curl -s -X POST localhost:8080/partitions/demo/events -H 'Content-Type: application/json' -d '{
  "payload":"head",
  "attempts":[{"delayMillis":5000,"behavior":"timeout"}],
  "maxAttempts":1}'
curl -s -X POST localhost:8080/partitions/demo/events -H 'Content-Type: application/json' -d '{
  "payload":"middle","attempts":[{"delayMillis":50,"behavior":"succeed"}]}'
curl -s -X POST localhost:8080/partitions/demo/events -H 'Content-Type: application/json' -d '{
  "payload":"tail","attempts":[{"delayMillis":80,"behavior":"succeed"}]}'

# 长轮询等待三条全部按序提交（队头是 TIMED_OUT 占位）
curl -s "localhost:8080/partitions/demo/results?afterSeq=-1&waitForSeq=2&waitMillis=5000" | jq .

# 两次失败后耗尽 -> FAILED 占位
curl -s -X POST localhost:8080/partitions/r/events -H 'Content-Type: application/json' -d '{
  "payload":"x",
  "attempts":[{"delayMillis":20,"behavior":"fail","error":"boom"}],
  "maxAttempts":2}'

# 取消一个事件
ID=$(curl -s -X POST localhost:8080/partitions/d/events -H 'Content-Type: application/json' \
  -d '{"payload":"z","attempts":[{"delayMillis":3000,"behavior":"succeed"}]}' | jq -r .id)
curl -s -X POST "localhost:8080/partitions/d/events/$ID/cancel"

# 分区状态
curl -s localhost:8080/partitions/demo/status | jq .
```

完整可复现演示（覆盖题目四项验收）：

```bash
scripts/demo.sh          # 默认端口 18080，自动起服、发请求、打印结果、退出
scripts/demo.sh 19091    # 若 18080 已被别的进程占用，换一个端口
```

脚本会校验端口确实由本服务占用（`/health` 返回本服务 JSON）；若端口被
其他进程占用会直接报错退出并提示换端口，而不是把请求发给别的服务。

## 6. 自动化测试

```bash
scripts/test.sh
```

- 编译主代码 + 测试代码，启动一个**随机端口**的真实服务实例，
  通过 JDK 内置 `java.net.http.HttpClient` 发真实 HTTP 请求。
- 不使用 JUnit（保持零依赖），自带极简断言框架；任一断言失败则进程退出码为 1。
- 共 **64 项断言，11 组用例**：

| 用例 | 覆盖的验收点 |
|---|---|
| ordered commit with slow head | 慢队头未完成时 0 输出；长轮询阻塞；后完成者按 0,1,2 提交 |
| first event times out | **首事件超时、后续先完成**；占位按序提交；尝试预算与截止切断 |
| retry exhaustion | 重试耗尽后 `FAILED` 占位仍占其序号，后续成功紧随其后 |
| in-flight buffer cap | **缓冲上限**：3 个接纳、其余 503；提交后名额归还 |
| cancel buffered | **取消缓冲事件**：不提交任何结果、序号空缺、名额释放、幂等 |
| cancel running | 取消运行中尝试：被放弃、无输出，后续事件正常提交 |
| cancel after commit | 已提交结果不可取消（409），输出保持一次 |
| partitions independent | 分区间不互相阻塞，上限各自独立 |
| retry then success | 首次失败、重试成功 → SUCCEEDED |
| validation | 非法 JSON/参数/分区名 → 400，未知资源/路由 → 404 |
| health | 基础可用性 |

## 7. 实测结果

测试环境：Linux x86_64（容器），Temurin **OpenJDK 17.0.20.1**。

- `scripts/test.sh`：`checks=64 failures=0`，退出码 0；连续多次运行稳定。
  完整输出见 `run-logs/test-output.txt`。
- `scripts/demo.sh`：四个场景实际输出见 `run-logs/demo-output.txt`，要点：
  - 队头挂死在 405ms 被截止切断（`TIMED_OUT`，`attempts=1`），
    50ms/80ms 已完成的两个事件随后按 `seq 0,1,2` 一起提交；
  - 两次失败（间隔退避）后 `FAILED` 占位出现在 `seq 0`，成功事件在 `seq 1`；
  - 在途上限 3：前 3 个 `202`，第 4、5 个 `503 ... buffer is full (limit 3)`；
  - 缓冲中的 `seq 2` 被取消后，输出只有 `seq 0,1`，被取消事件的 id 不出现。
- `scripts/stress.sh`（附加冒烟）：100 个随机延迟事件（每 17 个注入一次
  失败，`maxAttempts=1`），断言提交数=100、序号无空洞无跳变、每个序号的
  id 与状态正确。结果 `STRESS PASS`，见 `run-logs/stress-output.txt`。
  日志中的演示服务器启动横幅存于 `run-logs/demo-server.log`。

## 8. 目录结构

```
src/main/java/orderedevents/
  Main.java                 入口/参数解析
  model/                    EventState / EventSpec / AttemptSpec / EventRecord / ResultEntry
  json/                     零依赖 JSON 解析器 + 序列化
  service/                  EventService（提交/取消/查询/worker）、Partition（有序栅栏）、
                            AttemptExecutor（延迟/失败/超时注入、截止切断）、配置与异常
  web/                      JDK HttpServer 封装与路由
src/test/java/orderedevents/
  OrderedEventsHttpTest.java 端到端测试（64 断言）
src/perf/java/Stress.java     100 事件随机延迟顺序压力冒烟
scripts/                    build.sh / run.sh / test.sh / demo.sh / stress.sh
run-logs/                   上述脚本在交付环境的实际输出存档
```

## 9. 已知限制与未完成项

如实列出：

1. **无持久化**：分区、事件与结果只在内存中，进程退出即丢失；重启后不恢复。
2. **结果/取消记录只在内存**：已提交结果会随分区无限增长（演示用途）；
   已取消事件记录保留最近 10,000 条用于幂等取消与 GET，超出按最旧淘汰。
   生产化需要分页/滚动或外部存储。
3. **重试模型固定**：仅固定次数 + 固定退避，无指数退避/抖动/死信队列；
   超时不打断“挂死”的下游工作线程本身（`Future.cancel(true)` 发中断，
   但模拟的 sleep 响应中断；真实下游未必响应）。
4. **取消是协作式的**：缓冲队列靠 50ms 轮询许可并响应中断；已在执行的
   尝试在下一次等待点生效，不提供 kill 语义之外的强杀。
5. **无鉴权/TLS**：明文 HTTP、无认证，仅适合本地/内网演示。
6. **HTTP 层为固定 32 线程池**，没有按连接数做背压；请求体上限 256 KiB。
7. 测试为基于时序的集成测试（毫秒级 sleep/超时），在极度超载的机器上
   理论上可能需要放宽阈值；在提供的 16 核环境连续运行稳定。
