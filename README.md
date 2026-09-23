# qsketch — 流式分位数摘要服务（q-digest）

纯 Java 后端，实现**可合并的分位数摘要** [q-digest (Shrivastava et al., SIGMOD 2004)]，
基于 JDK 自带的 `com.sun.net.httpserver.HttpServer` 暴露 HTTP 接口。
**不保留任何原始样本**：只存储一棵二叉树上的加权节点。

- 运行时依赖：**零第三方依赖**，仅需 JDK 17+
- 测试依赖：JUnit Platform Console Standalone 1.10.2（仅测试期，校验和锁定，见 `deps/deps.lock`）
- 构建方式：`javac`/`jar` 直接编译，无需 Maven/Gradle

---

## 1. 快速开始

```bash
# 需要 JDK 17+
java -version

# 编译 + 打包
./scripts/build.sh        # 编译到 build/classes
./scripts/package.sh      # 生成 build/dist/qsketch.jar

# 启动（默认 8080，可用 --port 改端口）
java -jar build/dist/qsketch.jar --port 8080
```

跑自动化测试（首次会按锁定的 SHA-256 下载 JUnit jar）：

```bash
./scripts/test.sh
```

端到端接口演示脚本（需服务已启动，另开一个终端）：

```bash
./scripts/demo.sh         # 默认 http://localhost:8080
```

---

## 2. HTTP 接口

所有请求/响应均为 JSON。摘要存内存，重启即失。

| 方法 | 路径 | 说明 | 请求体 / 参数 |
|---|---|---|---|
| POST | `/summaries` | 创建摘要 | `{"id":"a","eps":0.01,"universe":100000}` |
| GET | `/summaries` | 列出全部摘要 | — |
| GET | `/summaries/{id}` | 查看参数与规模 | — |
| DELETE | `/summaries/{id}` | 删除 | — |
| POST | `/summaries/{id}/values` | 插入观测值（整数） | `{"values":[1,2,3]}` |
| GET | `/summaries/{id}/quantile?q=0.5` | 查询分位数 | `q∈[0,1]` |
| GET | `/summaries/{id}/rank?x=42` | 查询 x 的累计秩区间 | — |
| POST | `/summaries/merge` | 分片合并 | `{"target":"a","sources":["b","c"]}` |
| GET | `/summaries/{id}/serialize` | 序列化（base64 二进制） | — |
| POST | `/summaries/deserialize` | 反序列化创建摘要 | `{"id":"a2","data":"...."}` |

### 参数语义

- `eps`：秩误差参数，`0 < eps < 1`，越小越精确、节点越多。
- `universe`：取值宇宙大小；所有值必须是 `[0, universe)` 内的整数。
  （浮点/任意域可通过外部秩映射先映射成整数，本服务只接受整数。）
- 只有 `eps` 与 `universe` **完全相同**的摘要才能合并，否则返回
  `409 {"error":"incompatible summaries: ..."}`。

### 响应字段含义

`rank` 查询返回：

```json
{"rankLowerBound":29952,"rankUpperBound":30016,"estimatedRank":29984.0,"maxRankError":817.0}
```

真实累计秩 `F(x)=#{vᵢ≤x}` 一定落在 `[rankLowerBound, rankUpperBound]` 内；
`estimatedRank` 为区间中点；`maxRankError=⌈eps·n⌉+K`（见下节）。

`quantile` 查询返回的 `value` 是 ε-近似分位数，并附带该值在摘要中的秩区间。

### 序列化格式

二进制大端（`DataOutputStream`），格式版本号 1：

```
int version=1 | double eps | int universe | long n | int nodeCount
重复 nodeCount 次: int nodeId | long weight
```

反序列化时校验：版本号、参数范围、节点 id 合法性、权重为正、**权重之和必须等于 n**，
任一不满足返回 400。

### curl 示例

