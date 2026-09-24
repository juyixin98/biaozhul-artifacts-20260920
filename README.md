# sort-merge-join：外存排序合并连接服务

纯后端 Java 服务：对两个 JSONL 表做**等值连接**（键为 64 位整数或 `null`），
使用**外存排序 + 归并连接**，内存预算可配置。重复键产生笛卡尔匹配，`null` 键不参与匹配。
仅依赖 JDK（HTTP 用 `com.sun.net.httpserver`，JSON 解析/序列化为内置实现），无任何运行时第三方依赖。

## 目录结构

```
src/main/java/com/example/smj/
  Main.java             启动入口（命令行参数解析）
  HttpApi.java          JDK HttpServer 接口层
  TaskManager.java      异步任务池、取消、临时目录清理
  JoinEngine.java       编排：排序两表 -> 归并连接 -> 写输出
  ExternalSorter.java   外存排序：分块排序溢写 + 多趟多路归并（扇入 32）
  MergeJoiner.java      归并连接：按键分组，等值组笛卡尔输出
  SpillableGroup.java   键组缓冲：超过预算份额即溢写为临时文件，可重复扫描
  RowParser.java        键提取与校验（整数或 null，其余报错）
  Json.java             无依赖 JSON 解析/序列化（数字保留原文）
src/test/java/com/example/smj/   JUnit 5 自动化测试（18 个用例）
examples/               示例 JSONL 表与 requests.sh 请求样例
```

## 依赖与构建

- **JDK 17+**（开发验证环境：OpenJDK 17.0.20）
- **Maven 3.8+**（开发验证环境：Maven 3.8.7）
- 依赖已锁定在 `pom.xml`：仅 test 作用域 `org.junit.jupiter:junit-jupiter:5.11.4`；
  插件锁定 `maven-compiler-plugin:3.13.0`、`maven-surefire-plugin:3.5.2`、`maven-jar-plugin:3.4.2`。
  运行时零第三方依赖。

```bash
mvn test        # 运行全部自动化测试
mvn package     # 产出 target/sort-merge-join-1.0.0.jar（含 Main-Class）
```

## 启动

```bash
java -jar target/sort-merge-join-1.0.0.jar \
  --port 8080 \                 # 监听端口，默认 8080
  --temp-dir ./tmp \            # 溢写/临时文件根目录，默认 ./tmp
  --default-budget 8388608 \    # 默认内存预算（字节），默认 8 MiB
  --max-concurrent 2            # 并发任务数，默认 2
```

## 内存预算语义

预算是**应用侧记账值**：每行按「JSON 文本 UTF-8 字节数 + 16 字节对象开销」计重。

- **排序阶段**：单表在内存中累积到预算即排序溢写为一个 run；run 数超过 32 时多趟归并，
  最终游标最多同时归并 32 个 run（限制打开文件数）。
- **连接阶段**：左、右键组各分得预算的一半；任一侧键组超过其份额即整体溢写为临时文件，
  组间用**块嵌套循环**连接（左组分块，每块扫描一遍右组）。因此**单个热键组大于内存预算
  也能正确连接**，代价是右组溢写文件被重复扫描。
- 两表依次排序再连接，任意时刻驻留内存 ≈ 1 倍预算（排序）或 2 个半预算键组 + 1 个块（连接）。

## HTTP 接口

所有请求/响应均为 JSON。错误响应：`{"error": "..."}`。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 → `{"status":"ok"}` |
| POST | `/join` | 异步提交 → `202 {"taskId",...}` |
| POST | `/join/sync` | 同步执行，完成才返回；成功 `200`，失败/取消 `500`（含任务详情） |
| GET | `/tasks` | 任务列表 |
| GET | `/tasks/{id}` | 任务状态、统计、错误信息 |
| POST | `/tasks/{id}` | 不存在 → `404` |
| POST | `/tasks/{id}/cancel` | 请求取消（协作式）；已终态 → `409` |

### 提交参数（`/join`、`/join/sync`）

```json
{
  "leftPath": "/abs/left.jsonl",        // 必填，左表 JSONL 文件
  "rightPath": "/abs/right.jsonl",      // 必填，右表 JSONL 文件
  "keyField": "id",                     // 必填，连接键字段名
  "outputPath": "/abs/out.jsonl",       // 必填，结果输出文件
  "memoryBudgetBytes": 4096             // 可选，内存预算（>=4096），缺省用启动参数
}
```

校验失败返回 `400`；输入文件中键不是整数（如 `3.5`、`"1"`）或缺少键字段时，
任务进入 `FAILED` 并保留错误信息。`null` 键的行被丢弃并计数（`leftNullKeysDropped` 等）。

### 输入/输出格式

- 输入：JSONL，每行一个 JSON 对象，键字段为整数或 `null`。
- 输出：JSONL，每行 `{"left":<左行原文>,"right":<右行原文>}`；同一等值组内为笛卡尔积。
  输出行顺序不保证（按多重集语义核对）。

### 任务状态与统计

