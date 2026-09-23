# 向量近邻检索服务（纯 JDK，零第三方依赖）

本地向量近邻检索（k-NN）HTTP 服务，仅使用 JDK 自带能力实现：

- HTTP 层：JDK 内置 `com.sun.net.httpserver.HttpServer`
- JSON：自写的极简解析/序列化（`Json.java`）
- 距离：平方 L2 与余弦距离（`Distance.java`）
- 精确基线：暴力全量扫描
- 近似索引：自行实现的 IVF（固定种子 k-means 聚类 + 倒排列表 + nprobe 探测）
- 支持维度校验、删除（tombstone）、元数据过滤、搜索预算（距离计算次数上限）
- 余弦距离拒绝零向量（插入和查询均拒绝，返回 400）

无 UI，纯后端。

## 依赖与锁定

**运行期 / 编译期第三方依赖：无。** 只用 JDK 标准库。

| 项 | 版本 / 说明 |
|---|---|
| JDK | Java 17+（开发与实际验证使用 **Eclipse Temurin JDK 21.0.5+11**，见 `deps.lock`） |
| 构建 | `javac`，无需 Maven/Gradle（脚本自动探测 `JAVA_HOME` / `~/tools` / `PATH`） |
| 测试 | 自写极简断言框架 + JDK `java.net.http.HttpClient` 做真实 HTTP 端到端测试 |
| 第三方 jar | 无；不联网、不解析任何外部包 |

`deps.lock` 记录了验证环境的精确 JDK 版本与校验信息。

## 目录结构

```
src/main/java/com/example/vecsearch/
  Json.java            极简 JSON 解析/写出
  Metric.java          L2 / COSINE
  Distance.java        距离计算 + 每次搜索的距离计数器
  VectorStore.java     线程安全内存存储（含维度校验、删除）
  Filter.java          元数据等值过滤（AND）
  SearchHit.java       结果记录
  IvfIndex.java        自实现 IVF 近似索引（固定种子 k-means）
  SearchService.java   精确 / 近似搜索、预算、索引懒重建
  ApiException.java    带 HTTP 状态码的异常
  HttpServerMain.java  HTTP 路由与入口
src/test/java/...      自动化测试（核心逻辑 + HTTP 端到端，61 项检查）
src/eval/java/...      召回率 / 距离计算次数评测（固定种子）
scripts/               build.sh / test.sh / run-server.sh / eval.sh
examples/requests.sh   curl 请求样例
deps.lock              锁定的环境与依赖（本项目无外部依赖）
```

## 构建、测试、启动

需要 JDK 17+。如 `javac` 不在 PATH，可设置 `JAVA_HOME`，或把 Temurin 21 解压到 `~/tools/jdk-21.0.5+11`（脚本会自动识别）。

```bash
# 1. 编译（输出到 out/）
scripts/build.sh

# 2. 运行自动化测试（核心单测 + 真实 HTTP 端到端）
scripts/test.sh

# 3. 启动服务（默认 0.0.0.0:8080；PORT=9090 scripts/run-server.sh）
scripts/run-server.sh

# 4. 另一个终端：跑 curl 请求样例
examples/requests.sh          # 默认 BASE=http://localhost:8080

# 5. 召回率与距离计算次数评测（固定种子，可复现）
scripts/eval.sh
```

服务启动参数（JVM 系统属性，均可选）：`-Dport=8080 -Dthreads=8 -Dnlist=32 -DkmeansIterations=20 -Dseed=42`。

## HTTP 接口

请求/响应均为 JSON。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 |
| GET | `/stats` | 向量数、维度、索引状态 |
| POST | `/insert` | 插入/更新单条 |
| POST | `/batch` | 批量插入 |
| POST | `/delete` | 按 id 删除 |
| POST | `/search` | 精确 / 近似 k-NN 检索，支持过滤、预算、nprobe |
| POST | `/reindex` | 强制重建近似索引 |
| POST | `/reset` | 清空全部数据（测试辅助） |

### `/insert`

```json
{
  "id": "a1",
  "vector": [0.1, 0.2, 0.3],
  "metadata": {"cat": "red", "year": 2024},
  "metric": "L2"
}
```

