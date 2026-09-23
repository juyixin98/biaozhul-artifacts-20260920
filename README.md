# 异步结果有序提交服务（Java + JDK HttpServer）

纯后端服务：事件被异步处理，**同一分区内的输出严格按提交序号提交**；
后完成的事件即使先算完也必须排队等待（head-of-line buffering）。
仅使用 JDK 标准库（`com.sun.net.httpserver.HttpServer`），无任何第三方依赖，
无需 Maven/Gradle，`javac` 即可构建。

## 1. 依赖与环境

| 项 | 要求 |
| --- | --- |
| JDK | **Java 17+**（开发与验收使用 Temurin 17.0.13+11） |
| 第三方依赖 | **无**（编译期 / 运行期 / 测试期均为 0） |
| 构建工具 | JDK 自带 `javac` / `jar`，无需安装 Maven/Gradle |
| 使用的 JDK 模块 | `java.base`、`jdk.httpserver`（`jdeps` 验证，见 `dependencies.lock`） |
| 验收脚本额外用到 | `curl`、`python3`（仅演示脚本美化输出，服务本身不依赖） |

如果机器上没有 JDK 17，设置 `JAVAC` / `JAVA` 环境变量指向 JDK 17 即可，例如：

```bash
export JAVA_HOME="$HOME/jdks/jdk-17.0.13+11"
export JAVAC="$JAVA_HOME/bin/javac"
export JAVA="$JAVA_HOME/bin/java"
```

## 2. 构建、测试、启动

```bash
# 构建（产出 target/ordered-events.jar）
./build.sh

# 运行自动化测试（18 个用例，含 4 个 HTTP 端到端验收场景）
./test.sh

# 启动 HTTP 服务（默认 0.0.0.0:8080）
./run.sh

# 自定义参数启动
./run.sh --port 9090 --host 127.0.0.1 \
         --inflight-cap 4 --buffer-cap 32 \
         --default-delay-ms 500 --timeout-ms 2000 \
         --max-attempts 3 --retry-delay-ms 100 --http-threads 16
```

启动后另开终端执行验收演示：

```bash
./examples/acceptance-demo.sh                 # 默认 http://127.0.0.1:8080
```

原始 `curl` 请求样例见 [`examples/API.md`](examples/API.md)。

## 3. 核心语义

### 3.1 分区与顺序

- 每个提交事件获得分区内单调递增的 `seq`（从 0 开始），事件 id 为 `{分区}-{seq}`。
- 提交结果（成功结果或失败占位）只有在**所有更小 seq 的事件都已落定后**
  才会追加到分区输出，因此输出严格按输入序号排列。
- 分区间相互独立：一个分区的队头阻塞不影响其他分区。

### 3.2 在途数量限制（两层）

- **在途上限 `inflight-cap`**：固定大小 worker 池，真正并发执行的事件数
  不超过该值；拿不到线程的事件保持 `SCHEDULED`（计入 buffered）。
- **分区缓冲上限 `bufferCap`**：每个分区未结束事件数达到上限后，
  新提交直接返回 **429**，避免无界堆积；队头迟迟不结束会对提交方形成背压。

### 3.3 失败占位

- 事件所有尝试耗尽（或最后一次尝试超时）后，在它自己的 seq 位置提交
  `outcome=FAILURE` 占位条目，携带最后错误与尝试次数；事件终态为 `FAILED`。
- 失败占位同样参与顺序：它后面的事件要等占位提交后才能提交。

### 3.4 重试

- `maxAttempts` 为**总尝试次数**（含首次），失败后等待 `retryDelayMillis` 再重试。
- 每次尝试独立计时 `timeoutMillis`：超时会中断本次尝试，若仍有剩余次数则进入重试。

### 3.5 超时

- 单次尝试超过 `timeoutMillis`：定时任务中断 worker 线程并按失败处理。
- 默认处理器的 sleep 响应中断，因此超时会立即生效而不是等满 delay。

### 3.6 取消

- 可取消状态：排队中（含因在途上限排队）、运行中、重试等待中。
- 运行中的事件通过线程中断取消（协作式：处理器需响应 interrupt）。
- 取消后事件终态为 `CANCELLED`，**不产生任何结果条目，其 seq 位置被跳过**，
  不阻塞后续事件。
- 对已结束事件（SUCCEEDED/COMMITTED/FAILED）取消返回 **409**；重复取消幂等。

### 3.7 事件状态机