`GET /tasks/{id}` 返回 `status`（`QUEUED/RUNNING/SUCCEEDED/FAILED/CANCELLED`）、
`error`（失败/取消时保留）、`stats`：

```json
{
  "leftRows": 420, "rightRows": 380,
  "leftNullKeysDropped": 5, "rightNullKeysDropped": 5,
  "leftRuns": 6, "rightRuns": 5,
  "sortSpillWriteBytes": 32162,
  "matchedKeyGroups": 51,
  "spilledKeyGroups": 2,
  "groupSpillWriteBytes": 10400,
  "outputRows": 11369
}
```

### 请求样例

```bash
# 同步连接（小预算强制溢写）
curl -s -X POST http://127.0.0.1:8080/join/sync -H 'Content-Type: application/json' -d '{
  "leftPath": "/abs/examples/left.jsonl", "rightPath": "/abs/examples/right.jsonl",
  "keyField": "id", "outputPath": "/abs/examples/out.jsonl", "memoryBudgetBytes": 4096}'

# 异步提交 -> 轮询 -> 取消
TASK=$(curl -s -X POST http://127.0.0.1:8080/join -H 'Content-Type: application/json' \
  -d '{"leftPath":"...","rightPath":"...","keyField":"id","outputPath":"...","memoryBudgetBytes":4096}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["taskId"])')
curl -s http://127.0.0.1:8080/tasks/$TASK
curl -s -X POST http://127.0.0.1:8080/tasks/$TASK/cancel
```

`examples/requests.sh` 是可直接执行的完整样例（先启动服务，再 `./examples/requests.sh <port>`）。

## 取消与清理语义

- 取消是协作式的：排序读入、溢写、归并、连接各循环都检查取消标志，通常在毫秒级生效。
- 任务取消或失败后：**任务临时目录（排序 run、键组溢写文件）整体删除**，
  **未完成的输出文件也删除**（避免误导性的半成品结果），错误信息保留在任务对象上，
  之后可通过 `GET /tasks/{id}` 查询。

## 自动化测试（`src/test/java`，JUnit 5）

- `JsonTest`：解析/序列化往返、数字原文保留、转义、畸形输入报错。
- `ExternalSorterTest`：溢写排序正确性与多重集一致、NULL 键丢弃计数、预算充足时不溢写、
  多趟归并（run 数 > 32）、**排序中途取消后溢写文件全部清理**、非整数键报错。
- `MergeJoinTest`：重复键笛卡尔匹配、NULL 不匹配、**热键组大于内存预算**（两侧键组均溢写，
  与朴素哈希连接多重集比对一致）、连接中途取消清理临时文件、缺键字段报错。
- `HttpApiTest`：真实 HTTP 服务端到端——热键同步连接（统计断言 + 多重集比对）、
  **异步提交→运行中取消→临时目录清空、错误保留、半成品输出删除**、
  400 校验、非整数键任务 FAILED 且错误可再查询、未知任务 404。

## 实际运行记录（2026-09-24，OpenJDK 17.0.20 / Maven 3.8.7）

- `mvn test`：**Tests run: 18, Failures: 0, Errors: 0, Skipped: 0 —— BUILD SUCCESS**。
- 示例数据 `examples/left.jsonl`（425 行，热键 1001 共 120 行，5 行 NULL 键）、
  `examples/right.jsonl`（385 行，热键 1001 共 80 行，5 行 NULL 键），预算 4096 字节：
  - 同步连接返回 `SUCCEEDED`，统计：`leftRuns=6, rightRuns=5, sortSpillWriteBytes=32162,
    spilledKeyGroups=2, groupSpillWriteBytes=10400, outputRows=11369`
    （热键组约 5.4KB/3.6KB > 单侧 2KB 份额，两侧均溢写；热键笛卡尔积 120×80=9600 行）。
  - 用独立 Python 朴素哈希连接核对：**输出多重集完全一致（11369 行）**。
- 取消演示：两个 30 万行表、预算 4096，任务运行中产生 100 个排序溢写文件；
  `POST /tasks/{id}/cancel` 后状态变为 `CANCELLED`、`error="cancelled by user"` 保留、
  临时目录清空、半成品输出文件不存在。
- 错误演示：缺字段请求 → `400`；键为 `3.5` → 任务 `FAILED`，
  错误 `key field 'id' is not an integer: ...` 保留且可再查询。

## 已知限制 / 未完成项

- 内存预算是应用侧记账（行文本字节 + 固定开销），不是严格的 JVM 堆上限；
  JSON 解析器等内部开销未计入，极端长行场景请预留余量。
- 排序归并在等值键跨 run 时不保证输入顺序稳定（结果按多重集语义正确，行序不保证）。
- 块嵌套循环在双侧热键组都溢写时会重复扫描右组文件（正确性优先，未做哈希回退优化）。
- 任务元数据保存在内存中，服务重启后丢失；无鉴权，仅监听回环/内网使用。
- 输入表需为本地文件路径（服务端文件系统），暂不支持请求体直接上传数据。
