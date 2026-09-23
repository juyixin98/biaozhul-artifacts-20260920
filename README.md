# 增量连接聚合视图（Incremental Join-Aggregation View）

纯 Java 后端服务：本地维护「订单行事实表 ⋈ 商品维表」按商品类别的**数量和**与**金额和**，
支持事实表与维表**双边的插入、更新、删除**的增量计算，金额全程使用 `BigDecimal`
**定点数（scale=2，HALF_UP）**，绝不使用 `double`/`float`。

仅使用合成记录（synthetic data），无外部数据源。纯后端，无界面。

- 技术栈：**Java 17 + JDK 内置 `com.sun.net.httpserver.HttpServer`**
- 第三方依赖：**0 个**（JSON 解析/序列化、测试框架、HTTP 客户端全部手写或使用 JDK 自带）
- 线程安全：视图以单一监视器保护；HTTP 层使用固定线程池

## 目录结构

```
src/com/example/iview/
  json/Json.java               极简 JSON 解析/输出（整数->Long，小数->BigDecimal）
  model/Product.java           商品维表行 (productId, category)
  model/OrderLine.java         订单行 (orderLineId, productId, qty, amount[scale=2])
  view/MaterializedView.java   核心：增量视图 + 独立全量重算 + eventId 去重
  view/ApplyResult.java        单事件处理结果（duplicate/changed/notFound/迁移行数…）
  server/Main.java             HttpServer 启动入口
  server/ViewHandler.java      路由 / JSON 响应
  server/EventParser.java      请求体 -> 领域对象
test/com/example/iview/       零依赖测试（含随机端口的真实 HTTP 端到端测试）
examples/demo.sh              curl 验收演示脚本
examples/demo-output.txt      最近一次实际运行的完整输出
build.sh / test.sh / run.sh   编译 / 测试 / 启动
dependencies.lock             依赖锁定（JDK 版本 + 零三方构件声明）
```

## 依赖与运行环境

- **JDK 17**（仅需 `javac`/`java`，开发与验证实际使用 `OpenJDK 17.0.20.1`）
- 无 Maven/Gradle，无任何 jar；构建仅用 `javac`，见 `dependencies.lock`。

```bash
# 若 java/javac 已在 PATH 上：
./build.sh          # 编译到 out/main 与 out/test
./test.sh           # 运行全部自动化测试
PORT=8080 ./run.sh  # 启动 HTTP 服务（默认 HOST=0.0.0.0 PORT=8080）

# 或显式指定 JDK：
JAVA_HOME=/path/to/jdk-17 ./build.sh
JAVA_HOME=/path/to/jdk-17 ./test.sh
PORT=8099 JAVA_HOME=/path/to/jdk-17 ./run.sh
```

## HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/events` | 应用单个业务事件 |
| POST | `/events/batch` | 按数组顺序应用一批事件 |
| GET  | `/view` | 当前增量聚合结果与计数 |
| GET  | `/verify` | 增量结果与**独立全量重算**逐类别对比 |
| GET  | `/tables` | 原始维表/事实表当前行（诊断用） |
| POST | `/reset` | 清空全部内存状态（含已消费 eventId） |

事件体统一字段：

```json
{
  "eventId": "全局唯一业务事件ID",
  "side": "product | order_line",
  "op": "upsert | delete",
  "product":   { "productId": "P1", "category": "books" },
  "orderLine": { "orderLineId": "L1", "productId": "P1", "qty": 2, "amount": "10.00" }
}
```

- `upsert` 同时承载插入与更新（同一主键再次出现即更新）。
- `delete` 只需对应 id（`orderLine.orderLineId` 或 `product.productId`）。
- `amount` 接受数字或字符串；入库即规整为两位小数定点数。
- 订单行先于商品到达时计入伪类别 `(unmatched)`，商品到达后自动归位；
  删除商品会把其订单行退回 `(unmatched)`，从而保证与全量重算始终一致。

### 请求样例（curl）

