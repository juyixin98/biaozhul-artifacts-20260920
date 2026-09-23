# 流式事件 ID 去重服务（Event-ID Dedup with Event-Time Retention）

纯后端服务，基于 **Java 17 + JDK 内置 `com.sun.net.httpserver.HttpServer`**，零第三方依赖。
提供事件 ID 去重能力，保留期按**事件时间（event time）+ 水位（watermark）**定义；
过期后同一 ID 会被视作新事件。支持分区（partition key），分区迁移时去重状态随键转移，
并通过**路由版本号（epoch）**拒绝错误路由的写入。

---

## 1. 它解决什么问题

流式处理中同一条事件可能被重复投递（生产者重试、网络重传、消费者故障恢复……）。
需要回答：**“这个 eventId 我最近处理过吗？”**

关键约束：

1. **保留期是事件时间语义，不是墙上时钟**。事件乱序到达，因此用 watermark 代表
   “事件时间已推进到哪里”，而不是用 `System.currentTimeMillis()` 决定过期。
2. **过期后同 ID 视为新事件**（同一个逻辑 ID 在两个不相交的保留窗口内各出现一次，
   是两条合法事件，而不是重复）。
3. **内存必须随水位推进释放**，否则有界状态会无限增长。
4. **分区迁移（rebalance / failover）时去重状态要随键搬到新属主**；迁移窗口内
   生产者可能把事件发往新、旧任意一端，迁移后也可能有携带旧路由版本的迟到请求，
   这些都不能造成漏判或错误接收。

---

## 2. 环境与依赖

| 项 | 要求 |
|---|---|
| JDK | **17 或更高**（仅需 `java.base`、`jdk.httpserver`；测试额外用到 `java.net.http`） |
| 第三方依赖 | **无**。不需要 Maven/Gradle，构建测试不需要联网 |
| 运行示例脚本 | `bash`、`curl`、`jq`（仅 `scripts/examples.sh` 需要；服务本身不需要） |

依赖锁定见 [`deps.lock`](deps.lock)：没有任何外部坐标需要钉版本，锁定的是工具链。

> 本次开发环境原本没有系统 JDK 且无 root 权限，因此把 Ubuntu 的
> `openjdk-17-jre-headless` / `openjdk-17-jdk-headless` deb 解包到了
> `~/jdk17`。脚本在找不到 `JAVA_HOME` 和 PATH 中的 javac 时会自动回退到该目录。
> 你自己的机器上只要正常安装 JDK 17+ 即可，无需这一步。

验证所用版本：`OpenJDK 17.0.20.1 (build 17.0.20.1+1-1~24.04, amd64)`。

---

## 3. 快速开始

```bash
# 编译（产物在 build/classes）
scripts/build.sh

# 跑全部自动化测试（会先编译）
scripts/test.sh

# 启动服务（默认 8080，可传端口号，或用环境变量 DEDUP_PORT）
scripts/run.sh 8080

# 另一个终端：跑一遍 curl 全流程示例
scripts/examples.sh
```

也可以直接用 java 命令：

```bash
javac -d build/classes $(find src/main/java -name '*.java')
java -cp build/classes dedup.Main 8080
```

启动成功输出：

```json
{
  "service": "event-id-dedup",
  "port": 8080,
  "health": "GET http://localhost:8080/health"
}
```

---

## 4. HTTP 接口

所有请求/响应均为 JSON。epoch（路由版本）既可放在请求体的 `"epoch"` 字段，
也可用请求头 `X-Routing-Epoch`（头优先）。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 |
| GET | `/partitions` | 列出所有分区键 |
| PUT | `/partitions/{key}` | 创建分区：`{"epoch":1,"retentionMillis":10000}` |
| GET | `/partitions/{key}` | 分区状态（epoch、watermark、保留条目数等） |
| DELETE | `/partitions/{key}` | 删除分区（可带 epoch 做防护） |
| POST | `/partitions/{key}/events` | 投递事件，判重：`{"eventId":"A","eventTime":1000,"epoch":1}` |
| POST | `/partitions/{key}/watermark` | 推进水位：`{"watermark":11000,"epoch":1}` |
| POST | `/partitions/{key}/migration/export` | 冻结源分区并导出快照 |
| POST | `/partitions/{key}/migration/import` | 在新属主导入快照：`{"snapshot":{...},"newEpoch":2}` |
| POST | `/partitions/{key}/migration/abort` | 取消迁移（解冻，不丢数据） |
| POST | `/partitions/{key}/migration/complete` | 源端完成切流，退役旧 epoch 并清空状态 |

### 4.1 投递事件的响应

