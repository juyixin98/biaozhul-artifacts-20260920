# 可撤回精确 TopK（按分组滑动窗口）

纯后端服务：按 **group（分组）** 维护**滑动窗口**内的精确 TopK；支持事件**插入**与**撤回**，
分数为增量累加、**可为负**；同分时按 **itemId 字典序**决胜。只使用 JDK 内置
`com.sun.net.httpserver.HttpServer`，**零第三方依赖、零构建工具**。

> 关键正确性约束：**过期滑出与主动撤回对同一条事件的增量恰好只扣减一次**；
> 服务只保留“一个窗口内”的状态，**不保存全部历史再重算**（墓碑也只保留到事件本应过期）。

---

## 1. 依赖与环境

- **JDK 11+**（开发与实测在 OpenJDK 21.0.12；编译目标 `--release 11`）。
- 无任何第三方 jar，无 Maven/Gradle。JSON 与测试断言均为项目内小实现。
- 详见 [`DEPENDENCIES.md`](DEPENDENCIES.md)（含锁定版本与离线 JDK 获取方式）。

确认：

```bash
java -version    # 需要 11 或更高
```

如 `java/javac` 不在 PATH，设置 `JAVA_HOME` 即可，例如：

```bash
export JAVA_HOME=/path/to/jdk
```

---

## 2. 启动命令

```bash
# 用法: scripts/run.sh [port] [windowMs]
scripts/run.sh 8080 10000
# 或用环境变量
TOPK_PORT=8080 TOPK_WINDOW_MS=10000 scripts/run.sh
```

- `port`：监听端口，默认 `8080`。
- `windowMs`：滑动窗口长度（毫秒，逻辑时间），默认 `10000`，必须为正。

启动成功会打印：

```
TopK 服务已启动: http://localhost:8080  窗口 windowMs=10000
```

也可手动编译运行（不依赖脚本）：

```bash
javac --release 11 -d build/classes $(find src/main/java -name '*.java')
java -cp build/classes topk.TopKHttpServer 8080 10000
```

---

## 3. HTTP 接口

所有请求/响应均为 `application/json; charset=utf-8`。
时间戳 `ts` 为**逻辑毫秒时间，由调用方提供**（便于确定性测试/重放）；不传时用服务器墙钟。

**窗口语义**：维护单调水位 `watermark = max(见过的 ts)`；事件有效当且仅当
`event.ts > watermark - windowMs`（左边界为闭开边界：恰好等于左边界即判过期）。

### 3.1 `GET /health`

健康检查，返回窗口长度与分组数。

### 3.2 `POST /groups/{group}/events` — 插入事件（增量）

请求体：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `eventId` | string | 是 | 事件幂等键，组内唯一 |
| `itemId`  | string | 是 | 统计对象（TopK 的条目） |
| `delta`   | integer | 是 | 分数**增量**，**可为负** |
| `ts`      | integer | 否 | 事件时间，缺省为当前墙钟 |

响应 `status`：

- `INSERTED`（HTTP 200）：已并入窗口。
- `DUPLICATE`（HTTP 409）：`eventId` 在窗口内已存在，或处于撤回墓碑保留期内。
- `LATE`（HTTP 422）：事件时间已滑出窗口（迟到），拒绝且不占内存。

### 3.3 `POST /groups/{group}/retract` — 撤回事件（幂等）

请求体：`{"eventId":"e5","ts":4000}`（`ts` 可选）。
先按 `ts` 推进水位，再撤回。响应 `status`：

- `RETRACTED`（200）：撤回成功，增量已扣减一次。
- `ALREADY_RETRACTED`（200）：此前已撤回，本次幂等空操作，**不二次扣减**。
- `EVENT_UNKNOWN`（404）：事件不存在或已过期滑出窗口，无法撤回。

### 3.4 `GET /groups/{group}/topk?k=10&ts=2000` — 查询 TopK

- `k`（必填，非负整数）：取前 K 名。**K 大于元素数时返回全部；K=0 返回空列表。**
- `ts`（可选）：给出则先推进该组水位（用于触发滑出）。
- 排序：分数降序；**分数相同按 itemId 字典序升序**。返回 `rank` 从 1 开始。
- 只要 item 在窗口内仍有至少一条有效事件就在榜（总分可能为 0 或负）。