```bash
# 维表插入
curl -s -X POST localhost:8080/events -H 'Content-Type: application/json' -d '{
  "eventId":"e-1","side":"product","op":"upsert",
  "product":{"productId":"P1","category":"books"}}'

# 事实表插入
curl -s -X POST localhost:8080/events -H 'Content-Type: application/json' -d '{
  "eventId":"e-2","side":"order_line","op":"upsert",
  "orderLine":{"orderLineId":"L1","productId":"P1","qty":2,"amount":"10.00"}}'

# 维表分类变更（P1: books -> media，关联订单行随之迁移）
curl -s -X POST localhost:8080/events -H 'Content-Type: application/json' -d '{
  "eventId":"e-3","side":"product","op":"upsert",
  "product":{"productId":"P1","category":"media"}}'

# 删除订单行 / 删除商品
curl -s -X POST localhost:8080/events -H 'Content-Type: application/json' -d '{
  "eventId":"e-4","side":"order_line","op":"delete",
  "orderLine":{"orderLineId":"L1"}}'
curl -s -X POST localhost:8080/events -H 'Content-Type: application/json' -d '{
  "eventId":"e-5","side":"product","op":"delete","product":{"productId":"P1"}}'

# 增量 vs 全量重算
curl -s localhost:8080/verify
```

完整可执行样例与真实响应见 `examples/demo.sh` / `examples/demo-output.txt`：

```bash
PORT=8099 ./run.sh            # 终端 1
./examples/demo.sh            # 终端 2（可用参数覆盖 base URL）
```

## eventId 去重范围（明确语义）

- 去重键为**全局 `eventId`**，不区分 side/op，也不比对负载：
  同一 id 的第二次提交一律返回 `"duplicate": true` 且**不产生任何状态变化**，
  即使重放携带的是不同负载（重试串改、消息重复均如此处理）。
- **去重范围 = 单个服务实例的内存生命周期**。进程重启或调用 `POST /reset` 后，
  去重历史清空，同一 id 可被再次接受。本服务不持久化、不跨进程去重
  （这是刻意的范围界定，生产中通常需持久化去重表或依赖日志位点）。
- 删除不存在的记录返回 `changed:false, notFound:true`，但 eventId **照常被消费**；
  此后用同一 id 重放仍是 duplicate。

## 增量算法要点

- 每个类别维护可变累加器 `(qty, amount)`。
- 订单行 upsert：先对旧值做**反向贡献**（数量取负、金额 `negate()`），再加新值。
- 商品分类变更：把该商品下全部订单行的贡献从旧类别整桶搬到新类别，
  返回 `migratedOrderLines`；事实表本身一行不改。
- 商品删除：关联订单行搬入 `(unmatched)`；商品重新插入（可能是新类别）再搬回。
- 数量与金额都归零的类别桶被剪除；全量重算使用同一剪除口径，保证两者逐字节可比。

## 自动化测试与验收

`./test.sh` 运行三个套件（共 **122 项断言**，全部通过）：

1. `JsonTest`（12 项）：数字类型、转义、回环、非法输入拒绝。
2. `MaterializedViewTest`（71 项）：双边增改删、**维表分类变更迁移**、
   **重复业务事件（含不同负载）**、**删除不存在记录**、未匹配行归位、
   定点数 `0.10+0.20=0.30`、去重范围，以及两个不同随机种子各 **4000 步**
   混合事件历史，**每一步都与独立全量重算比对**。
3. `HttpIntegrationTest`（39 项）：随机端口启动真实服务，用 JDK `HttpClient`
   走完整 HTTP 链路覆盖上述验收场景、批处理顺序、400 校验与 `/reset`。

最近一次实际运行结果（`./test.sh`，OpenJDK 17.0.20.1）：

```
JsonTest: 12 checks, 0 failures
MaterializedViewTest: 71 checks, 0 failures
HttpIntegrationTest: 39 checks, 0 failures
ALL TEST SUITES PASSED
```

`examples/demo-output.txt` 是对运行中的服务执行 `examples/demo.sh` 的真实抓存，
其中可见：重复事件 `duplicate:true` 且聚合不变；P1 改类后
books=5.00 / media=10.00；`/verify` 两类均 `equal:true`；
删除不存在记录返回 `notFound:true`；批处理内重复 id 只生效一次且
food 精确为 `0.30`。

## 未完成项 / 已知边界（如实说明）

- **无持久化**：状态与去重历史仅在内存，重启即空；去重不跨进程/重启。
- 单进程内线程安全，但未做分布式一致性或多实例合并。
- 金额超过 `BigDecimal` 范围、类别/ID 长度等未设上限（演示用合成数据）。
- 无鉴权、TLS 与限流；仅适合本地/受控环境验证。
- 未提供 Maven/Gradle 构建文件（刻意保持零依赖，直接 `javac` 即可复现）。