```json
{
  "partitionKey": "demo",
  "epoch": 1,
  "eventId": "A",
  "eventTime": 1000,
  "status": "NEW",
  "duplicate": false,
  "late": false
}
```

- `status`：`NEW`（未见过，应处理）或 `DUPLICATE`（在保留窗口内见过，应跳过）。
- `late=true`：事件时间已经落在当前 watermark 的保留窗口之外（迟到事件）。
  未见过的迟到事件仍按 `NEW` 投递，但**不会写入去重状态**（写进去的瞬间就已过期，没有意义）。

### 4.2 错误响应

```json
{ "error": "INVALID_ROUTING_VERSION", "message": "..." }
```

| error | HTTP 状态 | 触发场景 |
|---|---|---|
| `INVALID_BODY` | 400 | JSON 非法、缺字段、retention 非正等 |
| `NOT_FOUND` | 404 | 分区/路由不存在 |
| `PARTITION_EXISTS` | 409 | 重复创建 |
| `INVALID_ROUTING_VERSION` | 409 | epoch 与分区当前版本不符；导入快照的新 epoch 不大于快照 epoch；向已 drain 的旧属主写入 |
| `PARTITION_MIGRATING` | 409 | 分区处于 MIGRATING 冻结态时写事件/水位 |
| `WATERMARK_MONOTONIC` | 409 | 水位倒退（相等允许，便于幂等重放） |
| `METHOD_NOT_ALLOWED` | 405 | 方法不支持 |

---

## 5. 核心语义

### 5.1 过期边界（含边界）

事件以其事件时间 `t` 记录锚点（anchor）。当且仅当

```
watermark >= t + retentionMillis
```

时该锚点过期，对应 ID 被清除，此后再次出现即为 `NEW`。
边界取 **`>=`（含边界）**：retention=10000、t=1000 时，watermark=10999 仍判重，
watermark=11000 起同 ID 视为新事件。

### 5.2 乱序重复与锚点提升

- 判重只看“ID 是否仍在保留集合中”，与到达顺序无关。先到 t=1000、后到 t=900
  的重发仍是 `DUPLICATE`。
- 一条重复若携带**更晚**的事件时间，锚点会被提升（lift anchor）到更晚时间，
  保证保留期只会被延长、不会被乱序重发缩短。

### 5.3 水位驱动内存释放

每次推进 watermark 都会清除所有 `anchor + retention <= watermark` 的条目。
分区状态里的 `retainedEventCount` 和 `retainedIdChars` 可直接观察这一释放过程。
测试 `retainedMemoryShrinksWithWatermark` 灌 1000 个 ID，验证条目数随水位
阶段性下降到 0。

### 5.4 路由版本（epoch）与错误路由拒绝

每个分区有单调的 epoch。任何写请求可携带它认为的 epoch，不匹配即 409。
迁移后新属主 epoch 增加，仍携带旧 epoch 的生产者无论把请求发到新属主
（版本落后）还是旧属主（已退役），都会被拒绝。

### 5.5 迁移交接（export → import → complete）

1. `export`：源分区进入 `MIGRATING`，拒绝一切事件/水位写入（关闭双写窗口），
   返回 `{partition, snapshot}`；快照只含**尚未过期**的条目和当前 watermark。
2. `import`：新属主安装快照，必须提供**严格大于**快照 epoch 的 `newEpoch`，
   否则返回 `INVALID_ROUTING_VERSION`（防止旧快照/重放快照覆盖新状态）。
   状态（条目、水位、保留期）随键转移，迁移窗口内重复投递到新属主的事件照常被判重。
3. `complete`：源端清空状态、epoch 自退退役（tombstone，`drained=true`），
   此后拒绝所有写入；迟到到旧属主的请求会被明确拒绝而不是误判为 NEW。
4. 任一步发现问题可 `abort`：仅解冻，数据不动。

> 单机演示中“新属主”用不同的分区键（如 `demo` → `demo-new`）来模拟；
> 快照内的 `partitionKey` 记录真实来源，不强制与目标键同名。真实集群里两者相同。

---

## 6. 请求样例

```bash
BASE=http://localhost:8080

# 创建分区
curl -s -X PUT $BASE/partitions/orders-7 \
  -H 'Content-Type: application/json' \
  -d '{"epoch":1,"retentionMillis":10000}'

# 投递事件
curl -s -X POST $BASE/partitions/orders-7/events \
  -H 'Content-Type: application/json' \
  -d '{"eventId":"evt-1001","eventTime":1000,"epoch":1}'

# 乱序重发（t 更早）→ DUPLICATE
curl -s -X POST $BASE/partitions/orders-7/events \
  -H 'Content-Type: application/json' \
  -d '{"eventId":"evt-1001","eventTime":900,"epoch":1}'

# 错误路由版本 → 409
curl -s -i -X POST $BASE/partitions/orders-7/events \
  -H 'Content-Type: application/json' \
  -d '{"eventId":"evt-2000","eventTime":2000,"epoch":99}'

# 推进水位并观察条目释放
curl -s -X POST $BASE/partitions/orders-7/watermark \
  -H 'Content-Type: application/json' -d '{"watermark":11000,"epoch":1}'
```