- 首个向量确定全局维度，之后维度不一致返回 `400`。
- 同一 `id` 再次插入为 upsert（替换向量与 metadata；维度仍校验）。
- `metric` 用于零向量校验：`COSINE` 下零向量返回 `400`；缺省按 `L2` 处理。
- `metadata` 可选，必须是 JSON 对象。

### `/batch`

```json
{
  "metric": "L2",
  "vectors": [
    {"id": "a1", "vector": [0.1, 0.2], "metadata": {"cat": "red"}},
    {"id": "a2", "vector": [0.3, 0.4]}
  ]
}
```

### `/delete`

```json
{"id": "a1"}
```

响应 `{"id":"a1","deleted":true,"size":3}`。再次删除返回 `deleted:false`。删除后任何搜索（精确/近似、带不带过滤）都不会再返回该 id；近似索引在下次近似搜索时自动重建。

### `/search`

```json
{
  "vector": [0.1, 0.2, 0.3],
  "metric": "COSINE",
  "k": 10,
  "mode": "approx",
  "nprobe": 8,
  "budget": 500,
  "filter": {"cat": "red"}
}
```

| 字段 | 取值 | 缺省 |
|---|---|---|
| `metric` | `L2` 或 `COSINE`（必填） | — |
| `k` | 正整数 | `10` |
| `mode` | `exact`（暴力基线）/ `approx`（IVF） | `exact` |
| `nprobe` | 近似模式探测的簇数（搜索预算） | `nlist/8`（此处即 4） |
| `budget` | 向量-向量距离计算次数上限，到数即停 | 无限制 |
| `filter` | 元数据等值条件，多条件为 AND；缺失键不匹配 | 空（全部通过） |

响应包含命中结果以及距离计算计数：

```json
{
  "mode": "approx", "metric": "COSINE", "k": 2, "count": 2,
  "hits": [
    {"id": "d1", "distance": 0.0, "metadata": {"g": 1}},
    {"id": "d2", "distance": 0.006116, "metadata": {"g": 1}}
  ],
  "vectorDistanceCalculations": 3,
  "centroidDistanceCalculations": 3,
  "totalDistanceCalculations": 6,
  "nprobe": 3,
  "indexBuiltThisRequest": true
}
```

说明：

- L2 返回**平方**欧氏距离（排序与 k-NN 结果同欧氏距离一致，省一次开方）。
- COSINE 返回 `1 - 余弦相似度`，范围 [0, 2]；零查询向量返回 `400`。
- `vectorDistanceCalculations` 是与存储向量的距离计算次数；`centroidDistanceCalculations` 是近似检索与质心的距离次数，分开计数。
- 过滤在距离计算之前短路：不满足过滤条件的候选**不产生**距离计算。
- 近似索引在数据变更后标记 dirty，下一次 `approx` 搜索自动重建；也可用 `/reindex` 主动重建。

### 错误码

| 状态码 | 场景 |
|---|---|
| 400 | JSON 非法、维度不一致、余弦零向量、参数非法 |
| 404 | 未知路径 |
| 405 | 方法不允许 |
| 409 | 空库上搜索 |

错误体：`{"error": "dimension mismatch: expected 2 but got 3"}`。

## 近似索引设计（IVF）

1. **训练**：对全量存活向量做固定种子（seed=42）k-means，`nlist=32`、20 轮迭代。
   - 质心初始化：固定 RNG 洗牌后取前 k 个向量（可复现）。
   - 空簇用随机存活向量重新播种。
   - 聚类在原始向量空间用 L2 分配；查询时质心排序使用实际搜索指标（L2/COSINE）。
2. **倒排列表**：每个向量按最终归属挂到一个簇。
3. **查询**：先对全部质心按搜索指标排序（计入 centroid 计数），只扫描前 `nprobe` 个簇内的向量（计入 vector 计数）。
4. 数据任何增删改后索引失效并懒重建，因此**近似搜索永远基于不含已删除 id 的数据**。

