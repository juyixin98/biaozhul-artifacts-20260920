# 增量连接聚合视图服务（Incremental Join Aggregation View）

纯后端、零界面的本地服务：维护 **订单行（事实表）⋈ 商品维表** 的增量物化视图，
按商品类别（category）聚合 **数量之和（qty）** 与 **金额之和（amount，定点数）**。
仅处理合成记录。双边（订单侧 / 商品侧）均支持插入、更新、删除；
每一步增量维护都可以与“丢弃聚合、从两张基表全量重算”的结果做差分比对。

- 语言/运行时：Java 17
- HTTP：JDK 自带 `com.sun.net.httpserver.HttpServer`
- 第三方依赖：**无**（JSON、测试框架均为项目内最小自实现）
- 金额类型：`BigDecimal`，统一 2 位小数 `HALF_UP`，杜绝浮点误差

---

## 1. 快速开始

需要 JDK 17+（实测 Temurin 17.0.20.1）。若 `java/javac` 不在 PATH，用环境变量指定：

```bash
export JAVA_HOME=$HOME/jdk17
export JAVAC=$JAVA_HOME/bin/javac JAVA=$JAVA_HOME/bin/java
```

```bash
./build.sh           # 编译 -> build/classes
./test.sh            # 编译 + 运行全部自动化测试（33 项）
./run.sh 8080        # 启动服务（默认端口 8080；也可用 PORT 环境变量）
```

启动后健康检查：

```bash
curl -s http://localhost:8080/health      # {"status":"UP"}
```

一键演示完整验收脚本（另开终端，服务启动后执行）：

```bash
BASE=http://localhost:8080 bash examples/demo.sh
```

---

## 2. 视图与事件模型

视图定义（等价 SQL）：

```sql
SELECT p.category,
       SUM(o.qty)    AS qty,
       SUM(o.amount) AS amount          -- 定点数
FROM   orders o
JOIN   products p ON o.product_id = p.id
GROUP  BY p.category;
```

- 连接采用**内连接**语义：订单行引用的商品不存在时，该行进入响应中的
  `orphan`（孤儿）合计，不属于任何类别；商品之后插入/重插，孤儿自动回归对应类别。
- 类别合计归零时该类别键从视图中移除（与全量 `GROUP BY` 口径一致）。

四类业务事件（JSON 字段）：

| type             | 含义             | 必需字段                                   |
|------------------|------------------|--------------------------------------------|
| `ORDER_UPSERT`   | 插入/更新订单行  | eventId, orderLineId, productId, qty, amount |
| `ORDER_DELETE`   | 删除订单行       | eventId, orderLineId                       |
| `PRODUCT_UPSERT` | 插入/更新商品(含改类) | eventId, productId, category           |
| `PRODUCT_DELETE` | 删除商品         | eventId, productId                         |

约束：qty 为非负整数；amount 为非负数字（按 BigDecimal 解析，序列化为 2 位小数）；
category 非空。`ORDER_UPSERT` 同一 `orderLineId` 再次出现即为**更新**：
旧贡献按旧外键/旧值从视图剔除，新贡献计入。

### 事件 ID 去重范围（重点，验收项）

- **全局唯一命名空间**：不区分事件来源，订单事件与商品事件共用同一个 eventId 空间
  （跨双边同 ID 也算重复）。
- **精确字符串匹配**，按 eventId 去重，不看载荷。
- **记录时机**：事件通过校验后、一旦进入应用流程即登记，**包括语义空操作**
  （如删除不存在的记录）。即“删一条不存在的记录”仍占用该 eventId，之后同 ID 重放为重复。
- **首达事件获胜（first-writer-wins）**：相同 eventId 重放一律跳过；
  若重放载荷与首次不同，响应中标记 `"conflict": true` 以便排查，但**绝不覆盖**首次结果。