完整的 12 步脚本（含迁移全流程）见 [`scripts/examples.sh`](scripts/examples.sh)，
一份真实运行输出保存在 [`docs/example-output.log`](docs/example-output.log)。

---

## 7. 自动化测试

零依赖的迷你测试框架（`src/test/java/dedup/test/`：注解、断言、反射 Runner），
覆盖领域层和真实 HTTP 端到端两层：

- `JsonTest`（5）：JSON 解析/转义/类型校验。
- `DedupCoreTest`（13）：
  - 乱序重复（早/晚时间戳的重发、锚点提升）；
  - 水位**含边界**过期、水位单调、迟到事件投递但不落盘；
  - 1000 个 ID 的内存随水位分阶段释放到 0；
  - 旧 epoch 在新、旧属主两侧均被拒绝；
  - 快照 epoch 不前进（相等/更小）被拒绝；
  - 迁移交接后在途重复被新属主判重、已过期条目不跨快照、abort 不丢数据；
  - 8 线程并发投递 + 并发推进水位下状态一致、最终全部释放。
- `HttpApiTest`（2）：在随机端口启动真实服务，用 JDK `HttpClient`
  走完整链路（健康检查、409/404/405/400、水位边界、迁移全流程）。

```bash
scripts/test.sh
```

最近一次真实运行结果见 [`docs/test-results.log`](docs/test-results.log)：
**20 passed, 0 failed**。

---

## 8. 项目结构

```
src/main/java/dedup/
  Main.java               入口
  Json.java               零依赖 JSON 解析/序列化
  ErrorCode.java          错误码 → HTTP 状态
  ApiException.java
  DedupEntry.java         单条 eventId + 锚点事件时间
  Snapshot.java           迁移快照（record）
  Partition.java          单分区状态、判重、过期清除、迁移冻结/导入/完成
  DedupService.java       分区注册表、epoch 围栏、快照编解码
  http/DedupHttpServer.java   JDK HttpServer 路由
src/test/java/dedup/test/   迷你测试框架 + 三套测试
scripts/build.sh | test.sh | run.sh | examples.sh
deps.lock                     依赖/工具链锁定（无第三方依赖）
docs/                         真实测试与示例输出
```

---

## 9. 设计取舍与边界

- **状态只在内存**：进程重启即丢。生产实现应把快照/检查点持久化（本项目刻意保持
  无依赖、单进程，未实现持久化与集群）。
- **去重集合用 `LinkedHashMap` 顺序扫描过期项**：每次水位推进做一次线性清理。
  超高吞吐场景可换成按到期时间排序的堆/时间轮，当前规模（测试 1000 条目）无压力。
- **没有鉴权/TLS**：`HttpServer` 为明文 HTTP，定位是本地/内网演示与验收。
- **“在途重复”的语义**：迁移冻结后、新属主接管前到达源端的事件会收到 409
  `PARTITION_MIGRATING`，由生产者带新版本重试到新属主；服务不替你缓冲这些事件。
- **时间单位**：`eventTime` / `watermark` / `retentionMillis` 均为毫秒 long
  （也可以是任意单调的逻辑时间单位，只要三者一致）。

---

## 10. 验收对照

| 验收点 | 覆盖位置 |
|---|---|
| 乱序重复 | `duplicatesAreDetectedRegardlessOfArrivalOrder`、`laterDuplicateLiftsAnchorAndExtendsRetention`、HTTP 用例步骤 3 |
| 水位边界 | `boundaryIsInclusiveAtAnchorPlusRetention`（10999 判重 / 11000 过期）、`watermarkMustBeMonotonic` |
| 迁移交接中的重复投递 | `migratedStateDedupsRedeliveredEvents`、HTTP 用例步骤 8–11、示例脚本第 8–12 步 |
| 拒绝错误路由版本 | `staleEpochIsRejectedOnEveryWrite`、`importRejectsSnapshotWithNonAdvancingEpoch`；旧属主 complete 后 drain 拒绝写入 |
| 去重内存随水位释放 | `retainedMemoryShrinksWithWatermark`，状态字段 `retainedEventCount`/`retainedIdChars` 可观测 |