### 3.5 `GET /groups/{group}/snapshot[?ts=...]` — 排障快照

返回该组完整排序与内部计数（`activeEvents/activeItems/tombstones/expiryQueue/watermark`）。

其它状态码：请求体非法 JSON / 缺字段 / 字段类型错 → **400**；未知路径 → **404**；方法不允许 → **405**。

---

## 4. 请求样例

可直接运行的完整脚本（自动拉起服务、逐步打印请求与响应、结束后关闭）：

```bash
examples/session.sh 18080
```

手工 curl 样例见 [`examples/requests.http`](examples/requests.http)。核心几条：

```bash
BASE=http://127.0.0.1:8080
G=$BASE/groups/leaderboard

# 插入：三件同 5 分 + 一件 8 分
curl -s -X POST $G/events -H 'Content-Type: application/json' \
  --data '{"eventId":"e1","itemId":"bob","delta":5,"ts":1000}'
curl -s -X POST $G/events -H 'Content-Type: application/json' \
  --data '{"eventId":"e2","itemId":"ada","delta":5,"ts":1001}'
curl -s -X POST $G/events -H 'Content-Type: application/json' \
  --data '{"eventId":"e3","itemId":"cyb","delta":5,"ts":1002}'
curl -s -X POST $G/events -H 'Content-Type: application/json' \
  --data '{"eventId":"e4","itemId":"dan","delta":8,"ts":1003}'

# Top2：dan=8 第一；ada/bob/cyb 同分按 ID，ada 第二
curl -s "$G/topk?k=2&ts=2000"

# 负增量；K=100 大于元素数 -> 返回全部
curl -s -X POST $G/events -H 'Content-Type: application/json' \
  --data '{"eventId":"e5","itemId":"ada","delta":-9,"ts":2001}'
curl -s "$G/topk?k=100&ts=3000"

# 撤回（幂等）
curl -s -X POST $G/retract -H 'Content-Type: application/json' --data '{"eventId":"e5","ts":4000}'
curl -s -X POST $G/retract -H 'Content-Type: application/json' --data '{"eventId":"e5","ts":4001}'

# 窗口滑出：水位 11000，左边界 1000 -> e1(bob,ts=1000) 滑出，分数只扣一次
curl -s "$G/topk?k=10&ts=11000"
```

`k=2` 的响应（同分按 ID）：

```json
{
  "group": "leaderboard", "k": 2, "windowMs": 10000, "count": 2,
  "items": [
    {"rank": 1, "itemId": "dan", "score": 8},
    {"rank": 2, "itemId": "ada", "score": 5}
  ],
  "watermark": 2000, "windowStart": -8000
}
```

完整真实输出保存在 [`docs/example-run.log`](docs/example-run.log)。

---

## 5. 设计与正确性说明

代码在 `src/main/java/topk/`：

| 文件 | 职责 |
|---|---|
| `Event.java` | 不可变事件（eventId/itemId/delta/ts） |
| `GroupState.java` | 单组滑动窗口核心状态与不变量 |
| `TopKService.java` | 按分组门面，组间独立加锁 |
| `TopKHttpServer.java` | JDK HttpServer、路由、JSON 校验 |
| `Json.java` | 零依赖 JSON 解析/序列化 |

### 数据结构（每个分组）

- `scores: itemId -> 当前窗口总分`；`counts: itemId -> 有效事件数`。
- `ranked: TreeSet<itemId>`，比较器为 `(-score, itemId)` —— **始终是窗口内的精确全序**，
  TopK 直接取前 K，查询 `O(K)`，不做全量重算。
- `events: eventId -> Event`：窗口内有效事件（撤回即删）。
- `expiry: PriorityQueue<Event>`，按 `(ts, eventId)` 排序，水位推进时惰性滑出。
- `tombstones: eventId -> 事件 ts`：撤回墓碑，**只保留到该事件本应过期的时刻**（随窗口清理）。

### “过期/撤回只扣一次”如何保证

每条事件在其生命周期内**至多**经历一次扣减：

- **撤回**：从 `events` 删除、从 `expiry` 队列移除、扣减增量、写入墓碑；
  之后队列里再也弹不到它，重复撤回命中墓碑返回 `ALREADY_RETRACTED`。
- **过期**：水位推进时从 `expiry` 弹出；若仍在 `events` 中才扣减；
  已撤回的事件不在 `events` 中，**不会再次扣减**。