- 去重是**无限期、进程内**的（见“局限”）。
- 批量接口 `/events/batch` 内事件按数组顺序逐个应用，各自独立给出 APPLIED/DUPLICATE 结论。

### 删除语义

- 删除存在的记录：正常剔除贡献（删商品会使其订单行变为孤儿）。
- 删除**不存在**的记录：幂等空操作，返回 `"status":"APPLIED","ignored":true`，视图不变；
  对应 eventId 仍留痕（再次同 ID 删除会得到 DUPLICATE）。

---

## 3. HTTP 接口

| 方法 | 路径               | 说明                                            |
|------|--------------------|-------------------------------------------------|
| GET  | `/health`          | 健康检查                                        |
| POST | `/events`          | 应用单个事件（请求体即事件对象）                |
| POST | `/events/batch`    | 批量应用 `{"events":[ ... ]}`，按序逐个报告     |
| GET  | `/view`            | 增量视图快照                                    |
| GET  | `/view/recompute`  | 从基表**全量重算**的视图（验收基准）            |
| GET  | `/view/diff`       | 增量 vs 全量的差异；`consistent:true` 即完全一致 |
| POST | `/admin/reset`     | 清空全部内存状态（演示/测试用，无鉴权）          |

错误：输入类问题返回 `400 {"error": ...}`，且因“先整体校验、后应用”，非法事件不改变状态；
未知路径 404；错误方法 405。

### 请求样例

```bash
# 商品维表：1 -> BOOKS
curl -s -X POST localhost:8080/events -H 'Content-Type: application/json' -d '{
  "eventId":"evt-0001","type":"PRODUCT_UPSERT","productId":1,"category":"BOOKS"}'

# 批量订单（第三条引用不存在的商品 2 -> 孤儿）
curl -s -X POST localhost:8080/events/batch -H 'Content-Type: application/json' -d '{
  "events":[
    {"eventId":"evt-0002","type":"ORDER_UPSERT","orderLineId":101,"productId":1,"qty":2,"amount":10.00},
    {"eventId":"evt-0003","type":"ORDER_UPSERT","orderLineId":102,"productId":1,"qty":3,"amount":5.50},
    {"eventId":"evt-0004","type":"ORDER_UPSERT","orderLineId":103,"productId":2,"qty":1,"amount":99.00}
  ]}'

curl -s localhost:8080/view
# {"categories":[{"category":"BOOKS","qty":5,"amount":15.50}],
#  "orphan":{"qty":1,"amount":99.00},"orderCount":3,"productCount":1,"appliedEventCount":4}

# 维表分类变更：BOOKS -> MEDIA（订单行贡献整类迁移）
curl -s -X POST localhost:8080/events -H 'Content-Type: application/json' -d '{
  "eventId":"evt-0005","type":"PRODUCT_UPSERT","productId":1,"category":"MEDIA"}'

# 重复事件：同 ID 同载荷 -> DUPLICATE/conflict=false；换成不同载荷 -> conflict=true，均跳过
curl -s -X POST localhost:8080/events -H 'Content-Type: application/json' -d '{
  "eventId":"evt-0002","type":"ORDER_UPSERT","orderLineId":101,"productId":1,"qty":2,"amount":10.00}'

# 删除不存在的订单行 -> APPLIED / ignored=true
curl -s -X POST localhost:8080/events -H 'Content-Type: application/json' -d '{
  "eventId":"evt-0006","type":"ORDER_DELETE","orderLineId":404}'

curl -s localhost:8080/view/diff     # {"consistent":true,"diffs":[]}
```

`examples/` 目录提供每步的 JSON 文件、`demo.sh` 与一份真实抓取的原始响应
[`sample-run.txt`](examples/sample-run.txt)。

---

## 4. 目录结构