```bash
B=http://localhost:8080

curl -s -X POST $B/summaries -H 'Content-Type: application/json' \
  -d '{"id":"a","eps":0.01,"universe":100000}'

curl -s -X POST $B/summaries/a/values -H 'Content-Type: application/json' \
  -d '{"values":[5,10,10,42,99]}'

curl -s "$B/summaries/a/quantile?q=0.5"
curl -s "$B/summaries/a/rank?x=42"

curl -s -X POST $B/summaries -H 'Content-Type: application/json' \
  -d '{"id":"b","eps":0.01,"universe":100000}'
curl -s -X POST $B/summaries/merge -H 'Content-Type: application/json' \
  -d '{"target":"a","sources":["b"]}'

# 参数不兼容 -> 409
curl -s -X POST $B/summaries -H 'Content-Type: application/json' \
  -d '{"id":"c","eps":0.05,"universe":100000}'
curl -s -i -X POST $B/summaries/merge -H 'Content-Type: application/json' \
  -d '{"target":"a","sources":["c"]}'

# 序列化 / 反序列化
curl -s $B/summaries/a/serialize
```

`scripts/demo.sh` 用 python3 批量造数，完整演示「两分片 → 拒绝不兼容合并 →
合并 → 查询 → 序列化」流程。

---

## 3. 算法与秩误差保证

### 数据结构

在取值宇宙上构造二叉树（宇宙向上取整到 2 的幂 `S`，高 `K=log₂S`；多余叶子恒空）。
每个树节点带一个整数权重：观测值 `x` 先落在叶子 `S+x`；压缩时小权重沿树上浮。
存储为稀疏的 `nodeId → weight` 哈希表，**从不保存样本本身**。

### 压缩不变量

阈值 `T = ⌊eps·n / K⌋`。对每个非根节点 v 反复做自底向上的一趟扫描：

```
若 count(v) + count(left(v)) + count(right(v)) ≤ T：
    把 count(v) 整体提升到父节点
```

插入采用**惰性压缩**：插入只 O(1) 累加叶子权重，在读查询/合并/序列化前才执行
一次扫描。自底向上的分发结果只取决于最终权重，与插入历史无关，因此惰性与逐条
压缩得到的摘要完全一致，但批量写入快得多。

### 秩误差界（本实现给出的严格保证）

对任意 x，返回区间 `[L(x), U(x)]`，真实累计秩 `F(x)=#{vᵢ≤x}` 必在其中：

- `L(x)`：范围完全 ≤ x 的节点权重之和（沿根→叶路径累加左侧整棵子树）；
- `U(x) = L(x) + 路径权重`，路径权重为根→叶(x) 路径上的节点权重和。

由压缩不变量可得经典的路径引理：路径上每对相邻节点（一个是另一个的父节点），
连同父节点的另一个孩子，构成一个权重和 `> T` 的不交族；故

```
路径权重 ≤ K·(T+1) ≤ eps·n + K
```

因此：

```
|F(x) − estimatedRank(x)| ≤ (eps·n + K)/2        （中点估计）
U(x) − L(x) ≤ eps·n + K                          （区间宽度，严格成立）
```

分位数查询在 `L/U` 上二分反演：返回最小的使 `L(v) ≥ target` 的 v，
v 占据的真实秩区间 `[F(v−1)+1, F(v)]` 与 `[target−(eps·n+K), target+(eps·n+K)]`
相交，即标准 ε-近似分位数。

> 说明：q-digest 的经典表述是加性误差 `eps·n`；有限流下由于整数阈值多出一个
> `K = ⌈log₂universe⌉` 的加性项。当 `K | eps·n` 时严格为 `eps·n`；
> 否则误差界为 `eps·n + K`。测试同时校验「区间包含真实秩」和该数值界。

### 空间界

每层上的活跃节点族（节点+两孩子）两两交叠有限且每族权重 `> T ≥ eps·n/K − 1`，
故每层活跃节点 `O(n/(T+1))`，总节点数

```
#nodes = O(K · n/(T+1)) = O(K²/eps)
```

**与样本量 n 无关**（只依赖 eps 和 log(universe)）。代码中 `nodeCountBound()`
给出每层 `2·⌈n/(T+1)⌉+1` 的保守上界，测试在 200 万样本下断言实际节点数不超过它，
且 10 万→200 万样本节点数同量级（实测约 1.3k–1.7k）。

### 合并