### “不保存全部历史”如何保证

- 过期事件在滑出时立即从 `events/expiry/scores/counts` 移除。
- 撤回墓碑只保留到 `event.ts <= watermark - windowMs`，随后删除、`eventId` 可复用。
- 因而常驻内存只与“一个窗口内的事件量 + 窗口内撤回量”同阶，与历史总事件数无关。
- 任何分数变更都先把 item 从 `TreeSet` 摘除、改分、再放回，避免可变比较器破坏全序。

> 语义取舍：时间戳由调用方提供（事件时间/ingest time 由你决定）；水位取见过的最大值，
> 窗口内允许**乱序**插入；窗口外的迟到事件直接拒绝。所有时间处理为确定性逻辑时间，便于测试。

---

## 6. 自动化测试

零依赖、自包含测试框架（`src/test/java/topk/`）。一键运行：

```bash
scripts/test.sh        # 编译并运行全部测试，任一失败则退出码非零
```

测试包含三部分，**全部实际运行通过**（日志见 [`docs/test-run.log`](docs/test-run.log)）：

1. **`AcceptanceTest`（10 个手工验收用例）**：逐事件对照窗口内完整排序，覆盖
   并列名次、负增量、K 大于元素数、K=0、窗口滑出、撤回幂等（撤回后过期不再扣）、
   撤回未知/已过期、迟到事件、重复 eventId、事件清零后退出排名与重新入榜、分组隔离。
2. **`PropertyTest`（6000 次随机差分测试）**：对实现喂随机插入/撤回/推进
   （含负增量、乱序、重复 ID、过期撤回、重复撤回），**每一步都与“朴素 oracle
   全量保存历史并重新求和+全排序”对照**完整排名、TopK、有效事件数、墓碑数、水位；
   固定随机种子，可复现。
3. **`HttpSmokeTest`（5 个端到端用例）**：进程内启动真实 HTTP 服务（端口 0 自动分配），
   用 JDK `java.net.http.HttpClient` 打真实请求，覆盖完整链路与
   400/404/405/409/422 状态码。

最近一次实际结果（OpenJDK 21.0.12.1，Ubuntu 24.04）：

```
==== 验收场景（手工构造） 结果: 10 通过, 0 失败, 共 10 ====
==== 随机差分测试（oracle 全量重算） 结果: 1 通过, 0 失败, 共 1 ====
==== HTTP 端到端冒烟 结果: 5 通过, 0 失败, 共 5 ====
全部测试通过 ✔
```

> 说明：差分测试中的 oracle 故意采用“保存全部历史、查询时全量重算”的朴素模型，
> 仅用于**对照**；生产实现（`GroupState`）并不保留历史，两者在 6000 步上逐刻一致。

---

## 7. 目录结构

```
.
├── README.md
├── DEPENDENCIES.md
├── scripts/
│   ├── build.sh          # 仅用 javac 编译主程序与测试
│   ├── run.sh            # 启动 HTTP 服务
│   └── test.sh           # 运行全部自动化测试
├── src/main/java/topk/   # 服务源码（5 个文件，零第三方依赖）
├── src/test/java/topk/   # 测试（自包含框架 + 3 个测试套件）
├── examples/
│   ├── requests.http     # curl 请求样例
│   └── session.sh        # 可一键运行的端到端示例
└── docs/
    ├── test-run.log      # 测试真实运行日志
    └── example-run.log   # 示例真实运行日志
```

---

## 8. 未完成项 / 已知边界

如实列出当前范围之外的内容：

- **纯内存、单实例**：无持久化，重启数据丢失；未做多实例复制/一致性。
- **单窗口长度**：窗口大小为进程级配置，所有分组共用；未做按组自定义窗口。
- **整数分数**：`delta` 限定为 64 位整数（`long`）；需要小数可扩展为 `double`/`BigDecimal`。
- **无鉴权 / 限流 / TLS**：定位为后端库式服务，生产部署应置于网关之后。
- **未做持久化回放**：逻辑时间戳便于重放，但服务本身不提供 WAL/快照恢复。
- 线程模型为固定 8 线程的 HttpServer 线程池；分组内串行、分组间并发，
  极高并发下单组热点会串行化（正确性优先，未做分片优化）。