搜索预算有两种含义，评测中都给出：
- `nprobe`：探测簇数（主要预算旋钮）；
- `budget`：硬性的向量距离计算次数上限，精确和近似模式均生效。

## 实际运行结果（如实记录）

以下为在本机（Ubuntu 24.04 x86_64，Temurin JDK 21.0.5+11，无外部依赖）的真实输出。

### 自动化测试：`scripts/test.sh`

```
RESULT: all 61 checks passed
```

覆盖：距离数值正确性、插入/upsert 维度校验、精确 L2/COSINE 排序、余弦零向量插入与查询被拒（400）、删除后精确与近似搜索均不返回已删 id、删除后可重新插入、元数据 AND 过滤与缺失键、过滤短路不计距离、预算上限精确停止、IVF nprobe=nlist 时召回 10/10、真实 HTTP 端到端（400/404/405/409、批量、索引复用）。

### 召回评测：`scripts/eval.sh`（seed=42，可复现）

数据：8 个高斯簇 × 150 点 + 60 个均匀离群点 = **1260** 个 16 维向量；50 个近簇查询；k=10；nlist=32。

**L2**

| nprobe | recall@10 | 平均向量距离/查询 | 相对精确基线节省 |
|---:|---:|---:|---:|
| 1 | 90.6% | 127.5 | 89.9% |
| 2 | 98.4% | 238.0 | 81.1% | 4 | 100.0% | 403.5 | 68.0% |
| 8 | 100.0% | 710.6 | 43.6% |
| 16 | 100.0% | 1220.7 | 3.1% |
| 32 | 100.0% | 1260.0 | 0.0% |

显式距离预算上限（nprobe=32，扫描到上限即停）：

| budget | recall@10 | 实际向量距离/查询 |
|---:|---:|---:|
| 50 | 34.8% | 50.0 |
| 100 | 68.6% | 100.0 |
| 200 | 100.0% | 200.0 |
| 400 | 100.0% | 400.0 |

**COSINE**

| nprobe | recall@10 | 平均向量距离/查询 | 相对精确基线节省 |
|---:|---:|---:|---:|
| 1 | 90.0% | 127.2 | 89.9% |
| 2 | 98.2% | 147.4 | 88.3% |
| 4 | 100.0% | 218.7 | 82.6% |
| 8 | 100.0% | 385.5 | 69.4% |
| 16 | 100.0% | 743.3 | 41.0% |
| 32 | 100.0% | 1260.0 | 0.0% |

显式预算：budget=50 → 35.8%，100 → 69.6%，200/400 → 100.0%。

精确基线固定为每次查询 **1260** 次向量距离计算（全量扫描）；质心距离每次近似查询固定 32 次（与 nprobe 无关，排序需评估全部质心）。

**离群查询**：20 个紧邻离群点的查询，精确 top-1 100% 命中该离群点；IVF nprobe=8 的 recall@10 为 **98.0%**（离群点远离簇质心，是近似检索最难的情形）。

**删除 + 过滤不变式**：删除 100 个 `color=red`（簇 0/1）向量后，对 100 个随机查询加 `filter color=red`：

```
Deleted ids returned by exact search:  0
Deleted ids returned by approx search: 0
Non-red hits returned by either mode:  0
Approx recall vs filtered exact set: 100.0%, avg vec distances/query: 300.0
```

完整文本见评测输出 `results/eval-output.txt`，HTTP 示例原始输出见 `results/example-output.txt`（重新运行命令即可再生）。

## 已知限制 / 未完成项

- 纯内存存储，进程退出数据即丢失（题目要求本地服务，未要求持久化）。
- 过滤只支持多键等值 AND；不支持范围、OR、嵌套表达式。
- k-means 在原始向量空间以 L2 聚类，COSINE 共用同一质心结构（查询时按余弦排序质心），对纯余弦场景不是最优；当前数据上 recall 仍达标。
- 近似索引为全量重建（数据变更后懒触发），没有增量插入；万级以上数据批量重建会有停顿。
- 单实例、无鉴权、无 TLS（定位为本地/实验服务）。
- HTTP 请求体未设置大小上限，超大请求依赖 JDK/OS 限制。