合并 = 两个摘要的节点权重逐点相加后跑一次压缩扫描，结果等于在两个流的并集上
直接构建摘要（q-digest 的可合并性）。合并要求 `eps`、`universe` 严格相等，
否则抛异常 / HTTP 409，且失败时目标摘要不被修改（先全量校验再写入）。

---

## 4. 测试与实测结果

`./scripts/test.sh` 实际运行（JDK 17，固定随机种子，2026-09 本机执行）：

```
12 tests, 12 successful, 0 failed   (总耗时约 4–7 秒)
```

关键用例与实测数字（固定种子，可复现）：

| 场景 | 规模 | 结果 |
|---|---|---|
| 升序 0..99999 | n=100k, eps=0.01 | 秩区间处处包含精确秩；中位数答 50048（真值 50000，界 1024+17） |
| 一半重复值 | n=200k, universe=10k | 区间包含精确秩；627 节点 |
| 重尾（Pareto α=1.16） | n=500k, universe=1M | 30,320 个不同值 → **2,355 节点**；q=0.5 答 1816，真实秩区间 [249976,250146]（目标 250000） |
| 误差界扫描 | eps∈{.05,.01,.005} | 最大中点误差分别 1482.5 / 296.5 / 143.5，均 < slack/2（15018/3018/1518） |
| 内存规模 | n=100k vs 2,000k | 节点 1690 → 1279（**不随 n 增长**），均低于理论界 |
| 8 分片合并 | 8×50k | 合并结果秩区间包含并集精确秩；正序/逆序合并分位数答案一致；与直接构建一致 |
| 不兼容合并 | eps / universe 不同 | 均被拒绝（异常信息含 eps/universe），目标摘要不变 |
| 序列化往返 | n=120k | 参数/计数/节点数/全部秩区间逐点一致；约 12 字节/节点 |
| 坏数据 | 截断、权重不平、版本 99 | 全部抛 IOException / HTTP 400 |
| HTTP 端到端 | 真实 HttpServer | 创建/插入/查询/合并拒绝/合并/序列化/404/405/400 全覆盖 |

HTTP 实测示例（universe=100000，合并 0..79999 共 8 万个值，eps=0.01）：

```
q=0.5  -> value=40000  rankInterval=[40000,40064]  (目标秩 40000，精确)
q=0.9  -> value=72000  rankInterval=[72000,72064]  (目标秩 72000，精确)
q=0.99 -> value=79232  rankInterval=[79232,79296]  (目标秩 79200，偏差 32 < 界 817)
rank(29999) -> [29952,30016] 中点 29984           (精确秩 30000)
不兼容合并 -> HTTP 409 incompatible summaries: eps 0.0100000 != 0.0500000
```

---

## 5. 目录结构

```
src/main/java/com/example/qsketch/
  QDigest.java                 # 摘要：压缩、合并、秩/分位数、二进制序列化
  Json.java                    # 零依赖极简 JSON 解析/序列化
  server/SummaryStore.java     # 线程安全的内存注册表
  server/HttpServerMain.java   # JDK HttpServer 路由与处理器
src/test/java/com/example/qsketch/
  QDigestTest.java             # 算法验收测试（11 个，含并发压力）
  HttpApiTest.java             # HTTP 端到端测试（1 个）
scripts/
  resolve-deps.sh              # 按 SHA-256 校验/下载 JUnit
  build.sh  package.sh  test.sh  demo.sh
deps/deps.lock                 # 测试依赖锁定（校验和 + URL）
```

## 6. 已知限制 / 未完成项

- 只接受 `[0, universe)` 的**整数**键；浮点或字符串域需调用方先做秩映射。
- 摘要只存内存，没有持久化（可用 `/serialize` 自行落盘）。
- 单实例、无鉴权；`HttpServer` 适合内网/演示，不是生产级服务器。
- 经典 q-digest 在 eps 很小且流很短（`⌊eps·n/K⌋=0`）时不压缩，此时退化为
  存频率表（仍不存原始样本，节点数 ≤ 不同值数）；误差界中的 +K 项同样来自此。
- 未实现 GK/T-Digest 等其他摘要做横向对比（q-digest 已满足可合并+严格秩界要求）。