```
SCHEDULED ──> RUNNING ──失败且还有次数──> SCHEDULED（等待重试）
                 │
                 ├─> SUCCEEDED（已算完，排队等待队头释放）──> COMMITTED（成功结果已提交）
                 ├─> FAILED（次数耗尽；FAILURE 占位已提交）
                 └─> CANCELLED（取消，跳过位置，无输出）
```

## 4. HTTP 接口

| 方法与路径 | 说明 |
| --- | --- |
| `GET  /health` | 健康检查 |
| `GET  /stats` | 全局计数（submitted / committed / outstanding / inFlight / buffered） |
| `GET  /partitions` | 分区列表及计数 |
| `PUT  /partitions/{p}` | 创建分区（幂等），body 可选 `{"bufferCap":16}` |
| `GET  /partitions/{p}` | 分区计数 |
| `POST /partitions/{p}/events` | 提交事件，返回 202 |
| `GET  /partitions/{p}/events/{id}` | 查询事件状态 |
| `POST /partitions/{p}/events/{id}/cancel` | 取消事件（body 可选 `{"reason":"..."}`） |
| `GET  /partitions/{p}/results` | 有序结果；支持 `waitMillis`（长轮询，≤120000）、`sinceSeq`（增量） |

### 提交事件请求体字段（均可省略，使用启动默认值）

| 字段 | 含义 | 默认 | 范围 |
| --- | --- | --- | --- |
| `payload` | 任意 JSON，原样记录/回显 | `null` | ≤1MiB 请求体 |
| `delayMillis` | 模拟处理耗时 | 500 | 0–600000 |
| `fail` | 每次尝试都注入失败 | false | — |
| `timeoutMillis` | 单次尝试超时 | 2000 | 1–600000 |
| `maxAttempts` | 总尝试次数 | 3 | 1–20 |
| `retryDelayMillis` | 重试间隔 | 100 | 0–600000 |

分区名规则：`[A-Za-z0-9._-]{1,64}`。错误统一为
`{"error":{"status":...,"message":...}}`。

## 5. 最小示例

```bash
curl -s -X PUT localhost:8080/partitions/demo -d '{}'
curl -s -X POST localhost:8080/partitions/demo/events \
  -H 'Content-Type: application/json' \
  -d '{"delayMillis":100}'
curl -s 'localhost:8080/partitions/demo/results?waitMillis=5000'
```

## 6. 验收点对照

| 验收要求 | 覆盖位置 |
| --- | --- |
| 首事件超时、后续先完成，输出仍有序 | 核心 `timeoutOnHeadReleases...`、HTTP `acceptanceTimeoutFirstLaterFinishesFirstOutputOrdered` |
| 缓冲上限（429 + 在途上限） | `bufferCapRejectsExtraEventsWith429`、`inflightCapBoundsRunningEvents`、HTTP `acceptanceBufferCapReturns429` |
| 重试耗尽后的顺序 | `failedAttemptsAreRetriedThenPlaceholderCommittedInOrder`、`timedOutAttemptIsRetried...`、HTTP `acceptanceRetriesExhaustedThenFailureInOrder` |
| 取消后不再提交结果 | `cancelQueued...`、`cancelRunning...`、`cancelIsIdempotent`、HTTP `acceptanceCancelProducesNoResult` |
| 分区间独立 | `partitionsAreIndependent` |

实际运行记录见 [`docs/RUN-RESULTS.md`](docs/RUN-RESULTS.md)。

## 7. 目录结构

```
src/main/java/io/example/orderedcommit/
  Main.java                 入口（参数解析）
  HttpEventServer.java      JDK HttpServer 路由层
  OrderedEventService.java  状态机 / 有序提交 / 重试 / 超时 / 取消
  Partition.java            分区状态（pending + committed + seq）
  Event.java / Committed.java / Status.java
  EventProcessor.java       可插拔处理器接口
  SleepingEventProcessor.java 默认处理器（可注入 delay/fail）
  Json.java / EventException.java / Args.java
src/test/java/...           零依赖测试框架 + 18 个用例
examples/                   curl 文档与验收演示脚本
dependencies.lock           依赖锁定（零三方依赖，JDK 17，java.base/jdk.httpserver）
build.sh / test.sh / run.sh
```

## 8. 已知限制

- 状态保存在内存中，重启即丢失；`eventsById` 不做 TTL 回收（演示用途）。
- 取消为协作式中断：自定义处理器若吞掉中断标志且不响应
  `InterruptedException`，取消/超时无法立即生效（默认处理器会正确响应）。
- 结果输出无持久化与分页；单分区结果建议配合 `sinceSeq` 增量拉取。