```
.
├── build.sh / test.sh / run.sh      # 编译 / 测试 / 启动（仅 javac+java）
├── DEPS.lock                        # 依赖锁定说明（零第三方依赖，JDK17）
├── README.md
├── examples/                        # 请求样例 JSON、演示脚本、真实运行记录
│   ├── 01-product-upsert.json … 06-delete-missing.json
│   ├── demo.sh
│   └── sample-run.txt
└── src
    ├── main/java/incagg
    │   ├── model/      Product / OrderLine / EventType / Event
    │   ├── store/      IncrementalViewStore（核心：增量维护+全量重算+差分）、Snapshot
    │   ├── json/       Json（零依赖解析/序列化，数字按 BigDecimal）
    │   └── web/        Main（HttpServer 入口）、ApiHandler、ApiCodec
    └── test/java/incagg
        ├── TestRunner.java          # 迷你断言框架
        ├── StoreTest.java           # 18 项核心验收测试（含 2000 步随机差分）
        └── web/HttpSmokeTest.java   # 15 项 HTTP 端到端冒烟（真实临时端口）
```

---

## 5. 自动化测试与验收对应

`./test.sh` 编译并运行两组测试，退出码非 0 即失败：

1. **`incagg.StoreTest`（18 项）**
   - **A. 维表分类变更**：商品改类后，引用它的订单行数量/金额整类迁移；连续两次改类不串账。
   - **B. 重复业务事件**：同 ID 同载荷 → DUPLICATE；同 ID 冲突载荷 → `conflict=true`
     且首达值保持；批量内重复；跨双边同 ID 仍去重（验证全局命名空间）。
   - **C. 删除不存在记录**：订单/商品两侧删不存在记录均幂等空操作，且 eventId 留痕；
     存在→删除→再删的第二次为空操作。
   - **D. 孤儿生命周期**：先单后品、删品、重插、删孤儿单。
   - **E. 定点数**：`0.10*3 == 0.30`（无 `0.30000000000000004`），更新精确扣减。
   - **F. 随机差分测试**：固定种子，2000 步随机双边增删改 + 旧事件重放，
     **每 50 步及最终都与全量重算逐项比对**。
   - 上述 A–E 的每个断言点都调用 `diffAgainstFull()` **逐步与全量重算比较**。
2. **`incagg.web.HttpSmokeTest`（15 项）**：真实起 `HttpServer` 临时端口，
   用 JDK `java.net.http.HttpClient` 打通健康检查、单发/批量、去重、改类、
   删不存在、diff/recompute、400/404/405 与错误后状态不被污染。

### 实测结果（2026-09-23，本机如实记录）

- `./test.sh`：**33 项全部通过，失败 0**（核心 18 + HTTP 15）。
- 实时服务 + `examples/demo.sh`：全部场景输出符合预期，最终
  `GET /view/diff` 为 `{"consistent":true,"diffs":[]}`；原始响应存档于
  `examples/sample-run.txt`。
- 过程中修复的两个真实问题：差分比较误用 `AggCell` 对象身份（Map.equals）导致假阳性；
  测试中设置受限请求头 `Content-Length`。均已修复并复测通过。

---

## 6. 局限与未完成项（如实说明）

- **纯内存态**：无持久化/WAL，重启后状态与去重日志丢失；未实现崩溃恢复或事件回放文件。
- **去重日志无界增长**：eventId 永久保留（含空操作事件），未做 TTL/分层存储/压缩。
- **单实例、无并发控制 beyond 单进程锁**：所有状态操作在单个 `synchronized` store 上串行，
  未考虑分布式多副本。
- **批量非分布式事务**：批量内逐事件应用、各自留痕；本场景无部分失败回滚需求，
  但这意味着批量中途若某事件非法（400 在应用前统一校验，故正常不会中途失败），
  语义上并非“全有或全无”。
- 连接仅支持 productId **等值内连接**；qty 用 `int`；未做鉴权、限流、TLS 与请求体大小限制
  （`/admin/reset` 裸露，仅限本地演示）。
- 未使用 Maven/Gradle 与 JUnit（刻意为零依赖）；CI 可直接调用 `./test.sh`。
