# 向量近邻检索服务（纯 JDK，零第三方依赖）

基于 **Java 17** 标准库（`com.sun.net.httpserver.HttpServer`）实现的本地向量近邻检索后端服务。
无界面、无框架、**零第三方运行时/编译期依赖**（JSON 解析为自行实现的极小解析器），
因此依赖锁即为"只用 JDK 17 API"这一条。

## 功能

- 两种距离度量（建集合时固定）：
  - **L2**：欧氏距离 `sqrt(Σ(a-b)²)`，允许零向量；
  - **COSINE**：余弦距离 `1 - cos`，**插入和检索都拒绝零向量**（角度无定义），返回 400。
- **维度校验**：首条向量锁定集合维度，后续维度不符一律 400；查询向量同样校验。
- **增删改查**：单条/批量 upsert（批量整体校验、原子提交）、按 id 删除、按 id 查询。
- **标签过滤**：每条向量可带 `filter` 键值对；检索按"全部相等"（AND）筛选。
- **精确基线**：暴力线性扫描，返回真实距离，并统计本次查询的**距离计算次数**。
- **近似索引（自行实现）**：IVF（倒排聚类索引）
  - 固定种子的 **k-means++ 初始化** + **Lloyd 迭代**（余弦模式为球面 k-means）；
  - 搜索预算 `nprobe`：只扫描离查询最近的 nprobe 个簇；
  - 支持用指定参数重新训练（`POST /v1/index/rebuild`），写入/删除同步维护倒排链。
- **删除安全**：删除即从倒排链物理移除，任何预算、是否带过滤都不可能返回已删除 id。
- **可复现**：数据生成与 k-means 训练都接受显式随机种子。

## 目录结构

```
src/main/java/vecsearch/
  Main.java                 入口（参数：--port/--host/--metric/--threads）
  core/                     Metric/Distances/DistanceMeter/TopK/TaggedVector/
                            SearchHit/SearchOutcome/VectorStore
  index/                    SearchIndex 接口、IvfIndex、StoreView、IndexOptions
  json/                     零依赖 JSON 解析器 / 序列化器
  server/                   HttpServer、路由处理器、请求解析
  util/ApiException.java    带 HTTP 状态码的业务异常
src/test/java/vecsearch/
  TestAll.java              测试总入口（零依赖迷你测试框架，不使用 JUnit）
  testutil/                 Assert、TestRunner
  tests/                    距离/存储/IVF/JSON/HTTP端到端 共 40 个用例
  eval/DataGenerator.java   固定种子：高斯聚簇 + 离群点 + 簇间边界查询
  eval/Evaluation.java      验收：多预算召回率、距离计算次数、过滤与删除校验
scripts/                    build / run-tests / run-eval / run-server
examples/                   http-demo.sh（curl 全流程）与 requests/*.json 样例
results/                    实际运行留存的报告
```

## 依赖与锁定

| 项目 | 版本/要求 |
| --- | --- |
| JDK | **17**（仅用标准库，开发/实测 `17.0.20.1`） |
| 第三方库 | **无**（无 Maven/Gradle 坐标，故 `dependency-lock` 即无外部坐标） |
| 构建方式 | 直接 `javac`，见 `scripts/build.sh` |
| HTTP 客户端（仅演示/测试） | `curl`、JDK 自带 `java.net.http.HttpClient` |

> 因为不引入任何外部 jar，也就不存在供应链依赖需要锁定；`javac -version` 与源码共同构成复现基线。

## 构建

```bash
# 若 java/javac 已在 PATH（JDK 17）
./scripts/build.sh

# 或显式指定 JAVA_HOME
JAVA_HOME=/path/to/jdk-17 ./scripts/build.sh
```

产物在 `build/classes`（主）与 `build/test-classes`（测试）。

## 启动服务

```bash
# L2（默认），端口 8080
./scripts/run-server.sh

# 余弦模式、指定端口与线程数
./scripts/run-server.sh --metric COSINE --port 9090 --threads 8
```

等价于：

```bash
java -cp build/classes vecsearch.Main --metric L2 --host 0.0.0.0 --port 8080
```

启动后：

```bash
curl http://localhost:8080/          # 接口清单
curl http://localhost:8080/healthz   # 健康检查
curl http://localhost:8080/v1/stats  # 集合与索引状态
```

## HTTP 接口

所有请求/响应均为 JSON。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/v1/vectors` | 单条 `{id,vector,filter?}` 或批量 `{vectors:[...]}` upsert |
| GET | `/v1/vectors/{id}` | 查询单条 |
| DELETE | `/v1/vectors/{id}` | 删除单条 |
| POST | `/v1/search/exact` | 精确基线：`{vector,k,filter?}` |
| POST | `/v1/search` | 近似检索：`{vector,k,filter?,nprobe}`（索引懒构建） |
| POST | `/v1/index/rebuild` | 重新训练：`{nlist?,maxIters?,seed?}` |
| GET | `/v1/stats` | 维度、数量、索引参数与各簇大小 |
| GET | `/healthz` | 存活探针 |

错误统一为 `{"error": "..."}`，状态码：400（参数/维度/零向量等）、404（不存在/路由）、405、500。

### 请求样例（`examples/requests/`）

```bash
# 批量插入
curl -s -X POST localhost:8080/v1/vectors \
  -H 'Content-Type: application/json' \
  -d @examples/requests/upsert-batch.json

# 精确检索（带过滤）
curl -s -X POST localhost:8080/v1/search/exact \
  -H 'Content-Type: application/json' \
  -d @examples/requests/search-exact.json

# 训练索引
curl -s -X POST localhost:8080/v1/index/rebuild \
  -H 'Content-Type: application/json' \
  -d @examples/requests/index-rebuild.json

# 近似检索（nprobe 为预算）
curl -s -X POST localhost:8080/v1/search \
  -H 'Content-Type: application/json' \
  -d @examples/requests/search-approx.json
```

检索响应字段：

```json
{
  "exact": false,
  "nprobe": 4,
  "distanceComputations": 273,
  "returned": 10,
  "hits": [{"id": "c03-0021", "distance": 0.1234, "filter": {"cluster": "3", "kind": "core"}}]
}
```

完整可执行的 curl 全流程演示（含维度报错与余弦零向量拒绝）：

```bash
./examples/http-demo.sh        # 留存输出见 results/http-demo-output.txt
```

## 自动化测试

```bash
./scripts/run-tests.sh
```

零依赖迷你测试框架（`testutil/`），共 **5 个套件 40 个用例**，覆盖：

1. 距离数值正确性、对称性、零向量策略、维度不匹配；
2. 存储维度锁定、零向量拒绝、upsert/delete、AND 过滤、距离计数、批量原子性；
3. IVF 全预算与暴力结果一致、预算-计算次数单调、删除物理移除、增量写入、
   覆盖不重复、过滤安全、余弦一致性、种子可复现、空索引；
4. JSON 解析/序列化往返与非法输入 400；
5. 真实 HTTP 端到端（JDK HttpClient）：状态码、JSON、过滤、删除、余弦零向量。

## 验收（固定种子召回率评测）

```bash
./scripts/run-eval.sh
```

数据集（`seed=20260923`，两种度量各跑一次）：24 维、16 个高斯簇 × 110 点 +
32 个离群点（共 **1792**）、120 个查询（其中 40% 是位于两簇之间的"边界查询"，
10% 指向离群区域），k=10，IVF `nlist=32`。以暴力精确检索为基线，对不同 `nprobe`
报告平均 recall@10、平均距离计算次数、实际扫描点占全集比例，并检查：

- 过滤结果是否全部满足过滤条件；
- 跨簇删除 24 个 id 后，任何预算（含叠加过滤）是否都不返回已删除 id；
- 全预算 `nprobe=nlist` 时 recall 是否严格为 1.0（正确性回归）。

实际运行结果见 **`results/evaluation-output.txt`** 与机读版
**`results/evaluation-report.json`**。

## 设计要点 / 成本口径

- 距离计算统一走 `DistanceMeter`，每对向量计 1 次；IVF 的成本 =
  `nlist` 次"查询-簇心"距离 + 被探测簇内的点距离，精确与近似因此可比。
- L2 内部比较用平方 L2（少开根号、排序等价），仅对最终命中换算回真实 L2。
- 余弦索引存储单位向量、簇心用归一化均值（球面 k-means），内部用 `1-dot`。
- 并发：`ReentrantReadWriteLock`，写串行、读（检索）并发；每查询独立 meter。

## 未完成项 / 已知边界

- 数据只在内存，无持久化（重启即清空）——需求仅要求本地服务，未包含磁盘存储。
- 未实现压缩/PQ/图索引（HNSW）等更复杂的 ANN；近似索引只有 IVF 一种。
- 过滤为精确键值相等匹配，没有数值区间、全文/多值标签等表达式。
- 标签值在 API 层统一按字符串处理（请求中非字符串值会被 `String.valueOf` 接受）。
- 没有鉴权/TLS，定位为本地/内网工具。
